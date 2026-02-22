#!/usr/bin/env python3
"""Quick trade performance report."""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, ticker, title, category, direction, contracts, entry_price, cost,
           fair_value, edge, status, fill_status, pnl, unrealized_pnl,
           current_market_price, correlation_group, exit_reason
    FROM trades ORDER BY id
""").fetchall()

resolved = [dict(r) for r in rows if r["status"] == "resolved" and r["pnl"] is not None]
open_trades = [dict(r) for r in rows if r["status"] in ("open", "exiting")]

for t in open_trades:
    mid = t.get("current_market_price") or 0
    c = t["contracts"]
    if t["direction"] == "yes":
        t["est_pnl"] = (mid - t["entry_price"]) * c
    else:
        no_val = 1.0 - mid if mid > 0 else 0
        t["est_pnl"] = (no_val - t["entry_price"]) * c
    t["est_pnl_pct"] = t["est_pnl"] / t["cost"] * 100 if t["cost"] > 0 else 0

print("=== RESOLVED TRADES ===")
if resolved:
    resolved.sort(key=lambda t: t["pnl"], reverse=True)
    total = sum(t["pnl"] for t in resolved)
    wins = sum(1 for t in resolved if t["pnl"] > 0)
    print(f"{len(resolved)} trades | {wins}W {len(resolved)-wins}L | PnL ${total:+.2f}\n")
    for t in resolved:
        tag = "WON " if t["pnl"] > 0 else "LOST"
        print(f"  #{t['id']:2d} {tag} ${t['pnl']:+6.2f} | {t['direction']:3s} {t['contracts']}x {t['ticker'][:35]:35s} @ ${t['entry_price']:.2f} | edge={t['edge']:.2f}")
else:
    print("  No resolved trades yet")

print("\n=== OPEN TRADES (by estimated P&L) ===")
open_trades.sort(key=lambda t: t["est_pnl"], reverse=True)
total_unr = sum(t["est_pnl"] for t in open_trades)
profitable = sum(1 for t in open_trades if t["est_pnl"] > 0)
print(f"{len(open_trades)} positions | {profitable} profitable, {len(open_trades)-profitable} underwater")
print(f"Unrealized total: ${total_unr:+.2f}\n")

print("BEST 5:")
for t in open_trades[:5]:
    mid = t.get("current_market_price") or 0
    print(f"  #{t['id']:2d} ${t['est_pnl']:+6.2f} ({t['est_pnl_pct']:+.0f}%) | {t['direction']:3s} {t['contracts']}x {t['ticker'][:35]:35s} @ ${t['entry_price']:.2f} mid=${mid:.2f}")

print("\nWORST 5:")
for t in open_trades[-5:]:
    mid = t.get("current_market_price") or 0
    print(f"  #{t['id']:2d} ${t['est_pnl']:+6.2f} ({t['est_pnl_pct']:+.0f}%) | {t['direction']:3s} {t['contracts']}x {t['ticker'][:35]:35s} @ ${t['entry_price']:.2f} mid=${mid:.2f}")

print("\n=== BY CATEGORY ===")
by_cat = defaultdict(lambda: {"count": 0, "pnl": 0.0, "cost": 0.0})
for t in open_trades:
    cat = t["category"] or "unknown"
    by_cat[cat]["count"] += 1
    by_cat[cat]["pnl"] += t["est_pnl"]
    by_cat[cat]["cost"] += t["cost"]
for t in resolved:
    cat = t["category"] or "unknown"
    by_cat[cat]["count"] += 1
    by_cat[cat]["pnl"] += t["pnl"]
    by_cat[cat]["cost"] += t["cost"]

for cat, s in sorted(by_cat.items()):
    roi = s["pnl"] / s["cost"] * 100 if s["cost"] > 0 else 0
    print(f"  {cat:12s}: {s['count']} trades, ${s['pnl']:+.2f} PnL on ${s['cost']:.2f} invested ({roi:+.0f}% ROI)")

# Balance
bal = conn.execute("SELECT balance FROM balances ORDER BY id DESC LIMIT 1").fetchone()
peak = conn.execute("SELECT MAX(peak) as p FROM balances").fetchone()
if bal:
    print(f"\nBalance: ${bal['balance']:.2f} | Peak: ${peak['p']:.2f} | Drawdown: {1-bal['balance']/peak['p']:.0%}")
