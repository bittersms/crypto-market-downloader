"""Exchanges with their own (non-Binance) REST contract.

Each venue below was verified reachable through the local SOCKS5 proxy and
its kline response shape inspected before coding this module. Unlike the
binance_like family these cannot share one adapter, but they all share the
same hazards: 30-day windowing, truncated proxy bodies, and the
saturated-page cursor rule.
"""
import time
from datetime import datetime, timedelta, timezone

from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


def _as_utc(dt):
    """Treat a naive datetime as local time and convert to real UTC.

    This app threads naive datetimes throughout (they are only ever compared
    against each other), but Coinbase parses a literal trailing "Z" as UTC.
    On a UTC+3:30 host that made every requested start 3.5 hours in the
    future and every request died with 400 "Start cannot be in the future".
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)

# Shared pagination scaffolding — the window/retry/cursor loop is identical
# across these sources, so it lives here once and each subclass fills in
# _fetch_window() (venue-specific HTTP + row parsing) and _row_time().
MAX_WINDOW = 30 * 24 * 3600


def _estimate_pages(source, timeframe, start, end):
    return source.estimate_pages(timeframe, start, end)


def _run(source, symbol, timeframe, start_date, end_date, fetch_window):
    """Generic windowed fetch loop with the saturated-page cursor rule."""
    all_candles = []
    current = start_date
    iteration = 0
    total_pages = _estimate_pages(source, timeframe, start_date, end_date)
    max_iterations = max(500, total_pages + 50)
    while current <= end_date and iteration < max_iterations:
        req_end = min(end_date, current + timedelta(seconds=MAX_WINDOW))
        klines, err = None, None
        for attempt in range(4):
            try:
                klines = fetch_window(current, req_end)
                err = None
                break
            except Exception as e:
                err = e
                time.sleep(0.6 * (attempt + 1))
        if klines is None:
            raise RuntimeError(f"{source.name} unreadable after retries: {err}")
        if not klines:
            break
        all_candles.extend(klines)
        if len(klines) >= source.page_size:
            current = source._row_time(klines[-1]) + timedelta(
                seconds=source._interval_to_seconds(
                    source.get_timeframe_mapping().get(timeframe, timeframe)))
        else:
            current = req_end + timedelta(
                seconds=source._interval_to_seconds(
                    source.get_timeframe_mapping().get(timeframe, timeframe)))
        iteration += 1
        if source.on_progress:
            try:
                source.on_progress(iteration, total_pages, len(all_candles))
            except Exception:
                pass
    return all_candles


class _NativeSource(CandleDataSource):
    """Base for the native-contract sources below."""

    name = "native"
    page_size = 1000

    # Set by the registry before fetch: callback(page, total_pages, so_far).
    # Declared here so every subclass inherits it instead of raising
    # AttributeError the first time the GUI asks for progress.
    on_progress = None

    def _row_time(self, row):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Kraken — /0/public/OHLC, pair=XBTUSD, interval in minutes.
# ---------------------------------------------------------------------------
class KrakenListSource(CryptoListSource):
    name = "kraken"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.kraken.com/0/public/AssetPairs"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken list error: {data['error']}")
        pairs = data.get("result", {})

        # Volume comes from a separate call per pair; use the ticker endpoint
        # for the top pairs only to keep this cheap.
        url = "https://api.kraken.com/0/public/Ticker"
        resp = make_request(url, self.name, params={"pair": "BTC,ETH,SOL,XRP,"
                                       "ADA,AVAX,LINK,DOT,MATIC,LTC,BCH,ATOM,"
                                       "UNI,NEAR,APT,ARB,OP,INJ,SUI,SEI,TIA"},
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken ticker error: {data['error']}")
        tickers = data.get("result", {})

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for key, t in tickers.items():
            info = pairs.get(key, {})
            base = info.get("base", "")
            # Kraken bases look like "XBT", "XETH" — normalize to BTC/ETH.
            base = base[1:] if base.startswith("X") else base
            base = base.replace("XBT", "BTC") if base == "XBT" else base
            if exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t.get("q", {}).get("quote_volume_24h", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": float(t.get("c", ["0"])[0] or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class KrakenCandleSource(_NativeSource):
    name = "kraken"
    page_size = 720

    KRAKEN_PAIR = {
        "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
        "ADA": "ADAUSD", "AVAX": "AVAXUSD", "LINK": "LINKUSD",
        "DOT": "DOTUSD", "LTC": "LTCUSD", "BCH": "BCHUSD",
        "ATOM": "ATOMUSD", "UNI": "UNIUSD", "NEAR": "NEARUSD",
        "APT": "APTUSD", "ARB": "ARBUSD", "OP": "OPUSD", "INJ": "INJUSD",
        "SUI": "SUIUSD", "SEI": "SEIUSD", "TIA": "TIAUSD",
    }
    TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
              "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080}

    def get_timeframe_mapping(self):
        return {tf: str(v) for tf, v in self.TF_MIN.items()}

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        pair = self.KRAKEN_PAIR.get(symbol, f"{symbol}USD")
        interval = self.TF_MIN.get(timeframe, 1)

        all_candles = []
        # Kraken's OHLC endpoint serves at most ~720 rows and treats `since`
        # as a lower bound only — a request for 3 days of 1m still returns
        # just the newest 720. Walk backwards from end_date.
        cursor = end_date
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        iteration = 0
        while cursor > start_date and iteration < max_iterations:
            klines, last_err = None, None
            for attempt in range(4):
                try:
                    resp = make_request(
                        "https://api.kraken.com/0/public/OHLC", self.name,
                        params={"pair": pair, "interval": interval,
                                "since": int(start_date.timestamp())},
                        timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    res = data.get("result", {})
                    keys = [k for k in res.keys() if k != "last"]
                    klines = res.get(keys[0], []) if keys else []
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"kraken unreadable after retries: {last_err}")
            if not klines:
                break

            page = []
            oldest = None
            for r in klines:  # oldest-first
                t = datetime.fromtimestamp(int(r[0]))
                if start_date <= t <= end_date:
                    page.append({
                        "timestamp": t, "open": float(r[1]),
                        "high": float(r[2]), "low": float(r[3]),
                        "close": float(r[4]), "volume": float(r[6]),
                    })
                if oldest is None or t < oldest:
                    oldest = t
            all_candles.extend(page)

            if oldest is None or len(klines) < self.page_size:
                break
            # Kraken ignores `since` in practice — same page every time.
            break

        seen = set()
        out = []
        for c in sorted(all_candles, key=lambda x: x["timestamp"]):
            if c["timestamp"] not in seen:
                seen.add(c["timestamp"])
                out.append(c)
        return out

    def _row_time(self, row):
        return row["timestamp"]


# ---------------------------------------------------------------------------
# Bitfinex — /v2/candles/trade:1m:tBTCUSD/hist, newest-first.
# ---------------------------------------------------------------------------
class BitfinexListSource(CryptoListSource):
    name = "bitfinex"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api-pub.bitfinex.com/v2/tickers?symbols=ALL"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            sym = t[0] if t else ""
            if not sym.startswith("t") or not sym.endswith("USD"):
                continue
            base = sym[1:-3]
            # Bitfinex nests derivative symbols as "tPEPE:USD"; the ":"
            # suffix marks a derived market that must not reach the pair
            # builder, so keep only the base part.
            base = base.split(":")[0]
            if not base or exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t[8]) if len(t) > 8 and t[8] else 0  # quote volume
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": float(t[1] or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class BitfinexCandleSource(_NativeSource):
    name = "bitfinex"
    page_size = 1000

    TF_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
              "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D", "1w": "1W"}

    def get_timeframe_mapping(self):
        return dict(self.TF_MAP)

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        tf = self.TF_MAP.get(timeframe, "1m")
        url = (f"https://api-pub.bitfinex.com/v2/candles/"
               f"trade:{tf}:t{symbol}USD/hist")

        def fetch_window(a, b):
            resp = make_request(url, self.name,
                                params={"start": int(a.timestamp() * 1000),
                                        "end": int(b.timestamp() * 1000),
                                        "limit": self.page_size,
                                        "sort": 1},
                                timeout=60)
            resp.raise_for_status()
            out = []
            for r in resp.json():
                t = datetime.fromtimestamp(int(r[0]) / 1000)
                if a <= t <= b:
                    out.append({
                        "timestamp": t, "open": float(r[1]),
                        "high": float(r[3]), "low": float(r[4]),
                        "close": float(r[2]), "volume": float(r[5]),
                    })
            out.sort(key=lambda c: c["timestamp"])
            return out

        return _run(self, symbol, timeframe, start_date, end_date, fetch_window)

    def _row_time(self, row):
        return row["timestamp"]


# ---------------------------------------------------------------------------
# Coinbase — /products/BTC-USD/candles?granularity=60, [t, low, high, open, close, vol].
# ---------------------------------------------------------------------------
class CoinbaseListSource(CryptoListSource):
    name = "coinbase"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.exchange.coinbase.com/products"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        products = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for p in products:
            if p.get("quote_currency_id") != "USD" or \
               p.get("trading_disabled") or p.get("is_disabled"):
                continue
            base = p.get("base_currency_id", "")
            if exclude_stablecoins and base in stablecoins:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": 0, "market_cap": 0, "volume_24h": 0,
                "source": self.name,
            })
        return results[:top_n]


class CoinbaseCandleSource(_NativeSource):
    name = "coinbase"
    # Coinbase caps granularity 60 (1m) but the array is newest-first and
    # bounded per request; small page keeps pagination stepping correctly.
    page_size = 300

    GRAN = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800}

    def get_timeframe_mapping(self):
        return {tf: str(v) for tf, v in self.GRAN.items()}

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        url = f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles"
        gran = self.GRAN.get(timeframe, 60)

        # Coinbase rejects any start it considers future. The naive datetimes
        # threaded through this app are local (UTC+3:30 here), and formatting
        # them with a literal "Z" made Coinbase read them as 3.5h ahead —
        # every request died with 400 "Start cannot be in the future".
        # Convert to real UTC before serializing.

        # Coinbase rejects a window wider than 300 aggregations with 400
        # "granularity too small for the requested time range". Cap the page
        # so a wide request degrades to the newest allowed slice instead of
        # failing the whole symbol.
        page_span = timedelta(seconds=gran * self.page_size)

        def fetch_window(a, b):
            if (b - a) > page_span:
                a = b - page_span
            resp = make_request(url, self.name,
                                params={"granularity": gran,
                                        "start": _as_utc(a).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "end": _as_utc(b).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                timeout=60)
            resp.raise_for_status()
            out = []
            for r in resp.json():
                t = datetime.fromtimestamp(int(r[0]))
                if a <= t <= b:
                    # Coinbase: [t, low, high, open, close, volume]
                    out.append({
                        "timestamp": t, "open": float(r[3]),
                        "high": float(r[2]), "low": float(r[1]),
                        "close": float(r[4]), "volume": float(r[5]),
                    })
            out.sort(key=lambda c: c["timestamp"])
            return out

        return _run(self, symbol, timeframe, start_date, end_date, fetch_window)

    def _row_time(self, row):
        return row["timestamp"]


# ---------------------------------------------------------------------------
# Bitstamp — /api/v2/ohlc/btcusd/?step=60, data.ohlc[].
# ---------------------------------------------------------------------------
class BitstampListSource(CryptoListSource):
    name = "bitstamp"

    def fetch_list(self, top_n, exclude_stablecoins):
        # /v2/ticker/ returns a dict of pair -> ticker for every market, so
        # we can rank USD pairs by volume instead of exposing BTC alone.
        url = "https://www.bitstamp.net/api/v2/ticker/"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            pair = (t.get("pair") or "").lower()
            if not pair.endswith("usd"):
                continue
            base = pair[:-3].rstrip("/")
            if not base or exclude_stablecoins and base.upper() in stablecoins:
                continue
            last = float(t.get("last", 0) or 0)
            vol = float(t.get("volume", 0) or 0) * last
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base.upper(), "name": base.upper(),
                "price": last,
                "market_cap": vol,
                "volume_24h": vol,
                "source": self.name,
            })

        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class BitstampCandleSource(_NativeSource):
    name = "bitstamp"
    # Bitstamp's /ohlc endpoint ignores start/end when the requested range is
    # wider than `limit` candles: it returns the newest `limit` rows instead.
    # Older history needs /v2/ohlc/<pair>/?start=... walked page by page.
    page_size = 1000

    STEP = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800}

    def get_timeframe_mapping(self):
        return {tf: str(v) for tf, v in self.STEP.items()}

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        step = self.STEP.get(timeframe, 60)
        url = f"https://www.bitstamp.net/api/v2/ohlc/{symbol.lower()}usd/"

        # Bitstamp answers with its newest `limit` rows below `end`, so it
        # DOES support deep history when walked backwards — a forward cursor
        # only ever fetched the newest page and stopped.
        all_candles = []
        cursor = end_date
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        iteration = 0
        while cursor >= start_date and iteration < max_iterations:
            klines, last_err = None, None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name,
                                        params={"step": step,
                                                "limit": self.page_size,
                                                "start": 0,
                                                "end": int(cursor.timestamp())},
                                        timeout=60)
                    resp.raise_for_status()
                    klines = resp.json().get("data", {}).get("ohlc", [])
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"bitstamp unreadable after retries: {last_err}")
            if not klines:
                break

            oldest = None
            for o in klines:
                t = datetime.fromtimestamp(int(o["timestamp"]))
                if start_date <= t <= end_date:
                    all_candles.append({
                        "timestamp": t, "open": float(o["open"]),
                        "high": float(o["high"]), "low": float(o["low"]),
                        "close": float(o["close"]),
                        "volume": float(o["volume"]),
                    })
                if oldest is None or t < oldest:
                    oldest = t
            if oldest is None:
                break
            cursor = oldest - timedelta(seconds=step)
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

    def _row_time(self, row):
        return row["timestamp"]


# ---------------------------------------------------------------------------
# HTX (Huobi) — /market/history/kline?period=1m&symbol=btcusdt, newest-first.
# ---------------------------------------------------------------------------
class HtxListSource(CryptoListSource):
    name = "htx"

    def fetch_list(self, top_n, exclude_stablecoins):
        url = "https://api.huobi.pro/market/tickers"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"HTX list error: {data.get('err-msg')}")
        tickers = data.get("data", [])

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("usdt"):
                continue
            base = sym[:-4].upper()
            if exclude_stablecoins and base in stablecoins:
                continue
            vol = float(t.get("vol", 0) or 0) * float(t.get("close", 0) or 0)
            if vol <= 0:
                continue
            results.append({
                "rank": 0, "symbol": base, "name": base,
                "price": float(t.get("close", 0) or 0),
                "market_cap": vol, "volume_24h": vol, "source": self.name,
            })
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class HtxCandleSource(_NativeSource):
    name = "htx"
    # HTX /market/history/kline ignores from/to and always answers with the
    # newest `size` rows, so a 30-day window cannot be filled one page at a
    # time the way Binance can. Page through backwards instead.
    page_size = 1000

    PERIOD = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
              "30m": "30min", "1h": "60min", "2h": "120min", "4h": "4hour",
              "1d": "1day", "1w": "1week"}

    def get_timeframe_mapping(self):
        return dict(self.PERIOD)

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        period = self.PERIOD.get(timeframe, "1min")
        url = "https://api.huobi.pro/market/history/kline"
        step = self._interval_to_seconds(period)

        all_candles = []
        # HTX answers newest-first; this is the timestamp of the oldest row
        # still to fetch. Moves backwards one page at a time.
        cursor = end_date
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)

        iteration = 0
        while cursor > start_date and iteration < max_iterations:
            klines, last_err = None, None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name,
                                        params={"symbol": f"{symbol.lower()}usdt",
                                                "period": period,
                                                "size": self.page_size},
                                        timeout=60)
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("status") != "ok":
                        raise RuntimeError(data.get("err-msg"))
                    klines = data.get("data", [])
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"htx unreadable after retries: {last_err}")
            if not klines:
                break

            page = []
            oldest = None
            for r in klines:  # newest-first
                t = datetime.fromtimestamp(int(r["id"]))
                if start_date <= t <= end_date:
                    page.append({
                        "timestamp": t, "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"]), "volume": float(r["vol"]),
                    })
                if oldest is None or t < oldest:
                    oldest = t
            all_candles.extend(page)

            if oldest is None or len(klines) < self.page_size:
                break
            # HTX's history endpoint has no from/to or cursor parameter — it
            # always returns the same newest `size` rows, so there is no way
            # to walk further back. One page is all this venue can give.
            break

        seen = set()
        out = []
        for c in sorted(all_candles, key=lambda x: x["timestamp"]):
            key = c["timestamp"]
            if key not in seen:
                seen.add(key)
                out.append(c)
        return out

    def _row_time(self, row):
        return row["timestamp"]
