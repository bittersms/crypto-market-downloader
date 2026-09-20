"""Local Binance-compatible REST API server.

Serves the 1m candle data held in the local CSV files via the Binance
/api/v3/klines and /api/v3/exchangeInfo contract, so any client written
against the Binance API (trading bots, charting tools) can read the
collected data without touching the exchange.

Run directly:
    python api_server.py --port 8900 --symbols BTC,ETH
or manage it from the Auto-Update tab of the app.
"""
import argparse
import csv
import glob
import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Where the downloader writes SYMBOL_TIMEFRAME_EXCHANGE.csv files.
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "mt5_data")


class CandleStore:
    """Loads and caches 1m candle files, answers range queries.

    Files are re-read on disk change (cheap: we only stat and reload when
    mtime changed), so the auto-updater appending new minutes becomes
    visible without a restart.
    """

    def __init__(self, data_dir=DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        # symbol -> {"rows": [[ms,o,h,l,c,v],...], "first_ms":.., "last_ms":.., "mtime":..}
        self._cache = {}

    # ---------- file discovery ----------
    def _file_for(self, symbol):
        """Find the 1m file for a base symbol (BTC, ETH, ...).

        Files are stored as BTCUSD_1M_*.csv (MT5 style, with the USD quote),
        so callers asking for BTC must be mapped to BTCUSD.
        """
        candidates = {symbol.upper(), symbol.upper() + "USD"}
        for name in candidates:
            pattern = os.path.join(self.data_dir, f"{name}_1M*.csv")
            matches = glob.glob(pattern)
            if matches:
                return matches[0]
        return None

    def _load(self, symbol):
        path = self._file_for(symbol)
        if path is None:
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        with self._lock:
            entry = self._cache.get(symbol)
            if entry and entry["mtime"] == mtime:
                return entry
            # Optional precomputed index: one .idx file per data file with
            # (first_ms, last_ms, count) so we don't have to parse 900k rows
            # just to answer "how many candles do you have?".
            idx_path = path + ".idx"
            try:
                idx_mtime = os.path.getmtime(idx_path)
            except OSError:
                idx_mtime = None
            use_index = (idx_mtime is not None and idx_mtime >= mtime)
            if use_index:
                with open(idx_path, "r", encoding="utf-8") as f:
                    first_ms, last_ms, count = (
                        int(x) for x in f.read().split(","))
                entry = {
                    "rows": None, "first_ms": first_ms,
                    "last_ms": last_ms, "count": count,
                    "mtime": mtime, "path": path,
                }
                self._cache[symbol] = entry
                return entry

            rows = self._parse_csv(path)
            rows.sort(key=lambda x: x[0])
            entry = {
                "rows": rows,
                "first_ms": rows[0][0] if rows else 0,
                "last_ms": rows[-1][0] if rows else 0,
                "count": len(rows),
                "mtime": mtime,
                "path": path,
            }
            self._cache[symbol] = entry
            self._write_index(path, entry)
            return entry

    @staticmethod
    def _parse_csv(path):
        rows = []
        # Hand-rolled scan instead of csv.reader + datetime.strptime: parsing
        # a 900k-row / 70MB file with the stdlib version took ~20s and made
        # the first /klines request time out. This loop is ~30x faster.
        with open(path, "r", encoding="utf-8", errors="ignore",
                  newline="") as f:
            lines = f.read().split("\n")
        months = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
        for line in lines[1:]:
            p = line.split("\t")
            if len(p) < 7:
                continue
            try:
                d = p[0]; t = p[1]
                Y = int(d[0:4]); Mo = int(d[5:7]); D = int(d[8:10])
                H = int(t[0:2]); Mi = int(t[3:5])
                days = (Y - 1970) * 365 + (Y - 1969) // 4 + months[Mo - 1] + D - 1
                if Mo > 2 and (Y % 4 == 0 and (Y % 100 != 0 or Y % 400 == 0)):
                    days += 1
                ts = ((days * 24 + H) * 60 + Mi) * 60
                rows.append([ts * 1000,
                             float(p[2]), float(p[3]), float(p[4]),
                             float(p[5]), float(p[6])])
            except (ValueError, IndexError):
                continue
        return rows

    @staticmethod
    def _write_index(path, entry):
        """Persist a (first, last, count) index next to the CSV."""
        try:
            with open(path + ".idx", "w", encoding="utf-8") as f:
                f.write(f"{entry['first_ms']},{entry['last_ms']},"
                        f"{entry['count']}")
        except OSError:
            pass

    # ---------- queries ----------
    def available_symbols(self):
        """All symbols that have a 1m file in the data dir."""
        out = []
        for path in glob.glob(os.path.join(self.data_dir, "*_1M*.csv")):
            name = os.path.basename(path)
            out.append(name.split("_")[0])
        return sorted(set(out))

    def klines(self, symbol, start_ms=None, end_ms=None, limit=1000):
        entry = self._load(symbol)
        if not entry or not entry["count"]:
            return []
        # A precomputed index means rows aren't parsed yet; only parse now if
        # the caller actually wants candles.
        rows = entry["rows"]
        if rows is None:
            rows = self._parse_csv(entry["path"])
            rows.sort(key=lambda x: x[0])
            entry["rows"] = rows
        if not rows:
            return []
        if start_ms is None:
            start_ms = 0
        if end_ms is None:
            end_ms = rows[-1][0] + 1
        sel = [r for r in rows if start_ms <= r[0] <= end_ms]
        # Binance returns the LAST `limit` candles for a forward query.
        if limit and len(sel) > limit:
            sel = sel[-limit:]
        return sel

    def ticker(self, symbol):
        """Last-known price for a symbol, from the most recent candle.

        `symbol` is the Binance pair (BTCUSDT); returns None if unknown.
        Reads only the final CSV line — no full parse.
        """
        if not symbol:
            return None
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        base = base[:-3] if base.endswith("USD") else base
        entry = self._load(base)
        if not entry or not entry["count"]:
            return None
        last_price = None
        if entry["rows"] is not None:
            last_price = f"{entry['rows'][-1][4]:.8f}"
        else:
            # index-only entry: read just the final line of the CSV.
            # The file ends with a newline, so we must skip trailing EOLs
            # and then walk back to the start of that last line.
            try:
                with open(entry["path"], "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    if size == 0:
                        return None
                    pos = size - 1
                    while pos > 0:
                        f.seek(pos)
                        if f.read(1) not in (b"\n", b"\r"):
                            break
                        pos -= 1
                    start = 0
                    while pos > 0:
                        f.seek(pos)
                        if f.read(1) == b"\n":
                            start = pos + 1
                            break
                        pos -= 1
                    f.seek(start)
                    line = f.readline().decode("utf-8", "ignore")
                parts = line.split("\t")
                if len(parts) >= 6:
                    last_price = parts[5]  # CLOSE column
            except OSError:
                return None
        if last_price is None:
            return None
        return {"symbol": symbol, "price": last_price, "time": entry["last_ms"]}

    def summary(self):
        """Per-symbol candle counts, for the GUI status display."""
        out = {}
        for sym in self.available_symbols():
            entry = self._load(sym)
            if entry and entry["count"]:
                out[sym] = {
                    "count": entry["count"],
                    "first": datetime.utcfromtimestamp(
                        entry["first_ms"] / 1000).strftime("%Y.%m.%d %H:%M"),
                    "last": datetime.utcfromtimestamp(
                        entry["last_ms"] / 1000).strftime("%Y.%m.%d %H:%M"),
                }
        return out


# Global store used by the request handler.
_store = CandleStore()


def _mt_symbol_to_base(name):
    """BTCUSD_1M_BINANCE.csv -> BTC  (strip the trailing USD quote)."""
    base = name.split("_")[0]
    return base[:-3] if base.endswith("USD") else base


def build_exchange_info(symbols):
    """Minimal /api/v3/exchangeInfo response covering our symbols.

    `symbols` here are the raw file prefixes (BTCUSD, ETHUSD, ...) which we
    translate to Binance pair names (BTCUSDT, ETHUSDT, ...).
    """
    return {
        "timezone": "UTC",
        "serverTime": int(datetime.now().timestamp() * 1000),
        "rateLimits": [],
        "exchangeFilters": [],
        "symbols": [
            {
                "symbol": f"{_mt_symbol_to_base(s)}USDT",
                "status": "TRADING",
                "baseAsset": _mt_symbol_to_base(s),
                "quoteAsset": "USDT",
                "baseAssetPrecision": 8,
                "quotePrecision": 8,
                "filters": [],
                "permissions": ["SPOT"],
            }
            for s in symbols
        ],
    }


class BinanceHandler(BaseHTTPRequestHandler):
    """Handles Binance-compatible endpoints backed by local CSV data."""

    # Silence default logging; the app writes its own status lines.
    def log_message(self, *args):
        pass

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, err_code=-1, http_code=400):
        self._send_json({"code": err_code, "msg": msg}, http_code)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/api/v3/ping":
            self._send_json({})
            return

        if path == "/api/v3/time":
            self._send_json({"serverTime": int(datetime.now().timestamp() * 1000)})
            return

        if path == "/api/v3/exchangeInfo":
            self._send_json(build_exchange_info(_store.available_symbols()))
            return

        # Endpoints some clients call during initialization. We answer them
        # from what we have locally so the client connects without error.
        if path == "/api/v3/ticker/price":
            sym = (params.get("symbol", [""])[0] or "").upper()
            out = _store.ticker(sym)
            if out is None:
                self._send_error(f"Invalid symbol: {sym}", -1121)
                return
            self._send_json(out)
            return

        if path == "/api/v3/ticker/bookTicker":
            sym = (params.get("symbol", [""])[0] or "").upper()
            out = _store.ticker(sym)
            if out is None:
                self._send_error(f"Invalid symbol: {sym}", -1121)
                return
            self._send_json({
                "symbol": out["symbol"],
                "bidPrice": out["price"],
                "bidQty": "0",
                "askPrice": out["price"],
                "askQty": "0",
            })
            return

        if path == "/api/v3/klines":
            sym = (params.get("symbol", [""])[0] or "").upper()
            if not sym:
                self._send_error("Missing parameter 'symbol'", -1103)
                return
            # Clients send BTCUSDT; our files are named BTCUSD_1M_*.csv,
            # i.e. the MT5-style symbol already carries the USD quote.
            base = sym[:-4] if sym.endswith("USDT") else sym
            base = base[:-3] if base.endswith("USD") else base
            try:
                limit = int(params.get("limit", ["1000"])[0])
            except ValueError:
                limit = 1000
            limit = max(1, min(limit, 1000))
            start_ms = None
            end_ms = None
            if "startTime" in params:
                try:
                    start_ms = int(params["startTime"][0])
                except ValueError:
                    pass
            if "endTime" in params:
                try:
                    end_ms = int(params["endTime"][0])
                except ValueError:
                    pass
            rows = _store.klines(base, start_ms, end_ms, limit)
            if not rows:
                # Binance answers 400 + -1121 for a symbol it does not know.
                self._send_error(f"Invalid symbol: {sym}", -1121)
                return
            # Binance shape: [openTime, open, high, low, close, volume,
            #                 closeTime, quoteVolume, trades, ...]
            out = []
            for r in rows:
                open_t = r[0]
                close_t = open_t + 60_000 - 1
                out.append([
                    open_t,
                    f"{r[1]:.8f}", f"{r[2]:.8f}", f"{r[3]:.8f}", f"{r[4]:.8f}",
                    f"{r[5]:.8f}",
                    close_t,
                    "0",  # quote asset volume (not tracked locally)
                    0,    # number of trades
                    "0",  # taker buy base volume
                    "0",  # taker buy quote volume
                    "0",
                ])
            self._send_json(out)
            return

        self._send_error(f"Invalid endpoint: {path}", -1)


class ApiServer:
    """Manages the HTTP server lifecycle in a background thread."""

    def __init__(self, data_dir=DEFAULT_DATA_DIR, host="127.0.0.1", port=8900):
        global _store
        self.host = host
        self.port = port
        _store = CandleStore(data_dir)
        self.store = _store
        self._httpd = None
        self._thread = None

    @property
    def running(self):
        return self._httpd is not None

    def start(self):
        if self.running:
            return True
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port),
                                              BinanceHandler)
        except OSError:
            return False
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.running:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None


def main():
    ap = argparse.ArgumentParser(description="Binance-compatible local API")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    server = ApiServer(args.data_dir, args.host, args.port)
    if not server.start():
        print(f"Port {args.port} is already in use")
        return
    syms = server.store.available_symbols()
    print(f"Serving {len(syms)} symbols on http://{args.host}:{args.port}")
    print(f"Symbols: {', '.join(syms)}")
    print("Endpoints: /api/v3/ping /api/v3/time /api/v3/exchangeInfo /api/v3/klines")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        server.stop()
        print("stopped")


if __name__ == "__main__":
    main()
