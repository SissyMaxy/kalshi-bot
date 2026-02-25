# Sanity Checks Integration Guide

## What it catches (mapped to actual bugs)

| Check | Bug it would have caught | Severity |
|---|---|---|
| `contracts_integrity` | Reconciliation inflating filled_contracts from Kalshi aggregate -> exponential sell | CRITICAL |
| `duplicate_open_tickers` | Duplicate bot instances placing overlapping orders | CRITICAL |
| `sigma_bounds` | T/B optimizer pushing sigma from 2.5 -> 4.5 in one update | CRITICAL |
| `sigma_drift` | Gradual sigma corruption over multiple optimizer runs | WARNING |
| `calibration_inversions` | T/B bug inverting ~28% of calibration samples | CRITICAL |
| `brier_score` | Model being worse than random (undetected for weeks) | CRITICAL |
| `position_reconciliation` | Orphaned positions, direction mismatches between DB and Kalshi | CRITICAL |
| `edge_sanity` | Model claiming 30-47% edge on crypto/financial (actual win rate ~47%) | WARNING |
| `trade_direction_consistency` | Fair value computed for wrong side of the bet | WARNING |
| `ledger_integrity` | Deleted rows from append-only audit trail | CRITICAL |
| `correlation_groups` | Missing correlation group bypassing duplicate prevention | WARNING |
| `exposure_sanity` | Total exposure exceeding bankroll due to sizing bugs | CRITICAL |

## Wire into bot.py

Add to imports at top of `bot.py`:

```python
from sanity_checks import SanityChecker
```

Add to `run_cycle()`, right after PHASE 2 (bankroll & risk), before PHASE 3:

```python
    # -- PHASE 2.5: SANITY CHECKS --
    if not scan_only:
        try:
            checker = SanityChecker(client, db, cycle_config)
            sanity = checker.run_all(bankroll=bankroll, skip_api=False)
            if sanity.has_critical:
                log.critical(f"SANITY CHECK CRITICAL: {sanity.summary}")
                log.critical("Halting new trades this cycle. Positions still managed.")
                halted = True
        except Exception as e:
            log.error(f"Sanity check error (non-fatal): {e}")
```

## Wire pre-trade guard into the trade execution loop

In the trade execution section of `run_cycle()`, add before `place_order_safe`:

```python
        # Pre-trade sanity gate
        if not scan_only:
            ok, reason = checker.check_pre_trade(
                ticker=market["ticker"],
                direction=direction,
                edge=edge,
                fair_value=fair_value,
                entry_price=entry_price,
                num_contracts=num_contracts,
                cost=position_cost,
                bankroll=bankroll,
            )
            if not ok:
                log.warning(f"  SANITY BLOCKED: {market['ticker']} -- {reason}")
                continue
```

## Wire pre-exit guard into position_manager.py

In `PositionManager.execute_exit()`, add before `place_order_safe`:

```python
        # Pre-exit sanity gate
        from sanity_checks import SanityChecker
        checker = SanityChecker(self.client, self.db, self.config)
        ok, reason = checker.check_pre_exit(action["trade_id"], contracts)
        if not ok:
            log.critical(f"  SANITY BLOCKED EXIT: trade #{trade['id']} -- {reason}")
            return False
```

## Run standalone

```bash
# Full check (DB + Kalshi API)
python sanity_checks.py

# DB-only (no API connection needed)
python sanity_checks.py --db-only
```

## What it does NOT check (future additions)

- NWS forecast staleness (is the forecast we're using actually current?)
- Settlement outcome vs calibration predicted_prob consistency
- Rate of P&L decline (detecting slow bleed vs sharp loss)
- Cross-market correlation (are we accidentally correlated across cities?)
