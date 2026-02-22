"""View the append-only order ledger."""
import sqlite3

conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT * FROM order_ledger ORDER BY id"
).fetchall()

if not rows:
    print("Ledger is empty.")
else:
    print(f"{'ID':>4}  {'Timestamp':19}  {'Action':6}  {'Ticker':<28}  "
          f"{'Side':4}  {'Qty':>3}  {'Price':>5}  {'Order ID':<14}  {'Result'}")
    print("-" * 115)
    for r in rows:
        oid = (r['order_id'] or '-')[:14]
        print(f"{r['id']:>4}  {r['timestamp'][:19]}  {r['action']:<6}  "
              f"{r['ticker']:<28}  {r['side']:<4}  {r['contracts']:>3}  "
              f"{r['price_cents']:>5}  {oid:<14}  {r['result']}")
    print(f"\nTotal: {len(rows)} entries")
