"""
Market Scanner
Discovers open markets on Kalshi and filters them by the strategy criteria.
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("kalshi-bot")

# Weather series tickers for major cities
WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLA", "KXHIGHMIA", "KXHIGHDC",
    "KXHIGHATL", "KXHIGHHOU", "KXHIGHDAL", "KXHIGHDEN", "KXHIGHSF",
    "KXLOWNY", "KXLOWCHI", "KXLOWLA",
    "KXRAINNY", "KXRAINCHI", "KXRAINLA", "KXRAINDC",
    "KXSNOWNY", "KXSNOWCHI", "KXSNOWDC",
]

# Economics series tickers — DISABLED (no model edge)
ECON_SERIES = []

# Crypto / Financial — DISABLED (model reports fake 30-47% edges, actual win rate ~47%)
FINANCIAL_SERIES = []


def _parse_market(m):
    """Normalize a market dict from the API."""
    yes_bid = float(m.get("yes_bid_dollars", "0") or "0")
    yes_ask = float(m.get("yes_ask_dollars", "0") or "0")
    no_bid = float(m.get("no_bid_dollars", "0") or "0")
    no_ask = float(m.get("no_ask_dollars", "0") or "0")
    return {
        "ticker": m["ticker"],
        "event_ticker": m.get("event_ticker", ""),
        "title": m.get("title", ""),
        "subtitle": m.get("subtitle", ""),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "midprice": (yes_bid + yes_ask) / 2 if yes_ask > 0 and yes_bid > 0 else 0,
        "spread": (yes_ask - yes_bid) if yes_ask > 0 and yes_bid > 0 else 999,
        "volume_24h": m.get("volume_24h", 0) or 0,
        "open_interest": m.get("open_interest", 0) or 0,
        "close_time": m.get("close_time", ""),
        "expiration_time": m.get("expiration_time", ""),
        "strike_type": m.get("strike_type", ""),
        "floor_strike": m.get("floor_strike"),
        "cap_strike": m.get("cap_strike"),
        "rules_primary": m.get("rules_primary", ""),
        "status": m.get("status", ""),
        "category": "weather",  # will be overridden below
        "series_ticker": "",
        "raw": m,
    }


class MarketScanner:
    def __init__(self, config):
        self.config = config

    def scan_all(self, client):
        """Scan all target categories and return filtered markets."""
        all_markets = []

        # Weather markets (primary target)
        for series in WEATHER_SERIES:
            markets = self._fetch_series(client, series, category="weather")
            all_markets.extend(markets)

        # Economics markets
        for series in ECON_SERIES:
            markets = self._fetch_series(client, series, category="economics")
            all_markets.extend(markets)

        # Financial / Crypto markets
        for series in FINANCIAL_SERIES:
            markets = self._fetch_series(client, series, category="financial")
            all_markets.extend(markets)

        # Filter
        filtered = [m for m in all_markets if self._passes_filter(m)]
        log.info(f"Scanner: {len(all_markets)} total → {len(filtered)} passed filters")
        return filtered

    def _fetch_series(self, client, series_ticker, category):
        """Fetch all open markets for a series."""
        try:
            data = client.get_open_markets(series_ticker=series_ticker)
            markets = []
            for m in data.get("markets", []):
                parsed = _parse_market(m)
                parsed["category"] = category
                parsed["series_ticker"] = series_ticker
                markets.append(parsed)
            if markets:
                log.debug(f"  {series_ticker}: {len(markets)} markets")
            return markets
        except Exception as e:
            log.debug(f"  {series_ticker}: error ({e})")
            return []

    def _passes_filter(self, m):
        """Apply strategy filters."""
        cfg = self.config

        # Must have actual pricing
        if m["midprice"] <= 0:
            return False

        # Volume filter (soft — volume_24h resets daily on weather markets)
        if m["volume_24h"] < cfg.get("min_volume_24h", 0):
            return False

        # Open interest filter (better liquidity proxy for weather)
        if m["open_interest"] < cfg.get("min_open_interest", 0):
            return False

        # Spread filter
        if m["spread"] > cfg["max_spread"] and m["spread"] < 999:
            return False

        # Skip "between" brackets for weather — narrow 1°F windows are traps.
        # Model can't distinguish 7% from 97% on these; a 1°F forecast error
        # flips the outcome completely. Only greater/less have robust edge.
        if m["category"] == "weather" and m.get("strike_type") == "between":
            return False

        # Extreme price filter (no value in trading near 0 or 1)
        if m["midprice"] < 0.05 or m["midprice"] > 0.95:
            return False

        # Time to close: must be > 30 min and < 7 days
        try:
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_to_close = (close - now).total_seconds() / 3600
            if hours_to_close < 0.5 or hours_to_close > 168:
                return False
        except (ValueError, KeyError):
            pass  # If we can't parse, let it through

        return True
