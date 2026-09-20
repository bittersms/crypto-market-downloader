"""KuCoin API source implementation."""
import time
from datetime import datetime, timedelta
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


class KuCoinListSource(CryptoListSource):
    name = "kucoin"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.kucoin.com/api/v1/market/allTickers"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != "200000":
            raise RuntimeError(f"KuCoin list error: {data.get('msg')}")

        tickers = data.get("data", {}).get("ticker", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for ticker in tickers:
            symbol_full = ticker.get("symbol", "")
            # Filter USDT pairs
            if not symbol_full.endswith("-USDT"):
                continue
            base_symbol = symbol_full.replace("-USDT", "")
            if base_symbol in stablecoins:
                continue
            vol = float(ticker.get("vol", 0))
            if vol <= 0:
                continue
            price = float(ticker.get("last", 0))
            quote_vol = vol * price  # USD trading volume
            results.append({
                "rank": 0,
                "symbol": base_symbol,
                "name": base_symbol,
                "price": price,
                "market_cap": quote_vol,  # USD trading volume = vol * price
                "volume_24h": vol,
                "source": self.name,
            })

        results.sort(key=lambda x: x["market_cap"], reverse=True)
        for i, r in enumerate(results[:top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class KuCoinCandleSource(CandleDataSource):
    name = "kucoin"
    page_size = 100

    def get_timeframe_mapping(self):
        # KuCoin uses: 1min, 3min, 5min, 15min, 30min, 1hour, 2hour, 4hour, 6hour, 8hour, 12hour, 1day, 1week
        return {
            "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1hour", "2h": "2hour", "4h": "4hour", "1d": "1day", "1w": "1week",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        tf_map = self.get_timeframe_mapping()
        interval = tf_map.get(timeframe, "1hour")

        url = "https://api.kucoin.com/api/v1/market/candles"
        all_candles = []

        # KuCoin returns candles in reverse order (newest first)
        # And hard-limits to 100 per request regardless of the limit parameter.
        # We paginate backward using endAt (timestamp in seconds).
        # Without endAt, KuCoin always returns the most recent 100 candles,
        # causing an infinite loop. With endAt, each batch fetches progressively
        # older data toward start_date.
        #
        # NOTE: 1m timeframe over 30 days = ~432 requests (100 candles/page).
        # This is inherently slow. Consider using 1h or resampling for long ranges.
        # max_iterations caps runaway loops; scale with the requested range so
        # long fetches are not silently truncated.
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)
        current_end = end_date
        iteration = 0
        while current_end > start_date and iteration < max_iterations:
            params = {
                "symbol": symbol.upper() + "-USDT",
                "type": interval,
                "limit": 100,
                "endAt": int(current_end.timestamp()),
            }
            # Proxy can truncate a body under load (HTTP 200, bad JSON).
            # Retry before letting the symbol fail.
            data = None
            last_err = None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params, timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if data is None:
                raise RuntimeError(f"KuCoin returned unreadable data after "
                                   f"retries: {last_err}")

            if data.get("code") != "200000":
                break

            candles = data.get("data", [])
            if not candles:
                break

            # KuCoin candle format: [time, open, close, high, low, volume, turnover]
            for c in candles:
                ts = datetime.fromtimestamp(int(c[0]))
                if ts < start_date or ts > end_date:
                    continue
                all_candles.append({
                    "timestamp": ts,
                    "open": float(c[1]),
                    "close": float(c[2]),
                    "high": float(c[3]),
                    "low": float(c[4]),
                    "volume": float(c[5]),
                })

            # Move to oldest candle and fetch older batch
            oldest_ts = min(int(c[0]) for c in candles)
            current_end = datetime.fromtimestamp(oldest_ts) - timedelta(seconds=1)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass
            if current_end < start_date:
                break

        all_candles.sort(key=lambda x: x["timestamp"])
        return all_candles
