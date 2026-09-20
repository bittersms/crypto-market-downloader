"""Data fetcher module for Crypto Market Data Downloader.

Provides abstract base and concrete implementations for fetching:
1. Cryptocurrency list (rank, symbol, price, market cap)
2. Historical candle data in MetaTrader-compatible format

Sources are modular plugins discovered dynamically.
"""
import requests
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict, Optional



class CryptoListSource(ABC):
    """Abstract base for cryptocurrency list sources."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Source name, must match database entry."""
        pass

    @abstractmethod
    def fetch_list(self, top_n: int, exclude_stablecoins: bool) -> List[Dict]:
        """Fetch cryptocurrency list.
        
        Returns list of dicts: rank, symbol, name, price, market_cap, volume_24h
        """
        pass


class CandleDataSource(ABC):
    """Abstract base for historical candle data sources.

    Attributes:
        page_size: candles returned per API request (drives progress reporting).
        on_progress: optional callback(page, total_pages, candles_so_far)
                     called from the fetch loop so the GUI can show sub-symbol
                     progress during long downloads.
    """

    # Candles returned per request; overridden by subclasses. Used by
    # estimate_pages() to compute how many paginated calls a fetch needs.
    page_size = 100

    def __init__(self):
        # Optional live-progress hook: (page, total_pages, candles_so_far)
        self.on_progress = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Source name, must match database entry."""
        pass

    @abstractmethod
    def fetch_candles(self, symbol: str, timeframe: str, 
                      start_date: datetime, end_date: datetime) -> List[Dict]:
        """Fetch candle data for a symbol.
        
        Returns list of dicts with keys: timestamp, open, high, low, close, volume
        """
        pass

    @abstractmethod
    def get_timeframe_mapping(self) -> Dict[str, str]:
        """Map standard timeframes to source-specific interval strings."""
        pass

    def estimate_pages(self, timeframe: str, start_date: datetime,
                       end_date: datetime) -> int:
        """Estimate number of paginated requests this fetch will need.

        Used to drive live progress reporting for long fetches. Subclasses
        override with their own page size. Default is a conservative guess.
        """
        try:
            tf_map = self.get_timeframe_mapping()
            interval = tf_map.get(timeframe, timeframe)
            secs = self._interval_to_seconds(interval)
            if secs <= 0:
                secs = 60
            total_candles = (end_date - start_date).total_seconds() / secs
            return max(1, int(total_candles / self.page_size) + 1)
        except Exception:
            return 0

    def _interval_to_seconds(self, interval: str) -> int:
        """Convert a source-specific interval string to seconds."""
        multi = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        try:
            num = int(interval[:-1])
            unit = interval[-1]
            return num * multi.get(unit, 60)
        except Exception:
            return 60
