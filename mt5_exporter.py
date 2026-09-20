"""MetaTrader 5 format exporter module.

Exports candle data in MT5-compatible CSV and TXT formats.
Supports all standard timeframes (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 1w).
"""
from datetime import datetime
from pathlib import Path
from typing import List, Dict


def export_to_mt5_format(candles: List[Dict], symbol: str, timeframe: str,
                         output_path: str, format_type: str = "csv"):
    """Export candle data to MetaTrader 5 compatible format.

    MT5 CSV format (tab-separated, with header):
    DATE\tTIME\tOPEN\tHIGH\tLOW\tCLOSE\tVOL
    2026.09.14\t17:00\t77649.000000\t77810.000000\t77475.000000\t77810.000000\t15.5

    Args:
        candles: list of candle dicts with keys: timestamp, open, high, low, close, volume
        symbol: trading symbol (e.g. "BTC")
        timeframe: timeframe string (e.g. "1m", "1h", "1d")
        output_path: file path to write
        format_type: "csv" or "txt"
    """
    if not candles:
        raise ValueError("No candle data to export")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Both csv and txt use the same MT5-compatible tab-separated format
    with open(output_path, "w", newline="") as f:
        # Write header
        f.write("DATE\tTIME\tOPEN\tHIGH\tLOW\tCLOSE\tVOL\n")

        # Write data rows
        for candle in candles:
            timestamp = candle["timestamp"]
            if isinstance(timestamp, datetime):
                date_str = timestamp.strftime("%Y.%m.%d")
                time_str = timestamp.strftime("%H:%M")
            else:
                dt = datetime.fromtimestamp(timestamp)
                date_str = dt.strftime("%Y.%m.%d")
                time_str = dt.strftime("%H:%M")

            f.write(f"{date_str}\t{time_str}\t{candle['open']:.6f}\t{candle['high']:.6f}\t{candle['low']:.6f}\t{candle['close']:.6f}\t{candle['volume']:.6f}\n")

    return str(output_path)


def get_mt5_filename(symbol: str, timeframe: str, start_date: datetime,
                     end_date: datetime, format_type: str = "csv",
                     source_name: str = None) -> str:
    """Generate a standard MT5-compatible filename.

    Format: SYMBOL_TIMEFRAME_SOURCE.csv   (e.g. BTC_1M_BINANCE.csv)

    The date range is intentionally omitted, but the exchange IS included:
    data from different venues must never be merged into one file, since
    candles from one exchange cannot extend another exchange's history.
    """
    ext = "csv" if format_type == "csv" else "txt"
    # Normalize symbol for MetaTrader: add USD suffix
    if not symbol.endswith("USD"):
        mt_symbol = f"{symbol}USD"
    else:
        mt_symbol = symbol
    suffix = f"_{source_name.upper()}" if source_name else ""
    return f"{mt_symbol}_{timeframe.upper()}{suffix}.{ext}"


def read_mt5_file(path: str) -> List[Dict]:
    """Read an existing MT5 CSV back into candle dicts.

    Used for incremental downloads: we read what we already have so we can
    fetch only the missing part of the range.

    Returns [] if the file is missing/unreadable (a fresh full download then
    happens automatically).
    """
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return []

    for line in lines[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y.%m.%d %H:%M")
            rows.append({
                "timestamp": dt,
                "open": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "close": float(parts[5]),
                "volume": float(parts[6]),
            })
        except (ValueError, IndexError):
            continue
    return rows


def merge_and_export(existing: List[Dict], new_candles: List[Dict],
                     output_path: str, format_type: str = "csv") -> int:
    """Merge existing + new candles and write the combined file.

    Deduplicates by timestamp and sorts ascending, so overlapping fetches
    (partial page retries, day-boundary duplicates) collapse cleanly.

    Returns the number of rows written.
    """
    merged = {c["timestamp"]: c for c in existing}
    for c in new_candles:
        merged[c["timestamp"]] = c
    all_candles = sorted(merged.values(), key=lambda c: c["timestamp"])
    if not all_candles:
        raise ValueError("No candle data to export")
    export_to_mt5_format(all_candles, "", "", output_path, format_type=format_type)
    return len(all_candles)


