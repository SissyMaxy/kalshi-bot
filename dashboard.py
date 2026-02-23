"""Web dashboard for monitoring the Kalshi trading bot."""

import os
import sqlite3
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")

# NWS forecast page URLs by series prefix (lat/lon from fair_value.py NWS_STATIONS)
NWS_URLS = {
    "KXHIGHNY":  "https://forecast.weather.gov/MapClick.php?lat=40.7789&lon=-73.9692",
    "KXLOWNY":   "https://forecast.weather.gov/MapClick.php?lat=40.7789&lon=-73.9692",
    "KXHIGHCHI": "https://forecast.weather.gov/MapClick.php?lat=41.9742&lon=-87.9073",
    "KXLOWCHI":  "https://forecast.weather.gov/MapClick.php?lat=41.9742&lon=-87.9073",
    "KXHIGHLA":  "https://forecast.weather.gov/MapClick.php?lat=34.0236&lon=-118.2912",
    "KXHIGHDC":  "https://forecast.weather.gov/MapClick.php?lat=38.8512&lon=-77.0402",
    "KXHIGHMIA": "https://forecast.weather.gov/MapClick.php?lat=25.7959&lon=-80.287",
    "KXHIGHDEN": "https://forecast.weather.gov/MapClick.php?lat=39.8466&lon=-104.6562",
    "KXLOWDEN":  "https://forecast.weather.gov/MapClick.php?lat=39.8466&lon=-104.6562",
}


def get_db():
    """Open a fresh read-only connection per request."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/summary")
def api_summary():
    conn = get_db()
    try:
        bal_row = conn.execute(
            "SELECT timestamp, balance, peak, portfolio_value "
            "FROM balances ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if bal_row:
            balance = bal_row["balance"]
            peak = bal_row["peak"]
            last_cycle = bal_row["timestamp"]
            stored_portfolio = bal_row["portfolio_value"]
        else:
            balance, peak, last_cycle, stored_portfolio = 0, 0, None, None

        drawdown_pct = (1 - balance / peak) * 100 if peak > 0 else 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
            "WHERE resolved_at LIKE ?", (f"{today}%",)
        ).fetchone()
        daily_pnl = daily_row["total"] if daily_row else 0

        exp_row = conn.execute(
            "SELECT COALESCE(SUM(cost), 0) as total FROM trades "
            "WHERE status IN ('open', 'exiting')"
        ).fetchone()
        exposure = exp_row["total"]

        unr_row = conn.execute(
            "SELECT COALESCE(SUM(unrealized_pnl), 0) as total FROM trades "
            "WHERE status IN ('open', 'exiting')"
        ).fetchone()
        unrealized = unr_row["total"]

        # Compute portfolio from live trade data (updated each cycle by position manager)
        portfolio_value = balance + exposure + unrealized
        survival = balance < 15.0 or drawdown_pct >= 50.0

        bot_status = "unknown"
        if last_cycle:
            try:
                last_dt = datetime.fromisoformat(
                    last_cycle.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - last_dt
                           ).total_seconds() / 60
                if age_min < 15:
                    bot_status = "active"
                elif age_min < 30:
                    bot_status = "idle"
                else:
                    bot_status = "stale"
            except ValueError:
                pass

        return jsonify({
            "balance": round(balance, 2),
            "portfolio_value": round(portfolio_value, 2),
            "exposure": round(exposure, 2),
            "unrealized_pnl": round(unrealized, 2),
            "peak": round(peak, 2),
            "drawdown_pct": round(drawdown_pct, 1),
            "daily_pnl": round(daily_pnl, 2),
            "survival_mode": survival,
            "bot_status": bot_status,
            "last_cycle": last_cycle,
        })
    finally:
        conn.close()


@app.route("/api/equity")
def api_equity():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, balance, peak, portfolio_value "
            "FROM balances ORDER BY id"
        ).fetchall()
        return jsonify({
            "labels": [r["timestamp"][:16] for r in rows],
            "balances": [round(r["balance"], 2) for r in rows],
            "peaks": [round(r["peak"], 2) for r in rows],
            "portfolio_values": [
                round(r["portfolio_value"], 2) if r["portfolio_value"] else None
                for r in rows
            ],
        })
    finally:
        conn.close()


@app.route("/api/positions")
def api_positions():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, ticker, title, category, direction, contracts,
                      original_contracts, entry_price, cost, fair_value, edge,
                      status, fill_status, current_market_price, unrealized_pnl,
                      correlation_group, forecast_temp, sigma_used, timestamp,
                      current_fair_value
               FROM trades WHERE status IN ('open', 'exiting')
               ORDER BY id DESC"""
        ).fetchall()

        positions = []
        for r in rows:
            contracts = r["original_contracts"] or r["contracts"]
            mid = r["current_market_price"] or 0
            if r["unrealized_pnl"] is not None:
                unrealized = r["unrealized_pnl"]
            elif mid > 0:
                unrealized = (mid - r["entry_price"]) * contracts
            else:
                unrealized = 0
            pnl_pct = (unrealized / r["cost"] * 100) if r["cost"] > 0 else 0

            # Build NWS URL from series prefix
            series = r["ticker"].split("-")[0] if r["ticker"] else ""
            nws_url = NWS_URLS.get(series)

            # Compute current edge from live fair value
            cfv = r["current_fair_value"]
            current_edge = None
            if cfv is not None and mid > 0:
                if r["direction"] == "yes":
                    current_edge = round(cfv - mid, 4)
                else:
                    current_edge = round(mid - cfv, 4)

            positions.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "title": r["title"],
                "category": r["category"],
                "direction": r["direction"],
                "contracts": contracts,
                "entry_price": round(r["entry_price"], 2),
                "cost": round(r["cost"], 2),
                "current_price": round(mid, 2) if mid else None,
                "unrealized_pnl": round(unrealized, 2),
                "pnl_pct": round(pnl_pct, 1),
                "entry_edge": round(r["edge"], 3) if r["edge"] else None,
                "current_edge": current_edge,
                "fair_value": round(r["fair_value"], 4) if r["fair_value"] else None,
                "current_fair_value": round(cfv, 4) if cfv else None,
                "forecast_temp": r["forecast_temp"],
                "sigma_used": round(r["sigma_used"], 2) if r["sigma_used"] else None,
                "status": r["status"],
                "fill_status": r["fill_status"],
                "opened_at": r["timestamp"],
                "nws_url": nws_url,
            })
        return jsonify({"positions": positions})
    finally:
        conn.close()


@app.route("/api/recent-trades")
def api_recent_trades():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, ticker, category, direction, contracts,
                      entry_price, exit_price, pnl, exit_reason, resolved_at, edge
               FROM trades WHERE status = 'resolved' AND pnl IS NOT NULL
               ORDER BY id DESC LIMIT 20"""
        ).fetchall()
        trades = []
        for r in rows:
            trades.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "category": r["category"],
                "direction": r["direction"],
                "contracts": r["contracts"],
                "entry_price": round(r["entry_price"], 2),
                "exit_price": round(r["exit_price"], 2) if r["exit_price"] else None,
                "pnl": round(r["pnl"], 2),
                "exit_reason": r["exit_reason"],
                "resolved_at": r["resolved_at"],
                "edge": round(r["edge"], 3) if r["edge"] else None,
                "won": (r["pnl"] or 0) > 0,
            })
        return jsonify({"trades": trades})
    finally:
        conn.close()


@app.route("/api/performance")
def api_performance():
    conn = get_db()
    try:
        all_resolved = conn.execute(
            "SELECT pnl, category, exit_reason FROM trades "
            "WHERE status = 'resolved' AND pnl IS NOT NULL"
        ).fetchall()

        total = len(all_resolved)
        wins = sum(1 for r in all_resolved if r["pnl"] > 0)
        total_pnl = sum(r["pnl"] for r in all_resolved)

        by_category = {}
        for r in all_resolved:
            cat = r["category"] or "unknown"
            if cat not in by_category:
                by_category[cat] = {"total": 0, "wins": 0, "pnl": 0}
            by_category[cat]["total"] += 1
            if r["pnl"] > 0:
                by_category[cat]["wins"] += 1
            by_category[cat]["pnl"] += r["pnl"]
        for s in by_category.values():
            s["win_rate"] = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0
            s["pnl"] = round(s["pnl"], 2)

        by_exit = {}
        for r in all_resolved:
            reason = r["exit_reason"] or "unknown"
            if reason not in by_exit:
                by_exit[reason] = {"total": 0, "wins": 0, "pnl": 0}
            by_exit[reason]["total"] += 1
            if r["pnl"] > 0:
                by_exit[reason]["wins"] += 1
            by_exit[reason]["pnl"] += r["pnl"]
        for s in by_exit.values():
            s["win_rate"] = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0
            s["pnl"] = round(s["pnl"], 2)

        cal_row = conn.execute(
            "SELECT COUNT(*) as count, AVG(brier_score) as avg_brier "
            "FROM calibration WHERE outcome IS NOT NULL"
        ).fetchone()

        return jsonify({
            "overall": {
                "total": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
                "total_pnl": round(total_pnl, 2),
            },
            "by_category": by_category,
            "by_exit_reason": by_exit,
            "calibration": {
                "samples": cal_row["count"] or 0,
                "avg_brier": round(cal_row["avg_brier"], 4) if cal_row["avg_brier"] else None,
            },
        })
    finally:
        conn.close()


@app.route("/api/learned-params")
def api_learned_params():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT lp.* FROM learned_params lp
               INNER JOIN (
                   SELECT param_name, param_scope, MAX(id) as max_id
                   FROM learned_params
                   GROUP BY param_name, param_scope
               ) latest ON lp.id = latest.max_id
               ORDER BY lp.timestamp DESC"""
        ).fetchall()
        params = []
        for r in rows:
            params.append({
                "param_name": r["param_name"],
                "param_scope": r["param_scope"],
                "old_value": round(r["old_value"], 4),
                "new_value": round(r["new_value"], 4),
                "reason": r["reason"],
                "sample_count": r["sample_count"],
                "win_rate": round(r["win_rate"] * 100, 1) if r["win_rate"] else None,
                "avg_pnl": round(r["avg_pnl"], 2) if r["avg_pnl"] else None,
                "timestamp": r["timestamp"],
            })
        return jsonify({"params": params})
    finally:
        conn.close()


@app.route("/api/ledger")
def api_ledger():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM order_ledger ORDER BY id DESC LIMIT 20"
        ).fetchall()
        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "action": r["action"],
                "ticker": r["ticker"],
                "side": r["side"],
                "contracts": r["contracts"],
                "price_cents": r["price_cents"],
                "order_id": r["order_id"],
                "result": r["result"],
            })
        return jsonify({"entries": entries})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
