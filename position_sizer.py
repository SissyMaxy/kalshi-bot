"""
Position Sizer -- Kelly Criterion
Calculates optimal position sizes for binary event contracts.
Supports dynamic accuracy multiplier for adaptive sizing.
"""

import logging

log = logging.getLogger("kalshi-bot")


def compute_accuracy_multiplier(db):
    """
    Compute Kelly scaling factor based on model calibration.
    Returns 0.2 to 1.0.

    - < 20 samples: 0.4 (conservative learning mode)
    - Brier < 0.15: 1.0 (excellent, full Kelly)
    - Brier 0.15-0.25: 0.7
    - Brier 0.25-0.35: 0.4
    - Brier > 0.35: 0.2 (poor, minimal sizing)
    """
    stats = db.get_calibration_stats()

    if stats["count"] < 20:
        return 0.4  # conservative until we have data

    brier = stats["avg_brier"]
    if brier < 0.15:
        return 1.0
    elif brier < 0.25:
        return 0.7
    elif brier < 0.35:
        return 0.4
    else:
        return 0.2


class PositionSizer:
    def calculate(self, edge, entry_price, bankroll, config, accuracy_mult=1.0,
                  confidence_mult=1.0):
        """
        Calculate position size in dollars using fractional Kelly.

        Args:
            edge: absolute edge (e.g., 0.12 = 12%)
            entry_price: price we'd pay per contract ($0.01 - $0.99)
            bankroll: current bankroll in dollars
            config: strategy config dict
            accuracy_mult: 0.2-1.0 scaling factor from calibration
            confidence_mult: 1.0-3.0 boost for high-sigma weather trades

        Returns:
            Position size in dollars (0 if no trade).
        """
        if edge <= 0 or entry_price <= 0 or entry_price >= 1 or bankroll <= 0:
            return 0

        survival = bankroll < config.get("survival_mode_threshold", 15.0)

        # Kelly fraction for binary bet:
        # f* = edge / (1 - entry_price)
        kelly_fraction = edge / (1 - entry_price)

        # Apply Kelly multiplier scaled by accuracy
        kelly_mult = config.get("kelly_multiplier", 0.25) * accuracy_mult
        adjusted_fraction = kelly_fraction * kelly_mult

        # Calculate dollar amount (boosted by confidence for high-sigma trades)
        position = adjusted_fraction * bankroll * confidence_mult

        # Apply caps
        max_position = bankroll * config.get("max_position_pct", 0.06)
        position = min(position, max_position)

        # Survival mode: if Kelly says position is too small but we can
        # afford 1 contract and the edge is strong, override to 1 contract.
        # At low bankroll, Kelly is too conservative to allow recovery.
        if survival and position < entry_price and entry_price <= max_position:
            position = entry_price  # exactly 1 contract
            log.debug(f"Survival override: 1 contract @ ${entry_price:.2f}")

        # Floor at $1 (Kalshi minimum) — except survival allows smaller
        min_trade = entry_price if survival else 1.0
        if position < min_trade:
            return 0

        # Round to whole contracts
        num_contracts = int(position / entry_price)
        if num_contracts < 1:
            return 0

        final_cost = num_contracts * entry_price

        log.debug(
            f"Sizing: edge={edge:.3f} price=${entry_price:.2f} "
            f"kelly={kelly_fraction:.3f} adj={adjusted_fraction:.3f} "
            f"acc_mult={accuracy_mult:.1f} surv={'Y' if survival else 'N'} "
            f"-> {num_contracts} contracts @ ${entry_price:.2f} = ${final_cost:.2f}"
        )

        return final_cost
