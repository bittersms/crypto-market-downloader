"""Binance API source implementation."""
import time
import requests
from datetime import datetime, timedelta
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


class BinanceListSource(CryptoListSource):
    name = "binance"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.binance.com/api/v3/ticker/24hr"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        # Filter to USDT pairs, exclude stablecoin pairs
        results = []
        for ticker in tickers:
            symbol = ticker["symbol"]
            if not symbol.endswith("USDT"):
                continue
            base_symbol = symbol.replace("USDT", "")
            if exclude_stablecoins and base_symbol in stablecoins:
                continue
            quote_vol = float(ticker.get("quoteVolume", 0))
            if quote_vol <= 0:
                continue
            results.append({
                "rank": 0,  # Will be set below
                "symbol": base_symbol,
                "name": base_symbol,
                "price": float(ticker.get("lastPrice", 0)),
                "market_cap": quote_vol,
                "volume_24h": quote_vol,
                "source": self.name,
            })

        # Sort by volume descending, assign ranks
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[:top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class BinanceCandleSource(CandleDataSource):
    name = "binance"
    page_size = 1000

    def get_timeframe_mapping(self):
        return {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d", "1w": "1w",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        tf_map = self.get_timeframe_mapping()
        interval = tf_map.get(timeframe, timeframe)

        url = "https://api.binance.com/api/v3/klines"
        all_candles = []
        current_start = start_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        # Binance rejects or stalls on extremely wide single requests. Cap the
        # per-request window; the loop still walks the whole range.
        max_window_seconds = 30 * 24 * 3600  # 30 days per request

        while current_start <= end_date and iteration < max_iterations:
            req_end = min(end_date,
                          current_start + timedelta(seconds=max_window_seconds))
            params = {
                "symbol": symbol + "USDT",
                "interval": interval,
                "startTime": int(current_start.timestamp() * 1000),
                "endTime": int(req_end.timestamp() * 1000),
                "limit": 1000,
            }

            # The SOCKS5 proxy occasionally hands back a truncated body even
            # with HTTP 200 — resp.json() then dies with a JSONDecodeError,
            # which used to fail the whole symbol. Retry the window a few
            # times before giving up.
            klines = None
            last_err = None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params, timeout=60)
                    resp.raise_for_status()
                    klines = resp.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"Binance returned unreadable data after "
                                   f"retries: {last_err}")

            if not klines:
                break

            for k in klines:
                all_candles.append({
                    "timestamp": datetime.fromtimestamp(k[0] / 1000),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })

            # Advance the cursor.
            # Two cases must both work:
            #  * 1h over a 30-day window returns exactly 720 rows (< 1000).
            #    Counting rows would wrongly stop here; the window is NOT
            #    exhausted, so we must advance to req_end and keep going.
            #  * 1m over a 30-day window returns the full limit (1000) because
            #    30 days = 43,200 minutes >> 1000. Advancing to req_end here
            #    would skip 29+ days. So when we saturate the limit, advance
            #    only to the last received candle instead.
            if len(klines) >= 1000:
                last_ts = datetime.fromtimestamp(klines[-1][0] / 1000)
                current_start = last_ts + timedelta(seconds=self._interval_to_seconds(interval))
            else:
                current_start = req_end + timedelta(seconds=self._interval_to_seconds(interval))
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        return all_candles

    def _interval_to_seconds(self, interval):
        """Convert interval string to seconds."""
        multi = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        num = int(interval[:-1])
        unit = interval[-1]
        return num * multi.get(unit, 60)
