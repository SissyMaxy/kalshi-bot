"""Query raw Kalshi positions for specific tickers + balance."""
import os
from dotenv import load_dotenv
load_dotenv()

from kalshi_client import KalshiClient

key_id = os.getenv("KALSHI_API_KEY_ID")
key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
client = KalshiClient(key_id, key_path, env="live")

# Balance
balance = client.get_balance()
print(f"Cash balance: ${balance:.2f}\n")

# All positions
positions = client.get_positions()

targets = ["KXHIGHMIA-26FEB14-T80", "KXHIGHCHI-26FEB14-T53"]

for ticker in targets:
    print(f"=== {ticker} ===")
    found = False
    for p in positions:
        if p.get("ticker") == ticker:
            found = True
            pos = p.get("position", 0)
            side = "YES" if pos > 0 else "NO" if pos < 0 else "NONE"
            print(f"  position field:    {pos}")
            print(f"  abs contracts:     {abs(pos)}")
            print(f"  side:              {side}")
            print(f"  market_exposure:   {p.get('market_exposure', '?')}")
            print(f"  resting_orders:    {p.get('resting_orders_count', '?')}")
            # Print all raw fields
            print(f"  raw: {dict(p)}")
    if not found:
        print("  NOT FOUND in positions")
    print()
