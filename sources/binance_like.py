"""Shared adapter base for Binance-compatible exchanges.

Many top exchanges speak (or closely imitate) the Binance REST contract:
  GET /api/v3/ticker/24hr      -> listing
  GET /api/v3/klines           -> candles

Rather than duplicating ~150 lines of windowing, retry and cursor logic per
exchange, this module provides one implementation parameterized by the small
set of things that actually differ between venues:

  quote_suffix  – quote currency of the traded pair ("USDT", "USD", "KRW")
  pair_glue     – separator inside a pair name ("", "-", "/", "_")
  symbol_upper  – whether the venue wants an upper- or lower-cased pair
  interval_map  – timeframe translation where the venue is not Binance-shaped
  page_size     – max candles per request (drives progress estimates)

Only exchanges that are genuinely Binance-shaped use this module. Exchanges
with their own contract (Upbit, Bitstamp, Coinbase, Kraken, HTX, Gate,
Bitfinex, Phemex, AscendEX, Bithumb, Coincheck, Korbit) get a dedicated
module because their response shapes do not fit this adapter.
"""
import time
from datetime import datetime, timedelta
from data_fetcher import CryptoListSource, CandleDataSource
from failover import make_request


class BinanceLikeListSource(CryptoListSource):
    """List source for an exchange speaking the Binance ticker/24hr dialect."""

    # Concrete subclasses set this; it satisfies the abstract property on
    # CandleDataSource/CryptoListSource.
    name = "binancelike"

    def __init__(self, base_url, quote_suffix="USDT", symbol_upper=True):
        self.base_url = base_url
        self.quote_suffix = quote_suffix
        self.symbol_upper = symbol_upper

    def fetch_list(self, top_n, exclude_stablecoins):
        url = f"{self.base_url}/api/v3/ticker/24hr"
        resp = make_request(url, self.name, timeout=30)
        resp.raise_for_status()
        tickers = resp.json()

        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        results = []
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            qs = self.quote_suffix
            if not symbol.endswith(qs):
                continue
            base_symbol = symbol[: -len(qs)]
            if exclude_stablecoins and base_symbol in stablecoins:
                continue
            quote_vol = float(ticker.get("quoteVolume", 0) or 0)
            if quote_vol <= 0:
                continue
            results.append({
                "rank": 0,
                "symbol": base_symbol,
                "name": base_symbol,
                "price": float(ticker.get("lastPrice", 0) or 0),
                "market_cap": quote_vol,
                "volume_24h": quote_vol,
                "source": self.name,
            })

        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(results[: top_n * 2]):
            r["rank"] = i + 1
        return results[:top_n]


class BinanceLikeCandleSource(CandleDataSource):
    """Candle source for an exchange speaking the Binance klines dialect.

    Handles the same hazards discovered for binance/mexc: wide ranges are
    split into 30-day windows, truncated proxy bodies are retried, and the
    cursor advances by data when the page is saturated (1m) but by window
    when it is not (1h), so neither timeframe silently drops months.
    """

    name = "binancelike"

    # Set by the registry before fetch: callback(page, total_pages, so_far).
    # Declared on the base so every subclass (and every new exchange added
    # via the registry) inherits it instead of raising AttributeError.
    on_progress = None

    def __init__(self, base_url, quote_suffix="USDT", pair_glue="",
                 symbol_upper=True, interval_map=None, page_size=1000):
        self.base_url = base_url
        self.quote_suffix = quote_suffix
        self.pair_glue = pair_glue
        self.symbol_upper = symbol_upper
        self.interval_map = interval_map or {}
        self.page_size = page_size

    def get_timeframe_mapping(self):
        base = {
            "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d", "1w": "1w",
        }
        base.update(self.interval_map)
        return base

    def _pair(self, symbol):
        s = symbol.upper() if self.symbol_upper else symbol.lower()
        if self.pair_glue:
            return f"{s}{self.pair_glue}{self.quote_suffix}"
        return f"{s}{self.quote_suffix}"

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        interval = self.get_timeframe_mapping().get(timeframe, timeframe)
        url = f"{self.base_url}/api/v3/klines"
        pair = self._pair(symbol)
        limit = self.page_size

        all_candles = []
        current_start = start_date
        iteration = 0
        total_pages = self.estimate_pages(timeframe, start_date, end_date)
        max_iterations = max(500, total_pages + 50)
        max_window_seconds = 30 * 24 * 3600  # 30 days per request

        while current_start <= end_date and iteration < max_iterations:
            req_end = min(end_date,
                          current_start + timedelta(seconds=max_window_seconds))
            params = {
                "symbol": pair,
                "interval": interval,
                "startTime": int(current_start.timestamp() * 1000),
                "endTime": int(req_end.timestamp() * 1000),
                "limit": limit,
            }

            # The SOCKS5 proxy can hand back a truncated body with HTTP 200;
            # retry the window a few times before failing the symbol.
            klines = None
            last_err = None
            for attempt in range(4):
                try:
                    resp = make_request(url, self.name, params=params,
                                        timeout=60)
                    resp.raise_for_status()
                    klines = resp.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(0.6 * (attempt + 1))
            if klines is None:
                raise RuntimeError(f"{self.name} returned unreadable data "
                                   f"after retries: {last_err}")

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

            # Advance the cursor. If the exchange saturated its page limit the
            # window is NOT exhausted (1m over 30 days = 43,200 candles vs a
            # 1000-row page), so resume after the last received candle.
            # Otherwise the window was fully served (1h over 30 days = 720
            # rows), so advance by the whole window.
            if len(klines) >= limit:
                last_ts = datetime.fromtimestamp(klines[-1][0] / 1000)
                current_start = last_ts + timedelta(
                    seconds=self._interval_to_seconds(interval))
            else:
                current_start = req_end + timedelta(
                    seconds=self._interval_to_seconds(interval))
            iteration += 1
            if self.on_progress:
                try:
                    self.on_progress(iteration, total_pages, len(all_candles))
                except Exception:
                    pass

        return all_candles
