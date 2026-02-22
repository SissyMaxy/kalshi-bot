import sqlite3
conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, timestamp, ticker, direction, contracts, entry_price, cost, edge, status "
    "FROM trades WHERE ticker LIKE 'KXINX%' ORDER BY id"
).fetchall()
for r in rows:
    print(f"#{r['id']:2d} {r['timestamp'][:19]}  {r['direction']:3s} {r['contracts']}x "
          f"@ ${r['entry_price']:.2f}  cost=${r['cost']:.2f}  edge={r['edge']:.2f}  "
          f"{r['status']}  {r['ticker']}")
