"""
Database — SQLite storage for trades, balances, calibration, and bot state.
"""

import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger("kalshi-bot")


class Database:
    def __init__(self, path="bot_data.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._run_migrations()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                title TEXT,
                category TEXT,
                direction TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                cost REAL NOT NULL,
                fair_value REAL,
                edge REAL,
                status TEXT DEFAULT 'open',
                exit_price REAL,
                pnl REAL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                peak REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                starting_balance REAL,
                ending_balance REAL,
                trades_placed INTEGER DEFAULT 0,
                trades_won INTEGER DEFAULT 0,
                trades_lost INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def _run_migrations(self):
        """Run additive schema migrations. Safe to call repeatedly."""
        row = self.conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        ).fetchone()
        current = row["v"] if row and row["v"] else 0

        if current < 1:
            self._migrate_v1()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

        if current < 2:
            self._migrate_v2()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (2, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

        if current < 3:
            self._migrate_v3()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (3, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

        if current < 4:
            self._migrate_v4()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (4, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

        if current < 5:
            self._migrate_v5()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (5, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

        if current < 6:
            self._migrate_v6()
            self.conn.execute(
                "INSERT INTO schema_version VALUES (6, ?)",
                (datetime.now(timezone.utc).isoformat(),)
            )
            self.conn.commit()

    def _migrate_v1(self):
        """V1: Add order tracking, calibration, and sigma tables."""
        # New columns on trades (each wrapped in try/except for idempotency)
        new_columns = [
            ("order_id", "TEXT"),
            ("fill_status", "TEXT DEFAULT 'unknown'"),
            ("filled_contracts", "INTEGER DEFAULT 0"),
            ("filled_avg_price", "REAL"),
            ("current_market_price", "REAL"),
            ("unrealized_pnl", "REAL DEFAULT 0"),
            ("correlation_group", "TEXT"),
            ("sigma_used", "REAL"),
            ("forecast_temp", "REAL"),
        ]
        for col_name, col_type in new_columns:
            try:
                self.conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # Calibration table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                market_date TEXT NOT NULL,
                city TEXT NOT NULL,
                market_type TEXT NOT NULL,
                predicted_prob REAL NOT NULL,
                market_price REAL NOT NULL,
                sigma_used REAL NOT NULL,
                forecast_temp REAL NOT NULL,
                actual_temp REAL,
                outcome INTEGER,
                brier_score REAL,
                logged_at TEXT NOT NULL,
                resolved_at TEXT
            )
        """)

        # Sigma adjustments table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sigma_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                market_type TEXT NOT NULL,
                days_out INTEGER NOT NULL,
                current_sigma REAL NOT NULL,
                sample_count INTEGER DEFAULT 0,
                rolling_brier REAL,
                last_updated TEXT NOT NULL,
                UNIQUE(city, market_type, days_out)
            )
        """)

        # Seed default sigma values
        now = datetime.now(timezone.utc).isoformat()
        defaults = [
            ("*", "high", 0, 2.5), ("*", "high", 1, 2.5),
            ("*", "high", 2, 4.0), ("*", "high", 3, 6.0),
            ("*", "low", 0, 2.5), ("*", "low", 1, 2.5),
            ("*", "low", 2, 4.0), ("*", "low", 3, 6.0),
        ]
        for city, mtype, days, sigma in defaults:
            try:
                self.conn.execute(
                    """INSERT INTO sigma_adjustments
                       (city, market_type, days_out, current_sigma, last_updated)
                       VALUES (?, ?, ?, ?, ?)""",
                    (city, mtype, days, sigma, now)
                )
            except sqlite3.IntegrityError:
                pass  # already seeded

        self.conn.commit()
        log.info("Database migrated to v1")

    def _migrate_v2(self):
        """V2: Add learned_params table and exit_reason column."""
        # New column on trades
        try:
            self.conn.execute(
                "ALTER TABLE trades ADD COLUMN exit_reason TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # Learned parameters table for strategy adaptation audit trail
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                param_name TEXT NOT NULL,
                param_scope TEXT NOT NULL,
                old_value REAL NOT NULL,
                new_value REAL NOT NULL,
                reason TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                win_rate REAL,
                avg_pnl REAL
            )
        """)

        self.conn.commit()
        log.info("Database migrated to v2")

    def _migrate_v3(self):
        """V3: Add original_contracts column — immutable after insertion."""
        try:
            self.conn.execute(
                "ALTER TABLE trades ADD COLUMN original_contracts INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # Backfill: copy contracts into original_contracts for all existing rows
        self.conn.execute(
            "UPDATE trades SET original_contracts = contracts "
            "WHERE original_contracts IS NULL"
        )
        self.conn.commit()
        log.info("Database migrated to v3")

    def _migrate_v4(self):
        """V4: Append-only order ledger."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS order_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                order_id TEXT,
                result TEXT NOT NULL
            )
        """)
        self.conn.commit()
        log.info("Database migrated to v4")

    def _migrate_v6(self):
        """V6: Add current_fair_value column to trades for live edge tracking."""
        try:
            self.conn.execute(
                "ALTER TABLE trades ADD COLUMN current_fair_value REAL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()
        log.info("Database migrated to v6")

    def _migrate_v5(self):
        """V5: Add portfolio_value column to balances table."""
        try:
            self.conn.execute(
                "ALTER TABLE balances ADD COLUMN portfolio_value REAL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()
        log.info("Database migrated to v5")

    # ── Trade methods ──────────────────────────────────────────────────

    def log_trade(self, ticker, title, category, direction, contracts,
                  entry_price, cost, fair_value, edge,
                  sigma_used=None, forecast_temp=None):
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT INTO trades
               (timestamp, ticker, title, category, direction, contracts,
                original_contracts,
                entry_price, cost, fair_value, edge, sigma_used, forecast_temp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, ticker, title, category, direction, contracts,
             contracts,
             entry_price, cost, fair_value, edge, sigma_used, forecast_temp)
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_open_trades(self):
        return self.conn.execute(
            "SELECT * FROM trades WHERE status IN ('open', 'exiting')"
        ).fetchall()

    def get_trade_by_id(self, trade_id):
        return self.conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()

    def get_open_trades_by_ticker(self, ticker):
        return self.conn.execute(
            "SELECT * FROM trades WHERE ticker = ? AND status IN ('open', 'exiting')",
            (ticker,)
        ).fetchall()

    def update_trade_fill(self, trade_id, fill_status, filled_contracts,
                          filled_avg_price=None):
        self.conn.execute(
            """UPDATE trades SET fill_status = ?, filled_contracts = ?,
               filled_avg_price = ? WHERE id = ?""",
            (fill_status, filled_contracts, filled_avg_price, trade_id)
        )
        self.conn.commit()

    def update_trade_market_price(self, trade_id, current_price, unrealized_pnl,
                                  current_fair_value=None):
        self.conn.execute(
            """UPDATE trades SET current_market_price = ?,
               unrealized_pnl = ?, current_fair_value = ? WHERE id = ?""",
            (current_price, unrealized_pnl, current_fair_value, trade_id)
        )
        self.conn.commit()

    def update_trade_order_id(self, trade_id, order_id):
        self.conn.execute(
            "UPDATE trades SET order_id = ? WHERE id = ?",
            (order_id, trade_id)
        )
        self.conn.commit()

    def update_trade_status(self, trade_id, status):
        self.conn.execute(
            "UPDATE trades SET status = ? WHERE id = ?",
            (status, trade_id)
        )
        self.conn.commit()

    def set_correlation_group(self, trade_id, group_key):
        self.conn.execute(
            "UPDATE trades SET correlation_group = ? WHERE id = ?",
            (group_key, trade_id)
        )
        self.conn.commit()

    def resolve_trade(self, trade_id, exit_price, pnl):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE trades SET status = 'resolved', exit_price = ?,
               pnl = ?, resolved_at = ? WHERE id = ?""",
            (exit_price, pnl, now, trade_id)
        )
        self.conn.commit()

    def set_exit_reason(self, trade_id, exit_reason):
        self.conn.execute(
            "UPDATE trades SET exit_reason = ? WHERE id = ?",
            (exit_reason, trade_id)
        )
        self.conn.commit()

    def log_to_ledger(self, action, ticker, side, contracts, price_cents,
                      order_id, result):
        """Append-only order ledger. No updates, no deletes — ever."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO order_ledger
               (timestamp, action, ticker, side, contracts, price_cents,
                order_id, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, action, ticker, side, contracts, price_cents,
             order_id, result)
        )
        self.conn.commit()

    def get_resolved_trades_by_category(self):
        """Returns {category: [trade_dicts]} for all resolved trades with pnl."""
        rows = self.conn.execute(
            """SELECT id, ticker, category, direction, entry_price, cost,
                      fair_value, edge, exit_price, pnl, exit_reason, resolved_at
               FROM trades
               WHERE status = 'resolved' AND pnl IS NOT NULL"""
        ).fetchall()
        result = {}
        for r in rows:
            cat = r["category"] or "unknown"
            result.setdefault(cat, []).append(dict(r))
        return result

    def get_resolved_trades_by_edge_bucket(self):
        """Returns {bucket: [trade_dicts]} bucketed by edge size."""
        rows = self.conn.execute(
            """SELECT id, ticker, category, direction, entry_price, cost,
                      edge, pnl, exit_reason, resolved_at
               FROM trades
               WHERE status = 'resolved' AND pnl IS NOT NULL AND edge IS NOT NULL"""
        ).fetchall()
        buckets = {}
        for r in rows:
            edge = r["edge"]
            if edge < 0.12:
                label = "08-12"
            elif edge < 0.16:
                label = "12-16"
            elif edge < 0.20:
                label = "16-20"
            else:
                label = "20+"
            buckets.setdefault(label, []).append(dict(r))
        return buckets

    def get_resolved_trades_by_exit_reason(self):
        """Returns {exit_reason: [trade_dicts]} for resolved trades."""
        rows = self.conn.execute(
            """SELECT id, ticker, category, direction, entry_price, cost,
                      edge, pnl, exit_reason, resolved_at
               FROM trades
               WHERE status = 'resolved' AND pnl IS NOT NULL
                     AND exit_reason IS NOT NULL"""
        ).fetchall()
        result = {}
        for r in rows:
            reason = r["exit_reason"] or "unknown"
            result.setdefault(reason, []).append(dict(r))
        return result

    # ── Learned params methods ────────────────────────────────────────

    def log_learned_param(self, param_name, param_scope, old_value, new_value,
                          reason, sample_count, win_rate=None, avg_pnl=None):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO learned_params
               (timestamp, param_name, param_scope, old_value, new_value,
                reason, sample_count, win_rate, avg_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, param_name, param_scope, old_value, new_value,
             reason, sample_count, win_rate, avg_pnl)
        )
        self.conn.commit()

    def get_latest_learned_param(self, param_name, param_scope):
        """Get the most recent learned value for a param+scope."""
        row = self.conn.execute(
            """SELECT * FROM learned_params
               WHERE param_name = ? AND param_scope = ?
               ORDER BY id DESC LIMIT 1""",
            (param_name, param_scope)
        ).fetchone()
        return dict(row) if row else None

    def get_learned_params_active(self):
        """Get all distinct latest learned param values."""
        rows = self.conn.execute(
            """SELECT lp.* FROM learned_params lp
               INNER JOIN (
                   SELECT param_name, param_scope, MAX(id) as max_id
                   FROM learned_params
                   GROUP BY param_name, param_scope
               ) latest ON lp.id = latest.max_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Exposure methods ───────────────────────────────────────────────

    def get_total_exposure(self):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) as total FROM trades WHERE status = 'open'"
        ).fetchone()
        return row["total"]

    def get_category_exposure(self, category):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) as total FROM trades "
            "WHERE status = 'open' AND category = ?",
            (category,)
        ).fetchone()
        return row["total"]

    def get_filled_exposure(self):
        """Exposure based only on confirmed fills."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(filled_contracts * entry_price), 0) as total "
            "FROM trades WHERE status IN ('open', 'exiting') AND fill_status = 'filled'"
        ).fetchone()
        return row["total"]

    def get_exposure_by_correlation_group(self):
        """Get open trades grouped by correlation group."""
        rows = self.conn.execute(
            """SELECT id, ticker, category, cost, correlation_group, fill_status,
                      filled_contracts, entry_price, direction
               FROM trades WHERE status = 'open'"""
        ).fetchall()
        groups = {}
        for row in rows:
            key = row["correlation_group"] or f"ungrouped_{row['id']}"
            if key not in groups:
                groups[key] = []
            groups[key].append(dict(row))
        return groups

    # ── Balance methods ────────────────────────────────────────────────

    def log_balance(self, balance, portfolio_value=None):
        now = datetime.now(timezone.utc).isoformat()
        peak = self.get_peak_balance()
        peak = max(peak, balance)
        self.conn.execute(
            "INSERT INTO balances (timestamp, balance, peak, portfolio_value) "
            "VALUES (?, ?, ?, ?)",
            (now, balance, peak, portfolio_value)
        )
        self.conn.commit()

    def get_peak_balance(self):
        row = self.conn.execute(
            "SELECT MAX(peak) as peak FROM balances"
        ).fetchone()
        return row["peak"] if row and row["peak"] else 0

    def get_daily_pnl(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE resolved_at LIKE ?",
            (f"{today}%",)
        ).fetchone()
        return row["total"]

    # ── Calibration methods ────────────────────────────────────────────

    def log_calibration(self, ticker, market_date, city, market_type,
                        predicted_prob, market_price, sigma_used, forecast_temp):
        # Avoid duplicate entries for same ticker in same cycle
        existing = self.conn.execute(
            "SELECT id FROM calibration WHERE ticker = ? AND market_date = ? "
            "AND ABS(predicted_prob - ?) < 0.001",
            (ticker, market_date, predicted_prob)
        ).fetchone()
        if existing:
            return

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO calibration
               (ticker, market_date, city, market_type, predicted_prob,
                market_price, sigma_used, forecast_temp, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, market_date, city, market_type, predicted_prob,
             market_price, sigma_used, forecast_temp, now)
        )
        self.conn.commit()

    def check_settlements(self, client):
        """Check unresolved calibration records for settled markets."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        unresolved = self.conn.execute(
            "SELECT * FROM calibration WHERE outcome IS NULL AND market_date < ?",
            (today,)
        ).fetchall()

        resolved_count = 0
        for record in unresolved:
            try:
                market_data = client.get_market(record["ticker"])
                market = market_data.get("market", {})
                result = market.get("result")
                # API returns '' for unsettled, 'yes'/'no' for settled
                if result in ("yes", "no"):
                    outcome = 1 if result == "yes" else 0
                    brier = (record["predicted_prob"] - outcome) ** 2
                    self.conn.execute(
                        """UPDATE calibration SET outcome = ?, brier_score = ?,
                           resolved_at = ? WHERE id = ?""",
                        (outcome, brier,
                         datetime.now(timezone.utc).isoformat(), record["id"])
                    )
                    resolved_count += 1
            except Exception as e:
                log.debug(f"Settlement check failed for {record['ticker']}: {e}")
                continue

        if resolved_count:
            self.conn.commit()
            log.info(f"Resolved {resolved_count} calibration records")

        # Also resolve open trades for settled markets
        # Fetch live API positions to detect direction mismatches
        api_positions = {}
        try:
            positions = client.get_positions()
            for pos in positions:
                ticker = pos.get("ticker", "")
                position = pos.get("position", 0)
                if ticker and position != 0:
                    api_positions[ticker] = {
                        "side": "yes" if position > 0 else "no",
                        "quantity": abs(position),
                    }
        except Exception:
            pass  # fall back to DB direction if API fails

        open_trades = self.get_open_trades()
        for trade in open_trades:
            try:
                market_data = client.get_market(trade["ticker"])
                market = market_data.get("market", {})
                result = market.get("result")
                # API returns '' for unsettled, 'yes'/'no' for settled
                if result in ("yes", "no"):
                    # Use actual API direction if available (handles legacy mismatches)
                    actual_direction = trade["direction"]
                    contracts = trade["filled_contracts"] or trade["contracts"]
                    api_pos = api_positions.get(trade["ticker"])
                    if api_pos and api_pos["side"] != trade["direction"]:
                        log.warning(
                            f"Settlement direction fix: trade #{trade['id']} "
                            f"DB={trade['direction']} -> API={api_pos['side']}"
                        )
                        actual_direction = api_pos["side"]
                        contracts = api_pos["quantity"]

                    won = (result == "yes" and actual_direction == "yes") or \
                          (result == "no" and actual_direction == "no")
                    if won:
                        pnl = contracts * (1.0 - trade["entry_price"])
                    else:
                        pnl = -(contracts * trade["entry_price"])
                    self.resolve_trade(trade["id"], 1.0 if won else 0.0, pnl)
                    # Set exit_reason if not already set (early exits already have one)
                    self.conn.execute(
                        "UPDATE trades SET exit_reason = 'settlement' "
                        "WHERE id = ? AND exit_reason IS NULL",
                        (trade["id"],)
                    )
                    self.conn.commit()
                    log.info(f"Settled trade #{trade['id']} {trade['ticker']}: "
                             f"{'WON' if won else 'LOST'} ${pnl:+.2f} "
                             f"(dir={actual_direction})")
            except Exception:
                continue

    def get_calibration_stats(self):
        """Get overall calibration stats for accuracy multiplier."""
        row = self.conn.execute(
            "SELECT COUNT(*) as count, AVG(brier_score) as avg_brier "
            "FROM calibration WHERE outcome IS NOT NULL"
        ).fetchone()
        return {
            "count": row["count"] or 0,
            "avg_brier": row["avg_brier"] if row["avg_brier"] is not None else 0.5,
        }

    def get_sigma(self, city, market_type, days_out):
        """Get current sigma for a city/type/days_out bucket."""
        days_bucket = min(days_out, 3)
        # Try city-specific first
        row = self.conn.execute(
            "SELECT current_sigma FROM sigma_adjustments "
            "WHERE city = ? AND market_type = ? AND days_out = ?",
            (city, market_type, days_bucket)
        ).fetchone()
        if row:
            return row["current_sigma"]
        # Fall back to wildcard
        row = self.conn.execute(
            "SELECT current_sigma FROM sigma_adjustments "
            "WHERE city = '*' AND market_type = ? AND days_out = ?",
            (market_type, days_bucket)
        ).fetchone()
        return row["current_sigma"] if row else None

    def get_calibration_records(self, market_type, days_out_max, limit=50):
        """Get recent resolved calibration records for sigma tuning."""
        return self.conn.execute(
            """SELECT * FROM calibration
               WHERE outcome IS NOT NULL AND market_type = ?
               ORDER BY resolved_at DESC LIMIT ?""",
            (market_type, limit)
        ).fetchall()

    def update_sigma(self, city, market_type, days_out, new_sigma, brier):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO sigma_adjustments
                   (city, market_type, days_out, current_sigma, rolling_brier, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(city, market_type, days_out)
               DO UPDATE SET current_sigma = ?, rolling_brier = ?, last_updated = ?""",
            (city, market_type, days_out, new_sigma, brier, now,
             new_sigma, brier, now)
        )
        self.conn.commit()

    # ── Daily stats ────────────────────────────────────────────────────

    def update_daily_stats(self, current_balance, trades_placed_this_cycle):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = self.conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        ).fetchone()

        if existing:
            self.conn.execute(
                """UPDATE daily_stats SET ending_balance = ?,
                   trades_placed = trades_placed + ? WHERE date = ?""",
                (current_balance, trades_placed_this_cycle, today)
            )
        else:
            last = self.conn.execute(
                "SELECT balance FROM balances ORDER BY id DESC LIMIT 1"
            ).fetchone()
            starting = last["balance"] if last else current_balance
            self.conn.execute(
                """INSERT INTO daily_stats
                   (date, starting_balance, ending_balance, trades_placed, pnl)
                   VALUES (?, ?, ?, ?, ?)""",
                (today, starting, current_balance, trades_placed_this_cycle,
                 current_balance - starting)
            )
        self.conn.commit()
