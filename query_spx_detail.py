import sqlite3
conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, timestamp, ticker, direction, contracts, entry_price, cost, edge, "
    "status, fill_status, filled_contracts, correlation_group, order_id "
    "FROM trades WHERE ticker LIKE 'KXINX%' ORDER BY id"
).fetchall()
for r in rows:
    print(f"#{r['id']:2d} {r['timestamp'][:19]}")
    print(f"   ticker: {r['ticker']}")
    print(f"   {r['direction']} {r['contracts']}x @ ${r['entry_price']:.2f}  cost=${r['cost']:.2f}  edge={r['edge']:.2f}")
    print(f"   status={r['status']}  fill={r['fill_status']}  filled={r['filled_contracts']}")
    print(f"   corr_group={r['correlation_group']}")
    print(f"   order_id={r['order_id']}")
    print()

# Also check: are there any other trades placed around the same time?
print("=== ALL TRADES 10:00-10:35 UTC ===")
rows2 = conn.execute(
    "SELECT id, timestamp, ticker, direction, contracts, correlation_group "
    "FROM trades WHERE timestamp BETWEEN '2026-02-12T10:00' AND '2026-02-12T10:35' "
    "ORDER BY timestamp"
).fetchall()
for r in rows2:
    print(f"#{r['id']:2d} {r['timestamp'][:19]}  {r['direction']:3s} {r['contracts']}x  "
          f"grp={r['correlation_group']}  {r['ticker']}")
