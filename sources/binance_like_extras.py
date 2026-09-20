"""Per-exchange adapters for venues that are *almost* Binance-shaped.

Each class below reuses BinanceLikeListSource / BinanceLikeCandleSource and
overrides only the parts that differ: the response envelope (code/data vs a
bare array), the symbol casing in the response, or the interval naming.

The shared parent already handles the hard parts: 30-day windowing, proxy
JSON-truncation retries, and the saturated-page cursor rule that keeps both
1m and 1h pagination correct.
"""
import time
from datetime import datetime, timedelta

from failover import make_request
from sources.binance_like import BinanceLikeListSource, BinanceLikeCandleSource

# Per-exchange constants. source_registry builds these classes with a
# no-argument cls() call, so each constructor below defaults `cfg` to its
# own entry here instead of taking a required argument.
CONFIG = {
    "bybit": {
        "base_url": "https://api.bybit.com",
        # Bybit interval strings: "1","3","5","15","30","60","120","240","D","W"
        "interval_map": {
            "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
            "1h": "60", "2h": "120", "4h": "240", "1d": "D", "1w": "W",
        },
        "page_size": 1000,
    },
    "xt": {
        "base_url": "https://sapi.xt.com",
        "page_size": 1000,
    },
    "digifinex": {
        "base_url": "https://openapi.digifinex.com",
        "page_size": 1000,
    },
}


class _BybitListSource(BinanceLikeListSource):
    """Bybit v5: /v5/market/tickers?category=spot with a retCode envelope."""

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["bybit"]
        super().__init__(cfg["base_url"], "USDT", True)
        self.name = "bybit"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{self.base_url}/v5/market/tickers"
        resp = make_request(url, self.name,
                            params={"category": "spot"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("retCode", -1)) != 0:
            raise RuntimeError(f"Bybit list error: {data.get('retMsg')}")
        tickers = data.get("result", {}).get("list", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            base = symbol[:-4]
            if exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t.get("turnover24h", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": float(t.get("lastPrice", 0) or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class _BybitCandleSource(BinanceLikeCandleSource):
    """Bybit v5 klines: /v5/market/kline with interval strings like "1","60"."""

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["bybit"]
        super().__init__(cfg["base_url"], "USDT", "", True,
                         cfg["interval_map"], cfg["page_size"])
        self.name = "bybit"
        self.kline_path = "/v5/market/kline"

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        interval = self.get_timeframe_mapping().get(timeframe, timeframe)
        url = f"{self.base_url}{self.kline_path}"
        pair = self._pair(symbol)
        limit = self.page_size

        all_candles = []
        # Bybit answers newest-first and saturates every page, so a forward
        # cursor jumps straight to `end_date` and then stalls on empty pages.
        # Walk backwards from end_date instead; each page is bounded by the
        # oldest row of the previous one.
        cursor = end_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)
        step_s = self._interval_to_seconds(interval)

        while cursor >= start_date and iteration < max_iterations:
            params = {
                "category": "spot",
                "symbol": pair,
                "interval": interval,
                "start": int(start_date.timestamp() * 1000),
                "end": int(cursor.timestamp() * 1000),
                "limit": limit,
            }
            klines, last_err = None, None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    if int(data.get("retCode", -1)) != 0:
                        raise RuntimeError(data.get("retMsg"))
                    klines = data.get("result", {}).get("list", [])
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"bybit unreadable after retries: {last_err}")
            if not klines:
                break

            page_ms = [int(k[0]) for k in klines]
            oldest_ms = min(page_ms)
            newest_ms = max(page_ms)

            for k in klines:
                t = datetime.fromtimestamp(int(k[0]) / 1000)
                if start_date <= t <= end_date:
                    all_candles.append({
                        "timestamp": t,
                        "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5]),
                    })

            cursor = datetime.fromtimestamp(oldest_ms / 1000) - \
                timedelta(seconds=step_s)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        seen = set()
        out = []
        for c in sorted(all_candles, key=lambda x: x["timestamp"]):
            if c["timestamp"] not in seen:
                seen.add(c["timestamp"])
                out.append(c)
        return out


class _BingxListSource(BinanceLikeListSource):
    """BingX spot: /openApi/spot/v1/market/ticker/24hr, dashed pairs."""

    def __init__(self, cfg=None):
        cfg = cfg or {"base_url": "https://open-api.bingx.com"}
        super().__init__(cfg["base_url"], "USDT", True)
        self.name = "bingx"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{self.base_url}/openApi/spot/v1/market/ticker/24hr"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        body = resp.json()

        # BingX wraps most market data in {"code":0,"data":...}.
        tickers = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(tickers, list):
            raise RuntimeError(f"BingX list unexpected shape: {str(body)[:120]}")

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("-USDT"):
                continue
            base = symbol[:-5]
            if exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t.get("quoteVolume", 0) or t.get("volume", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": float(t.get("lastPrice", 0) or t.get("price", 0) or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class _BingxCandleSource(BinanceLikeCandleSource):
    """BingX spot kline: code/data envelope, dashed symbol, newest-first."""

    def __init__(self, cfg=None):
        cfg = cfg or {"base_url": "https://open-api.bingx.com"}
        super().__init__(cfg["base_url"], "USDT", "-", True, None, 1000)
        self.name = "bingx"

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        # BingX ignores startTime/endTime entirely and always answers with
        # its newest 1000 rows (verified: a 20-minute window still returns
        # 1000 rows starting ~17h back). There is no cursor to walk, so this
        # is a single bounded request; rows are clipped to the asked range.
        interval = self.get_timeframe_mapping().get(timeframe, timeframe)
        url = f"{self.base_url}/openApi/spot/v1/market/kline"
        pair = self._pair(symbol)   # BTC-USDT

        params = {"symbol": pair, "interval": interval, "limit": self.page_size}
        klines, last_err = None, None
        for attempt in range(4):
            try:
                resp = make_request(url, self.name, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                if int(data.get("code", -1)) != 0:
                    raise RuntimeError(f"BingX kline error: {data}")
                klines = data.get("data", [])
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        if klines is None:
            raise RuntimeError(f"bingx unreadable after retries: {last_err}")

        out = []
        for k in klines:
            t = datetime.fromtimestamp(int(k[0]) / 1000)
            if start_date <= t <= end_date:
                out.append({
                    "timestamp": t,
                    "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]),
                    "volume": float(k[5]),
                })
        out.sort(key=lambda c: c["timestamp"])
        if self.on_progress:
            try:
                self.on_progress(1, 1, len(out))
            except Exception:
                pass
        return out


class _XtListSource(BinanceLikeListSource):
    """XT: /v4/public/ticker/24h with a result envelope, lowercase pairs."""

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["xt"]
        super().__init__(cfg["base_url"], "usdt", False)
        self.name = "xt"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{self.base_url}/v4/public/ticker/24h"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("rc", -1)) != 0:
            raise RuntimeError(f"XT list error: {data.get('mc')}")
        tickers = data.get("result", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            symbol = (t.get("s") or "").lower()
            if not symbol.endswith("_usdt"):
                continue
            base = symbol[:-5]
            if exclude_stablecoins and base.upper() in stablecoins:
                continue
            vol = float(t.get("q", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base.upper(), "name": base.upper(),
                "price": float(t.get("c", 0) or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class _XtCandleSource(BinanceLikeCandleSource):
    """XT /v4/public/kline: result[].t o c h l q v — newest-first."""

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["xt"]
        super().__init__(cfg["base_url"], "usdt", "_", False, None,
                         cfg["page_size"])
        self.name = "xt"

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        interval = self.get_timeframe_mapping().get(timeframe, timeframe)
        url = f"{self.base_url}/v4/public/kline"
        pair = self._pair(symbol)
        limit = self.page_size

        all_candles = []
        # XT answers newest-first and saturates every page; a forward cursor
        # jumps to end_date and then stalls on empty pages. Walk backwards.
        cursor = end_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)
        step_s = self._interval_to_seconds(interval)

        while cursor >= start_date and iteration < max_iterations:
            params = {
                "symbol": pair, "interval": interval,
                "startTime": int(start_date.timestamp() * 1000),
                "endTime": int(cursor.timestamp() * 1000),
                "limit": limit,
            }
            klines, last_err = None, None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    if int(data.get("rc", -1)) != 0:
                        raise RuntimeError(data.get("mc"))
                    klines = data.get("result", [])
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"xt unreadable after retries: {last_err}")
            if not klines:
                break

            oldest_ms = min(int(k["t"]) for k in klines)

            for k in klines:
                t = datetime.fromtimestamp(int(k["t"]) / 1000)
                if start_date <= t <= end_date:
                    all_candles.append({
                        "timestamp": t,
                        "open": float(k["o"]), "high": float(k["h"]),
                        "low": float(k["l"]), "close": float(k["c"]),
                        "volume": float(k["v"]),
                    })

            cursor = datetime.fromtimestamp(oldest_ms / 1000) - \
                timedelta(seconds=step_s)
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        seen = set()
        out = []
        for c in sorted(all_candles, key=lambda x: x["timestamp"]):
            if c["timestamp"] not in seen:
                seen.add(c["timestamp"])
                out.append(c)
        return out


class _DigifinexListSource(BinanceLikeListSource):
    """DigiFinex /v3/ticker with a code/data envelope."""

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["digifinex"]
        super().__init__(cfg["base_url"], "usdt", False)
        self.name = "digifinex"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{self.base_url}/v3/ticker"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"DigiFinex list error: {data}")
        tickers = data.get("data", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            symbol = (t.get("symbol") or "").lower()
            if not symbol.endswith("_usdt"):
                continue
            base = symbol[:-5]
            if exclude_stablecoins and base.upper() in stablecoins:
                continue
            vol = float(t.get("quote_volume", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base.upper(), "name": base.upper(),
                "price": float(t.get("last", 0) or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class _DigifinexCandleSource(BinanceLikeCandleSource):
    """DigiFinex /v3/kline: code/data, [ts, vol, o, h, l, c].

    Contract confirmed by probe: symbol is UPPERCASE (BTC_USDT), period is a
    NUMBER (1, 5, 15, 60...) not a string, and start/end are epoch SECONDS.
    Lowercase symbol or "1m" returns code 10004 silently.
    """

    PERIOD = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
              "2h": "120", "4h": "240", "1d": "1440", "1w": "10080"}

    def __init__(self, cfg=None):
        cfg = cfg or CONFIG["digifinex"]
        super().__init__(cfg["base_url"], "USDT", "_", True, self.PERIOD,
                         cfg["page_size"])
        self.name = "digifinex"

    def get_timeframe_mapping(self):
        return dict(self.PERIOD)

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        # DigiFinex caps at 500 rows regardless of `limit` and ignores `end`
        # (verified: limit=2000 still returns 500; end=12h ago still returns
        # the newest 500). Single bounded request; rows clipped to range.
        interval = self.get_timeframe_mapping().get(timeframe, "1")
        url = f"{self.base_url}/v3/kline"
        pair = self._pair(symbol)  # BTC_USDT (uppercase)

        params = {"symbol": pair, "period": interval, "limit": self.page_size}
        klines, last_err = None, None
        for attempt in range(4):
            try:
                resp = make_request(url, self.name, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                if int(data.get("code", -1)) != 0:
                    raise RuntimeError(f"DigiFinex kline error: {data}")
                klines = data.get("data", [])
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(0.6 * (attempt + 1))
        if klines is None:
            raise RuntimeError(f"digifinex unreadable after retries: {last_err}")

        out = []
        for k in klines:  # [ts(seconds), volume, open, high, low, close]
            t = datetime.fromtimestamp(int(k[0]))
            if start_date <= t <= end_date:
                out.append({
                    "timestamp": t,
                    "volume": float(k[1]), "open": float(k[2]),
                    "high": float(k[3]), "low": float(k[4]),
                    "close": float(k[5]),
                })
        out.sort(key=lambda c: c["timestamp"])
        if self.on_progress:
            try:
                self.on_progress(1, 1, len(out))
            except Exception:
                pass
        return out
