#!/usr/bin/env python3
"""
Kalshi Autonomous Trading Bot (v2 — Adaptive)
==============================================
Usage:
  1. Copy .env.example -> .env, fill in your keys
  2. pip install -r requirements.txt
  3. python bot.py              # demo mode (default)
  4. python bot.py --live        # real money
  5. python bot.py --scan-only   # just show edges, don't trade
"""

import os
import sys
import io
import time
import logging
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "buffer") and stream.encoding != "utf-8":
            setattr(sys, stream_name,
                    io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))

from kalshi_client import KalshiClient
from scanner import MarketScanner
from fair_value import FairValueEstimator
from position_sizer import PositionSizer, compute_accuracy_multiplier
from risk_manager import RiskManager
from database import Database
from order_manager import OrderManager, compute_correlation_group
from position_manager import PositionManager
from strategy_adapter import StrategyAdapter
from safe_order import place_order_safe

CONFIG = {
    "scan_interval_minutes": 5,
    "kelly_multiplier": 0.25,
    "max_position_pct": 0.15,
    "max_category_pct": 0.20,
    "max_total_exposure_pct": 0.50,
    "min_edge_threshold": 0.08,
    # Category-specific edge thresholds (override global min_edge_threshold)
    "min_edge_weather": 0.06,       # weather is our edge — take more at-bats
    "min_edge_financial": 0.30,     # no edge in crypto/financial — effectively disabled
    "min_edge_economics": 0.30,     # no edge in economics — effectively disabled
    "min_volume_24h": 25,
    "min_open_interest": 50,
    "max_spread": 0.25,
    "daily_loss_limit_pct": 0.10,
    # Survival mode: tighten everything when bankroll is critically low
    "survival_mode_threshold": 15.00,
    "survival_edge_threshold": 0.15,
    "survival_max_position_pct": 0.25,  # allow meaningful 1-contract trades
    "survival_max_trades_per_cycle": 2,
    "survival_max_concurrent": 5,
    "max_concurrent": 7,
}

# Configure logging with UTF-8 file handler
log_handlers = [logging.FileHandler("bot.log", encoding="utf-8")]
if sys.platform == "win32":
    # Use UTF-8 stream handler on Windows
    log_handlers.append(logging.StreamHandler(
        io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "buffer") else sys.stderr
    ))
else:
    log_handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers,
)
log = logging.getLogger("kalshi-bot")


def run_cycle(client, scanner, estimator, sizer, risk_mgr, db,
              order_mgr, pos_mgr, adapter, config, scan_only=False, dry_run=False):
    """One full adaptive scan-evaluate-manage-trade cycle."""

    log.info("=== Scan cycle start ===")
    estimator.clear_cache()

    # ── PHASE 1: RECONCILE ──────────────────────────────────────────
    if not scan_only:
        try:
            recon = order_mgr.reconcile_all()
            log.info(f"Reconcile: {recon['filled']} filled, "
                     f"{recon['unfilled']} unfilled, {recon['settled']} settled")
            canceled = order_mgr.cancel_stale_orders()
            if canceled:
                log.info(f"Canceled {canceled} stale orders")
        except Exception as e:
            log.error(f"Reconciliation error: {e}")

    # ── PHASE 2: BANKROLL & RISK ────────────────────────────────────
    if not scan_only:
        try:
            bankroll = client.get_balance()
        except Exception as e:
            log.error(f"Could not fetch balance: {e}")
            return
        # Compute real portfolio value from Kalshi positions
        portfolio_value = bankroll
        try:
            positions = client.get_positions()
            total_exposure = sum(
                p.get("market_exposure", 0) / 100 for p in positions
                if p.get("position", 0) != 0
            )
            portfolio_value = bankroll + total_exposure
        except Exception as e:
            log.debug(f"Could not fetch positions for portfolio value: {e}")
        db.log_balance(bankroll, portfolio_value=portfolio_value)
    else:
        bankroll = 100.0

    peak = db.get_peak_balance() or bankroll
    daily_pnl = db.get_daily_pnl()
    drawdown = 1 - (bankroll / peak) if peak > 0 else 0

    log.info(
        f"Bankroll: ${bankroll:.2f} | Peak: ${peak:.2f} | "
        f"Drawdown: {drawdown:.1%} | Daily P&L: ${daily_pnl:+.2f}"
    )

    cycle_config = dict(config)
    halted = False
    kelly_scale = 1.0
    if not scan_only:
        action, kelly_scale = risk_mgr.check_circuit_breakers(
            bankroll, peak, daily_pnl, cycle_config
        )
        if action == "HALT":
            halted = True
            log.warning("HALTED by circuit breaker -- managing positions only")
        # Apply progressive Kelly scaling from drawdown
        cycle_config["kelly_multiplier"] *= kelly_scale

    # Survival mode: triggered by low bankroll OR deep drawdown
    survival = (bankroll < cycle_config["survival_mode_threshold"]) or (drawdown >= 0.50)
    if survival and not scan_only:
        log.warning(f"SURVIVAL MODE (${bankroll:.2f}) -- high edge, tiny positions")
        survival_edge = cycle_config["survival_edge_threshold"]
        cycle_config["min_edge_threshold"] = survival_edge
        cycle_config["max_position_pct"] = cycle_config["survival_max_position_pct"]
        # Raise category-specific edges to at least survival threshold
        for key in list(cycle_config):
            if key.startswith("min_edge_") and key != "min_edge_threshold":
                cycle_config[key] = max(cycle_config[key], survival_edge)

    if not scan_only:
        log.info(
            f"Risk: kelly_scale={kelly_scale:.2f} | "
            f"effective_kelly={cycle_config['kelly_multiplier']:.4f} | "
            f"min_edge={cycle_config['min_edge_threshold']:.2f} | "
            f"max_pos={cycle_config['max_position_pct']:.0%} | "
            f"survival={'YES' if survival else 'no'}"
        )

    # ── PHASE 3: EVALUATE EXISTING POSITIONS ────────────────────────
    # Always runs, even when halted — must manage/exit positions
    if not scan_only:
        try:
            actions = pos_mgr.evaluate_positions(bankroll=bankroll)
            exits = 0
            for act in actions:
                if act["action"] == "exit":
                    log.info(f"EXIT: trade #{act['trade_id']} -- {act['reason']}")
                    if pos_mgr.execute_exit(act):
                        exits += 1
                elif act["action"] == "hold":
                    unr = act.get("unrealized", 0)
                    log.debug(f"HOLD: trade #{act['trade_id']} -- {act['reason']} "
                              f"(unrealized ${unr:+.2f})")
            if exits:
                log.info(f"Executed {exits} exit(s)")
        except Exception as e:
            log.error(f"Position evaluation error: {e}")

    # ── PHASE 4: CALIBRATION ────────────────────────────────────────
    accuracy_mult = 0.4  # safe default
    if not scan_only:
        try:
            db.check_settlements(client)
            accuracy_mult = compute_accuracy_multiplier(db)
            cal_stats = db.get_calibration_stats()
            log.info(f"Calibration: {cal_stats['count']} samples, "
                     f"brier={cal_stats['avg_brier']:.3f}, "
                     f"accuracy_mult={accuracy_mult:.1f}x")
        except Exception as e:
            log.error(f"Calibration error: {e}")

        # In survival mode, drawdown scaling already provides caution.
        # Don't also penalize for lack of calibration data — we need to trade.
        if survival and accuracy_mult < 1.0:
            log.info(f"Survival override: accuracy_mult {accuracy_mult:.1f} -> 1.0")
            accuracy_mult = 1.0
    else:
        accuracy_mult = 1.0

    # ── PHASE 4.5: STRATEGY ADAPTATION ─────────────────────────────
    if not scan_only:
        try:
            adapter.load_learned_params(cycle_config)
            adapt_changes = adapter.adapt(cycle_config)
        except Exception as e:
            log.error(f"Strategy adaptation error: {e}")

    # ── PHASE 5: SCAN & TRADE ───────────────────────────────────────
    if halted:
        log.info("=== Cycle complete: HALTED (positions managed only) ===\n")
        return

    try:
        markets = scanner.scan_all(client)
    except Exception as e:
        log.error(f"Scanner error: {e}")
        return

    log.info(f"Markets passing filters: {len(markets)}")

    # Fetch live Kalshi positions ONCE for this cycle (source of truth)
    if not scan_only:
        order_mgr.refresh_positions()
        api_held = order_mgr.get_api_positions()
        api_held_groups = order_mgr.get_held_correlation_groups()
        log.info(f"API positions: {len(api_held)} tickers, "
                 f"{len(api_held_groups)} correlation groups")
    else:
        api_held = {}
        api_held_groups = set()

    # ── Build candidate list, pick best edge per correlation group ──
    candidates = []
    for market in markets:
        try:
            est_result = estimator.estimate(market, db=db)
            if est_result is None:
                continue
            fair_value, est_meta = est_result

            midprice = market["midprice"]
            raw_edge = fair_value - midprice

            # Determine direction from midprice edge, then compute true edge
            # against the ask price we'd actually pay
            direction = "yes" if raw_edge > 0 else "no"
            entry_price = market["yes_ask"] if direction == "yes" else market["no_ask"]
            if entry_price <= 0 or entry_price >= 1:
                continue

            # True edge: our estimated value minus what we'd actually pay
            if direction == "yes":
                edge = fair_value - entry_price  # YES value = fair_value, cost = yes_ask
            else:
                edge = (1 - fair_value) - entry_price  # NO value = 1-fair_value, cost = no_ask

            # Use category-specific min_edge if learned, else global
            effective_min_edge = cycle_config.get(
                f"min_edge_{market['category']}",
                cycle_config["min_edge_threshold"]
            )
            if edge < effective_min_edge:
                continue

            # Weather payoff ratio filter: ensure at least ~1:1 risk/reward.
            # NO at $0.65 risks $0.65 to make $0.35 — needs 65%+ win rate.
            # YES at $0.35 risks $0.35 to make $0.65 — acceptable.
            # Cap entry prices to avoid unfavorable asymmetry.
            if market["category"] == "weather":
                max_entry = 0.65 if direction == "no" else 0.35
                if entry_price > max_entry:
                    continue

            # Same-day priority: NWS forecasts are most accurate day-of.
            # Require higher edge for multi-day weather trades where
            # sigma is larger and estimates are lower confidence.
            if market["category"] == "weather":
                hours_to_close = est_meta.get("hours_to_close", 24)
                if hours_to_close > 36:  # 2+ days out
                    if edge < effective_min_edge * 1.5:
                        continue
                elif hours_to_close > 18:  # next-day
                    if edge < effective_min_edge * 1.25:
                        continue

            # Skip if we already have a position in this ticker (DB check)
            existing = db.get_open_trades_by_ticker(market["ticker"])
            if existing:
                continue

            # Skip if Kalshi API shows we already hold this ticker
            if market["ticker"] in api_held:
                log.debug(f"Skipping {market['ticker']}: already held on Kalshi API")
                continue

            corr_group = compute_correlation_group(
                market["ticker"], market["series_ticker"]
            )
            candidates.append({
                "market": market, "fair_value": fair_value,
                "edge": edge, "direction": direction,
                "entry_price": entry_price, "corr_group": corr_group,
                "est_meta": est_meta,
            })
        except Exception as e:
            log.error(f"Error evaluating {market.get('ticker', '?')}: {e}")

    # Pick only the best edge per correlation group
    best_by_group = {}
    for c in candidates:
        group = c["corr_group"]
        if group not in best_by_group or abs(c["edge"]) > abs(best_by_group[group]["edge"]):
            best_by_group[group] = c

    # Check correlation groups against BOTH DB and live API positions
    existing_groups = set()
    for trade in db.get_open_trades():
        if trade["correlation_group"]:
            existing_groups.add(trade["correlation_group"])
    existing_groups |= api_held_groups  # merge API-held groups

    selected = [c for c in best_by_group.values()
                if c["corr_group"] not in existing_groups]

    # Sort by absolute edge descending (best opportunities first)
    selected.sort(key=lambda c: abs(c["edge"]), reverse=True)

    cat_counts = {}
    for c in candidates:
        cat = c["market"].get("category", "?")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    cat_str = ", ".join(f"{k}={v}" for k, v in sorted(cat_counts.items()))
    log.info(f"Candidates: {len(candidates)} edges ({cat_str}), "
             f"{len(best_by_group)} groups, {len(selected)} actionable")

    # ── Enforce concurrent position limits ────────────────────────────
    open_count = len([t for t in db.get_open_trades()
                      if t["fill_status"] in ("filled", "unknown")
                      and t["status"] != "exiting"])
    max_trades_cycle = len(selected)  # default: all actionable
    if not scan_only:
        if survival:
            max_concurrent = cycle_config.get("survival_max_concurrent", 3)
            remaining_slots = max(0, max_concurrent - open_count)
            max_trades_cycle = min(
                cycle_config.get("survival_max_trades_per_cycle", 1),
                remaining_slots,
            )
            if max_trades_cycle <= 0:
                log.info(f"Survival: {open_count} open positions, "
                         f"max {max_concurrent} -- no new trades")
            else:
                log.info(f"Survival: {remaining_slots} slots, "
                         f"allowing {max_trades_cycle} new trade(s)")
        else:
            max_concurrent = cycle_config.get("max_concurrent", 7)
            remaining_slots = max(0, max_concurrent - open_count)
            max_trades_cycle = min(max_trades_cycle, remaining_slots)
            if remaining_slots <= 0:
                log.info(f"Position cap: {open_count}/{max_concurrent} "
                         f"-- no new trades")
            elif remaining_slots < len(selected):
                log.info(f"Position cap: {remaining_slots}/{max_concurrent} "
                         f"slots available")

    # ── Execute trades from selected candidates ──────────────────────
    trades_this_cycle = 0
    for c in selected:
        if trades_this_cycle >= max_trades_cycle:
            break

        market = c["market"]
        fair_value = c["fair_value"]
        edge = c["edge"]
        direction = c["direction"]
        entry_price = c["entry_price"]
        corr_group = c["corr_group"]

        # Sanity check (optional, uses Anthropic API)
        trade_kelly_mult = 1.0
        if abs(edge) < 0.15:
            verdict = estimator.sanity_check(market, fair_value)
            if verdict == "SKIP":
                log.info(f"  Claude says SKIP: {market['ticker']}")
                continue
            if verdict == "REDUCE_SIZE":
                trade_kelly_mult = 0.5

        # Confidence multiplier from sigma distance (weather only)
        est_meta = c.get("est_meta", {})
        sigma_conf = est_meta.get("sigma_confidence", 0)
        hours_to_close = est_meta.get("hours_to_close", 999)
        confidence_mult = 1.0
        if sigma_conf >= 3.0 and hours_to_close <= 12:
            confidence_mult = 3.0
        elif sigma_conf >= 2.0:
            confidence_mult = 2.0
        # Cap confidence boost in survival mode to limit concentration
        if survival and confidence_mult > 2.0:
            confidence_mult = 2.0

        # Position sizing with accuracy multiplier
        position_cost = sizer.calculate(
            edge=abs(edge),
            entry_price=entry_price,
            bankroll=bankroll,
            config=cycle_config,
            accuracy_mult=accuracy_mult,
            confidence_mult=confidence_mult * trade_kelly_mult,
        )
        if position_cost <= 0:
            continue

        num_contracts = int(position_cost / entry_price)
        price_cents = int(entry_price * 100)

        if scan_only:
            log.info(
                f"EDGE: {direction.upper()} {num_contracts}x {market['ticker']} "
                f"@ ${entry_price:.2f} (edge {edge:+.2f}, fair {fair_value:.2f}, "
                f"cost ${position_cost:.2f})"
            )
            trades_this_cycle += 1
            continue

        # Correlation-aware exposure check
        if not risk_mgr.check_exposure_correlated(
            position_cost, corr_group, market["category"],
            db, bankroll, cycle_config
        ):
            continue

        # SAFETY: Final pre-trade API position check (belt-and-suspenders)
        if not scan_only and order_mgr.is_held_on_exchange(market["ticker"]):
            log.warning(f"BLOCKED: {market['ticker']} already held on Kalshi "
                        f"(caught by final safety check)")
            continue

        conf_tag = f", conf={sigma_conf:.1f}\u03c3 x{confidence_mult:.0f}" if confidence_mult > 1 else ""
        log.info(
            f"TRADE: {direction.upper()} {num_contracts}x {market['ticker']} "
            f"@ ${entry_price:.2f} (edge {edge:+.2f}, fair {fair_value:.2f}{conf_tag})"
        )

        # Execute
        try:
            result = place_order_safe(
                client, db, market["ticker"],
                action="buy", side=direction,
                quantity=num_contracts, price_cents=price_cents,
                bankroll=bankroll, dry_run=dry_run,
            )
            if result is None:
                continue
            order_id = result.get("order", {}).get("order_id", "unknown")
            log.info(f"  Order {'simulated' if dry_run else 'placed'}: {order_id}")

            # Capture forecast temp at trade time for weather exit logic
            trade_forecast_temp = None
            if market.get("category") == "weather":
                trade_forecast_temp = estimator.get_current_forecast_temp(
                    market["ticker"])

            trade_id = db.log_trade(
                ticker=market["ticker"],
                title=market["title"],
                category=market["category"],
                direction=direction,
                contracts=num_contracts,
                entry_price=entry_price,
                cost=position_cost,
                fair_value=fair_value,
                edge=abs(edge),
                forecast_temp=trade_forecast_temp,
            )
            # Track order and correlation group
            order_mgr.record_order(trade_id, order_id)
            db.set_correlation_group(trade_id, corr_group)

            trades_this_cycle += 1

        except Exception as e:
            log.error(f"  Order failed: {e}")

    # ── PHASE 6: DAILY STATS ────────────────────────────────────────
    if not scan_only:
        try:
            db.update_daily_stats(bankroll, trades_this_cycle)
        except Exception as e:
            log.error(f"Daily stats error: {e}")

    label = "edges found" if scan_only else "trades placed"
    log.info(f"=== Cycle complete: {trades_this_cycle} {label} ===\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Trading Bot")
    parser.add_argument("--live", action="store_true",
                        help="Use LIVE (real money) environment")
    parser.add_argument("--scan-only", action="store_true",
                        help="Scan and show edges without trading")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full cycle but skip actual Kalshi API orders")
    args = parser.parse_args()

    env = "live" if args.live else "demo"

    # Prevent multiple instances from running simultaneously
    lockfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
    lock_fd = None
    if not args.scan_only and not args.dry_run:
        try:
            import msvcrt
            lock_fd = open(lockfile, "w")
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
        except (IOError, OSError):
            print("ERROR: Another bot instance is already running!")
            print("   Kill the other process first, or delete .bot.lock if stale.")
            sys.exit(1)

    # Validate credentials
    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or None

    if not key_id:
        print("KALSHI_API_KEY_ID not set in .env file")
        print("   Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    if not os.path.exists(key_path):
        print(f"Private key file not found: {key_path}")
        print("   Download your private key from Kalshi and place it in this folder.")
        sys.exit(1)

    if args.live and not args.scan_only:
        print("\n** LIVE MODE -- Real money will be used! **")
        print("    Press Ctrl+C within 5 seconds to cancel...")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nCancelled.")
            sys.exit(0)

    if args.dry_run:
        log.info("** DRY RUN MODE — orders will be simulated, not sent to Kalshi **")
    log.info(f"Starting Kalshi Bot v2 -- env: {env}, scan_only: {args.scan_only}")
    if anthropic_key:
        log.info("Claude sanity checks: enabled")
    else:
        log.info("Claude sanity checks: disabled (no ANTHROPIC_API_KEY)")

    # Initialize components
    client = KalshiClient(key_id, key_path, env=env)
    db = Database()
    scanner = MarketScanner(CONFIG)
    estimator = FairValueEstimator(CONFIG, anthropic_key, db=db)
    sizer = PositionSizer()
    risk_mgr = RiskManager()
    order_mgr = OrderManager(client, db)
    pos_mgr = PositionManager(client, db, estimator, CONFIG, dry_run=args.dry_run)
    adapter = StrategyAdapter(db, CONFIG)

    # Initial balance check
    if not args.scan_only:
        try:
            balance = client.get_balance()
            portfolio_value = balance
            try:
                positions = client.get_positions()
                total_exposure = sum(
                    p.get("market_exposure", 0) / 100 for p in positions
                    if p.get("position", 0) != 0
                )
                portfolio_value = balance + total_exposure
            except Exception:
                pass
            db.log_balance(balance, portfolio_value=portfolio_value)
            log.info(f"Starting balance: ${balance:.2f} | Portfolio: ${portfolio_value:.2f}")
        except Exception as e:
            log.error(f"Cannot connect to Kalshi: {e}")
            sys.exit(1)

    # Run
    if args.once:
        run_cycle(client, scanner, estimator, sizer, risk_mgr, db,
                  order_mgr, pos_mgr, adapter, CONFIG, args.scan_only, args.dry_run)
        return

    interval = CONFIG["scan_interval_minutes"] * 60
    while True:
        try:
            run_cycle(client, scanner, estimator, sizer, risk_mgr, db,
                      order_mgr, pos_mgr, adapter, CONFIG, args.scan_only, args.dry_run)
        except KeyboardInterrupt:
            log.info("Shutting down (Ctrl+C)")
            break
        except Exception as e:
            log.error(f"Unhandled error in cycle: {e}", exc_info=True)

        log.info(f"Sleeping {CONFIG['scan_interval_minutes']} minutes...\n")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Shutting down (Ctrl+C)")
            break


if __name__ == "__main__":
    main()
