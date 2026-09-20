"""Hyperliquid — a decentralized perpetual venue with a POST/JSON REST API.

Everything below was verified against the live API through the local SOCKS5
proxy before coding, unlike every other exchange here which is GET-shaped:

  POST /info {"type":"meta"}                -> universe = [{name, szDecimals}]
  POST /info {"type":"metaAndAssetCtxs"}    -> [meta, [{dayNtlVlm, markPx}]]
  POST /info {"type":"candleSnapshot",
              "req":{"coin","interval","startTime","endTime"}}
      -> [{"t": ms_open, "o","c","h","l","v","n":trades}]  OLDEST FIRST

Candle facts the adapter has to respect:
  * rows come back oldest-first and both bounds are honored, so a 3-day 1m
    window arrives complete in ONE request (4321 rows);
  * a wide window (>= ~30d) gets killed by the proxy's SSL layer mid-body, so
    the fetch still splits into bounded pages like the other sources;
  * volume is in base coins (not quote) — kept as-is, matching the app's
    MT5 VOL column convention;
  * only the *perp* universe has volume data; spot pairs (spotMeta) are
    tiny and are skipped.

Symbols are bare ("BTC"), which matches how the app keys its lists, so no
pair building is needed for candles.
"""

import time
from datetime import datetime, timedelta

import requests
from proxy_manager import get_proxy_for_source
from data_fetcher import CryptoListSource, CandleDataSource

_BASE = "https://api.hyperliquid.xyz/info"

# Reuse the shared session's connection pool / retry config but POST through
# it; make_request() is GET-only and adding POST to it would touch the core
# failover path used by every other exchange.
_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})


def _post(source_name, body, tries=4, timeout=90):
    """POST with retry — the proxy resets SSL on wide snapshots."""
    proxies = get_proxy_for_source(source_name)
    last = None
    for _ in range(tries):
        try:
            resp = _SESSION.post(_BASE, json=body, proxies=proxies,
                                 timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"{source_name} unreadable after retries: {last}")


# Hyperliquid interval names are the same as ours, but the API rejects
# anything not in this set.
_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
    "2h": "2h", "4h": "4h", "8h": "8h", "12h": "12h", "1d": "1d", "1w": "1w",
}

# Verified live: a 1m request is hard-capped at ~5198 rows — the server
# silently truncates instead of erroring, which looked like "end of data"
# to the paging loop and stopped the fetch early (5m worked because 7d of
# 5m is only ~2000 rows). Size pages by CANDLE COUNT, not wall time, so
# every interval stays safely under the ceiling.
_CANDLE_CAP = 5000


class HyperliquidListSource(CryptoListSource):
    name = "hyperliquid"

    def fetch_list(self, top_n, exclude_stablecoins):
        from database import get_stablecoins
        stablecoins = set(get_stablecoins()) if exclude_stablecoins else set()

        meta, ctxs = _post(self.name, {"type": "metaAndAssetCtxs"})
        universe = (meta or {}).get("universe", [])

        rows = []
        for spec, ctx in zip(universe, ctxs):
            if not isinstance(spec, dict) or spec.get("isDelisted"):
                continue
            name = (spec.get("name") or "").upper()
            if not name or name in stablecoins:
                continue
            vol = 0.0
            try:
                vol = float(ctx.get("dayNtlVlm", 0) or 0)
            except (TypeError, ValueError):
                pass
            if vol <= 0:
                continue
            rows.append({
                "rank": 0,
                "symbol": name,
                "name": name,
                "price": _tof(ctx.get("markPx")),
                "market_cap": vol,
                "volume_24h": vol,
                "source": self.name,
            })

        rows.sort(key=lambda x: x["volume_24h"], reverse=True)
        for i, r in enumerate(rows[: top_n * 2]):
            r["rank"] = i + 1
        return rows[:top_n]


class HyperliquidCandleSource(CandleDataSource):
    name = "hyperliquid"

    # Set by the registry before fetch.
    on_progress = None

    def get_timeframe_mapping(self):
        return dict(_INTERVALS)

    def fetch_candles(self, symbol, timeframe, start_date, end_date):
        interval = _INTERVALS.get(timeframe)
        if interval is None:
            raise RuntimeError(f"hyperliquid has no {timeframe} interval")
        # The coin name is the bare symbol for perps ("BTC"); a quote suffix
        # would be rejected by the API.
        coin = symbol.upper().split("/")[0].split(":")[0]

        from source_registry import TF_MINUTES
        step_minutes = TF_MINUTES.get(timeframe, 1)

        def snap(a, b):
            """One request. Returns parsed rows, or None if the proxy
            truncated the body (JSONDecodeError) — caller should shrink."""
            try:
                return _post(self.name, {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": interval,
                        "startTime": int(a.timestamp() * 1000),
                        "endTime": int(b.timestamp() * 1000),
                    },
                }, tries=2, timeout=60)
            except Exception:
                return None

        def snap_adaptive(a, b):
            """Fetch [a,b]. If the proxy cuts the body, halve the window and
            retry until it parses; floor at 100 candles so we always make
            progress. Returns rows."""
            width_min = max(1, int((b - a).total_seconds() // 60))
            while True:
                rows = snap(a, b)
                if rows is not None:
                    return rows
                if width_min <= 100:
                    return []
                half = timedelta(minutes=width_min // 2)
                return (snap_adaptive(a, a + half)
                        + snap_adaptive(min(b, a + half), b))

        all_candles = []
        seen = set()
        # Page by candle count so every interval starts under the row cap;
        # the adaptive split handles anything the proxy still cuts.
        page_delta = timedelta(minutes=step_minutes * (_CANDLE_CAP - 10))
        total_pages = max(1, int((end_date - start_date) / page_delta) + 1)
        max_pages = max(500, total_pages + 50)
        page = 0

        window = start_date
        while window <= end_date and page < max_pages:
            req_end = min(end_date, window + page_delta)
            rows = snap_adaptive(window, req_end)

            for k in rows:
                ts = datetime.fromtimestamp(int(k["t"]) / 1000)
                if ts in seen:
                    # Overlapping pages re-send the boundary candle; skip it.
                    continue
                seen.add(ts)
                all_candles.append({
                    "timestamp": ts,
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k.get("v", 0) or 0),
                })

            page += 1
            if self.on_progress:
                try:
                    self.on_progress(page, max(1, total_pages),
                                     len(all_candles))
                except Exception:
                    pass

            if not rows:
                # Genuine gap: advance and keep going.
                window = req_end + timedelta(seconds=1)
                continue
            last_t = int(rows[-1]["t"]) / 1000
            new_window = datetime.fromtimestamp(last_t) + timedelta(seconds=1)
            if new_window <= req_end:
                # Fewer rows than the window covers. This is either the real
                # end of data or a proxy cut that the adaptive split already
                # shrank past. Advance past whatever we DID get and continue,
                # so a truncation can never stop the whole fetch; a genuine
                # end of data terminates when the next window comes back empty.
                if len(rows) <= 5:
                    # Persistently empty: this is the real end of history.
                    break
                window = new_window
                continue
            window = new_window

        all_candles.sort(key=lambda c: c["timestamp"])
        return all_candles


def _tof(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
