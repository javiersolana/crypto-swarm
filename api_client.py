"""
Crypto Swarm Intelligence System - API Clients with Rate Limiting & Caching

v3.0: Exponential backoff with jitter, thread-safe rate limiting, adaptive delays.
"""
import random
import threading
import time
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from utils import get_logger, safe_float

log = get_logger("api_client")


class APIClient:
    """Base API client with rate limiting, retry logic, and response caching.

    v4.0: Per-host rate limiting via class-level registry. All APIClient instances
    sharing the same host (e.g. two DexScreenerClient()) share ONE throttle state,
    preventing collective rate limit violations.
    """

    # Class-level registry: host -> {"lock", "last_request", "delay", "base_delay", "consecutive_429s"}
    _host_registry = {}
    _registry_lock = threading.Lock()

    @classmethod
    def _get_host_throttle(cls, host: str) -> dict:
        """Get or create the shared throttle state for a host."""
        with cls._registry_lock:
            if host not in cls._host_registry:
                rate_limits = getattr(config, 'HOST_RATE_LIMITS', {})
                rate = rate_limits.get(host, rate_limits.get("_default", 60))
                base_delay = 60.0 / rate
                cls._host_registry[host] = {
                    "lock": threading.Lock(),
                    "last_request": 0.0,
                    "delay": base_delay,
                    "base_delay": base_delay,
                    "consecutive_429s": 0,
                }
            return cls._host_registry[host]

    def __init__(self, base_url: str, delay: float, name: str = "api"):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self._request_count = 0
        self._cache = {}  # url -> (timestamp, data)  — per-instance (correct)
        self._cache_ttl = config.GECKO_CACHE_TTL
        self._cache_lock = threading.Lock()  # per-instance cache lock

        # Register with shared host throttle
        self._host = urllib.parse.urlparse(self.base_url).hostname or "unknown"
        throttle = self._get_host_throttle(self._host)
        # If caller-provided delay is more conservative than config, respect it
        if delay > throttle["base_delay"]:
            with throttle["lock"]:
                throttle["delay"] = delay
                throttle["base_delay"] = delay

        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        # Don't let urllib3 retry 429s - we handle those ourselves with smarter backoff
        retry = Retry(
            total=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_BASE,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "CryptoSwarm/3.0",
        })
        return session

    def _rate_limit(self):
        throttle = self._host_registry[self._host]
        with throttle["lock"]:
            elapsed = time.time() - throttle["last_request"]
            if elapsed < throttle["delay"]:
                sleep_time = throttle["delay"] - elapsed
                time.sleep(sleep_time)
            throttle["last_request"] = time.time()

    def _backoff_429(self, attempt: int):
        """Exponential backoff with jitter for 429 rate limit errors."""
        throttle = self._host_registry[self._host]
        base_wait = throttle["delay"] * (2 ** attempt)
        jitter = random.uniform(0, base_wait * 0.3)
        wait = min(base_wait + jitter, 60)  # cap at 60s
        log.warning(f"[{self.name}] 429 rate limited (attempt {attempt+1}), "
                    f"backing off {wait:.1f}s...")
        time.sleep(wait)
        # Adaptively increase base delay after repeated 429s — affects ALL clients for this host
        with throttle["lock"]:
            throttle["consecutive_429s"] += 1
            if throttle["consecutive_429s"] >= 3:
                throttle["delay"] = min(throttle["delay"] * 1.5, throttle["base_delay"] * 4)
                log.warning(f"[{self.name}] Adaptive: {self._host} delay increased to {throttle['delay']:.1f}s")

    def _reset_backoff(self):
        """Reset adaptive delay after successful request."""
        throttle = self._host_registry[self._host]
        with throttle["lock"]:
            if throttle["consecutive_429s"] > 0:
                throttle["consecutive_429s"] = 0
                throttle["delay"] = throttle["base_delay"]

    def _get_cache_key(self, url: str, params: dict = None) -> str:
        param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        return f"{url}?{param_str}"

    def _get_cached(self, cache_key: str):
        with self._cache_lock:
            if cache_key in self._cache:
                ts, data = self._cache[cache_key]
                if time.time() - ts < self._cache_ttl:
                    return data
                del self._cache[cache_key]
        return None

    def _set_cached(self, cache_key: str, data):
        with self._cache_lock:
            self._cache[cache_key] = (time.time(), data)

    def get(self, endpoint: str, params: dict = None, use_cache: bool = True) -> dict | list | None:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        cache_key = self._get_cache_key(url, params)

        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        max_429_retries = 3
        for attempt in range(max_429_retries + 1):
            self._rate_limit()
            self._request_count += 1
            try:
                resp = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
                if resp.status_code == 429:
                    if attempt < max_429_retries:
                        self._backoff_429(attempt)
                        continue
                    else:
                        log.error(f"[{self.name}] 429 after {max_429_retries} retries: {url}")
                        return None
                resp.raise_for_status()
                data = resp.json()
                self._reset_backoff()
                if use_cache:
                    self._set_cached(cache_key, data)
                return data
            except requests.exceptions.RequestException as e:
                log.warning(f"[{self.name}] Request failed: {url} - {e}")
                return None

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
                        trade_volume_in_usd_greater_than: float = 10) -> list:
        """Get recent trades for a pool. Default min volume $10 to reduce noise."""
        data = self.get(
            f"/networks/{network}/pools/{pool_address}/trades",
            params={"trade_volume_in_usd_greater_than": trade_volume_in_usd_greater_than},
        )
        if data and "data" in data:
            return data["data"] if isinstance(data["data"], list) else []
        return []


class RugcheckClient(APIClient):
    """Rugcheck.xyz API - Free Solana token security reports."""

    def __init__(self):
        super().__init__(config.RUGCHECK_BASE, config.RUGCHECK_DELAY, "rugcheck")

    def get_token_report(self, mint_address: str) -> dict | None:
        """Get security report for a Solana token."""
        return self.get(f"/v1/tokens/{mint_address}/report")


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
