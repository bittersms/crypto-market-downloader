# Crypto Market Data Downloader

A fast, modular cryptocurrency market data downloader with a full GUI, designed for MetaTrader M1+ candle data export.

## Features

1. **Cryptocurrency List Fetching** - Get top N cryptos by market cap (rank 1+), excluding stablecoins
2. **Favorite Lists** - Save interesting cryptos into named custom lists
3. **Historical Data Download** - Download complete market data with failover across sources
4. **Pluggable Sources** - Editable source list with priority ordering and per-source proxy settings
5. **Proxy Support** - SOCKS4/SOCKS5/HTTP proxy, configurable per source
6. **Modular Architecture** - Source plugins are auto-discovered; add new ones easily
7. **MT5-Compatible Export** - CSV and TXT formats for MetaTrader 5
8. **Candle Resampling** - Resample 1m data to any timeframe (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 1w)
9. **Fast Downloads** - Parallel fetching with progress bar

## Project Structure

```
crypto_market_downloader/
├── launcher.py          # Entry point
├── app.py               # Main GUI application
├── gui.py               # Theme colors
├── widgets.py           # Themed UI components
├── config.py            # Settings management
├── database.py          # SQLite storage
├── proxy_manager.py     # Proxy configuration
├── failover.py          # Failover logic across sources
├── source_registry.py   # Source registry and batch fetching
├── data_fetcher.py      # Abstract base classes for sources
├── mt5_exporter.py      # MetaTrader 5 format exporter
├── progress.py          # Terminal progress bar
└── sources/             # Source plugins
    ├── __init__.py
    ├── coingecko.py     # CoinGecko API (list + candles)
    ├── binance.py       # Binance API (list + candles, proxy)
    ├── mexc.py          # MEXC API (list + candles)
    └── kucoin.py        # KuCoin API (list + candles)
```

## Usage

```bash
cd ~/AppData/Local/hermes/crypto_market_downloader
python launcher.py
```

## Adding a New Source

1. Create a new file in `sources/` (e.g., `myexchange.py`)
2. Implement `CryptoListSource` and/or `CandleSource` classes
3. Register the source in `source_registry.py`:
   - Add to `LIST_SOURCES` dict for list fetching
   - Add to `CANDLE_SOURCES` dict for candle data
4. Add to the database sources table via the Sources tab

## Requirements

- Python 3.11+
- tkinter
- requests
- PySocks (for SOCKS proxy support)

## License

MIT
