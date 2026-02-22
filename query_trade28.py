"""Investigate trades #28 and #29 - when placed, what order IDs, and check Kalshi orders."""
import sqlite3
import os
from dotenv import load_dotenv
load_dotenv()

conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row

print("=== TRADE #28 and #29 DETAILS ===")
for tid in [28, 29]:
    r = conn.execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
    if r:
        print(f"\n#{r['id']} {r['ticker']}")
        print(f"  placed:     {r['timestamp']}")
        print(f"  direction:  {r['direction']} {r['contracts']}x @ ${r['entry_price']:.2f}")
        print(f"  order_id:   {r['order_id']}")
        print(f"  fill:       {r['fill_status']} ({r['filled_contracts']} filled)")
        print(f"  status:     {r['status']}")
        print(f"  corr_group: {r['correlation_group']}")

# Check all orders on these tickers via Kalshi API
from kalshi_client import KalshiClient
key_id = os.getenv("KALSHI_API_KEY_ID")
key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
client = KalshiClient(key_id, key_path, env="live")

for ticker in ["KXHIGHMIA-26FEB14-T80", "KXHIGHCHI-26FEB14-T53"]:
    print(f"\n=== KALSHI FILLS for {ticker} ===")
    try:
        fills = client.get_fills(ticker=ticker)
        for f in fills.get("fills", []):
            print(f"  {f.get('created_time', '?')[:19]}  {f.get('action','?')} {f.get('side','?')} "
                  f"{f.get('count','?')}x @ {f.get('yes_price','?')}c  "
                  f"order={f.get('order_id','?')[:12]}")
    except Exception as e:
        print(f"  Error: {e}")

    print(f"\n=== KALSHI ORDERS for {ticker} ===")
    try:
        orders = client.get_orders(ticker=ticker)
        for o in orders.get("orders", []):
            print(f"  {o.get('created_time', '?')[:19]}  {o.get('action','?')} {o.get('side','?')} "
                  f"{o.get('remaining_count','?')}/{o.get('count','?')}x @ {o.get('yes_price','?')}c  "
                  f"status={o.get('status','?')}  id={o.get('order_id','?')[:12]}")
    except Exception as e:
        print(f"  Error: {e}")
