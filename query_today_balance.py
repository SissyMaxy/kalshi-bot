import sqlite3
conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT timestamp, balance, peak FROM balances WHERE timestamp >= '2026-02-13' ORDER BY id"
).fetchall()
print("=== Feb 13 Balance History ===")
for r in rows:
    dd = 1 - r['balance'] / r['peak'] if r['peak'] > 0 else 0
    print(f"  {r['timestamp'][:19]}  ${r['balance']:>8.2f}  drawdown={dd:.0%}")
if rows:
    print(f"\nStart of day:  ${rows[0]['balance']:.2f}")
    print(f"Current:       ${rows[-1]['balance']:.2f}")
    print(f"Day change:    ${rows[-1]['balance'] - rows[0]['balance']:+.2f}")
