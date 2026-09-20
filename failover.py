"""Failover source handler module for Crypto Market Data Downloader.

Manages the failover mechanism: tries sources in priority order,
falls through to the next source if one fails.
"""
import requests
import time
from datetime import datetime
from database import get_sources, update_source_status
from proxy_manager import get_proxy_for_source
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _build_session():
    """Requests session with automatic retries for transient proxy/SSL errors.

    The local SOCKS5 proxy intermittently drops connections with SSLError /
    "Remote end closed connection". A single un-retried failure makes the
    whole failover chain fall through to a much slower source, so retry a
    few times with backoff before giving up.
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# One shared session — connection pooling keeps repeated pagination fast.
_SESSION = _build_session()


def fetch_with_failover(endpoint_func, source_names=None, settings=None, est_candles=0):
    """Execute a fetch function with failover across sources.
    
    Args:
        endpoint_func: callable that takes (source_config) and returns data or raises exception
        source_names: list of source names to try (None = all enabled sources in priority order)
        settings: app settings dict
    
    Returns:
        data from the first successful source, or None if all fail
    
    Raises:
        Exception if all sources fail
    """
    sources = _get_sources_to_try(source_names, settings, est_candles)
    
    if not sources:
        raise RuntimeError("No enabled data sources available")
    
    errors = []
    for source in sources:
        try:
            data = endpoint_func(source)
            # An empty result is NOT a success — a source that returned no
            # candles for this symbol/range should fail over to the next one.
            if not data:
                msg = f"{source['name']}: returned no data"
                errors.append(msg)
                update_source_status(source["name"], "no data")
                continue
            update_source_status(source["name"], "success")
            return data
        except Exception as e:
            error_msg = f"{source['name']}: {str(e)}"
            errors.append(error_msg)
            update_source_status(source["name"], f"failed: {str(e)[:100]}")
    
    raise RuntimeError(f"All sources failed:\n" + "\n".join(errors))


PAGE_SIZE = {
    "binance": 1000,
    "mexc": 500,
    "kucoin": 100,
    "coingecko": 100,
}

# Above this many candles, prefer the fastest-paging source.
FAST_SOURCE_THRESHOLD = 5000


def _get_sources_to_try(source_names, settings, est_candles=0):
    """Get list of enabled sources to try, in priority order from settings.json.

    For large fetches (est_candles >= FAST_SOURCE_THRESHOLD), sources are
    re-sorted by page size (candles per request) so the fastest source is
    tried first instead of the statically-configured priority. This makes
    long 1m ranges download ~10x faster without changing failover behavior.
    
    Falls back to database order if no settings provided.
    """
    all_sources = get_sources()

    # Merge with settings priority order (if available)
    if settings and "source_priorities" in settings:
        db_map = {s["name"]: s for s in all_sources}
        merged = []
        for sp in settings["source_priorities"]:
            if sp.get("enabled", True) and sp["name"] in db_map:
                # Use settings priority, database enabled/use_proxy state
                src = dict(db_map[sp["name"]])
                src["priority"] = sp["priority"]
                merged.append(src)
        # Add any DB sources not in settings
        settings_names = {sp["name"] for sp in settings.get("source_priorities", [])}
        for s in all_sources:
            if s["name"] not in settings_names:
                merged.append(s)
        merged.sort(key=lambda s: s.get("priority", 999))
        all_sources = merged

    if source_names:
        # Filter to only requested sources
        name_set = set(source_names)
        sources = [s for s in all_sources if s["name"] in name_set and s.get("enabled", True)]
    else:
        sources = [s for s in all_sources if s.get("enabled", True)]

    # Bulk-optimization: for big fetches, prefer the source with the largest
    # page size (fewer round-trips). Falls back to normal priority on ties.
    if settings and settings.get("fast_source_for_bulk", True) and \
            est_candles >= FAST_SOURCE_THRESHOLD:
        sources.sort(key=lambda s: -PAGE_SIZE.get(s["name"], 100))

    return sources


def make_request(url, source_name, params=None, headers=None, timeout=30):
    """Make an HTTP request using appropriate proxy for the source.
    
    Args:
        url: request URL
        source_name: name of the source (for proxy lookup)
        params: query parameters
        headers: request headers
        timeout: request timeout in seconds
    
    Returns:
        requests.Response object
    """
    proxies = get_proxy_for_source(source_name)

    # Retry transient proxy/SSL failures; a proxy reset should not push the
    # whole download onto a 10x slower source.
    last_err = None
    for attempt in range(3):
        try:
            return _SESSION.get(url, params=params, headers=headers,
                                proxies=proxies, timeout=timeout)
        except requests.exceptions.SSLError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
        except requests.exceptions.ConnectionError as e:
            # urllib3 Retry already handled recoverable cases; only
            # hard connection resets reach here — still worth one more try.
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err
