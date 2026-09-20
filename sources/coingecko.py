"""Coingecko API source implementation.

Uses /coins/markets for list (sorted by market_cap) and /coins/{id}/ohlc
for candlestick data. The coin ID is resolved from the markets endpoint
rather than /coins/list (which maps BTC->batcat incorrectly).
"""
from datetime import datetime
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


# Cache symbol -> coin_id from markets endpoint
_COINGECKO_ID_CACHE = {}
_COINGECKO_ID_LOADED = False


class CoingeckoListSource(CryptoListSource):
    name = "coingecko"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": min(top_n * 2, 250),
            "page": 1,
            "sparkline": False,
        }
        resp = make_request(url, self.name, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for coin in data:
            symbol = coin["symbol"].upper()
            if symbol in stablecoins:
                continue
            results.append({
                "rank": coin.get("market_cap_rank", len(results) + 1),
                "symbol": symbol,
                "name": coin["name"],
                "price": coin.get("current_price", 0),
                "market_cap": coin.get("market_cap", 0),
                "volume_24h": coin.get("total_volume", 0),
                "source": self.name,
            })
            if len(results) >= top_n:
                break

        # Cache coin IDs for candle resolution
        _cache_coin_ids(data)

        return results


def _cache_coin_ids(markets_data):
    """Cache symbol -> coin_id mapping from markets data."""
    global _COINGECKO_ID_CACHE, _COINGECKO_ID_LOADED
    for coin in markets_data:
        _COINGECKO_ID_CACHE[coin["symbol"].upper()] = coin["id"]
    _COINGECKO_ID_LOADED = True


class CoingeckoCandleSource(CandleDataSource):
    name = "coingecko"
    # CoinGecko's OHLC endpoint cannot serve intraday candles for multi-day
    # ranges: days=1 gives 1m candles, but any larger range silently returns
    # 4-hour candles regardless of the requested timeframe. Accepting that
    # would silently write wrong data to the CSV, so mark the true page size.
    page_size = 100

    # Timeframes this source can genuinely serve, and the max lookback (days)
    # for which the returned granularity actually matches the request.
    SUPPORTED_INTRADAY = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"}

    def get_timeframe_mapping(self):
        # CoinGecko OHLC returns:
        #   days=1  -> 1-minute candles (last 24h)
        #   days<=365 -> hourly candles
        #   days>365 -> daily candles
        return {
            "1m": "1m", "3m": "1m", "5m": "1m", "15m": "1m", "30m": "1m",
            "1h": "1h", "2h": "1h", "4h": "1h", "1d": "1d", "1w": "1d",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        # Load coin ID cache if not yet loaded
        global _COINGECKO_ID_LOADED
        if not _COINGECKO_ID_LOADED:
            _load_coin_ids()

        coin_id = _COINGECKO_ID_CACHE.get(symbol.upper())
        if not coin_id:
            raise ValueError(f"Could not resolve coin ID for {symbol}")

        days = (end_date - start_date).days + 1

        # Granularity guard: CoinGecko's free OHLC endpoint always returns
        # 4-hour granularity for multi-day ranges, no matter what timeframe
        # was requested (even "1d"). Accepting that would silently write
        # wrong data to the export. Fail loudly so failover moves to a
        # source that can actually honor the request.
        if timeframe in self.SUPPORTED_INTRADAY and days > 1:
            raise ValueError(
                f"CoinGecko cannot serve {timeframe} candles for a {days}-day "
                f"range (silently returns 4h candles); skipping this source")

        # CoinGecko OHLC only accepts specific day values: 1, 7, 14, 30, 90, 180, 365
        valid_days = [1, 7, 14, 30, 90, 180, 365]
        days = min(valid_days, key=lambda x: abs(x - days))

        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
        params = {"vs_currency": "usd", "days": days}
        resp = make_request(url, self.name, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        # CoinGecko OHLC returns arrays: [timestamp, open, high, low, close]
        # No volume data from this endpoint
        result = []
        for item in data:
            result.append({
                "timestamp": datetime.fromtimestamp(item[0] / 1000),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": 0,
            })
        return result


def _load_coin_ids():
    """Load coin ID cache from markets endpoint."""
    global _COINGECKO_ID_CACHE, _COINGECKO_ID_LOADED
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
              "page": 1, "sparkline": False}
    try:
        resp = make_request(url, "coingecko", params=params, timeout=30)
        resp.raise_for_status()
        _cache_coin_ids(resp.json())
    except Exception:
        pass
