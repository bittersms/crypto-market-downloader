"""OKX API source implementation.

OKX uses its own contract: dashed instId (BTC-USDT), uppercase interval
units, and a code/data envelope.
"""
import time
from datetime import datetime
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request

BASE = "https://www.okx.com"


class OkxListSource(CryptoListSource):
    name = "okx"

    def fetch_list(self, top_n, exclude_stablecoins):
        # OKX has no ranked list; pull tickers and rank by volume ourselves.
        url = f"{BASE}/api/v5/market/tickers?instType=SPOT"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX list error: {data.get('msg')}")
        tickers = data.get("data", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            inst = t.get("instId", "")
            if not inst.endswith("-USDT"):
                continue
            base = inst.replace("-USDT", "")
            if exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t.get("volCcy24h", 0) or 0)
            price = float(t.get("last", 0) or 0)
            quote_vol = vol * price
            if quote_vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": price, "market_cap": quote_vol,
                "volume_24h": quote_vol, "source": self.name,
            })

        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class OkxCandleSource(CandleDataSource):
    name = "okx"
    # OKX history/candles caps at 100 per request for most instruments.
    page_size = 100

    def get_timeframe_mapping(self):
        return {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D", "1w": "1W",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        interval = self.get_timeframe_mapping().get(timeframe, "1m")
        url = f"{BASE}/api/v5/market/history-candles"
        inst = f"{symbol.upper()}-USDT"

        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        # OKX returns newest-first and paginates with `after` (exclusive
        # lower bound, in ms). Walk backwards from end_date.
        all_candles = []
        current_end = end_date
        iteration = 0
        while current_end > start_date and iteration < max_iterations:
            params = {
                "instId": inst,
                "bar": interval,
                "after": int(current_end.timestamp() * 1000),
                "limit": 100,
            }
            data = None
            last_err = None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if data is None:
                raise RuntimeError(f"OKX returned unreadable data after "
                                   f"retries: {last_err}")
            if str(data.get("code")) != "0":
                break

            rows = data.get("data", [])
            if not rows:
                break

            # Row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            for r in rows:
                try:
                    ts = datetime.fromtimestamp(int(r[0]) / 1000)
                    if ts < start_date or ts > end_date:
                        continue
                    all_candles.append({
                        "timestamp": ts, "open": float(r[1]),
                        "high": float(r[2]), "low": float(r[3]),
                        "close": float(r[4]), "volume": float(r[5]),
                    })
                except (ValueError, IndexError):
                    continue

            # oldest timestamp in this batch becomes the new exclusive bound
            oldest = min(int(r[0]) for r in rows)
            current_end = datetime.fromtimestamp(oldest / 1000)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        all_candles.sort(key=lambda x: x["timestamp"])
        return all_candles
