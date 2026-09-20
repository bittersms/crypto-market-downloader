"""Mexc API source implementation.

Uses the Binance-compatible REST API. NOTE: Mexc uses different
interval formats (e.g. 60m instead of 1h).
"""
import time
from datetime import datetime, timedelta
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


class MexcListSource(CryptoListSource):
    name = "mexc"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.mexc.com/api/v3/ticker/24hr"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for ticker in tickers:
            symbol = ticker["symbol"]
            if not symbol.endswith("USDT"):
                continue
            base_symbol = symbol.replace("USDT", "")
            if base_symbol in stablecoins:
                continue
            quote_vol = float(ticker.get("quoteVolume", 0))
            if quote_vol <= 0:
                continue
            results.append({
                "rank": 0,
                "symbol": base_symbol,
                "name": base_symbol,
                "price": float(ticker.get("lastPrice", 0)),
                "market_cap": quote_vol,
                "volume_24h": quote_vol,
                "source": self.name,
            })

        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[:top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class MexcCandleSource(CandleDataSource):
    name = "mexc"
    # MEXC ignores limit above 500 in practice.
    page_size = 500

    def get_timeframe_mapping(self):
        # Mexc supports: 1m, 5m, 15m, 30m, 60m, 4h, 1d (not 3m, 2h, 1w)
        return {
            "1m": "1m", "3m": "5m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "60m", "2h": "60m", "4h": "4h", "1d": "1d", "1w": "1d",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        tf_map = self.get_timeframe_mapping()
        interval = tf_map.get(timeframe, timeframe)

        url = "https://api.mexc.com/api/v3/klines"
        all_candles = []
        current_start = start_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        # MEXC times out when the requested window is extremely wide even with
        # a small limit, because startTime/endTime bound the whole query. Cap
        # the per-request window so each call stays responsive; the pagination
        # loop still walks the full range.
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
            # Proxy can truncate a body under load (HTTP 200, bad JSON).
            # Retry before letting the symbol fail.
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
                raise RuntimeError(f"MEXC returned unreadable data after "
                                   f"retries: {last_err}")

            if not klines:
                # Empty window: move forward by the window size rather than
                # stopping, so a single gap doesn't truncate the whole range.
                current_start = req_end + timedelta(minutes=1)
                iteration += 1
                continue

            for k in klines:
                all_candles.append({
                    "timestamp": datetime.fromtimestamp(k[0] / 1000),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })

            # MEXC caps a page at 500 rows even when `limit` asks for more,
            # so a page that is "short" of 1000 is usually just a full 500-row
            # page, not an exhausted window. Advance past the last row
            # received; jumping to req_end instead skips past end_date and
            # truncates the whole range to one page.
            last_ts = datetime.fromtimestamp(klines[-1][0] / 1000)
            current_start = last_ts + timedelta(minutes=1)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        return all_candles
