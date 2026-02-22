"""
Position Manager — Evaluates open positions and decides hold/exit.
Re-estimates fair value each cycle, executes exits when edge disappears.
"""

import logging
from datetime import datetime, timezone
from safe_order import place_order_safe

log = logging.getLogger("kalshi-bot")


class PositionManager:
    def __init__(self, client, db, estimator, config, dry_run=False):
        self.client = client
        self.db = db
        self.estimator = estimator
        self.config = config
        self.dry_run = dry_run

    def evaluate_positions(self, bankroll=None):
        """
        Re-evaluate all open, filled positions.
        Returns list of action dicts: {trade_id, action, reason, ...}

        bankroll: if provided, enables tighter exit thresholds in survival mode.
        """
        open_trades = self.db.get_open_trades()
        actions = []

        survival = bankroll is not None and bankroll < self.config.get(
            "survival_mode_threshold", 15.0
        )

        for trade in open_trades:
            # Skip trades already marked for exit — prevents exit snowball
            if trade["status"] == "exiting":
                continue

            # Only evaluate filled positions
            if trade["fill_status"] not in ("filled", "unknown"):
                continue

            action = self._evaluate_single(trade, survival=survival)
            if action:
                actions.append(action)

        return actions

    def _evaluate_single(self, trade, survival=False):
        """Evaluate a single position and recommend action.

        In survival mode, thresholds tighten:
        - Stop loss: -30% (normal: -50%)
        - Edge reversal: any negative edge (normal: < -3%)
        - Edge evaporation: < 5% edge (normal: < 3%)
        """
        ticker = trade["ticker"]

        # Get current market data
        try:
            market_data = self.client.get_market(ticker)
            market = market_data.get("market", {})
        except Exception:
            return {"trade_id": trade["id"], "action": "hold", "reason": "api_error"}

        # Check if market is already settled (API returns '' for unsettled)
        if market.get("result") in ("yes", "no"):
            return None  # settlement handled by check_settlements

        # Current prices
        yes_bid = float(market.get("yes_bid_dollars", "0") or "0")
        yes_ask = float(market.get("yes_ask_dollars", "0") or "0")
        no_bid = float(market.get("no_bid_dollars", "0") or "0")
        no_ask = float(market.get("no_ask_dollars", "0") or "0")
        current_mid = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0

        if current_mid <= 0:
            return {"trade_id": trade["id"], "action": "hold", "reason": "no_market"}

        # What we could sell for (hit the bid)
        # Use original contract count — never more than what we ordered
        contracts = trade["original_contracts"] or trade["contracts"]
        if trade["direction"] == "yes":
            exit_price = yes_bid
            unrealized = (exit_price - trade["entry_price"]) * contracts
        else:
            exit_price = no_bid
            unrealized = (exit_price - trade["entry_price"]) * contracts

        # Update mark-to-market
        self.db.update_trade_market_price(trade["id"], current_mid, unrealized)

        # Re-estimate fair value
        scanner_market = self._build_scanner_market(market, trade)
        est_result = self.estimator.estimate(scanner_market)
        new_fair_value = est_result[0] if est_result is not None else None

        if new_fair_value is None:
            return {"trade_id": trade["id"], "action": "hold",
                    "reason": "no_estimate", "unrealized": unrealized}

        # Calculate current edge
        if trade["direction"] == "yes":
            current_edge = new_fair_value - current_mid
        else:
            current_edge = current_mid - new_fair_value

        # Time to close
        hours_to_close = self._hours_to_close(market.get("close_time", ""))
        pnl_pct = unrealized / trade["cost"] if trade["cost"] > 0 else 0

        # Adaptive thresholds — survival mode tightens, learned params may adjust
        edge_reversal_threshold = -0.01 if survival else -0.03
        edge_gone_threshold = 0.05 if survival else self.config.get("edge_gone_threshold", 0.03)
        stop_loss_threshold = -0.30 if survival else self.config.get("stop_loss_pct", -0.50)

        # EXIT: edge reversed
        if current_edge < edge_reversal_threshold:
            return {
                "trade_id": trade["id"], "action": "exit",
                "reason": f"edge_reversed ({current_edge:+.2f})",
                "exit_price": exit_price, "unrealized": unrealized,
                "direction": trade["direction"], "contracts": contracts,
                "ticker": ticker,
            }

        # EXIT: edge evaporated with time remaining
        if current_edge < edge_gone_threshold and hours_to_close > 2:
            # Weather: hold if NWS forecast hasn't materially changed
            if (trade["category"] == "weather"
                    and self._weather_forecast_stable(trade, market)):
                log.info(
                    f"  HOLD trade #{trade['id']} {ticker}: edge_gone "
                    f"({current_edge:+.2f}) but forecast stable — "
                    f"market converging to fair value"
                )
            else:
                return {
                    "trade_id": trade["id"], "action": "exit",
                    "reason": f"edge_gone ({current_edge:+.2f})",
                    "exit_price": exit_price, "unrealized": unrealized,
                    "direction": trade["direction"], "contracts": contracts,
                    "ticker": ticker,
                }

        # EXIT: take profit near close
        if hours_to_close < 2 and unrealized > 0:
            return {
                "trade_id": trade["id"], "action": "exit",
                "reason": f"take_profit (${unrealized:+.2f})",
                "exit_price": exit_price, "unrealized": unrealized,
                "direction": trade["direction"], "contracts": contracts,
                "ticker": ticker,
            }

        # EXIT: stop loss
        if pnl_pct < stop_loss_threshold:
            return {
                "trade_id": trade["id"], "action": "exit",
                "reason": f"stop_loss ({pnl_pct:.0%})",
                "exit_price": exit_price, "unrealized": unrealized,
                "direction": trade["direction"], "contracts": contracts,
                "ticker": ticker,
            }

        return {
            "trade_id": trade["id"], "action": "hold",
            "reason": f"edge={current_edge:+.2f} pnl={pnl_pct:+.0%}",
            "unrealized": unrealized,
        }

    def execute_exit(self, action):
        """Execute an exit trade. Returns True if order placed."""
        trade = self.db.get_trade_by_id(action["trade_id"])
        if not trade:
            return False

        exit_price = action.get("exit_price", 0)
        exit_price_cents = max(1, int(exit_price * 100))
        # CRITICAL: Never sell more than we originally ordered.
        # filled_contracts can be inflated by reconciliation (Kalshi position count
        # includes ALL positions on that ticker, not just this trade's).
        original_contracts = trade["original_contracts"] or trade["contracts"]
        contracts = min(
            action.get("contracts", original_contracts),
            original_contracts,
        )
        direction = action.get("direction", trade["direction"])

        if contracts <= 0 or exit_price <= 0:
            log.warning(f"  Cannot exit trade #{trade['id']}: "
                        f"contracts={contracts}, price={exit_price}")
            return False

        try:
            result = place_order_safe(
                self.client, self.db, trade["ticker"],
                action="sell", side=direction,
                quantity=contracts, price_cents=exit_price_cents,
                bankroll=0,  # no dollar cap on exits
                trade_id=trade["id"], dry_run=self.dry_run,
            )
            if result is None:
                return False
            order_id = result.get("order", {}).get("order_id", "unknown")
            log.info(f"  Exit order {order_id}: {action['reason']}")

            # Store exit reason and mark as exiting
            exit_reason = action.get("reason", "unknown").split(" ")[0]
            self.db.set_exit_reason(trade["id"], exit_reason)
            self.db.update_trade_status(trade["id"], "exiting")
            return True
        except Exception as e:
            log.error(f"  Exit failed for trade #{trade['id']}: {e}")
            return False

    def _weather_forecast_stable(self, trade, market):
        """
        Check if NWS forecast has materially changed since trade entry.
        Returns True (stable, hold) if forecast moved < 1 deg toward threshold.
        Returns False (changed, exit) if no baseline, can't fetch, or moved 1+.
        """
        try:
            original_temp = trade["forecast_temp"]
        except (KeyError, IndexError):
            return False  # legacy trade, no baseline — normal exit
        if original_temp is None:
            return False

        current_temp = self.estimator.get_current_forecast_temp(trade["ticker"])
        if current_temp is None:
            return False  # can't verify — normal exit

        threshold = self._extract_threshold(market)
        if threshold is None:
            return False

        # How much did the forecast move toward the threshold?
        original_distance = abs(original_temp - threshold)
        current_distance = abs(current_temp - threshold)
        moved_toward = original_distance - current_distance

        if moved_toward < 1.0:
            log.debug(
                f"  Weather stable: {trade['ticker']} forecast "
                f"{original_temp}\u2192{current_temp}\u00b0F, threshold={threshold}, "
                f"moved_toward={moved_toward:+.1f}\u00b0F"
            )
            return True

        log.info(
            f"  Weather shifted: {trade['ticker']} forecast "
            f"{original_temp}\u2192{current_temp}\u00b0F, threshold={threshold}, "
            f"moved_toward={moved_toward:+.1f}\u00b0F — exit warranted"
        )
        return False

    def _extract_threshold(self, market):
        """Extract the nearest strike threshold from API market data."""
        strike_type = market.get("strike_type", "")
        try:
            if strike_type == "greater" and market.get("floor_strike") is not None:
                return float(market["floor_strike"])
            elif strike_type == "less" and market.get("cap_strike") is not None:
                return float(market["cap_strike"])
            elif strike_type == "between":
                floor = float(market["floor_strike"]) if market.get("floor_strike") else None
                cap = float(market["cap_strike"]) if market.get("cap_strike") else None
                if floor is not None and cap is not None:
                    mid = (floor + cap) / 2
                    return floor if mid > float(market["floor_strike"]) else cap
        except (ValueError, TypeError):
            pass
        return None

    def _build_scanner_market(self, api_market, trade):
        """Reconstruct a scanner-compatible market dict from API data."""
        yes_bid = float(api_market.get("yes_bid_dollars", "0") or "0")
        yes_ask = float(api_market.get("yes_ask_dollars", "0") or "0")
        no_bid = float(api_market.get("no_bid_dollars", "0") or "0")
        no_ask = float(api_market.get("no_ask_dollars", "0") or "0")

        # Extract series_ticker from correlation group or ticker
        series_ticker = ""
        if trade["correlation_group"]:
            series_ticker = trade["correlation_group"].split("_")[0]
        else:
            # Parse from ticker: KXHIGHNY-26FEB12-B36.5 -> KXHIGHNY
            parts = trade["ticker"].split("-")
            if parts:
                series_ticker = parts[0]

        return {
            "ticker": trade["ticker"],
            "title": trade["title"] or "",
            "category": trade["category"] or "weather",
            "series_ticker": series_ticker,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "midprice": (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0,
            "spread": (yes_ask - yes_bid) if yes_ask > 0 and yes_bid > 0 else 999,
            "strike_type": api_market.get("strike_type", ""),
            "floor_strike": api_market.get("floor_strike"),
            "cap_strike": api_market.get("cap_strike"),
            "rules_primary": api_market.get("rules_primary", ""),
            "close_time": api_market.get("close_time", ""),
            "volume_24h": api_market.get("volume_24h", 0) or 0,
            "open_interest": api_market.get("open_interest", 0) or 0,
            "raw": api_market,
        }

    def _hours_to_close(self, close_time_str):
        """Calculate hours until market close."""
        if not close_time_str:
            return 999
        try:
            close_time_str = close_time_str.replace("Z", "+00:00")
            close_time = datetime.fromisoformat(close_time_str)
            now = datetime.now(timezone.utc)
            return max(0, (close_time - now).total_seconds() / 3600)
        except (ValueError, TypeError):
            return 999
