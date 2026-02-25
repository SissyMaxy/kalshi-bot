"""
Strategy Adapter -- Self-adaptive parameter tuning based on trade performance.
Analyzes resolved trades by category, edge bucket, and exit effectiveness,
then adjusts cycle_config parameters within safe bounds.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("kalshi-bot")

# Minimum resolved trades before any bucket can trigger adjustments
MIN_SAMPLE_SIZE = 10

# Maximum adjustment per cycle: +/- 20% of the baseline value
MAX_ADJUSTMENT_RATIO = 0.20

# Only analyze trades from after critical bug fixes (sigma calibration,
# reconciliation fix, position cap, etc.). Earlier trades had inflated
# contracts, sigma=0, and stop losses destroying weather positions.
MIN_TRADE_ID = 80

# Win rate thresholds
CATEGORY_DISABLE_WIN_RATE = 0.30
CATEGORY_BOOST_WIN_RATE = 0.60
EDGE_BUCKET_LOSS_WIN_RATE = 0.35


class StrategyAdapter:
    def __init__(self, db, base_config):
        self.db = db
        self.base_config = dict(base_config)

    def load_learned_params(self, cycle_config):
        """
        Apply previously learned parameter adjustments to cycle_config.
        Ensures adjustments persist across bot restarts.
        """
        active_params = self.db.get_learned_params_active()
        applied = 0

        for param in active_params:
            param_name = param["param_name"]
            param_scope = param["param_scope"]
            value = param["new_value"]

            if param_scope == "global":
                if param_name in cycle_config:
                    cycle_config[param_name] = value
                    applied += 1
            elif param_scope.startswith("category:"):
                category = param_scope.split(":", 1)[1]
                config_key = f"min_edge_{category}"
                # Never go below the CONFIG baseline for category edges
                floor = self.base_config.get(config_key, 0)
                value = max(value, floor)
                cycle_config[config_key] = value
                applied += 1

        if applied:
            log.info(f"Strategy adapter: loaded {applied} learned parameter(s)")

    def adapt(self, cycle_config):
        """
        Main entry point. Reads resolved trade data, computes metrics,
        and adjusts cycle_config in-place. Returns summary dict of changes.
        """
        changes = {}

        try:
            changes.update(self._adapt_by_category(cycle_config))
        except Exception as e:
            log.error(f"Strategy adapter category error: {e}")

        try:
            changes.update(self._adapt_by_edge_bucket(cycle_config))
        except Exception as e:
            log.error(f"Strategy adapter edge bucket error: {e}")

        try:
            changes.update(self._adapt_exit_thresholds(cycle_config))
        except Exception as e:
            log.error(f"Strategy adapter exit error: {e}")

        if changes:
            log.info(f"Strategy adapter: {len(changes)} adjustment(s) applied")
        else:
            log.info("Strategy adapter: no adjustments (insufficient data or no triggers)")

        return changes

    def _adapt_by_category(self, cycle_config):
        """
        Analyze win rate and avg PnL by category.
        - Win rate < 30%: raise min_edge for that category
        - Win rate > 60%: lower min_edge slightly
        """
        changes = {}
        by_category = self.db.get_resolved_trades_by_category(min_trade_id=MIN_TRADE_ID)

        for category, trades in by_category.items():
            if len(trades) < MIN_SAMPLE_SIZE:
                continue

            wins = sum(1 for t in trades if t["pnl"] and t["pnl"] > 0)
            win_rate = wins / len(trades)
            avg_pnl = sum(t["pnl"] for t in trades if t["pnl"] is not None) / len(trades)

            param_name = "min_edge_threshold"
            param_scope = f"category:{category}"
            base_edge = self.base_config["min_edge_threshold"]

            current_learned = self.db.get_latest_learned_param(param_name, param_scope)
            current_value = current_learned["new_value"] if current_learned else base_edge

            new_value = current_value

            if win_rate < CATEGORY_DISABLE_WIN_RATE:
                # Poor performance: raise min_edge
                increase = base_edge * 0.15
                new_value = min(current_value + increase,
                                base_edge * (1 + MAX_ADJUSTMENT_RATIO))
                reason = f"poor win rate {win_rate:.0%} ({wins}/{len(trades)}), avg_pnl=${avg_pnl:+.2f}"

            elif win_rate > CATEGORY_BOOST_WIN_RATE:
                # Good performance: lower min_edge slightly
                decrease = base_edge * 0.05
                new_value = max(current_value - decrease,
                                base_edge * (1 - MAX_ADJUSTMENT_RATIO * 0.5))
                reason = f"strong win rate {win_rate:.0%} ({wins}/{len(trades)}), avg_pnl=${avg_pnl:+.2f}"

            else:
                continue

            # Absolute bounds — never go below CONFIG baseline
            config_floor = self.base_config.get(f"min_edge_{category}", 0.05)
            new_value = max(config_floor, min(new_value, 0.35))

            if abs(new_value - current_value) > 0.001:
                self.db.log_learned_param(
                    param_name=param_name,
                    param_scope=param_scope,
                    old_value=current_value,
                    new_value=new_value,
                    reason=reason,
                    sample_count=len(trades),
                    win_rate=win_rate,
                    avg_pnl=avg_pnl,
                )
                cycle_config[f"min_edge_{category}"] = new_value
                changes[f"{param_scope}/{param_name}"] = {
                    "old": current_value, "new": new_value, "reason": reason
                }
                log.info(f"  Category '{category}': min_edge {current_value:.3f} -> {new_value:.3f} "
                         f"(win_rate={win_rate:.0%}, n={len(trades)})")

        return changes

    def _adapt_by_edge_bucket(self, cycle_config):
        """
        Analyze win rate by edge size bucket.
        If small edges consistently lose, raise global min_edge_threshold.
        """
        changes = {}
        by_bucket = self.db.get_resolved_trades_by_edge_bucket(min_trade_id=MIN_TRADE_ID)

        # Check if small edges are unprofitable
        small_bucket = by_bucket.get("08-12", [])
        if len(small_bucket) >= MIN_SAMPLE_SIZE:
            wins = sum(1 for t in small_bucket if t["pnl"] and t["pnl"] > 0)
            win_rate = wins / len(small_bucket)
            avg_pnl = sum(t["pnl"] for t in small_bucket
                          if t["pnl"] is not None) / len(small_bucket)

            if win_rate < EDGE_BUCKET_LOSS_WIN_RATE:
                param_name = "min_edge_threshold"
                param_scope = "global"
                base_edge = self.base_config["min_edge_threshold"]

                current_learned = self.db.get_latest_learned_param(param_name, param_scope)
                current_value = current_learned["new_value"] if current_learned else base_edge

                new_value = max(current_value, 0.12)
                new_value = min(new_value, base_edge * (1 + MAX_ADJUSTMENT_RATIO))

                if new_value > current_value + 0.001:
                    reason = (f"small edges (8-12%) losing: win_rate={win_rate:.0%} "
                              f"({wins}/{len(small_bucket)}), avg_pnl=${avg_pnl:+.2f}")
                    self.db.log_learned_param(
                        param_name=param_name,
                        param_scope=param_scope,
                        old_value=current_value,
                        new_value=new_value,
                        reason=reason,
                        sample_count=len(small_bucket),
                        win_rate=win_rate,
                        avg_pnl=avg_pnl,
                    )
                    cycle_config["min_edge_threshold"] = new_value
                    changes[f"{param_scope}/{param_name}"] = {
                        "old": current_value, "new": new_value, "reason": reason
                    }
                    log.info(f"  Edge bucket 08-12%: raising min_edge to {new_value:.3f} "
                             f"(win_rate={win_rate:.0%}, n={len(small_bucket)})")

        # Log confidence for large edge buckets
        large_bucket = by_bucket.get("20+", [])
        if len(large_bucket) >= MIN_SAMPLE_SIZE:
            wins = sum(1 for t in large_bucket if t["pnl"] and t["pnl"] > 0)
            win_rate = wins / len(large_bucket)
            log.info(f"  Edge bucket 20%+: win_rate={win_rate:.0%} (n={len(large_bucket)}) "
                     f"-- {'model validated' if win_rate > 0.5 else 'model needs review'}")

        return changes

    def _adapt_exit_thresholds(self, cycle_config):
        """
        Analyze whether early exits were correct decisions.
        If stop_loss exits would have mostly won, loosen the stop loss.
        """
        changes = {}
        by_exit = self.db.get_resolved_trades_by_exit_reason(min_trade_id=MIN_TRADE_ID)

        # Analyze stop_loss exits
        stop_losses = by_exit.get("stop_loss", [])
        if len(stop_losses) >= MIN_SAMPLE_SIZE:
            premature = sum(1 for t in stop_losses if self._would_have_won(t))
            premature_rate = premature / len(stop_losses)

            if premature_rate > 0.60:
                param_name = "stop_loss_pct"
                param_scope = "global"
                base_value = -0.50

                current_learned = self.db.get_latest_learned_param(param_name, param_scope)
                current_value = current_learned["new_value"] if current_learned else base_value

                # Loosen by 10% (more negative = more lenient)
                new_value = current_value * 1.10
                new_value = max(new_value, -0.60)  # absolute max leniency

                if abs(new_value - current_value) > 0.01:
                    reason = (f"stop_loss premature rate {premature_rate:.0%} "
                              f"({premature}/{len(stop_losses)})")
                    self.db.log_learned_param(
                        param_name=param_name,
                        param_scope=param_scope,
                        old_value=current_value,
                        new_value=new_value,
                        reason=reason,
                        sample_count=len(stop_losses),
                    )
                    cycle_config["stop_loss_pct"] = new_value
                    changes[f"{param_scope}/{param_name}"] = {
                        "old": current_value, "new": new_value, "reason": reason
                    }
                    log.info(f"  Stop loss: loosening {current_value:.0%} -> {new_value:.0%} "
                             f"(premature rate={premature_rate:.0%})")

        # Analyze edge_gone + edge_reversed exits
        edge_exits = by_exit.get("edge_gone", []) + by_exit.get("edge_reversed", [])
        if len(edge_exits) >= MIN_SAMPLE_SIZE:
            correct = sum(1 for t in edge_exits if not self._would_have_won(t))
            correct_rate = correct / len(edge_exits)

            if correct_rate < 0.40:
                param_name = "edge_gone_threshold"
                param_scope = "global"
                base_value = 0.03

                current_learned = self.db.get_latest_learned_param(param_name, param_scope)
                current_value = current_learned["new_value"] if current_learned else base_value

                new_value = current_value * 0.80
                new_value = max(new_value, 0.01)

                if abs(new_value - current_value) > 0.001:
                    reason = (f"edge exits mostly premature: correct_rate={correct_rate:.0%} "
                              f"({correct}/{len(edge_exits)})")
                    self.db.log_learned_param(
                        param_name=param_name,
                        param_scope=param_scope,
                        old_value=current_value,
                        new_value=new_value,
                        reason=reason,
                        sample_count=len(edge_exits),
                    )
                    cycle_config["edge_gone_threshold"] = new_value
                    changes[f"{param_scope}/{param_name}"] = {
                        "old": current_value, "new": new_value, "reason": reason
                    }
                    log.info(f"  Edge exits: loosening threshold {current_value:.3f} -> {new_value:.3f}")

        return changes

    def _would_have_won(self, trade):
        """
        Determine if a trade that exited early would have won at settlement.
        Checks calibration table and other resolved trades on same ticker.
        """
        ticker = trade["ticker"]

        # Check calibration table for settlement outcome
        row = self.db.conn.execute(
            "SELECT outcome FROM calibration WHERE ticker = ? AND outcome IS NOT NULL LIMIT 1",
            (ticker,)
        ).fetchone()

        if row:
            outcome = row["outcome"]
            direction = trade["direction"]
            return (direction == "yes" and outcome == 1) or \
                   (direction == "no" and outcome == 0)

        # Check if another trade on the same ticker was resolved as a win
        row = self.db.conn.execute(
            """SELECT pnl FROM trades
               WHERE ticker = ? AND status = 'resolved' AND pnl IS NOT NULL
               AND id != ? LIMIT 1""",
            (ticker, trade["id"])
        ).fetchone()

        if row:
            return row["pnl"] > 0

        # Cannot determine -- assume exit was correct (conservative)
        return False
