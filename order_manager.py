"""
Order Manager — Tracks order lifecycle, reconciles with Kalshi API,
and manages stale/unfilled orders.
"""

import re
import logging
from datetime import datetime, timezone

log = logging.getLogger("kalshi-bot")


# Group related series into a single asset for correlation
ASSET_GROUPS = {
    "KXBTC": "BTC", "KXBTCD": "BTC",
    "KXETH": "ETH", "KXETHD": "ETH",
    "KXINX": "SPX", "KXSPX": "SPX",
    "KXNDX": "NDX", "KXCOMP": "NDX",
}


def compute_correlation_group(ticker, series_ticker):
    """
    Compute a correlation group key. Groups related series together:
    - KXBTC + KXBTCD -> BTC (same underlying: Bitcoin price)
    - KXETH + KXETHD -> ETH
    - KXINX + KXSPX -> SPX
    - Weather series stay as-is (KXHIGHCHI, KXLOWCHI are different metrics)

    Handles all ticker formats:
    - KXHIGHNY-26FEB12-B36.5     -> KXHIGHNY_26FEB12
    - KXBTC-26FEB1217-B69000     -> BTC_26FEB12
    - KXINX-26FEB13H1600-B7037   -> SPX_26FEB13
    """
    asset = ASSET_GROUPS.get(series_ticker, series_ticker)
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
    if m:
        date_part = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        return f"{asset}_{date_part}"
    return f"{asset}_unknown"


class OrderManager:
    def __init__(self, client, db):
        self.client = client
        self.db = db
        self._cached_positions = None  # refreshed each cycle

    def record_order(self, trade_id, order_id):
        """Store order_id against the trade record after placement."""
        self.db.update_trade_order_id(trade_id, order_id)

    def get_api_positions(self):
        """
        Fetch live positions from Kalshi API.
        Returns dict: {ticker: {"position": int, "side": "yes"/"no"}}
        Caches result for the current cycle (call refresh_positions to re-fetch).
        """
        if self._cached_positions is not None:
            return self._cached_positions

        self._cached_positions = {}
        try:
            api_positions = self.client.get_positions()
            for pos in api_positions:
                ticker = pos.get("ticker", "")
                position = pos.get("position", 0)
                if ticker and position != 0:
                    self._cached_positions[ticker] = {
                        "position": position,
                        "side": "yes" if position > 0 else "no",
                        "quantity": abs(position),
                    }
        except Exception as e:
            log.error(f"Failed to fetch API positions: {e}")
            self._cached_positions = {}

        return self._cached_positions

    def refresh_positions(self):
        """Clear cached positions so next get_api_positions() re-fetches."""
        self._cached_positions = None

    def is_held_on_exchange(self, ticker):
        """Check if we hold a live position on Kalshi for this ticker."""
        positions = self.get_api_positions()
        return ticker in positions

    def get_held_correlation_groups(self):
        """
        Get correlation groups for all positions held on Kalshi.
        Returns set of correlation group keys.
        """
        positions = self.get_api_positions()
        groups = set()
        for ticker in positions:
            # Parse series_ticker from ticker (e.g., KXHIGHNY-26FEB12-B36.5 -> KXHIGHNY)
            parts = ticker.split("-")
            series_ticker = parts[0] if parts else ticker
            group = compute_correlation_group(ticker, series_ticker)
            groups.add(group)
        return groups

    def reconcile_all(self):
        """
        Sync local DB with Kalshi API positions and orders.
        Detects orphaned positions and direction mismatches.
        Returns summary dict.
        """
        summary = {"filled": 0, "unfilled": 0, "settled": 0,
                    "orphaned": 0, "mismatched": 0, "errors": 0}

        # Refresh cached positions for this cycle
        self.refresh_positions()

        # Get actual positions from Kalshi
        try:
            api_positions = self.client.get_positions()
        except Exception as e:
            log.error(f"Failed to fetch positions: {e}")
            return summary

        # Index by ticker for O(1) lookup
        pos_by_ticker = {}
        for pos in api_positions:
            ticker = pos.get("ticker", "")
            if ticker:
                pos_by_ticker[ticker] = pos

        # Get resting (unfilled) orders
        resting_tickers = set()
        try:
            orders_data = self.client.get_orders(status="resting")
            for order in orders_data.get("orders", []):
                resting_tickers.add(order.get("ticker", ""))
        except Exception as e:
            log.debug(f"Failed to fetch resting orders: {e}")

        # Track which API positions we've matched to DB trades
        matched_api_tickers = set()

        # Reconcile each open trade in DB
        open_trades = self.db.get_open_trades()
        for trade in open_trades:
            try:
                ticker = trade["ticker"]

                if ticker in pos_by_ticker:
                    pos = pos_by_ticker[ticker]
                    position = pos.get("position", 0)
                    position_count = abs(position)
                    api_side = "yes" if position > 0 else "no"
                    matched_api_tickers.add(ticker)

                    if position_count > 0:
                        # Check for direction mismatch
                        if trade["direction"] != api_side:
                            log.warning(
                                f"DIRECTION MISMATCH: trade #{trade['id']} {ticker} "
                                f"DB={trade['direction']}, Kalshi={api_side} "
                                f"({position_count} contracts)"
                            )
                            summary["mismatched"] += 1

                        self.db.update_trade_fill(
                            trade["id"],
                            fill_status="filled",
                            filled_contracts=position_count,
                        )
                        summary["filled"] += 1
                    else:
                        # Position is zero — might be settled
                        self.db.update_trade_fill(
                            trade["id"],
                            fill_status="filled",
                            filled_contracts=trade["contracts"],
                        )
                        summary["settled"] += 1

                elif ticker in resting_tickers:
                    self.db.update_trade_fill(
                        trade["id"],
                        fill_status="unfilled",
                        filled_contracts=0,
                    )
                    summary["unfilled"] += 1

                elif trade["fill_status"] == "unknown":
                    # Not in positions, not resting — check if market settled
                    try:
                        market_data = self.client.get_market(ticker)
                        market = market_data.get("market", {})
                        result = market.get("result")
                        if result in ("yes", "no"):
                            # Market settled — mark as filled (was filled before settlement)
                            self.db.update_trade_fill(
                                trade["id"],
                                fill_status="filled",
                                filled_contracts=trade["contracts"],
                            )
                            summary["settled"] += 1
                        else:
                            # Order likely expired unfilled
                            self.db.update_trade_fill(
                                trade["id"],
                                fill_status="unfilled",
                                filled_contracts=0,
                            )
                            summary["unfilled"] += 1
                    except Exception:
                        summary["errors"] += 1

            except Exception as e:
                log.debug(f"Reconcile error for trade #{trade['id']}: {e}")
                summary["errors"] += 1

        # Detect orphaned positions: on Kalshi but NOT in our DB
        db_tickers = {t["ticker"] for t in open_trades}
        for ticker, pos in pos_by_ticker.items():
            position = pos.get("position", 0)
            if position != 0 and ticker not in db_tickers:
                log.warning(
                    f"ORPHANED POSITION: {ticker} has {abs(position)}x "
                    f"{'YES' if position > 0 else 'NO'} on Kalshi "
                    f"but NO matching trade in DB"
                )
                summary["orphaned"] += 1

        if summary["orphaned"]:
            log.warning(f"Found {summary['orphaned']} orphaned position(s) on Kalshi")
        if summary["mismatched"]:
            log.warning(f"Found {summary['mismatched']} direction mismatch(es)")

        return summary

    def cancel_stale_orders(self):
        """Cancel unfilled limit orders for markets closing within 30 min."""
        canceled = 0
        try:
            orders_data = self.client.get_orders(status="resting")
            for order in orders_data.get("orders", []):
                ticker = order.get("ticker", "")
                try:
                    market_data = self.client.get_market(ticker)
                    market = market_data.get("market", {})
                    close_time = market.get("close_time", "")
                    if close_time and self._closing_soon(close_time, minutes=30):
                        order_id = order.get("order_id", "")
                        if order_id:
                            self.client.cancel_order(order_id)
                            log.info(f"Canceled stale order {order_id} for {ticker}")

                            # Update DB trade if we have one
                            trades = self.db.get_open_trades_by_ticker(ticker)
                            for t in trades:
                                if t["fill_status"] in ("unknown", "unfilled"):
                                    self.db.update_trade_fill(
                                        t["id"], "canceled", 0
                                    )
                                    self.db.update_trade_status(t["id"], "resolved")

                            canceled += 1
                except Exception as e:
                    log.debug(f"Error checking/canceling order for {ticker}: {e}")
        except Exception as e:
            log.debug(f"Failed to fetch resting orders for cleanup: {e}")

        return canceled

    def _closing_soon(self, close_time_str, minutes=30):
        """Check if a market is closing within N minutes."""
        try:
            # Handle both Z and +00:00 formats
            close_time_str = close_time_str.replace("Z", "+00:00")
            close_time = datetime.fromisoformat(close_time_str)
            now = datetime.now(timezone.utc)
            remaining = (close_time - now).total_seconds()
            return 0 < remaining < (minutes * 60)
        except (ValueError, TypeError):
            return False
