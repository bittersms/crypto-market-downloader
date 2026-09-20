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
    # Populated at boot from SOURCE_SEED_DEFAULTS in source_registry.py so the
    # venues available here and in the Data Sources table can never drift
    # apart. Used by failover/proxy_manager on the first run, before the user
    # toggles anything (which rewrites it via _sync_settings_after_toggle).
    "source_priorities": [],
}


def _seed_source_priorities(settings):
    """Make sure source_priorities covers every registry source.

    On a fresh machine this list used to be a hardcoded 4-entry literal, so
    failover/proxy_manager only ever saw kucoin/mexc/binance/coingecko. Build
    it from SOURCE_SEED_DEFAULTS (the same table the Data Sources tab seeds
    the DB from) so the two can never drift apart. Preserves an existing
    user's toggles; only fills in sources that are missing.
    """
    try:
        from source_registry import LIST_SOURCES, SOURCE_SEED_DEFAULTS
    except Exception:
        return settings

    known = {sp.get("name") for sp in settings.get("source_priorities", [])}
    if known == set(LIST_SOURCES):
        return settings

    # Keep existing entries untouched, append the missing ones in registry order.
    updated = [dict(sp) for sp in settings.get("source_priorities", [])]
    for prio, name in enumerate(LIST_SOURCES, start=1):
        if name in known:
            continue
        d = SOURCE_SEED_DEFAULTS.get(name, {})
        updated.append({
            "name": name, "type": "api", "priority": prio,
            "enabled": bool(d.get("enabled", 1)),
            "proxy": bool(d.get("use_proxy", 0)),
        })
    settings["source_priorities"] = updated
    return settings


def load_settings():
    """Load settings from file or create defaults."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                settings = json.load(f)
            # Merge with defaults for any missing keys
            merged = DEFAULT_SETTINGS.copy()
            merged.update(settings)
            return _seed_source_priorities(merged)
        except (json.JSONDecodeError, IOError):
            pass
    # Save defaults
    defaults = _seed_source_priorities(DEFAULT_SETTINGS.copy())
    save_settings(defaults)
    return defaults


def save_settings(settings):
    """Save settings to file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(settings, f, indent=2)
