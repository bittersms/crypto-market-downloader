"""LBank (api.lbank.info) — own dialect, sharing the adapter plumbing.

Verified against the live API through the local SOCKS5 proxy before coding:

  GET /v2/currencyPairs.do                        -> data = ["btc_usdt", ...]
  GET /v2/ticker/24hr.do?symbol=btc_usdt           -> data[0].ticker
  GET /v2/kline.do?symbol=btc_usdt&type=minute1
                  &time=<start_sec>&size=2000
      -> data = [[ts_sec, o, h, l, c, vol], ...]   OLDEST FIRST

Kline quirks that make a plain Binance adapter wrong here:
  * `time` is a *start* second (not an end), so pagination walks forward
    from the requested start, never backwards;
  * the timestamp is in seconds (ms everywhere else);
  * intervals have their own names ("minute1", "hour1", "day1");
  * a page holds 2000 rows and a 3-day 1m request needs 3 gap-free pages,
    each starting one minute past the last row of the previous page.
"""

from datetime import datetime, timedelta

from failover import make_request
from sources.binance_like import BinanceLikeListSource, BinanceLikeCandleSource


class _LbankListSource(BinanceLikeListSource):
    """LBank pair list: a bare JSON array of "base_quote" strings."""

    name = "lbank"

    def __init__(self, cfg=None):
        super().__init__("https://api.lbank.info", quote_suffix="usdt",
                         symbol_upper=False)
        self.name = "lbank"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.lbank.info/v2/currencyPairs.do"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        pairs = resp.json().get("data", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        # LBank exposes no volume on this endpoint, so fall back to the
        # per-pair ticker below and rank by quote turnover instead.
        # LBank's 24h ticker only answers one symbol per call (verified: a
        # multi-symbol request returns result=false), so pull a bounded batch
        # of the top pairs individually and rank by quote turnover. Capping at
        # ~3x top_n keeps the list tab fast; volume ordering still holds.
        tickers = {}
        for pair in pairs[: max(top_n * 3, 20)]:
            if not isinstance(pair, str) or not pair.endswith("_usdt"):
                continue
            try:
                r = make_request("https://api.lbank.info/v2/ticker/24hr.do",
                                 self.name, params={"symbol": pair},
                                 timeout=30)
                if r.status_code == 200 and r.json().get("result") == "true":
                    for row in r.json().get("data", []):
                        tickers[row.get("symbol")] = row.get("ticker", {})
            except Exception:
                continue

        results = []
        for pair in pairs:
            if not isinstance(pair, str) or not pair.endswith("_usdt"):
                continue
            base = pair[: -len("_usdt")]
            if exclude_stablecoins and base.upper() in stablecoins:
                continue
            tk = tickers.get(pair, {})
            results.append({
                "rank": 0,
                "symbol": base.upper(),
                "name": base.upper(),
                "price": float(tk.get("latest", 0) or 0),
                "market_cap": float(tk.get("turnover", 0) or 0),
                "volume_24h": float(tk.get("turnover", 0) or 0),
                "source": self.name,
            })

        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class _LbankCandleSource(BinanceLikeCandleSource):
    """LBank klines: second-resolution timestamps, forward pagination."""

    name = "lbank"

    def __init__(self, cfg=None):
        super().__init__("https://api.lbank.info", quote_suffix="usdt",
                         pair_glue="_", symbol_upper=False,
                         page_size=2000)
        self.name = "lbank"

    def get_timeframe_mapping(self):
        return {
            "1m": "minute1", "5m": "minute5", "15m": "minute15",
            "30m": "minute30", "1h": "hour1", "2h": "hour2", "4h": "hour4",
            "6h": "hour6", "8h": "hour8", "12h": "hour12",
            "1d": "day1", "1w": "week1",
        }

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        # LBank `time` is a start bound and rows come back oldest-first, so
        # page boundaries are advanced by the data itself (like the saturated
        # 1m rule in the parent) rather than by the 30-day window.
        interval = self.get_timeframe_mapping().get(timeframe, timeframe)
        url = "https://api.lbank.info/v2/kline.do"
        pair = self._pair(symbol)
        limit = self.page_size

        all_candles = []
        current_start = start_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        while current_start <= end_date and iteration < max_iterations:
            params = {
                "symbol": pair,
                "type": interval,
                "time": int(current_start.timestamp()),
                "size": limit,
            }

            klines = None
            last_err = None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    resp.raise_for_status()
                    body = resp.json()
                    if body.get("result") != "true":
                        raise RuntimeError(
                            f"lbank kline error: {body.get('msg')}")
                    klines = body.get("data", [])
                    break
                except Exception as e:
                    last_err = e
            if klines is None:
                raise RuntimeError(
                    f"lbank unreadable after retries: {last_err}")

            for k in klines:
                # Row: [ts_sec, open, high, low, close, vol]
                ts = int(k[0]) * 1000
                dt = datetime.fromtimestamp(ts / 1000)
                all_candles.append({
                    "timestamp": dt,
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })

            if not klines:
                break

            last_ts = datetime.fromtimestamp(int(klines[-1][0]))
            if len(klines) < limit:
                # A short page means the server has no more rows past `time`
                # in the requested window — stop instead of looping forever.
                break
            current_start = last_ts + timedelta(minutes=1)
            iteration += 1

            if self.on_progress:
                try:
                    self.on_progress(iteration, max(1, total_pages),
                                     len(all_candles))
                except Exception:
                    pass

        # Older-first pages can still arrive out of order when the proxy
        # retries a page, so keep the output sorted and gap-free.
        all_candles.sort(key=lambda c: c["timestamp"])
        return all_candles
