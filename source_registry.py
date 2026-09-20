"""Source registry and factory for Crypto Market Data Downloader.

Dynamically discovers and loads source implementations, provides
failover orchestration for fetching data.
"""
import importlib
from datetime import datetime
from typing import List, Dict

from failover import fetch_with_failover

# Approximate minutes per candle. Kept for the capability lookup below; the
# old bulk-optimization that consumed it was part of failover selection,
# which no longer applies since downloads use one user-chosen exchange.
TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080,
}


# History depth per source, measured by the live 3-day 1m smoke test.
# "full"  = the venue served the whole requested range with no gaps;
# a number = a hard API ceiling (own rows) beyond which it only ever
# returns the newest N candles regardless of the requested window.
# These limits are properties of each exchange's API, not of this app —
# shown in the Sources tab instead of the now-meaningless priority column.
HISTORY_LIMIT = {
    "binance": "full",
    "bitfinex": "full",
    "bitstamp": "full",
    "bybit": "full",
    "gate": "full",
    "kucoin": "full",
    "mexc": "full",
    "okx": "full",
    "xt": "full",
    "lbank": "full",
    "hyperliquid": "5m: full / 1m: ~3.5d",
    "bingx": "newest 1000",
    "digifinex": "newest 500",
    "kraken": "newest 720",
    "htx": "newest 1000",
    "coinbase": "newest 300",
    "coingecko": "4h only",
}


def get_source_capability(source_name):
    """Return (history_limit, page_size) for a source, both as strings.

    Used by the Sources tab to show what each venue can actually deliver
    instead of a priority number that no longer drives any selection.
    """
    limit = HISTORY_LIMIT.get(source_name, "unknown")
    try:
        inst = get_candle_source_instance(source_name)
        page = getattr(inst, "page_size", None) or getattr(inst, "PAGE_SIZE", None)
    except Exception:
        page = None
    return limit, (page if page else "?")


# Available source modules — maps source name to module path
LIST_SOURCES = {
    "coingecko": "sources.coingecko",
    "binance": "sources.binance",
    "mexc": "sources.mexc",
    "kucoin": "sources.kucoin",
    "gate": "sources.gate",
    "okx": "sources.okx",
    "bybit": "sources.binance_like_extras",
    "bingx": "sources.binance_like_extras",
    "xt": "sources.binance_like_extras",
    "digifinex": "sources.binance_like_extras",
    "kraken": "sources.native_exchanges",
    "bitfinex": "sources.native_exchanges",
    "coinbase": "sources.native_exchanges",
    "bitstamp": "sources.native_exchanges",
    "htx": "sources.native_exchanges",
    "lbank": "sources.lbank",
    "hyperliquid": "sources.hyperliquid",
}

CANDLE_SOURCES = dict(LIST_SOURCES)

# Concrete class names per source
LIST_CLASS_NAMES = {
    "coingecko": "CoingeckoListSource",
    "binance": "BinanceListSource",
    "mexc": "MexcListSource",
    "kucoin": "KuCoinListSource",
    "gate": "GateListSource",
    "okx": "OkxListSource",
    "bybit": "_BybitListSource",
    "bingx": "_BingxListSource",
    "xt": "_XtListSource",
    "digifinex": "_DigifinexListSource",
    "kraken": "KrakenListSource",
    "bitfinex": "BitfinexListSource",
    "coinbase": "CoinbaseListSource",
    "bitstamp": "BitstampListSource",
    "htx": "HtxListSource",
    "lbank": "_LbankListSource",
    "hyperliquid": "HyperliquidListSource",
}

CANDLE_CLASS_NAMES = {
    "coingecko": "CoingeckoCandleSource",
    "binance": "BinanceCandleSource",
    "mexc": "MexcCandleSource",
    "kucoin": "KuCoinCandleSource",
    "gate": "GateCandleSource",
    "okx": "OkxCandleSource",
    "bybit": "_BybitCandleSource",
    "bingx": "_BingxCandleSource",
    "xt": "_XtCandleSource",
    "digifinex": "_DigifinexCandleSource",
    "kraken": "KrakenCandleSource",
    "bitfinex": "BitfinexCandleSource",
    "coinbase": "CoinbaseCandleSource",
    "bitstamp": "BitstampCandleSource",
    "htx": "HtxCandleSource",
    "lbank": "_LbankCandleSource",
    "hyperliquid": "HyperliquidCandleSource",
}


def load_source_class(module_name, class_name):
    """Dynamically load a source class from a module."""
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def get_list_source_instance(source_name):
    """Load and instantiate a list source by name."""
    module_name = LIST_SOURCES.get(source_name)
    class_name = LIST_CLASS_NAMES.get(source_name)
    if not module_name or not class_name:
        raise ValueError(f"Unknown source: {source_name}")
    cls = load_source_class(module_name, class_name)
    return cls()


def get_candle_source_instance(source_name):
    """Load and instantiate a candle source by name."""
    module_name = CANDLE_SOURCES.get(source_name)
    class_name = CANDLE_CLASS_NAMES.get(source_name)
    if not module_name or not class_name:
        raise ValueError(f"Unknown source: {source_name}")
    cls = load_source_class(module_name, class_name)
    return cls()


def get_crypto_list(top_n: int, exclude_stablecoins: bool = True, settings=None) -> List[Dict]:
    """Fetch cryptocurrency list with failover across list sources."""
    def try_source(source_config):
        source = get_list_source_instance(source_config["name"])
        return source.fetch_list(top_n, exclude_stablecoins)

    try:
        data = fetch_with_failover(try_source, settings=settings)
        from database import save_crypto_list
        save_crypto_list(data)
        return data
    except Exception as e:
        raise RuntimeError(f"Failed to fetch crypto list: {e}")


def get_candles_from_source(source_name: str, symbol: str, timeframe: str,
                            start_date: datetime, end_date: datetime,
                            on_progress=None) -> List[Dict]:
    """Fetch candles from ONE specific exchange — no failover.

    Incremental downloads must keep exchanges separate: one venue's history
    cannot extend another's, so the user picks a single exchange in the
    download tab and only that venue is queried and written to its own file.
    """
    source = get_candle_source_instance(source_name)
    if on_progress and hasattr(source, "on_progress"):
        source.on_progress = on_progress
    return source.fetch_candles(symbol, timeframe, start_date, end_date)
