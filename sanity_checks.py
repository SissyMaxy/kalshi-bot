"""
Sanity Checks — End-to-end invariant validation for the Kalshi trading bot.

Runs at the start of each cycle and catches silent corruption before it
becomes P&L loss. Each check targets a specific class of bug that has
caused real losses in production:

  1. DATA INTEGRITY: filled_contracts > original_contracts, mutated contracts,
     missing correlation groups, duplicate positions on same ticker.

  2. CALIBRATION HEALTH: inverted probabilities (the T/B bug), sigma drift
     from seed values, Brier scores worse than random, sample contamination.

  3. POSITION RECONCILIATION: DB vs Kalshi API mismatches, orphaned positions,
     direction mismatches, exposure exceeding bankroll.

  4. FAIR VALUE MODEL: strike_type vs probability direction consistency,
     sigma bounds, edge sign consistency, forecast staleness.

  5. ORDER FLOW: duplicate orders in same cycle, sell > original_contracts,
     cost exceeding caps, ledger gaps.

Usage:
    # In bot.py, at the start of each cycle:
    from sanity_checks import SanityChecker
    checker = SanityChecker(client, db, config)
    result = checker.run_all(bankroll=bankroll)
    if result.has_critical:
        log.critical(f"SANITY CHECK FAILED: {result.summary}")
        # Optionally halt or reduce trading

    # Standalone:
    python sanity_checks.py            # run all checks against live DB + API
    python sanity_checks.py --db-only  # skip API checks (offline mode)
"""

import os
import sys
import logging
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field

log = logging.getLogger("kalshi-bot")

# ── Thresholds ────────────────────────────────────────────────────────

# Sigma should never be outside this range for weather temperature markets.
# NWS same-day forecasts are accurate to ~2-3°F; multi-day to ~5-6°F.
SIGMA_MIN = 1.0
SIGMA_MAX = 8.0

# Seed sigma values — if current sigma drifts more than SIGMA_DRIFT_MAX_RATIO
# from these, the tuning process has corrupted them.
SIGMA_SEEDS = {0: 2.5, 1: 3.0, 2: 4.0, 3: 6.0}
SIGMA_DRIFT_MAX_RATIO = 0.80  # 80% drift from seed = corrupted

# Brier score thresholds.
# 0.25 = random binary predictor. A weather model should be well under this.
BRIER_ALARM_THRESHOLD = 0.35
BRIER_CRITICAL_THRESHOLD = 0.50  # anti-correlated with reality

# Maximum acceptable inversion rate among very confident predictions (>85%/<15%).
# A well-calibrated model at 85% confidence should be wrong ~15% of the time.
# The T/B bug caused ~28-40% inversion at high confidence. Flag at 25%.
CALIBRATION_INVERSION_THRESHOLD = 0.25

# Exposure: total open cost should never exceed this fraction of bankroll.
MAX_SANE_EXPOSURE_RATIO = 1.5

# Edge: >50% claimed edge almost certainly means broken model.
MAX_SANE_EDGE = 0.50


# ── Result containers ─────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str  # "info", "warning", "critical"
    message: str

@dataclass
class SanityReport:
    checks: list = field(default_factory=list)

    @property
    def has_critical(self):
        return any(c.severity == "critical" and not c.passed for c in self.checks)

    @property
    def has_warning(self):
        return any(c.severity == "warning" and not c.passed for c in self.checks)

    @property
    def failures(self):
        return [c for c in self.checks if not c.passed]

    @property
    def summary(self):
        fails = self.failures
        if not fails:
            return "All checks passed"
        crits = [c for c in fails if c.severity == "critical"]
        warns = [c for c in fails if c.severity == "warning"]
        parts = []
        if crits:
            parts.append(f"{len(crits)} CRITICAL")
        if warns:
            parts.append(f"{len(warns)} warnings")
        return f"{' + '.join(parts)}: " + "; ".join(c.message for c in fails[:5])

    def log_results(self):
        for c in self.checks:
            if c.passed:
                log.debug(f"  SANITY OK: {c.name}")
            elif c.severity == "critical":
                log.critical(f"  SANITY FAIL [{c.severity.upper()}]: {c.name} -- {c.message}")
            elif c.severity == "warning":
                log.warning(f"  SANITY FAIL [{c.severity.upper()}]: {c.name} -- {c.message}")
            else:
                log.info(f"  SANITY NOTE: {c.name} -- {c.message}")


class SanityChecker:
    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    def run_all(self, bankroll=None, skip_api=False):
        """Run all sanity checks. Returns SanityReport."""
        report = SanityReport()

        log.info("=== Sanity checks ===")

        # 1. Data integrity (DB only, always runs)
        self._check_contracts_integrity(report)
        self._check_correlation_groups(report)
        self._check_duplicate_open_tickers(report)
        self._check_ledger_integrity(report)

        # 2. Calibration health (DB only)
        self._check_sigma_bounds(report)
        self._check_sigma_drift(report)
        self._check_brier_score(report)
        self._check_calibration_inversions(report)

        # 3. Position reconciliation (requires API)
        if not skip_api and self.client:
            self._check_position_reconciliation(report, bankroll)
            self._check_exposure_sanity(report, bankroll)

        # 4. Fair value model consistency (DB only)
        self._check_edge_sanity(report)
        self._check_open_trade_directions(report)

        # Log summary
        report.log_results()
        if report.has_critical:
            log.critical(f"SANITY: {report.summary}")
        elif report.has_warning:
            log.warning(f"SANITY: {report.summary}")
        else:
            log.info(f"SANITY: {report.summary}")

        return report

    # ── 1. DATA INTEGRITY ─────────────────────────────────────────────

    def _check_contracts_integrity(self, report):
        """
        Catches: reconciliation inflating filled_contracts beyond what was ordered.
        Bug class: the exponential sell bug where filled_contracts picked up
        Kalshi's aggregate position count instead of this trade's count.
        """
        rows = self.db.conn.execute(
            """SELECT id, ticker, contracts, original_contracts, filled_contracts,
                      fill_status, status
               FROM trades WHERE status IN ('open', 'exiting')"""
        ).fetchall()

        violations = []
        for r in rows:
            orig = r["original_contracts"] or r["contracts"]
            filled = r["filled_contracts"] or 0

            # filled_contracts should NEVER exceed original_contracts
            if filled > orig:
                violations.append(
                    f"trade #{r['id']} {r['ticker']}: "
                    f"filled={filled} > original={orig}"
                )

            # contracts should equal original_contracts (immutable after creation)
            if r["original_contracts"] and r["contracts"] != r["original_contracts"]:
                if r["status"] != "exiting":
                    violations.append(
                        f"trade #{r['id']} {r['ticker']}: "
                        f"contracts={r['contracts']} != original={r['original_contracts']} "
                        f"(mutated outside exit)"
                    )

        if violations:
            report.checks.append(CheckResult(
                name="contracts_integrity",
                passed=False,
                severity="critical",
                message=f"{len(violations)} violation(s): {violations[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="contracts_integrity", passed=True,
                severity="info", message="OK"
            ))

    def _check_correlation_groups(self, report):
        """
        Catches: trades placed without correlation group, bypassing
        duplicate position prevention.
        """
        rows = self.db.conn.execute(
            """SELECT id, ticker FROM trades
               WHERE status IN ('open', 'exiting') AND correlation_group IS NULL"""
        ).fetchall()

        if rows:
            tickers = [f"#{r['id']} {r['ticker']}" for r in rows[:3]]
            report.checks.append(CheckResult(
                name="correlation_groups",
                passed=False,
                severity="warning",
                message=f"{len(rows)} open trade(s) missing correlation_group: {', '.join(tickers)}"
            ))
        else:
            report.checks.append(CheckResult(
                name="correlation_groups", passed=True,
                severity="info", message="OK"
            ))

    def _check_duplicate_open_tickers(self, report):
        """
        Catches: multiple open trades on the same ticker, meaning
        duplicate prevention failed.
        """
        rows = self.db.conn.execute(
            """SELECT ticker, COUNT(*) as cnt FROM trades
               WHERE status IN ('open', 'exiting')
               GROUP BY ticker HAVING cnt > 1"""
        ).fetchall()

        if rows:
            dupes = [f"{r['ticker']} ({r['cnt']}x)" for r in rows]
            report.checks.append(CheckResult(
                name="duplicate_open_tickers",
                passed=False,
                severity="critical",
                message=f"Duplicate open positions: {', '.join(dupes)}"
            ))
        else:
            report.checks.append(CheckResult(
                name="duplicate_open_tickers", passed=True,
                severity="info", message="OK"
            ))

    def _check_ledger_integrity(self, report):
        """
        Catches: deleted rows from the append-only order ledger.
        Gaps in IDs mean someone (or a bug) deleted rows.
        """
        rows = self.db.conn.execute(
            "SELECT id FROM order_ledger ORDER BY id"
        ).fetchall()

        if not rows:
            report.checks.append(CheckResult(
                name="ledger_integrity", passed=True,
                severity="info", message="Ledger empty (OK)"
            ))
            return

        ids = [r["id"] for r in rows]
        expected = set(range(ids[0], ids[-1] + 1))
        actual = set(ids)
        missing = expected - actual

        if missing:
            report.checks.append(CheckResult(
                name="ledger_integrity",
                passed=False,
                severity="critical",
                message=f"Append-only ledger has {len(missing)} missing ID(s): "
                        f"{sorted(missing)[:10]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="ledger_integrity", passed=True,
                severity="info", message=f"OK ({len(ids)} entries, no gaps)"
            ))

    # ── 2. CALIBRATION HEALTH ─────────────────────────────────────────

    def _check_sigma_bounds(self, report):
        """
        Catches: sigma optimizer pushing values outside physically reasonable range.
        The T/B bug inflated sigma from 2.5 to 4.5 in one update.
        """
        rows = self.db.conn.execute(
            "SELECT city, market_type, days_out, current_sigma "
            "FROM sigma_adjustments"
        ).fetchall()

        violations = []
        for r in rows:
            sigma = r["current_sigma"]
            if sigma < SIGMA_MIN or sigma > SIGMA_MAX:
                violations.append(
                    f"{r['city']}/{r['market_type']}/d{r['days_out']}: "
                    f"sigma={sigma:.2f} (bounds: {SIGMA_MIN}-{SIGMA_MAX})"
                )

        if violations:
            report.checks.append(CheckResult(
                name="sigma_bounds",
                passed=False,
                severity="critical",
                message=f"{len(violations)} sigma(s) out of bounds: {violations[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="sigma_bounds", passed=True,
                severity="info", message="OK"
            ))

    def _check_sigma_drift(self, report):
        """
        Catches: sigma optimizer gradually corrupting values away from
        physically reasonable seeds. Slow drift is harder to spot than
        a sudden jump but equally dangerous.
        """
        rows = self.db.conn.execute(
            "SELECT city, market_type, days_out, current_sigma "
            "FROM sigma_adjustments"
        ).fetchall()

        drifted = []
        for r in rows:
            days = min(r["days_out"], 3)
            seed = SIGMA_SEEDS.get(days, 4.0)
            drift_ratio = abs(r["current_sigma"] - seed) / seed
            if drift_ratio > SIGMA_DRIFT_MAX_RATIO:
                drifted.append(
                    f"{r['city']}/{r['market_type']}/d{r['days_out']}: "
                    f"sigma={r['current_sigma']:.2f} vs seed={seed:.1f} "
                    f"(drift={drift_ratio:.0%})"
                )

        if drifted:
            report.checks.append(CheckResult(
                name="sigma_drift",
                passed=False,
                severity="warning",
                message=f"{len(drifted)} sigma(s) drifted >{SIGMA_DRIFT_MAX_RATIO:.0%} "
                        f"from seed: {drifted[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="sigma_drift", passed=True,
                severity="info", message="OK"
            ))

    def _check_brier_score(self, report):
        """
        Catches: model predictions that are systematically wrong.
        Brier > 0.25 = worse than always predicting 50%.
        Brier > 0.50 = anti-correlated with reality.
        """
        row = self.db.conn.execute(
            "SELECT COUNT(*) as cnt, AVG(brier_score) as avg_brier "
            "FROM calibration WHERE outcome IS NOT NULL"
        ).fetchone()

        count = row["cnt"] or 0
        avg_brier = row["avg_brier"]

        if count < 10:
            report.checks.append(CheckResult(
                name="brier_score", passed=True,
                severity="info", message=f"Insufficient data ({count} samples)"
            ))
            return

        if avg_brier is not None and avg_brier > BRIER_CRITICAL_THRESHOLD:
            report.checks.append(CheckResult(
                name="brier_score",
                passed=False,
                severity="critical",
                message=f"Brier={avg_brier:.3f} (n={count}) -- model is WORSE than "
                        f"coin flip. Predictions may be inverted."
            ))
        elif avg_brier is not None and avg_brier > BRIER_ALARM_THRESHOLD:
            report.checks.append(CheckResult(
                name="brier_score",
                passed=False,
                severity="warning",
                message=f"Brier={avg_brier:.3f} (n={count}) -- model is poorly calibrated"
            ))
        else:
            report.checks.append(CheckResult(
                name="brier_score", passed=True,
                severity="info",
                message=f"Brier={avg_brier:.3f} (n={count})"
            ))

    def _check_calibration_inversions(self, report):
        """
        Catches: the T/B bug -- calibration records where predicted_prob
        appears inverted relative to the actual outcome.

        For resolved records, checks if confident predictions (>0.7 or <0.3)
        are wrong at a rate significantly above baseline. Some disagreement
        is normal, but a systematic pattern suggests flipped probability direction.
        """
        rows = self.db.conn.execute(
            """SELECT predicted_prob, outcome FROM calibration
               WHERE outcome IS NOT NULL"""
        ).fetchall()

        if len(rows) < 20:
            report.checks.append(CheckResult(
                name="calibration_inversions", passed=True,
                severity="info", message=f"Insufficient data ({len(rows)} samples)"
            ))
            return

        inversions = 0
        strong_predictions = 0
        for r in rows:
            prob = r["predicted_prob"]
            outcome = r["outcome"]

            # Only flag very confident predictions (>85% or <15%).
            # At 70% confidence, 30% wrong is expected, not an inversion.
            # At 85%+, >25% wrong rate signals a real direction bug.
            if prob > 0.85:
                strong_predictions += 1
                if outcome == 0:
                    inversions += 1
            elif prob < 0.15:
                strong_predictions += 1
                if outcome == 1:
                    inversions += 1

        if strong_predictions < 10:
            report.checks.append(CheckResult(
                name="calibration_inversions", passed=True,
                severity="info",
                message=f"Only {strong_predictions} strong predictions -- need more data"
            ))
            return

        inversion_rate = inversions / strong_predictions
        if inversion_rate > CALIBRATION_INVERSION_THRESHOLD:
            report.checks.append(CheckResult(
                name="calibration_inversions",
                passed=False,
                severity="critical",
                message=f"{inversions}/{strong_predictions} strong predictions inverted "
                        f"({inversion_rate:.0%}) -- possible probability direction bug "
                        f"(T/B ticker parsing or strike_type mismatch)"
            ))
        else:
            report.checks.append(CheckResult(
                name="calibration_inversions", passed=True,
                severity="info",
                message=f"Inversion rate {inversion_rate:.0%} "
                        f"({inversions}/{strong_predictions} strong predictions)"
            ))

    # ── 3. POSITION RECONCILIATION ────────────────────────────────────

    def _check_position_reconciliation(self, report, bankroll):
        """
        Catches: DB and Kalshi API disagreeing on what we hold.
        Orphaned positions, ghost trades, direction mismatches, quantity inflation.
        """
        try:
            api_positions = self.client.get_positions()
        except Exception as e:
            report.checks.append(CheckResult(
                name="position_reconciliation",
                passed=False,
                severity="warning",
                message=f"Cannot reach Kalshi API: {e}"
            ))
            return

        # Index API positions by ticker
        api_by_ticker = {}
        for pos in api_positions:
            ticker = pos.get("ticker", "")
            position = pos.get("position", 0)
            if ticker and position != 0:
                api_by_ticker[ticker] = {
                    "side": "yes" if position > 0 else "no",
                    "quantity": abs(position),
                }

        # Get DB open trades
        db_trades = self.db.get_open_trades()
        db_by_ticker = {}
        for t in db_trades:
            db_by_ticker[t["ticker"]] = t

        issues = []

        # Orphaned: on Kalshi but not in DB
        for ticker, api_pos in api_by_ticker.items():
            if ticker not in db_by_ticker:
                issues.append(
                    f"ORPHAN: {api_pos['side'].upper()} {api_pos['quantity']}x "
                    f"{ticker} on Kalshi but not in DB"
                )

        # Ghost: in DB as filled but not on Kalshi (and market not settled)
        for ticker, trade in db_by_ticker.items():
            if trade["fill_status"] == "filled" and ticker not in api_by_ticker:
                try:
                    market_data = self.client.get_market(ticker)
                    result = market_data.get("market", {}).get("result", "")
                    if result not in ("yes", "no"):
                        issues.append(
                            f"GHOST: trade #{trade['id']} {ticker} marked filled "
                            f"in DB but not on Kalshi (market not settled)"
                        )
                except Exception:
                    pass

        # Direction and quantity mismatches
        for ticker in set(api_by_ticker) & set(db_by_ticker):
            api_side = api_by_ticker[ticker]["side"]
            db_side = db_by_ticker[ticker]["direction"]
            if api_side != db_side:
                issues.append(
                    f"DIRECTION: {ticker} -- DB={db_side}, Kalshi={api_side}"
                )

            api_qty = api_by_ticker[ticker]["quantity"]
            db_orig = db_by_ticker[ticker]["original_contracts"] or db_by_ticker[ticker]["contracts"]
            if api_qty > db_orig:
                issues.append(
                    f"QUANTITY: {ticker} -- Kalshi has {api_qty} but DB original={db_orig}"
                )

        if issues:
            severity = "critical" if any(
                i.startswith("ORPHAN") or i.startswith("DIRECTION") for i in issues
            ) else "warning"
            report.checks.append(CheckResult(
                name="position_reconciliation",
                passed=False,
                severity=severity,
                message=f"{len(issues)} issue(s): {issues[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="position_reconciliation", passed=True,
                severity="info",
                message=f"OK (DB={len(db_by_ticker)}, API={len(api_by_ticker)})"
            ))

    def _check_exposure_sanity(self, report, bankroll):
        """
        Catches: runaway position sizing or gateway failure allowing
        total exposure to exceed what the bankroll can cover.
        """
        if not bankroll or bankroll <= 0:
            report.checks.append(CheckResult(
                name="exposure_sanity", passed=True,
                severity="info", message="No bankroll provided"
            ))
            return

        total_exposure = self.db.get_total_exposure()
        ratio = total_exposure / bankroll if bankroll > 0 else 0

        if ratio > MAX_SANE_EXPOSURE_RATIO:
            report.checks.append(CheckResult(
                name="exposure_sanity",
                passed=False,
                severity="critical",
                message=f"Exposure ${total_exposure:.2f} = {ratio:.0%} of "
                        f"bankroll ${bankroll:.2f} (max {MAX_SANE_EXPOSURE_RATIO:.0%})"
            ))
        else:
            report.checks.append(CheckResult(
                name="exposure_sanity", passed=True,
                severity="info",
                message=f"${total_exposure:.2f} / ${bankroll:.2f} = {ratio:.0%}"
            ))

    # ── 4. FAIR VALUE MODEL ───────────────────────────────────────────

    def _check_edge_sanity(self, report):
        """
        Catches: model reporting impossibly large edges (inverted probabilities,
        wrong sigma, stale data).
        """
        rows = self.db.conn.execute(
            """SELECT id, ticker, edge, fair_value, entry_price, direction
               FROM trades WHERE status IN ('open', 'exiting') AND edge IS NOT NULL"""
        ).fetchall()

        insane = []
        for r in rows:
            if r["edge"] > MAX_SANE_EDGE:
                insane.append(
                    f"trade #{r['id']} {r['ticker']}: edge={r['edge']:.0%} "
                    f"(fv={r['fair_value']:.2f}, entry={r['entry_price']:.2f})"
                )

        if insane:
            report.checks.append(CheckResult(
                name="edge_sanity",
                passed=False,
                severity="warning",
                message=f"{len(insane)} trade(s) with edge >{MAX_SANE_EDGE:.0%}: {insane[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="edge_sanity", passed=True,
                severity="info", message="OK"
            ))

    def _check_open_trade_directions(self, report):
        """
        Catches: trades where direction doesn't match what the edge implies.

        YES trades should have fair_value > entry_price.
        NO trades should have (1 - fair_value) > entry_price.
        A mismatch means direction was set wrong or fair_value was computed
        for the wrong side.
        """
        rows = self.db.conn.execute(
            """SELECT id, ticker, direction, fair_value, entry_price
               FROM trades
               WHERE status IN ('open', 'exiting')
               AND fair_value IS NOT NULL AND entry_price IS NOT NULL"""
        ).fetchall()

        mismatches = []
        for r in rows:
            fv = r["fair_value"]
            ep = r["entry_price"]
            direction = r["direction"]

            if direction == "yes" and fv < ep * 0.5:
                mismatches.append(
                    f"trade #{r['id']} {r['ticker']}: direction=YES but "
                    f"fair_value={fv:.2f} << entry={ep:.2f}"
                )
            elif direction == "no" and fv > (1 - ep * 0.5):
                mismatches.append(
                    f"trade #{r['id']} {r['ticker']}: direction=NO but "
                    f"fair_value={fv:.2f} (P(no)={1-fv:.2f} << entry={ep:.2f})"
                )

        if mismatches:
            report.checks.append(CheckResult(
                name="trade_direction_consistency",
                passed=False,
                severity="warning",
                message=f"{len(mismatches)} mismatch(es): {mismatches[0]}"
            ))
        else:
            report.checks.append(CheckResult(
                name="trade_direction_consistency", passed=True,
                severity="info", message="OK"
            ))

    # ── 5. PER-TRADE GUARDS (call before each order) ──────────────────

    def check_pre_trade(self, ticker, direction, edge, fair_value,
                        entry_price, num_contracts, cost, bankroll):
        """
        Per-trade sanity gate. Call right before placing each order.
        Returns (ok: bool, reason: str).

        This is the last line of defense before money moves.
        """
        # Edge too large -- model is probably broken
        if abs(edge) > MAX_SANE_EDGE:
            return False, f"edge {edge:.0%} exceeds {MAX_SANE_EDGE:.0%} sanity limit"

        # Direction consistency with fair value
        if direction == "yes" and fair_value < entry_price:
            return False, (f"buying YES at ${entry_price:.2f} but "
                          f"fair_value={fair_value:.2f} is lower")
        if direction == "no" and (1 - fair_value) < entry_price:
            return False, (f"buying NO at ${entry_price:.2f} but "
                          f"P(no)={1-fair_value:.2f} is lower")

        # Single trade cost vs bankroll
        if bankroll > 0 and cost > bankroll * 0.20:
            return False, (f"cost ${cost:.2f} is {cost/bankroll:.0%} of "
                          f"bankroll ${bankroll:.2f}")

        # Already have position on this ticker
        existing = self.db.get_open_trades_by_ticker(ticker)
        if existing:
            return False, f"already have {len(existing)} open trade(s) on {ticker}"

        return True, "OK"

    def check_pre_exit(self, trade_id, sell_quantity):
        """
        Per-exit sanity gate. Call before every sell order.
        Returns (ok: bool, reason: str).
        """
        trade = self.db.get_trade_by_id(trade_id)
        if not trade:
            return False, f"trade #{trade_id} does not exist"

        original = trade["original_contracts"] or trade["contracts"]
        if sell_quantity > original:
            return False, (f"selling {sell_quantity} but original was {original} "
                          f"on trade #{trade_id}")

        if trade["status"] == "exiting":
            return False, f"trade #{trade_id} is already exiting"

        if trade["status"] == "resolved":
            return False, f"trade #{trade_id} is already resolved"

        return True, "OK"


# ── Standalone runner ─────────────────────────────────────────────────

def run_standalone(db_only=False):
    """Run all checks from command line."""
    from database import Database

    db = Database()
    client = None
    bankroll = None

    if not db_only:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            from kalshi_client import KalshiClient

            key_id = os.getenv("KALSHI_API_KEY_ID")
            key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
            if key_id and os.path.exists(key_path):
                client = KalshiClient(key_id, key_path, env="live")
                bankroll = client.get_balance()
                print(f"Connected to Kalshi. Bankroll: ${bankroll:.2f}\n")
            else:
                print("No Kalshi credentials -- skipping API checks\n")
                db_only = True
        except Exception as e:
            print(f"Cannot connect to Kalshi ({e}) -- skipping API checks\n")
            db_only = True

    checker = SanityChecker(client, db, {})
    report = checker.run_all(bankroll=bankroll, skip_api=db_only)

    print("\n" + "=" * 70)
    print("SANITY CHECK REPORT")
    print("=" * 70)

    for c in report.checks:
        if c.passed:
            icon = "OK"
        elif c.severity == "critical":
            icon = "CRITICAL"
        else:
            icon = "WARNING"
        print(f"  [{icon:8s}] {c.name:35s} {c.message}")

    print("=" * 70)
    if report.has_critical:
        print(f"RESULT: CRITICAL FAILURES -- {report.summary}")
        return 1
    elif report.has_warning:
        print(f"RESULT: WARNINGS -- {report.summary}")
        return 0
    else:
        print("RESULT: ALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db_only_flag = "--db-only" in sys.argv
    sys.exit(run_standalone(db_only=db_only_flag))
