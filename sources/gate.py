"""Gate.io API source implementation.

Gate's spot contract is its own shape: underscored pairs (BTC_USDT), a
candlesticks endpoint returning [ts, vol, open, close, high, low, amount]
in *seconds*, and pagination via `from`/`to` epoch bounds.
"""
import time
from datetime import datetime, timedelta
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request

BASE = "https://api.gateio.ws"
# Gate candle row: [timestamp_s, volume, open, close, high, low, amount]
# NOTE the unusual order: volume is index 1, close is index 3.


class GateListSource(CryptoListSource):
    name = "gate"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{BASE}/api/v4/spot/tickers"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            pair = t.get("currency_pair", "")
            if not pair.endswith("_USDT"):
                continue
            base = pair.replace("_USDT", "")
            if exclude_stablecoins and base in stablecoins:
                continue
            # base_volume is the base-asset volume; multiply by last price
            # for a comparable USD figure.
            vol = float(t.get("base_volume", 0) or 0)
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


class GateCandleSource(CandleDataSource):
    name = "gate"
    page_size = 1000

    def get_timeframe_mapping(self):
        return {
            "1m": "10s", "5m": "1m", "15m": "5m", "30m": "15m", "1h": "30m",
            "4h": "4h", "1d": "1d", "1w": "1d",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        # Gate has no server-side 1m candle: its finest is 10s. Fetching 10s
        # and aggregating 6 rows into one minute is exact but 6x the traffic,
        # so request 10s and aggregate locally.
        tf_map = self.get_timeframe_mapping()
        interval = tf_map.get(timeframe, "1m")
        url = f"{BASE}/api/v4/spot/candlesticks"
        pair = f"{symbol.upper()}_USDT"

        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)
        # Gate caps a window; keep requests bounded. Two independent limits:
        # 1) 1000 data POINTS per request — 1m is served as 10s, so a 30-day
        #    window would ask for 259k rows and the API rejects it with 400
        #    "range too broad".
        # 2) 10000 points BACK from now — older data is refused with 400
        #    "Candlestick too long ago", so 10s history only reaches ~16.7h.
        _sec = {"10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                "4h": 14400, "1d": 86400}
        step_s = _sec.get(interval, 60)
        max_window_seconds = min(30 * 24 * 3600, 900 * step_s)

        raw = []
        # Gate refuses anything older than 10000 points from "now", so fetch
        # newest-first and walk backwards; starting at the oldest window hits
        # that limit immediately and returns nothing.
        cursor = end_date
        iteration = 0
        while cursor >= start_date and iteration < max_iterations:
            req_start = max(start_date,
                            cursor - timedelta(seconds=max_window_seconds))
            params = {
                "currency_pair": pair,
                "interval": interval,
                "from": int(req_start.timestamp()),
                "to": int(cursor.timestamp()),
                "limit": 1000,
            }
            rows = None
            last_err = None
            last_body = ""
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    last_body = resp.text[:300]
                    resp.raise_for_status()
                    rows = resp.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if rows is None:
                # Gate refuses data older than 10000 points from now with
                # 400 "Candlestick too long ago". That is a hard history
                # limit, not a transient failure: stop instead of looping.
                # raise_for_status() drops the body, so it is captured above.
                hay = (str(last_err) + " " + last_body).lower()
                if "too long ago" in hay or "too broad" in hay or \
                        "invalid_param_value" in hay:
                    break
                raise RuntimeError(f"gate returned unreadable data after "
                                   f"retries: {last_err}")
            if not rows:
                cursor = req_start - timedelta(seconds=60)
                iteration += 1
                continue

            for r in rows:
                try:
                    raw.append((int(r[0]), float(r[2]), float(r[4]),
                                float(r[5]), float(r[3]), float(r[1])))
                except (ValueError, IndexError):
                    continue

            # Gate answers oldest-first; step back past the oldest row.
            oldest = min(int(r[0]) for r in rows)
            cursor = datetime.fromtimestamp(oldest) - timedelta(seconds=1)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(raw))
                except Exception:
                    pass

        # Aggregate the sub-minute series up to the requested timeframe.
        return self._aggregate(raw, timeframe, start_date, end_date)

    @staticmethod
    def _aggregate(raw, timeframe, start_date, end_date):
        """Bucket the raw (ts, o, h, l, c, v) rows into `timeframe` candles."""
        secs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                "4h": 14400, "1d": 86400, "1w": 604800}.get(timeframe, 60)
        buckets = {}
        for ts, o, h, l, c, v in raw:
            bucket = ts - (ts % secs)
            if bucket not in buckets:
                buckets[bucket] = [o, h, l, c, v]
            else:
                b = buckets[bucket]
                b[1] = max(b[1], h)
                b[2] = min(b[2], l)
                b[3] = c
                b[4] += v
        out = []
        for ts in sorted(buckets):
            b = buckets[ts]
            dt = datetime.fromtimestamp(ts)
            if start_date <= dt <= end_date:
                out.append({"timestamp": dt, "open": b[0], "high": b[1],
                            "low": b[2], "close": b[3], "volume": b[4]})
        return out
