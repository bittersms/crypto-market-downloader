"""Configuration module for Crypto Market Data Downloader.

Manages application settings, source priorities, proxy configuration,
and persistent state via SQLite database.
"""
import json
import os
from pathlib import Path

# Base paths
APP_DIR = Path(os.environ.get("CMD_APP_DIR", os.path.expanduser("~/.crypto_market_downloader")))
DB_PATH = APP_DIR / "config.db"
CONFIG_FILE = APP_DIR / "settings.json"

# Ensure app directory exists
APP_DIR.mkdir(parents=True, exist_ok=True)

# Default settings
DEFAULT_SETTINGS = {
    "top_n": 100,  # Number of top cryptocurrencies to fetch
    "exclude_stablecoins": True,  # Whether to exclude stablecoins
    "default_proxy": "127.0.0.1:10808",
    "proxy_type": "socks5",  # none, http, socks4, socks5
    "source_priorities": [
        {"name": "kucoin", "type": "api", "priority": 1, "enabled": True, "proxy": False},
        {"name": "mexc", "type": "api", "priority": 2, "enabled": True, "proxy": False},
        {"name": "binance", "type": "api", "priority": 3, "enabled": True, "proxy": True},
        {"name": "coingecko", "type": "api", "priority": 4, "enabled": True, "proxy": False},
    ],
    "candle_timeframes": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"],
    "max_workers": 10,
    "request_timeout": 30,
    "retry_attempts": 3,
    "retry_delay": 1,
    "mt5_export_format": "csv",  # csv or txt
    "show_progress": True,
    # Crypto-list source. Empty/"Auto (failover)" = failover across enabled
    # sources (historical behaviour). A venue name fetches the list from that
    # source only, so the coin list is actually switchable in the GUI.
    "crypto_list_source": "",
}


def load_settings():
    """Load settings from file or create defaults."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                settings = json.load(f)
            # Merge with defaults for any missing keys
            merged = DEFAULT_SETTINGS.copy()
            merged.update(settings)
            return merged
        except (json.JSONDecodeError, IOError):
            pass
    # Save defaults
    save_settings(DEFAULT_SETTINGS.copy())
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(settings, f, indent=2)
