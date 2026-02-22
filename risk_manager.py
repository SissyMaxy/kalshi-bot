"""
Risk Manager
Progressive risk scaling, drawdown-based Kelly reduction, and exposure limits.
Never fully halts — always allows trading, just with smaller sizes.
"""

import logging

log = logging.getLogger("kalshi-bot")


class RiskManager:
    def check_circuit_breakers(self, bankroll, peak, daily_pnl, config):
        """
        Progressive risk scaling based on drawdown and daily loss.

        Instead of halting, returns a Kelly multiplier (0.0 to 1.0) that
        scales down position sizes as drawdown increases.

        Returns: (action, kelly_scale)
            action: "OK", "REDUCE", or "HALT" (only if bankroll < $2)
            kelly_scale: 0.0-1.0 multiplier applied to Kelly fraction
        """
        if peak <= 0:
            return "OK", 1.0

        # Absolute floor — can't make any trade below $2
        if bankroll < 2.0:
            log.warning(f"Bankroll ${bankroll:.2f} below $2 minimum — HALT")
            return "HALT", 0.0

        drawdown = 1 - (bankroll / peak)
        daily_limit = config.get("daily_loss_limit_pct", 0.10)

        # Start with full Kelly scale
        kelly_scale = 1.0

        # Progressive drawdown scaling — never zero, always allows trading
        if drawdown >= 0.70:
            kelly_scale = 0.05   # 5% of normal — micro positions only
            log.warning(f"Drawdown {drawdown:.0%} — extreme caution (5% Kelly)")
        elif drawdown >= 0.50:
            kelly_scale = 0.10   # 10% of normal
            log.warning(f"Drawdown {drawdown:.0%} — heavy reduction (10% Kelly)")
        elif drawdown >= 0.35:
            kelly_scale = 0.20
            log.warning(f"Drawdown {drawdown:.0%} — reduced (20% Kelly)")
        elif drawdown >= 0.20:
            kelly_scale = 0.40
            log.warning(f"Drawdown {drawdown:.0%} — cautious (40% Kelly)")
        elif drawdown >= 0.10:
            kelly_scale = 0.70
            log.info(f"Drawdown {drawdown:.0%} — slightly reduced (70% Kelly)")

        # Daily loss penalty — reduce Kelly scale if daily loss exceeds limit
        if daily_pnl < 0 and bankroll > 0 and abs(daily_pnl) > bankroll * daily_limit:
            kelly_scale *= 0.7
            log.warning(f"Daily loss ${daily_pnl:.2f} exceeds limit — reducing Kelly to {kelly_scale:.2f}")

        action = "OK" if kelly_scale >= 0.70 else "REDUCE"
        return action, kelly_scale

    def check_exposure(self, new_cost, current_exposure, bankroll, category,
                       category_exposure, config):
        """
        Simple exposure check (legacy fallback).
        Returns True if trade is allowed.
        """
        max_total = bankroll * config.get("max_total_exposure_pct", 0.50)
        max_cat = bankroll * config.get("max_category_pct", 0.20)

        if current_exposure + new_cost > max_total:
            log.info(f"Blocked: total exposure ${current_exposure + new_cost:.2f} > ${max_total:.2f}")
            return False

        if category_exposure + new_cost > max_cat:
            log.info(f"Blocked: {category} exposure ${category_exposure + new_cost:.2f} > ${max_cat:.2f}")
            return False

        return True

    def check_exposure_correlated(self, new_cost, new_group, category,
                                  db, bankroll, config):
        """
        Correlation-aware exposure check.

        For correlated bets (same city, same day), effective exposure is
        max(cost) * 1.5 within the group (capped at sum of costs).
        This avoids double-counting highly correlated positions.

        Returns True if trade is allowed.
        """
        try:
            groups = db.get_exposure_by_correlation_group()
        except Exception:
            # Fall back to simple exposure check
            return self.check_exposure(
                new_cost, db.get_total_exposure(), bankroll,
                category, db.get_category_exposure(category), config
            )

        # Calculate effective total exposure
        effective_total = 0
        effective_by_category = {}

        for group_key, trades in groups.items():
            cat = trades[0]["category"] if trades else "unknown"

            # If this is the group we're adding to, include the new trade
            group_trades = list(trades)
            if group_key == new_group:
                group_trades.append({"cost": new_cost, "category": category})

            costs = [t["cost"] for t in group_trades]
            group_effective = min(max(costs) * 1.5, sum(costs))

            effective_total += group_effective
            effective_by_category[cat] = effective_by_category.get(cat, 0) + group_effective

        # If the new trade starts a new group
        if new_group not in groups:
            effective_total += new_cost
            effective_by_category[category] = effective_by_category.get(category, 0) + new_cost

        # Check total
        max_total = bankroll * config.get("max_total_exposure_pct", 0.50)
        if effective_total > max_total:
            log.info(f"Blocked: correlated total ${effective_total:.2f} > ${max_total:.2f}")
            return False

        # Check category
        max_cat = bankroll * config.get("max_category_pct", 0.20)
        cat_exposure = effective_by_category.get(category, 0)
        if cat_exposure > max_cat:
            log.info(f"Blocked: {category} correlated ${cat_exposure:.2f} > ${max_cat:.2f}")
            return False

        return True
