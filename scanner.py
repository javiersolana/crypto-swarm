"""
THE SCOUT - Token Discovery Scanner
Scans DEX APIs for new tokens matching opportunity criteria.

v3.0: Priority-based network scanning, reduced pages, parallel DexScreener enrichment.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import config
from api_client import DexScreenerClient, GeckoTerminalClient, CoinGeckoClient
from utils import get_logger, safe_float, safe_int, parse_iso, days_ago, score_range, clamp

log = get_logger("scout")


class Scout:
    """Discovers new tokens across multiple DEX networks."""

    def __init__(self):
        self.gecko = GeckoTerminalClient()
        self.dex = DexScreenerClient()
        self.coingecko = CoinGeckoClient()

    def scan(self) -> list[dict]:
        """Run full scan across all networks. Returns scored candidates.

        v3.0: Priority networks get more pages. DexScreener + CoinGecko run in parallel.
        """
        log.info("=== THE SCOUT: Starting token scan ===")
        t_start = time.monotonic()
        candidates = []
        seen_addresses = set()

        def _add_candidate(token):
            if not token:
                return
            addr = token.get("address", "")
            if addr and addr in seen_addresses:
                return
            if addr:
                seen_addresses.add(addr)
            candidates.append(token)

        priority_networks = getattr(config, 'PRIORITY_NETWORKS', ["solana", "base"])
        pages_new = getattr(config, 'SCAN_NEW_POOLS_PAGES', 2)
        pages_new_lp = getattr(config, 'SCAN_NEW_POOLS_PAGES_LOW_PRIO', 1)
        pages_trend = getattr(config, 'SCAN_TRENDING_PAGES', 2)
        pages_trend_lp = getattr(config, 'SCAN_TRENDING_PAGES_LOW_PRIO', 1)

        # 1. GeckoTerminal new pools - priority networks get more pages
        for network_name, network_id in config.NETWORKS.items():
            is_priority = network_name in priority_networks
            pages = pages_new if is_priority else pages_new_lp
            log.info(f"Scanning new pools on {network_name} ({pages} pages, "
                     f"{'priority' if is_priority else 'low-prio'})...")
            pools = self.gecko.get_new_pools_paginated(network_id, pages=pages)
            log.info(f"  Found {len(pools)} new pools on {network_name}")
            for pool in pools:
                _add_candidate(self._parse_gecko_pool(pool, network_name, network_id))

        # 2. GeckoTerminal trending pools - priority networks get more pages
        for network_name, network_id in config.NETWORKS.items():
            is_priority = network_name in priority_networks
            pages = pages_trend if is_priority else pages_trend_lp
            log.info(f"Scanning trending pools on {network_name} ({pages} pages)...")
            pools = self.gecko.get_trending_pools_paginated(network_id, pages=pages)
            log.info(f"  Found {len(pools)} trending pools on {network_name}")
            for pool in pools:
                token = self._parse_gecko_pool(pool, network_name, network_id)
                if token:
                    token["source"] = "geckoterminal_trending"
                _add_candidate(token)

        # 3 + 4: DexScreener boosted + CoinGecko trending in parallel (both are fast APIs)
        dex_candidates = []
        cg_candidates = []

        def _fetch_boosted():
            log.info("Checking DexScreener boosted tokens...")
            boosted = self.dex.get_boosted_tokens()
            results = []
            for item in boosted[:30]:  # reduced from 50 to 30 - diminishing returns
                results.append(self._parse_dex_boosted(item))
            return results

        def _fetch_trending():
            log.info("Checking CoinGecko trending...")
            trending = self.coingecko.get_trending()
            results = []
            for item in trending[:5]:  # reduced from 10 to 5 - most are CEX tokens anyway
                results.append(self._enrich_coingecko_trending(item))
            return results

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_boosted = pool.submit(_fetch_boosted)
            fut_trending = pool.submit(_fetch_trending)

            for token in fut_boosted.result():
                _add_candidate(token)

            for token in fut_trending.result():
                if token and not any(c.get("name", "").lower() == token.get("name", "").lower()
                                     for c in candidates):
                    _add_candidate(token)

        elapsed = time.monotonic() - t_start
        log.info(f"Total raw candidates: {len(candidates)} (scan took {elapsed:.1f}s)")

        # Filter and score
        filtered = self._apply_filters(candidates)
        log.info(f"After filters: {len(filtered)} candidates")

        scored = self._score_candidates(filtered)
        scored.sort(key=lambda x: x["scout_score"], reverse=True)

        top = scored[:config.SCAN_MAX_CANDIDATES]
        log.info(f"=== THE SCOUT: Returning top {len(top)} candidates ===")
        return top

    def _parse_gecko_pool(self, pool: dict, network_name: str, network_id: str) -> dict | None:
        """Parse a GeckoTerminal pool into a candidate dict."""
        try:
            attrs = pool.get("attributes", {})
            if not attrs:
                return None

            # Get base token info
            name = attrs.get("name", "")
            pool_address = attrs.get("address", "")

            # Token address: try attributes first, then relationships
            token_address = attrs.get("base_token_address", "")
            if not token_address:
                rels = pool.get("relationships", {})
                base_token_data = rels.get("base_token", {}).get("data", {})
                token_id = base_token_data.get("id", "")
                # Format: "solana_<address>" or "eth_<address>"
                if "_" in token_id:
                    token_address = token_id.split("_", 1)[1]

            # Parse creation time
            created_at = attrs.get("pool_created_at")
            if not created_at:
                return None
            pool_age_days = days_ago(parse_iso(created_at))

            # Extract numeric values
            liquidity = safe_float(attrs.get("reserve_in_usd"))
            volume_24h = safe_float(attrs.get("volume_usd", {}).get("h24"))
            price_usd = safe_float(attrs.get("base_token_price_usd"))
            mcap = safe_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))

            # Price changes
            price_change_24h = safe_float(attrs.get("price_change_percentage", {}).get("h24"))

            # Transaction counts
            txns = attrs.get("transactions", {})
            buys_24h = safe_int(txns.get("h24", {}).get("buys"))
            sells_24h = safe_int(txns.get("h24", {}).get("sells"))

            return {
                "name": name,
                "address": token_address,
                "pool_address": pool_address,
                "network": network_name,
                "network_id": network_id,
                "chain": config.DEXSCREENER_CHAINS.get(network_name, network_name),
                "source": "geckoterminal",
                "pool_age_days": pool_age_days,
                "liquidity_usd": liquidity,
                "volume_24h": volume_24h,
                "price_usd": price_usd,
                "mcap": mcap,
                "price_change_24h": price_change_24h,
                "buys_24h": buys_24h,
                "sells_24h": sells_24h,
                "created_at": created_at,
            }
        except Exception as e:
            log.debug(f"Error parsing gecko pool: {e}")
            return None

    def _parse_dex_boosted(self, item: dict) -> dict | None:
        """Parse a DexScreener boosted token."""
        try:
            chain = item.get("chainId", "")
            token_address = item.get("tokenAddress", "")
            if not chain or not token_address:
                return None

            # Get pair data for this token
            pairs = self.dex.get_token_pairs(chain, token_address)
            if not pairs:
                return None

            pair = pairs[0]  # use highest liquidity pair
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                # DexScreener returns milliseconds
                created_dt = datetime.fromtimestamp(pair_created / 1000, tz=timezone.utc)
                pool_age_days = days_ago(created_dt)
                created_at = created_dt.isoformat()
            else:
                pool_age_days = 999
                created_at = None

            liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
            volume_24h = safe_float(pair.get("volume", {}).get("h24"))
            price_usd = safe_float(pair.get("priceUsd"))
            mcap = safe_float(pair.get("marketCap") or pair.get("fdv"))
            price_change_24h = safe_float(pair.get("priceChange", {}).get("h24"))
            txns = pair.get("txns", {}).get("h24", {})
            buys_24h = safe_int(txns.get("buys"))
            sells_24h = safe_int(txns.get("sells"))

            # Map chain to our network names
            network = chain
            for name, dex_chain in config.DEXSCREENER_CHAINS.items():
                if dex_chain == chain:
                    network = name
                    break

            return {
                "name": pair.get("baseToken", {}).get("name", ""),
                "address": token_address,
                "pool_address": pair.get("pairAddress", ""),
                "network": network,
                "network_id": config.NETWORKS.get(network, chain),
                "chain": chain,
                "source": "dexscreener_boosted",
                "pool_age_days": pool_age_days,
                "liquidity_usd": liquidity,
                "volume_24h": volume_24h,
                "price_usd": price_usd,
                "mcap": mcap,
                "price_change_24h": price_change_24h,
                "buys_24h": buys_24h,
                "sells_24h": sells_24h,
                "created_at": created_at,
            }
        except Exception as e:
            log.debug(f"Error parsing dex boosted: {e}")
            return None

    def _enrich_coingecko_trending(self, item: dict) -> dict | None:
        """Enrich a CoinGecko trending coin with DexScreener DEX data."""
        try:
            name = item.get("name", "")
            symbol = item.get("symbol", "")
            coin_id = item.get("id", "")

            if not symbol:
                return None

            # Search DexScreener for this token
            pairs = self.dex.search_pairs(symbol)
            if not pairs:
                return None

            # Find a matching pair with real liquidity
            for pair in pairs:
                base = pair.get("baseToken", {})
                if base.get("symbol", "").lower() != symbol.lower():
                    continue

                liquidity = safe_float(pair.get("liquidity", {}).get("usd"))
                if liquidity < 1000:
                    continue

                chain = pair.get("chainId", "")
                pair_created = pair.get("pairCreatedAt")
                if pair_created:
                    created_dt = datetime.fromtimestamp(pair_created / 1000, tz=timezone.utc)
                    pool_age_days = days_ago(created_dt)
                    created_at = created_dt.isoformat()
                else:
                    pool_age_days = 999
                    created_at = None

                network = chain
                for net_name, dex_chain in config.DEXSCREENER_CHAINS.items():
                    if dex_chain == chain:
                        network = net_name
                        break

                return {
                    "name": name,
                    "symbol": symbol,
                    "address": base.get("address", ""),
                    "pool_address": pair.get("pairAddress", ""),
                    "network": network,
                    "network_id": config.NETWORKS.get(network, chain),
                    "chain": chain,
                    "source": "coingecko_trending",
                    "pool_age_days": pool_age_days,
                    "liquidity_usd": liquidity,
                    "volume_24h": safe_float(pair.get("volume", {}).get("h24")),
                    "price_usd": safe_float(pair.get("priceUsd")),
                    "mcap": safe_float(pair.get("marketCap") or pair.get("fdv")),
                    "price_change_24h": safe_float(pair.get("priceChange", {}).get("h24")),
                    "buys_24h": safe_int(pair.get("txns", {}).get("h24", {}).get("buys")),
                    "sells_24h": safe_int(pair.get("txns", {}).get("h24", {}).get("sells")),
                    "created_at": created_at,
                    "coingecko_id": coin_id,
                }

            return None
        except Exception as e:
            log.debug(f"Error enriching coingecko trending: {e}")
            return None

    def _apply_filters(self, candidates: list[dict]) -> list[dict]:
        """Apply basic sanity filters."""
        filtered = []
        for c in candidates:
            # Must have an address
            if not c.get("address"):
                continue

            source = c.get("source", "")
            is_trending = "trending" in source

            # Age filter: stricter for new pools, relaxed for trending
            max_age = 30 if is_trending else config.SCAN_MAX_AGE_DAYS
            if c["pool_age_days"] > max_age and c["pool_age_days"] != 0:
                continue

            # Liquidity filter
            liq = c["liquidity_usd"]
            if liq > 0 and liq < config.SCAN_MIN_LIQUIDITY:
                continue
            if liq > config.SCAN_MAX_LIQUIDITY:
                continue

            # Market cap filter
            mcap = c["mcap"]
            if mcap > 0 and mcap > config.SCAN_MAX_MCAP:
                continue

            # Volume/Liquidity ratio (only filter extremely low)
            if liq > 0 and c["volume_24h"] > 0:
                vol_liq = c["volume_24h"] / liq
                if vol_liq < config.SCAN_VOL_LIQ_RATIO_MIN:
                    continue

            filtered.append(c)

        return filtered

    def _score_candidates(self, candidates: list[dict]) -> list[dict]:
        """Assign scout_score (1-10) to each candidate.

        v2.0: Added early entry detection and coordinated pump rejection.
        """
        for c in candidates:
            scores = []
            c["early_entry_signals"] = []

            # Liquidity score: sweet spot around $100k-$200k
            liq = c["liquidity_usd"]
            if liq > 0:
                if liq < 100_000:
                    scores.append(score_range(liq, config.SCAN_MIN_LIQUIDITY, 100_000))
                else:
                    scores.append(10.0 - score_range(liq, 100_000, config.SCAN_MAX_LIQUIDITY) * 0.3)
            else:
                scores.append(3.0)  # unknown

            # Age score: newer is more interesting (but not too new)
            age = c["pool_age_days"]
            age_hours = age * 24
            if 0.5 <= age <= 3:
                scores.append(9.0)
            elif 3 < age <= 5:
                scores.append(7.0)
            elif 5 < age <= 7:
                scores.append(5.0)
            elif age < 0.5:
                scores.append(4.0)  # too new, risky
            else:
                scores.append(2.0)

            # ─── EARLY ENTRY DETECTION (v2.0) ──────────────────────────────
            price_change = c.get("price_change_24h", 0)

            # Penalize: >6h old AND already pumped >100% = too late
            old_pump_age = getattr(config, 'EARLY_ENTRY_OLD_PUMP_AGE_HOURS', 6)
            old_pump_max = getattr(config, 'EARLY_ENTRY_OLD_PUMP_MAX_CHANGE', 100)
            if age_hours > old_pump_age and price_change > old_pump_max:
                penalty = min(3.0, (price_change - old_pump_max) / 100)
                scores.append(max(1.0, 4.0 - penalty))
                c["early_entry_signals"].append(f"late_entry_{age_hours:.0f}h_+{price_change:.0f}pct")
            elif age_hours < getattr(config, 'EARLY_ENTRY_VERY_NEW_HOURS', 1):
                # Very new pool (<1h) - potentially early but risky
                scores.append(7.0)
                c["early_entry_signals"].append(f"very_early_{age_hours:.1f}h")
            elif price_change < 20:
                # Pool exists but hasn't pumped yet - ideal entry
                scores.append(8.5)
                c["early_entry_signals"].append("pre_pump_entry")
            else:
                scores.append(6.0)

            # ─── COORDINATED PUMP DETECTION (v2.0) ──────────────────────────
            vol = c["volume_24h"]
            # Estimate 1h volume as ~1/24 of 24h (rough but useful without extra API call)
            vol_1h_est = vol / 24 if vol > 0 else 0
            pump_ratio_max = getattr(config, 'PUMP_DETECT_VOL_LIQ_1H_MAX', 3.0)
            if liq > 0 and vol_1h_est > 0:
                vol_liq_1h = vol_1h_est / liq
                if vol_liq_1h > pump_ratio_max:
                    scores.append(2.0)  # likely coordinated pump
                    c["early_entry_signals"].append(f"coordinated_pump_vol1h_liq={vol_liq_1h:.1f}")
                elif vol_liq_1h > pump_ratio_max * 0.6:
                    scores.append(4.0)
                    c["early_entry_signals"].append("elevated_vol_liq_1h")

            # Volume activity score
            if liq > 0 and vol > 0:
                vol_liq = vol / liq
                scores.append(score_range(vol_liq, 0.3, 1.0))
            else:
                scores.append(3.0)

            # Buy pressure score
            buys = c["buys_24h"]
            sells = c["sells_24h"]
            if buys + sells > 0:
                buy_ratio = buys / (buys + sells)
                scores.append(score_range(buy_ratio, 0.4, 0.7))
            else:
                scores.append(3.0)

            # Source bonus
            source_bonus = {
                "geckoterminal": 0,
                "dexscreener_boosted": 1.0,  # boosted tokens have some validation
                "coingecko_trending": 0.5,
            }
            bonus = source_bonus.get(c["source"], 0)

            c["scout_score"] = clamp(sum(scores) / len(scores) + bonus, 1, 10)

        return candidates
