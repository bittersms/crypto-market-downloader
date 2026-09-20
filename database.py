"""Database module for Crypto Market Data Downloader.

Handles all SQLite operations: crypto list caching, favorite lists,
downloaded data storage, and source management.
"""
import sqlite3
from datetime import datetime

from config import DB_PATH


def get_connection():
    """Get a SQLite connection with row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Initialize database tables."""
    conn = get_connection()
    c = conn.cursor()

    # Cached cryptocurrency list (from sources)
    c.execute("""
        CREATE TABLE IF NOT EXISTS crypto_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rank INTEGER,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL,
            market_cap REAL,
            volume_24h REAL,
            source TEXT,
            last_updated TEXT,
            UNIQUE(symbol)
        )
    """)

    # Favorite lists (user-defined collections)
    c.execute("""
        CREATE TABLE IF NOT EXISTS favorite_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Items in favorite lists
    c.execute("""
        CREATE TABLE IF NOT EXISTS favorite_list_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER REFERENCES favorite_lists(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(list_id, symbol)
        )
    """)

    # Downloaded market data metadata
    c.execute("""
        CREATE TABLE IF NOT EXISTS downloaded_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER REFERENCES favorite_lists(id),
            list_name TEXT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            file_path TEXT,
            downloaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Data sources configuration
    c.execute("""
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            priority INTEGER DEFAULT 999,
            enabled INTEGER DEFAULT 1,
            use_proxy INTEGER DEFAULT 0,
            api_key TEXT,
            base_url TEXT,
            last_tested TEXT,
            last_status TEXT
        )
    """)

    # Stablecoin symbols to exclude
    c.execute("""
        CREATE TABLE IF NOT EXISTS stablecoins (
            symbol TEXT PRIMARY KEY
        )
    """)

    # Insert default stablecoins
    default_stablecoins = [
        "USDT", "USDC", "BUSD", "DAI", "UST", "BTCB", "TUSD", "USDP",
        "USDD", "USDP", "LUSD", "FRAX", "FEI", "ALUSD", "SUSD", "MIM",
        "NUSDC", "XUSD", "EURT", "TRXD", "TUSD", "PAX", "USTC", "RSERVE",
        "GUSD", "USDP", "BUSD", "USDT", "MATIC", "WBTC", "WETH",
    ]
    for sc in default_stablecoins:
        c.execute("INSERT OR IGNORE INTO stablecoins (symbol) VALUES (?)", (sc,))

    # Seed data sources from the source registry — the registry (LIST_SOURCES
    # in source_registry.py) is the single source of truth for which venues
    # exist. A hardcoded list here used to seed only 4 sources, so on a fresh
    # machine (no config.db) the Data Sources tab and every source dropdown
    # showed only kucoin/mexc/binance/coingecko.
    #
    # Imported lazily: source_registry imports failover, which imports this
    # module, so a top-level import here would be circular.
    try:
        from source_registry import LIST_SOURCES, SOURCE_SEED_DEFAULTS
    except ImportError:
        LIST_SOURCES, SOURCE_SEED_DEFAULTS = {}, {}
    c.execute("SELECT COUNT(*) as cnt FROM data_sources")
    if c.fetchone()[0] == 0:
        for prio, name in enumerate(LIST_SOURCES, start=1):
            d = SOURCE_SEED_DEFAULTS.get(name, {})
            c.execute("""
                INSERT INTO data_sources (name, type, priority, enabled, use_proxy, base_url)
                VALUES (?, 'api', ?, ?, ?, ?)
            """, (name, prio, d.get("enabled", 1), d.get("use_proxy", 0), d.get("base_url", "")))

    conn.commit()
    conn.close()


def save_crypto_list(cryptos):
    """Save or update the crypto list in the database.
    
    Args:
        cryptos: list of dicts with keys: rank, symbol, name, price, market_cap, 
                 volume_24h, source
    """
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    
    for crypto in cryptos:
        c.execute("""
            INSERT INTO crypto_list (rank, symbol, name, price, market_cap, 
                volume_24h, source, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                rank = excluded.rank,
                name = excluded.name,
                price = excluded.price,
                market_cap = excluded.market_cap,
                volume_24h = excluded.volume_24h,
                source = excluded.source,
                last_updated = excluded.last_updated
        """, (
            crypto["rank"], crypto["symbol"], crypto["name"],
            crypto["price"], crypto["market_cap"], crypto["volume_24h"],
            crypto.get("source", "unknown"), now
        ))
    
    conn.commit()
    conn.close()


def get_crypto_list(exclude_stablecoins=True, limit=None):
    """Retrieve cached crypto list from database.
    
    Args:
        exclude_stablecoins: whether to exclude stablecoins
        limit: maximum number of records to return
    
    Returns:
        list of dicts with crypto data
    """
    conn = get_connection()
    c = conn.cursor()
    
    query = "SELECT rank, symbol, name, price, market_cap, volume_24h, source, last_updated FROM crypto_list"
    params = []
    
    if exclude_stablecoins:
        query += " WHERE symbol NOT IN (SELECT symbol FROM stablecoins)"
    
    query += " ORDER BY rank ASC"
    if limit:
        query += f" LIMIT ?"
        params.append(limit)
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def create_favorite_list(name):
    """Create a new favorite list. Returns the list ID.
    
    If a list with this name already exists, returns the existing list ID
    (so callers can add symbols to it without a UNIQUE constraint error).
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM favorite_lists WHERE name = ?", (name,))
    row = c.fetchone()
    if row:
        list_id = row[0]
    else:
        c.execute("INSERT INTO favorite_lists (name) VALUES (?)", (name,))
        list_id = c.lastrowid
        conn.commit()
    conn.close()
    return list_id


def get_favorite_lists():
    """Get all favorite lists."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT fl.id, fl.name, fl.created_at, COUNT(fl_items.symbol) as item_count
        FROM favorite_lists fl
        LEFT JOIN favorite_list_items fl_items ON fl.id = fl_items.list_id
        GROUP BY fl.id
        ORDER BY fl.name
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_favorite_list(list_id):
    """Get items in a favorite list."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT fl_items.id, fl_items.symbol, fl_items.added_at, 
               cl.name, cl.price, cl.market_cap
        FROM favorite_list_items fl_items
        LEFT JOIN crypto_list cl ON fl_items.symbol = cl.symbol
        WHERE fl_items.list_id = ?
        ORDER BY fl_items.added_at DESC
    """, (list_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def add_to_favorite_list(list_id, symbol):
    """Add a symbol to a favorite list."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO favorite_list_items (list_id, symbol) VALUES (?, ?)",
              (list_id, symbol))
    conn.commit()
    conn.close()


def remove_from_favorite_list(list_id, symbol):
    """Remove a symbol from a favorite list."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM favorite_list_items WHERE list_id = ? AND symbol = ?",
              (list_id, symbol))
    conn.commit()
    conn.close()


def delete_favorite_list(list_id):
    """Delete a favorite list and all its items."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM favorite_lists WHERE id = ?", (list_id,))
    conn.commit()
    conn.close()


def save_downloaded_data(list_name, symbol, timeframe, start_date, end_date, 
                         source, file_path):
    """Record a downloaded dataset in the database."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO downloaded_data (list_name, symbol, timeframe, start_date,
            end_date, source, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (list_name, symbol, timeframe, start_date, end_date, source, str(file_path)))
    conn.commit()
    conn.close()


def get_sources():
    """Get all data sources sorted by priority."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, name, type, priority, enabled, use_proxy, api_key, base_url,
               last_tested, last_status
        FROM data_sources
        ORDER BY priority ASC
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def save_sources(sources):
    """Save data sources to database."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM data_sources")
    for s in sources:
        c.execute("""
            INSERT INTO data_sources (name, type, priority, enabled, use_proxy, 
                api_key, base_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            s["name"], s["type"], s["priority"], s.get("enabled", True),
            s.get("use_proxy", False), s.get("api_key", ""), s.get("base_url", "")
        ))
    conn.commit()
    conn.close()


def update_source(source_name, type_=None, priority=None, enabled=None,
                  use_proxy=None, api_key=None, base_url=None):
    """Update a single data source's fields in the database.
    Only updates fields that are not None."""
    conn = get_connection()
    c = conn.cursor()
    updates = []
    params = []
    if type_ is not None:
        updates.append("type = ?")
        params.append(type_)
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(int(bool(enabled)))
    if use_proxy is not None:
        updates.append("use_proxy = ?")
        params.append(int(bool(use_proxy)))
    if api_key is not None:
        updates.append("api_key = ?")
        params.append(api_key)
    if base_url is not None:
        updates.append("base_url = ?")
        params.append(base_url)
    if updates:
        params.append(source_name)
        c.execute(f"UPDATE data_sources SET {', '.join(updates)} WHERE name = ?", params)
        conn.commit()
    conn.close()


def update_source_status(source_name, status):
    """Update the last test status of a source."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE data_sources 
        SET last_tested = ?, last_status = ?
        WHERE name = ?
    """, (datetime.now().isoformat(), status, source_name))
    conn.commit()
    conn.close()


def get_stablecoins():
    """Get the list of stablecoin symbols."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT symbol FROM stablecoins")
    rows = c.fetchall()
    conn.close()
    return [row["symbol"] for row in rows]


# Initialize database on module load
init_db()
