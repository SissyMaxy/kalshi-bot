"""
Fair Value Estimator
Builds probability estimates for Kalshi markets using external data.
- Weather: NWS/NOAA forecast temperatures + normal CDF
- Crypto: CoinGecko real-time prices + volatility model
- Financial indices: Yahoo Finance prices + volatility model
Supports calibration logging and dynamic sigma adjustment.
"""

import re
import math
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("kalshi-bot")

# ── Weather configuration ─────────────────────────────────────────────

# Canonical station coordinates — gridpoints resolved dynamically at startup.
# lat/lon are the Kalshi-specified weather stations from market rules.
NWS_STATIONS = {
    "KXHIGHNY":  {"lat": 40.7789, "lon": -73.9692, "station": "KNYC", "city": "NYC"},
    "KXLOWNY":   {"lat": 40.7789, "lon": -73.9692, "station": "KNYC", "city": "NYC"},
    "KXHIGHCHI": {"lat": 41.9742, "lon": -87.9073, "station": "KORD", "city": "Chicago"},
    "KXLOWCHI":  {"lat": 41.9742, "lon": -87.9073, "station": "KORD", "city": "Chicago"},
    "KXHIGHLA":  {"lat": 34.0236, "lon": -118.2912, "station": "KCQT", "city": "LA"},
    "KXHIGHDC":  {"lat": 38.8512, "lon": -77.0402, "station": "KDCA", "city": "DC"},
    "KXHIGHMIA": {"lat": 25.7959, "lon": -80.2870, "station": "KMIA", "city": "Miami"},
    "KXHIGHDEN": {"lat": 39.8466, "lon": -104.6562, "station": "KDEN", "city": "Denver"},
    "KXLOWDEN":  {"lat": 39.8466, "lon": -104.6562, "station": "KDEN", "city": "Denver"},
}

# Resolved at startup by _resolve_gridpoints() — maps series to (office, x, y, city)
NWS_GRIDPOINTS = {}


def _resolve_gridpoints():
    """Resolve NWS gridpoints from station coordinates at import time."""
    global NWS_GRIDPOINTS
    for series, info in NWS_STATIONS.items():
        try:
            url = f"https://api.weather.gov/points/{info['lat']},{info['lon']}"
            headers = {"User-Agent": "KalshiBot/1.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            office = props.get("gridId")
            x = props.get("gridX")
            y = props.get("gridY")
            if office and x is not None and y is not None:
                NWS_GRIDPOINTS[series] = (office, x, y, info["city"])
                log.info(f"  NWS gridpoint {series}: {office}/{x},{y} ({info['city']})")
            else:
                log.warning(f"  NWS gridpoint resolve failed for {series}: missing fields")
        except Exception as e:
            log.warning(f"  NWS gridpoint resolve failed for {series}: {e}")

    if not NWS_GRIDPOINTS:
        log.error("FATAL: No NWS gridpoints resolved — weather trading disabled")


# Resolve on import
_resolve_gridpoints()

DEFAULT_SIGMAS = {0: 3.5, 1: 4.5, 2: 5.5, 3: 8.0}

# Per-city corrections derived from calibration data (10-11 date samples each).
# sigma_mult: scales sigma to match observed forecast error variance.
# bias: systematic forecast error (positive = actuals run hotter than forecast).
# NYC is baseline (most accurate). Chicago & Denver have warm biases.
# City corrections DISABLED — derived from 10-11 samples each, caused more harm
# than good. Denver +2.8F directly caused B66.5 disaster. Chicago +2.2F didn't
# prevent -$17.89 losses. Zeroed out until we have 50+ samples per city.
CITY_CORRECTIONS = {
    "NYC":     {"sigma_mult": 1.0, "bias": 0.0},
    "Chicago": {"sigma_mult": 1.0, "bias": 0.0},
    "Miami":   {"sigma_mult": 1.0, "bias": 0.0},
    "Denver":  {"sigma_mult": 1.0, "bias": 0.0},
    "LA":      {"sigma_mult": 1.0, "bias": 0.0},
    "DC":      {"sigma_mult": 1.0, "bias": 0.0},
}

# UTC offsets for sigma collapse (standard time — late Feb is before DST)
CITY_UTC_OFFSETS = {
    "NYC": -5, "Chicago": -6, "LA": -8, "DC": -5,
    "Miami": -5, "Denver": -7,
}

# ── Crypto configuration ──────────────────────────────────────────────
# default_daily_vol: typical 1-day percentage move (BTC ~3%, ETH ~4%)

CRYPTO_ASSETS = {
    "KXBTC":  {"coingecko_id": "bitcoin",  "symbol": "BTC", "default_daily_vol": 0.030},
    "KXBTCD": {"coingecko_id": "bitcoin",  "symbol": "BTC", "default_daily_vol": 0.030},
    "KXETH":  {"coingecko_id": "ethereum", "symbol": "ETH", "default_daily_vol": 0.040},
    "KXETHD": {"coingecko_id": "ethereum", "symbol": "ETH", "default_daily_vol": 0.040},
}

# ── Financial index configuration ─────────────────────────────────────
# default_daily_vol: typical 1-day percentage move (SPX ~1.2%, NDX ~1.5%)

INDEX_ASSETS = {
    "KXINX":  {"yahoo_symbol": "%5EGSPC", "symbol": "SPX", "default_daily_vol": 0.012},
    "KXSPX":  {"yahoo_symbol": "%5EGSPC", "symbol": "SPX", "default_daily_vol": 0.012},
    "KXNDX":  {"yahoo_symbol": "%5EIXIC", "symbol": "NDX", "default_daily_vol": 0.015},
    "KXCOMP": {"yahoo_symbol": "%5EIXIC", "symbol": "NDX", "default_daily_vol": 0.015},
}


def normal_cdf(x, mu, sigma):
    """Standard normal CDF."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


class FairValueEstimator:
    def __init__(self, config, anthropic_key=None, db=None):
        self.config = config
        self.anthropic_key = anthropic_key
        self.db = db
        self._forecast_cache = {}

    def estimate(self, market, db=None):
        """
        Return (fair_value, metadata_dict) for the given market.
        fair_value is a probability 0.0 to 1.0.
        metadata contains sigma_confidence and hours_to_close for weather.
        Returns None if we can't estimate.
        """
        active_db = db or self.db
        series = market["series_ticker"]

        if market["category"] == "weather":
            return self._estimate_weather(market, active_db)
        elif series in CRYPTO_ASSETS:
            fv = self._estimate_crypto(market, active_db)
            return (fv, {}) if fv is not None else None
        elif series in INDEX_ASSETS:
            fv = self._estimate_index(market, active_db)
            return (fv, {}) if fv is not None else None
        return None

    def get_current_forecast_temp(self, ticker):
        """
        Fetch current NWS forecast temperature for a weather ticker.
        Returns forecast_temp (float) or None.
        """
        parts = ticker.split("-")
        if not parts:
            return None
        series = parts[0]

        if series not in NWS_GRIDPOINTS:
            return None

        office, grid_x, grid_y, city = NWS_GRIDPOINTS[series]
        forecast = self._get_nws_forecast(office, grid_x, grid_y)
        if not forecast:
            return None

        target_date = self._extract_date_from_ticker(ticker)
        if not target_date:
            return None

        forecast_high, forecast_low = self._find_forecast_for_date(forecast, target_date)
        is_high = "HIGH" in series.upper()
        return forecast_high if is_high else forecast_low

    def _estimate_weather(self, market, db=None):
        """Estimate fair value for a weather market using NWS forecast."""
        series = market["series_ticker"]

        # Only estimate temperature markets (HIGH/LOW), not rain/snow
        if "RAIN" in series.upper() or "SNOW" in series.upper():
            return None

        if series not in NWS_GRIDPOINTS:
            return None

        office, grid_x, grid_y, city = NWS_GRIDPOINTS[series]

        # Get NWS forecast (cached per cycle)
        forecast = self._get_nws_forecast(office, grid_x, grid_y)
        if not forecast:
            return None

        # Extract the relevant day's forecast
        target_date = self._extract_date_from_ticker(market["ticker"])
        if not target_date:
            return None

        forecast_high, forecast_low = self._find_forecast_for_date(forecast, target_date)
        if forecast_high is None:
            return None

        # Determine what the market is asking
        is_high = "HIGH" in series.upper()
        market_type = "high" if is_high else "low"
        forecast_temp = forecast_high if is_high else forecast_low

        if forecast_temp is None:
            return None

        # Get sigma — try database first, fall back to defaults
        days_out = self._days_until(target_date)
        days_bucket = min(days_out, 3)
        sigma = None
        if db:
            sigma = db.get_sigma(city, market_type, days_bucket)
        if sigma is None:
            sigma = DEFAULT_SIGMAS.get(days_bucket, 6.0)

        # Sanity check: compare forecast against observation station (same-day only)
        # Only flag divergence in the "impossible" direction:
        #   HIGH markets: obs ABOVE forecast = stale/wrong (obs below is normal at night)
        #   LOW markets: obs BELOW forecast = stale/wrong (obs above is normal during day)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        is_same_day = (target_date == today_str)
        station_id = NWS_STATIONS.get(series, {}).get("station")
        if station_id and is_same_day:
            obs_temp = self._get_station_observation(station_id, is_high)
            if obs_temp is not None:
                if is_high and obs_temp > forecast_temp + 2:
                    log.warning(
                        f"  FORECAST STALE {market['ticker']}: "
                        f"obs {obs_temp}°F already > forecast {forecast_temp}°F "
                        f"— using obs as floor"
                    )
                    forecast_temp = obs_temp
                elif not is_high and obs_temp < forecast_temp - 2:
                    log.warning(
                        f"  FORECAST STALE {market['ticker']}: "
                        f"obs {obs_temp}°F already < forecast {forecast_temp}°F "
                        f"— using obs as ceiling"
                    )
                    forecast_temp = obs_temp

        # Apply city-specific corrections from calibration data
        corrections = CITY_CORRECTIONS.get(city, {})
        sigma *= corrections.get("sigma_mult", 1.0)
        forecast_temp += corrections.get("bias", 0.0)

        # Same-day sigma collapse: by afternoon the actual temp is known,
        # so forecast uncertainty is much smaller than the morning sigma.
        if is_same_day:
            collapse_mult = self._same_day_sigma_mult(city, is_high)
            if collapse_mult < 1.0:
                log.debug(
                    f"  Sigma collapse {market['ticker']}: "
                    f"{sigma:.2f} x {collapse_mult:.2f} = {sigma * collapse_mult:.2f}"
                )
                sigma *= collapse_mult

        # Skip trades where forecast is too close to threshold (coin flip territory)
        nearest_threshold = self._get_nearest_threshold(market)
        if nearest_threshold is not None:
            distance = abs(forecast_temp - nearest_threshold)
            min_distance = sigma * 0.4  # ~1.5°F at sigma=4.0
            if distance < min_distance:
                log.debug(
                    f"  Skip {market['ticker']}: forecast {forecast_temp}°F "
                    f"only {distance:.1f}°F from threshold {nearest_threshold} "
                    f"(min {min_distance:.1f}°F required at sigma={sigma:.1f})"
                )
                return None

        # Calculate probability
        fair_value = self._calc_probability(market, forecast_temp, sigma)
        if fair_value is None:
            return None

        # Compute sigma-confidence and hours to close
        sigma_confidence = self._calc_sigma_distance(market, forecast_temp, sigma)
        hours_to_close = self._hours_to_close_from_market(market)

        # Log calibration data
        if db:
            try:
                db.log_calibration(
                    ticker=market["ticker"],
                    market_date=target_date,
                    city=city,
                    market_type=market_type,
                    predicted_prob=fair_value,
                    market_price=market["midprice"],
                    sigma_used=sigma,
                    forecast_temp=forecast_temp,
                )
            except Exception as e:
                log.debug(f"Calibration log failed: {e}")

        return (fair_value, {
            "sigma_confidence": sigma_confidence,
            "hours_to_close": hours_to_close,
        })

    def update_sigmas(self, db):
        """
        Sigma auto-tuning — DISABLED.
        The previous implementation had a bug: it used ticker prefix (T/B) to infer
        market direction instead of the actual strike_type from the Kalshi API.
        T-prefix markets can be EITHER "greater than" or "less than" depending on
        the specific market. This caused ~28% of calibration samples to have inverted
        probabilities, which led the optimizer to inflate sigma from 2.5 to 4.5,
        making the model catastrophically overconfident on tail bets.
        TODO: Re-enable after storing strike_type in calibration table.
        """
        log.info("Sigma auto-tuning: DISABLED (pending strike_type fix)")

    def _parse_threshold_from_ticker(self, ticker):
        """Extract temperature threshold from ticker like KXHIGHNY-26FEB19-B40.5."""
        parts = ticker.split("-")
        if len(parts) < 3:
            return None
        bracket = parts[-1]
        if bracket and bracket[0] in ("B", "T"):
            try:
                return float(bracket[1:])
            except ValueError:
                return None
        return None

    def _calc_sigma_distance(self, market, mu, sigma):
        """How many sigmas the forecast is from the market's nearest threshold."""
        if sigma <= 0:
            return 0

        strike_type = market["strike_type"]
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")

        try:
            if floor is not None:
                floor = float(floor)
            if cap is not None:
                cap = float(cap)
        except (ValueError, TypeError):
            return 0

        if strike_type == "greater" and floor is not None:
            return abs(mu - floor) / sigma
        elif strike_type == "less" and cap is not None:
            return abs(mu - cap) / sigma
        elif strike_type == "between" and floor is not None and cap is not None:
            if mu < floor:
                return abs(mu - floor) / sigma
            elif mu > cap:
                return abs(mu - cap) / sigma
            else:
                return min(abs(mu - floor), abs(mu - cap)) / sigma
        return 0

    def _get_nearest_threshold(self, market):
        """Extract the nearest strike threshold from market data."""
        strike_type = market.get("strike_type", "")
        try:
            if strike_type == "greater" and market.get("floor_strike") is not None:
                return float(market["floor_strike"])
            elif strike_type == "less" and market.get("cap_strike") is not None:
                return float(market["cap_strike"])
            elif strike_type == "between":
                floor = float(market["floor_strike"]) if market.get("floor_strike") else None
                cap = float(market["cap_strike"]) if market.get("cap_strike") else None
                if floor is not None and cap is not None:
                    return (floor + cap) / 2
        except (ValueError, TypeError):
            pass
        return None

    def _calc_probability(self, market, mu, sigma):
        """Calculate probability that the outcome falls in the market's range."""
        strike_type = market["strike_type"]
        floor = market.get("floor_strike")
        cap = market.get("cap_strike")

        # Convert strikes to float (API may return int, float, or string)
        try:
            if floor is not None:
                floor = float(floor)
            if cap is not None:
                cap = float(cap)
        except (ValueError, TypeError):
            return self._parse_rules_and_calc(market, mu, sigma)

        if strike_type == "greater" and floor is not None:
            return 1 - normal_cdf(floor, mu, sigma)
        elif strike_type == "less" and cap is not None:
            return normal_cdf(cap, mu, sigma)
        elif strike_type == "between" and floor is not None and cap is not None:
            return normal_cdf(cap + 0.5, mu, sigma) - normal_cdf(floor - 0.5, mu, sigma)
        else:
            return self._parse_rules_and_calc(market, mu, sigma)

    def _parse_rules_and_calc(self, market, mu, sigma):
        """Fallback: parse the rules_primary text to figure out the strike."""
        rules = market.get("rules_primary", "")

        m = re.search(r"greater than ([\d,.]+)", rules)
        if m:
            threshold = float(m.group(1).replace(",", ""))
            return 1 - normal_cdf(threshold, mu, sigma)

        m = re.search(r"less than ([\d,.]+)", rules)
        if m:
            threshold = float(m.group(1).replace(",", ""))
            return normal_cdf(threshold, mu, sigma)

        m = re.search(r"between ([\d,.]+)(?:\s*(?:and|-)\s*)([\d,.]+)", rules)
        if m:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", ""))
            return normal_cdf(hi + 0.5, mu, sigma) - normal_cdf(lo - 0.5, mu, sigma)

        return None

    # ── Crypto estimator ─────────────────────────────────────────────

    def _estimate_crypto(self, market, db=None):
        """Estimate fair value for crypto bracket/directional markets."""
        series = market["series_ticker"]
        asset_cfg = CRYPTO_ASSETS[series]

        current_price = self._get_crypto_price(asset_cfg["coingecko_id"])
        if current_price is None:
            return None

        hours_remaining = self._hours_to_close_from_market(market)
        if hours_remaining <= 0:
            return None

        # Scale volatility by sqrt(time remaining)
        days_remaining = max(hours_remaining / 24, 1 / 24)
        sigma = current_price * asset_cfg["default_daily_vol"] * math.sqrt(days_remaining)

        fair_value = self._calc_probability(market, current_price, sigma)
        if fair_value is None:
            return None

        if db:
            target_date = self._extract_date_from_ticker(market["ticker"])
            try:
                db.log_calibration(
                    ticker=market["ticker"],
                    market_date=target_date or "",
                    city=asset_cfg["symbol"],
                    market_type=market.get("strike_type", "unknown"),
                    predicted_prob=fair_value,
                    market_price=market["midprice"],
                    sigma_used=sigma,
                    forecast_temp=current_price,
                )
            except Exception as e:
                log.debug(f"Calibration log failed: {e}")

        return fair_value

    def _get_crypto_price(self, coingecko_id):
        """Fetch current crypto prices from CoinGecko. Cached per cycle."""
        # Batch-fetch all crypto prices in one call
        cache_key = "crypto_prices"
        if cache_key not in self._forecast_cache:
            try:
                url = ("https://api.coingecko.com/api/v3/simple/price"
                       "?ids=bitcoin,ethereum&vs_currencies=usd")
                headers = {"User-Agent": "KalshiBot/1.0"}
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                prices = {}
                for coin_id, values in data.items():
                    p = values.get("usd")
                    if p:
                        prices[coin_id] = p
                self._forecast_cache[cache_key] = prices
                log.info(f"Crypto prices: " + ", ".join(
                    f"{k}=${v:,.2f}" for k, v in prices.items()))
            except Exception as e:
                log.warning(f"CoinGecko price fetch failed: {e}")
                self._forecast_cache[cache_key] = {}

        return self._forecast_cache[cache_key].get(coingecko_id)

    # ── Financial index estimator ─────────────────────────────────────

    def _estimate_index(self, market, db=None):
        """Estimate fair value for S&P 500 / Nasdaq index markets."""
        series = market["series_ticker"]
        asset_cfg = INDEX_ASSETS[series]

        current_price = self._get_index_price(
            asset_cfg["yahoo_symbol"], asset_cfg["symbol"])
        if current_price is None:
            return None

        hours_remaining = self._hours_to_close_from_market(market)
        if hours_remaining <= 0:
            return None

        days_remaining = max(hours_remaining / 24, 1 / 24)
        sigma = current_price * asset_cfg["default_daily_vol"] * math.sqrt(days_remaining)

        fair_value = self._calc_probability(market, current_price, sigma)
        if fair_value is None:
            return None

        if db:
            target_date = self._extract_date_from_ticker(market["ticker"])
            try:
                db.log_calibration(
                    ticker=market["ticker"],
                    market_date=target_date or "",
                    city=asset_cfg["symbol"],
                    market_type=market.get("strike_type", "unknown"),
                    predicted_prob=fair_value,
                    market_price=market["midprice"],
                    sigma_used=sigma,
                    forecast_temp=current_price,
                )
            except Exception as e:
                log.debug(f"Calibration log failed: {e}")

        return fair_value

    def _get_index_price(self, yahoo_symbol, label):
        """Fetch current index price from Yahoo Finance. Cached per cycle."""
        cache_key = f"index_{label}"
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]

        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{yahoo_symbol}?range=1d&interval=1d")
            headers = {"User-Agent": "KalshiBot/1.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            result = data["chart"]["result"][0]
            price = result["meta"]["regularMarketPrice"]
            if price:
                self._forecast_cache[cache_key] = price
                log.info(f"Index {label}: {price:,.2f}")
                return price
            return None
        except Exception as e:
            log.warning(f"Yahoo Finance fetch failed for {label}: {e}")
            return None

    def _get_station_observation(self, station_id, is_high):
        """Fetch latest observation from NWS station. Returns temp in °F or None."""
        try:
            url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
            headers = {"User-Agent": "KalshiBot/1.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is not None:
                return round(temp_c * 9 / 5 + 32)
        except Exception as e:
            log.debug(f"Station observation fetch failed for {station_id}: {e}")
        return None

    # ── Shared helpers ────────────────────────────────────────────────

    def _hours_to_close_from_market(self, market):
        """Calculate hours until market close from market dict."""
        close_time_str = market.get("close_time", "")
        if not close_time_str:
            return 24  # default: assume 1 day
        try:
            close_time_str = close_time_str.replace("Z", "+00:00")
            close_time = datetime.fromisoformat(close_time_str)
            now = datetime.now(timezone.utc)
            return max(0, (close_time - now).total_seconds() / 3600)
        except (ValueError, TypeError):
            return 24

    # ── Weather helpers ───────────────────────────────────────────────

    def _same_day_sigma_mult(self, city, is_high):
        """Scale down sigma for same-day markets based on local time of day.

        HIGH temps peak ~2-4 PM local. By afternoon, the high is essentially
        determined. LOW temps bottom out ~5-7 AM, so by late morning they're known.

        Returns a multiplier 0.15 - 1.0 to apply to sigma.
        """
        utc_offset = CITY_UTC_OFFSETS.get(city, -5)
        local_hour = (datetime.now(timezone.utc).hour + utc_offset) % 24

        if is_high:
            # Daily high peaks ~2-4 PM local
            if local_hour < 10:
                return 1.0     # morning: full uncertainty
            elif local_hour < 13:
                return 0.7     # late morning: warming, partial info
            elif local_hour < 16:
                return 0.35    # afternoon: near/at peak
            else:
                return 0.15    # evening: peak passed, high is known
        else:
            # Daily low bottoms ~5-7 AM local
            if local_hour < 5:
                return 0.7     # pre-dawn: cooling, partial info
            elif local_hour < 8:
                return 0.35    # dawn: near/at minimum
            else:
                return 0.15    # daytime: low already happened

    def _get_nws_forecast(self, office, grid_x, grid_y):
        """Fetch NWS gridpoint forecast. Cached per scan cycle."""
        cache_key = f"{office}/{grid_x},{grid_y}"
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]

        try:
            url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
            headers = {"User-Agent": "KalshiBot/1.0 (contact@example.com)"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._forecast_cache[cache_key] = data
            return data
        except Exception as e:
            log.warning(f"NWS forecast fetch failed for {cache_key}: {e}")
            return None

    def clear_cache(self):
        """Clear forecast cache at start of each scan cycle."""
        self._forecast_cache.clear()

    def _find_forecast_for_date(self, forecast, target_date):
        """Extract high/low temperatures for a specific date from NWS forecast."""
        periods = forecast.get("properties", {}).get("periods", [])
        high = None
        low = None

        for period in periods:
            start = period.get("startTime", "")
            if target_date not in start:
                continue

            temp = period.get("temperature")
            is_daytime = period.get("isDaytime", True)

            if is_daytime and (high is None or temp > high):
                high = temp
            elif not is_daytime and (low is None or temp < low):
                low = temp

        return high, low

    def _extract_date_from_ticker(self, ticker):
        """Extract date from ticker. Handles multiple formats:
        'KXHIGHNY-26FEB12-B35.5'      -> '2026-02-12'
        'KXBTC-26FEB1217-B69000'       -> '2026-02-12'
        'KXINX-26FEB13H1600-B7037'     -> '2026-02-13'
        """
        m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
        if not m:
            return None

        year = 2000 + int(m.group(1))
        month_str = m.group(2)
        day = int(m.group(3))

        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
        }
        month = months.get(month_str)
        if not month:
            return None

        return f"{year}-{month:02d}-{day:02d}"

    def _days_until(self, date_str):
        """Calculate days from now until the target date (rounded, not truncated)."""
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta_days = (target - now).total_seconds() / 86400
            return max(0, round(delta_days))
        except ValueError:
            return 3

    def sanity_check(self, market, fair_value):
        """
        Optional: ask Claude to sanity-check the trade.
        Returns: "TRADE", "SKIP", or "REDUCE_SIZE"
        """
        if not self.anthropic_key:
            return "TRADE"

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.anthropic_key)

            midprice = (market["yes_bid"] + market["yes_ask"]) / 2
            prompt = f"""Market: "{market['title']}"
Bot's fair value: {fair_value:.2f}
Current market price: {midprice:.2f}
Category: {market['category']}
Rules: {market.get('rules_primary', 'N/A')[:200]}

Is there anything the model might be missing? Any major risk factors,
news events, or reasons the market price might be correct despite
the apparent mispricing?

Reply with exactly one word: TRADE, SKIP, or REDUCE_SIZE"""

            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = response.content[0].text.strip().upper()
            if answer in ("TRADE", "SKIP", "REDUCE_SIZE"):
                return answer
            return "TRADE"
        except Exception as e:
            log.warning(f"Sanity check failed: {e}")
            return "TRADE"
