"""
Crypto Swarm Intelligence System - API Clients with Rate Limiting & Caching
"""
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from utils import get_logger, safe_float

log = get_logger("api_client")


class APIClient:
    """Base API client with rate limiting, retry logic, and response caching."""

    def __init__(self, base_url: str, delay: float, name: str = "api"):
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.name = name
        self._last_request = 0.0
        self._request_count = 0
        self._cache = {}  # url -> (timestamp, data)
        self._cache_ttl = config.GECKO_CACHE_TTL
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_BASE,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "CryptoSwarm/2.0",
        })
        return session

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            sleep_time = self.delay - elapsed
            time.sleep(sleep_time)
        self._last_request = time.time()

    def _get_cache_key(self, url: str, params: dict = None) -> str:
        param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        return f"{url}?{param_str}"

    def _get_cached(self, cache_key: str):
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                return data
            del self._cache[cache_key]
        return None

    def get(self, endpoint: str, params: dict = None, use_cache: bool = True) -> dict | list | None:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        cache_key = self._get_cache_key(url, params)

        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        self._rate_limit()
        self._request_count += 1
        try:
            resp = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 429:
                # Adaptive: double delay on rate limit hit
                wait = self.delay * 2
                log.warning(f"[{self.name}] Rate limited, waiting {wait:.1f}s...")
                time.sleep(wait)
                resp = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if use_cache:
                self._cache[cache_key] = (time.time(), data)
            return data
        except requests.exceptions.RequestException as e:
            log.warning(f"[{self.name}] Request failed: {url} - {e}")
            return None

    @property
    def request_count(self):
        return self._request_count


class DexScreenerClient(APIClient):
    """DexScreener API - 300 req/min. Best for pair/token data."""

    def __init__(self):
        super().__init__(config.DEXSCREENER_BASE, config.DEXSCREENER_DELAY, "dexscreener")

    def search_pairs(self, query: str) -> list:
        """Search for pairs matching a query string."""
        data = self.get("/latest/dex/search", params={"q": query})
        if data and "pairs" in data:
            return data["pairs"] or []
        return []

    def get_token_pairs(self, chain: str, token_address: str) -> list:
        """Get all pairs for a specific token on a chain."""
        data = self.get(f"/tokens/v1/{chain}/{token_address}")
        if isinstance(data, list):
            return data
        return []

    def get_pairs(self, chain: str, pair_addresses: list[str]) -> list:
        """Get pair data by chain and pair addresses (up to 30)."""
        addresses = ",".join(pair_addresses[:30])
        data = self.get(f"/pairs/v1/{chain}/{addresses}")
        if isinstance(data, list):
            return data
        return []

    def get_tokens_batch(self, chain: str, token_addresses: list[str]) -> list:
        """Get token pairs by chain and token addresses (comma-separated)."""
        addresses = ",".join(token_addresses[:30])
        data = self.get(f"/tokens/v1/{chain}/{addresses}")
        if isinstance(data, list):
            return data
        return []

    def get_boosted_tokens(self) -> list:
        """Get tokens with active boosts (promoted)."""
        data = self.get("/token-boosts/latest/v1")
        if isinstance(data, list):
            return data
        return []

    def get_token_profiles(self) -> list:
        """Get latest token profiles."""
        data = self.get("/token-profiles/latest/v1")
        if isinstance(data, list):
            return data
        return []


class GeckoTerminalClient(APIClient):
    """GeckoTerminal API - 10 req/min. Best for discovering new pools."""

    def __init__(self):
        super().__init__(config.GECKOTERMINAL_BASE, config.GECKOTERMINAL_DELAY, "geckoterminal")

    def _extract_pools(self, data: dict | None) -> list:
        if data and "data" in data:
            return data["data"] if isinstance(data["data"], list) else [data["data"]]
        return []

    def get_new_pools(self, network: str, page: int = 1) -> list:
        """Get newly created pools for a network."""
        data = self.get(f"/networks/{network}/new_pools", params={"page": page})
        return self._extract_pools(data)

    def get_new_pools_paginated(self, network: str, pages: int = 5) -> list:
        """Get new pools across multiple pages for broader coverage."""
        all_pools = []
        for page in range(1, pages + 1):
            pools = self.get_new_pools(network, page=page)
            if not pools:
                break
            all_pools.extend(pools)
            log.info(f"  [gecko] {network} new_pools page {page}: {len(pools)} pools")
        return all_pools

    def get_trending_pools(self, network: str, page: int = 1) -> list:
        """Get trending pools for a network."""
        data = self.get(f"/networks/{network}/trending_pools", params={"page": page})
        return self._extract_pools(data)

    def get_trending_pools_paginated(self, network: str, pages: int = 3) -> list:
        """Get trending pools across multiple pages."""
        all_pools = []
        for page in range(1, pages + 1):
            pools = self.get_trending_pools(network, page=page)
            if not pools:
                break
            all_pools.extend(pools)
        return all_pools

    def get_pool(self, network: str, pool_address: str) -> dict | None:
        """Get detailed pool info."""
        data = self.get(f"/networks/{network}/pools/{pool_address}")
        if data and "data" in data:
            return data["data"]
        return None

    def get_pool_ohlcv(self, network: str, pool_address: str,
                       timeframe: str = "hour", aggregate: int = 1,
                       limit: int = 168) -> list:
        """Get OHLCV candles for a pool.

        timeframe: 'minute', 'hour', 'day'
        aggregate: candle size multiplier
        limit: number of candles (max ~1000)
        """
        data = self.get(
            f"/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}",
            params={"aggregate": aggregate, "limit": limit, "currency": "usd"},
        )
        if data and "data" in data and "attributes" in data["data"]:
            return data["data"]["attributes"].get("ohlcv_list", [])
        return []

    def get_pool_trades(self, network: str, pool_address: str,
                        trade_volume_in_usd_greater_than: float = 0) -> list:
        """Get recent trades for a pool."""
        data = self.get(
            f"/networks/{network}/pools/{pool_address}/trades",
            params={"trade_volume_in_usd_greater_than": trade_volume_in_usd_greater_than},
        )
        if data and "data" in data:
            return data["data"] if isinstance(data["data"], list) else []
        return []


class CoinGeckoClient(APIClient):
    """CoinGecko API - 30 req/min, 10k/month. Trending tokens."""

    def __init__(self):
        super().__init__(config.COINGECKO_BASE, config.COINGECKO_DELAY, "coingecko")

    def get_trending(self) -> list:
        """Get trending coins."""
        data = self.get("/search/trending")
        if data and "coins" in data:
            return [c.get("item", c) for c in data["coins"]]
        return []

    def get_new_listings(self) -> list:
        """Get recently added coins, sorted by newest."""
        data = self.get("/coins/list", params={"include_platform": "true"})
        if isinstance(data, list):
            # CoinGecko doesn't sort by date, but newest tend to be at the end
            return data[-50:]  # last 50 are likely newest
        return []

    def get_coin_data(self, coin_id: str) -> dict | None:
        """Get coin detail including market data."""
        return self.get(
            f"/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "true",
                "developer_data": "false",
            },
        )
