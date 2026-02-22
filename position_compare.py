#!/usr/bin/env python3
"""Compare Kalshi API positions with bot DB trades."""
import os
import sqlite3
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from kalshi_client import KalshiClient

key_id = os.getenv("KALSHI_API_KEY_ID")
key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
client = KalshiClient(key_id, key_path, env="live")

positions = client.get_positions()

conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
db_trades = conn.execute(
    "SELECT id, ticker, direction, contracts, entry_price, cost, status, fill_status "
    "FROM trades WHERE status IN ('open', 'exiting') ORDER BY ticker, id"
).fetchall()

db_by_ticker = defaultdict(list)
for t in db_trades:
    db_by_ticker[t["ticker"]].append(dict(t))

by_event = defaultdict(list)
for p in positions:
    ticker = p.get("ticker", "")
    parts = ticker.rsplit("-", 1)
    event = parts[0] if len(parts) == 2 else ticker
    by_event[event].append(p)

print("POSITION COMPARISON: Kalshi API vs Bot DB")
print("=" * 80)

for event, pos_list in sorted(by_event.items()):
    competing = len(pos_list) > 1
    label = "** MULTIPLE STRIKES **" if competing else ""
    print(f"\n{event} {label}")
    print("-" * 60)

    for p in pos_list:
        ticker = p.get("ticker", "")
        position = p.get("position", 0)
        side = "YES" if position > 0 else "NO"
        qty = abs(position)

        try:
            mdata = client.get_market(ticker)
            market = mdata.get("market", {})
            subtitle = market.get("subtitle", "")[:60]
            yes_bid = market.get("yes_bid_dollars", "?")
            yes_ask = market.get("yes_ask_dollars", "?")
        except Exception:
            subtitle = ""
            yes_bid = "?"
            yes_ask = "?"

        print(f"  KALSHI: {side} {qty}x {ticker}")
        print(f"          {subtitle}")
        print(f"          yes bid/ask: ${yes_bid}/${yes_ask}")

        db_for = db_by_ticker.get(ticker, [])
        if db_for:
            for dt in db_for:
                print(f"  DB:     #{dt['id']:2d} {dt['direction']:3s} {dt['contracts']}x "
                      f"@ ${dt['entry_price']:.2f} ({dt['status']}, {dt['fill_status']})")
        else:
            print(f"  DB:     (no matching trades in DB)")
        print()

print("=" * 80)
multi = sum(1 for v in by_event.values() if len(v) > 1)
print(f"Kalshi: {len(positions)} positions across {len(by_event)} events")
print(f"DB: {len(list(db_trades))} open trades")
print(f"Events with multiple strikes: {multi}")
