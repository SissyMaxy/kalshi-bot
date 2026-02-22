"""
Kalshi API Client
Handles authentication (RSA-PSS signing) and all API calls.
"""

import time
import base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
API_PATH_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(self, api_key_id, private_key_path, env="demo"):
        self.api_key_id = api_key_id
        self.env = env
        self.base_url = DEMO_BASE if env == "demo" else PROD_BASE

        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    def _sign(self, timestamp_ms, method, path):
        path_no_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_no_query}"
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method, path):
        ts = str(int(time.time() * 1000))
        sig = self._sign(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _auth_request(self, method, path, **kwargs):
        url = self.base_url + path
        full_path = API_PATH_PREFIX + path
        headers = self._auth_headers(method, full_path)
        resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:200]
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason}: {detail}", response=resp)
        return resp.json()

    def _public_request(self, path):
        url = self.base_url + path
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Public endpoints (no auth) ───────────────────────────────────────

    def get_open_markets(self, series_ticker=None, limit=200, cursor=None):
        path = f"/markets?status=open&limit={limit}"
        if series_ticker:
            path += f"&series_ticker={series_ticker}"
        if cursor:
            path += f"&cursor={cursor}"
        return self._public_request(path)

    def get_market(self, ticker):
        return self._public_request(f"/markets/{ticker}")

    def get_orderbook(self, ticker, depth=10):
        return self._public_request(f"/markets/{ticker}/orderbook?depth={depth}")

    def get_series(self, ticker):
        return self._public_request(f"/series/{ticker}")

    # ── Authenticated endpoints ──────────────────────────────────────────

    def get_balance(self):
        data = self._auth_request("GET", "/portfolio/balance")
        return data["balance"] / 100  # cents to dollars

    def get_positions(self):
        data = self._auth_request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    def place_order(self, ticker, side, quantity, price_cents, order_type="limit"):
        """
        Place an order on Kalshi.
        - ticker: market ticker (e.g., "KXHIGHNY-26FEB12-B35.5")
        - side: "yes" or "no"
        - quantity: number of contracts
        - price_cents: price in cents (1-99)
        - order_type: "limit" or "market"
        """
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": order_type,
            "count": quantity,
        }
        if order_type == "limit":
            body["yes_price"] = price_cents if side == "yes" else (100 - price_cents)

        return self._auth_request("POST", "/portfolio/orders", json=body)

    def sell_position(self, ticker, side, quantity, price_cents):
        """Sell existing contracts."""
        body = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "type": "limit",
            "count": quantity,
            "yes_price": price_cents if side == "yes" else (100 - price_cents),
        }
        return self._auth_request("POST", "/portfolio/orders", json=body)

    def cancel_order(self, order_id):
        return self._auth_request("DELETE", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker=None, status=None):
        path = "/portfolio/orders?"
        if ticker:
            path += f"ticker={ticker}&"
        if status:
            path += f"status={status}&"
        return self._auth_request("GET", path.rstrip("&?"))

    def get_fills(self, ticker=None):
        path = "/portfolio/fills"
        if ticker:
            path += f"?ticker={ticker}"
        return self._auth_request("GET", path)
