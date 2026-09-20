"""Proxy manager module for Crypto Market Data Downloader.

Handles SOCKS4, SOCKS5, and HTTP proxy configuration with per-source
proxy settings. Uses socks5h:// for remote DNS resolution through proxy.
"""
from config import load_settings


def get_proxy_for_source(source_name):
    """Determine if a source should use proxy and return proxy config.

    Logic:
    1. Read per-source proxy setting from settings.json source_priorities
    2. If not found, fall back to DB use_proxy setting
    3. If source is marked to NOT use proxy, return None (direct connection)
    4. If source is marked to use proxy, use global proxy config

    Args:
        source_name: name of the data source

    Returns:
        dict with proxy configuration or None
    """
    settings = load_settings()
    proxy_type = settings.get("proxy_type", "none")
    proxy_addr = settings.get("default_proxy", "")

    if not proxy_addr or proxy_type == "none":
        return None

    # Check source-specific proxy setting in settings.json
    sources = settings.get("source_priorities", [])
    for source in sources:
        if source["name"] == source_name:
            if "proxy" in source:
                if source["proxy"]:
                    return _build_proxy_config(proxy_type, proxy_addr)
                else:
                    return None  # Source uses direct connection
            break  # Found but no explicit setting, fall through to global

    # Fallback: check DB use_proxy setting
    try:
        from database import get_sources
        for s in get_sources():
            if s["name"] == source_name:
                if not s.get("use_proxy", False):
                    return None
                break
    except Exception:
        pass

    # Default: use global proxy if configured
    return _build_proxy_config(proxy_type, proxy_addr)


def _build_proxy_config(proxy_type, proxy_addr):
    """Build proxy configuration dict from settings."""
    if not proxy_addr or proxy_type == "none":
        return None

    if proxy_type == "http":
        return {"http": proxy_addr, "https": proxy_addr}
    elif proxy_type == "socks5":
        # socks5h:// for remote DNS resolution through the proxy
        return {
            "http": f"socks5h://{proxy_addr}",
            "https": f"socks5h://{proxy_addr}",
        }
    elif proxy_type == "socks4":
        return {
            "http": f"socks4://{proxy_addr}",
            "https": f"socks4://{proxy_addr}",
        }
    return None


def test_proxy_connection(proxy_type, proxy_addr):
    """Test if a proxy connection works.

    Returns:
        tuple (success: bool, message: str)
    """
    import requests

    if not proxy_addr or proxy_type == "none":
        return False, "No proxy address specified"

    proxies = _build_proxy_config(proxy_type, proxy_addr)
    if not proxies:
        return False, f"Unknown proxy type: {proxy_type}"

    # Test against a few endpoints
    test_urls = [
        "https://api.coingecko.com/api/v3/ping",
        "https://api.mexc.com/api/v3/time",
        "https://api.kucoin.com/api/v1/time",
    ]

    for url in test_urls:
        try:
            resp = requests.get(url, proxies=proxies, timeout=15)
            if resp.status_code == 200:
                host = url.split("/")[2]
                return True, f"Proxy connection successful (tested via {host})"
        except Exception:
            continue

    return False, f"Proxy connection failed (tried {len(test_urls)} endpoints)"
