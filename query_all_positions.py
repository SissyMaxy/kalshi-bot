"""List all open Kalshi positions with current market prices."""
import os
from dotenv import load_dotenv
load_dotenv()
from kalshi_client import KalshiClient

key_id = os.getenv("KALSHI_API_KEY_ID")
key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")
client = KalshiClient(key_id, key_path, env="live")

balance = client.get_balance()
print(f"Cash balance: ${balance:.2f}\n")

positions = client.get_positions()
total_exposure = 0
print(f"{'Ticker':<30} {'Side':4} {'Qty':>4} {'Exposure':>10} {'Realized':>10}")
print("-" * 70)
for p in positions:
    pos = p.get("position", 0)
    if pos != 0:
        side = "YES" if pos > 0 else "NO"
        ticker = p.get("ticker", "?")
        exposure = p.get("market_exposure", 0) / 100
        realized = p.get("realized_pnl", 0) / 100
        total_exposure += exposure
        print(f"{ticker:<30} {side:<4} {abs(pos):>4} ${exposure:>9.2f} ${realized:>9.2f}")

print("-" * 70)
print(f"Total exposure: ${total_exposure:.2f}")
print(f"Portfolio value: ${balance + total_exposure:.2f}")
