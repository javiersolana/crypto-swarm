"""
THE FORENSE - Scam Detection Auditor (CRITICAL SAFETY LAYER)
Analyzes tokens for rug pull indicators, honeypots, and other red flags.
Score of 0 = auto-reject. Minimum score of 7 to proceed.

v2.0: DexScreener-first strategy to reduce GeckoTerminal load.
v3.0: Parallel audit with ThreadPoolExecutor. Conditional trade analysis.
v4.0: Audit blacklist — rejected tokens cached for AUDIT_BLACKLIST_TTL.
v8.2: [SAFETY] REJECTED verbose logs with exact metric values.
v8.5: Anti-Fomo filter (RSI + h1 volume).
v8.6: SOL Trend filter — blocks ALL buys if SOL bleeding.
v8.8: Sweet Spot — relaxed Anti-Fomo: h1_vol $10k (was $50k), RSI 70 (was 65),
      RSI period 9 (was 14), NEW vol_h1/liquidity ratio >= 0.3 filter.
      Keeps safety (RSI catches GIRAFFLUNA -98%) while allowing fresh gems
      (SPIRIT +1630%, SWAM +1929% would now pass).
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from api_client import DexScreenerClient, GeckoTerminalClient, RugcheckClient
from alpha.smart_wallet_tracker import SolanaRPCClient, check_creator_balance
from utils import get_logger, safe_float, safe_int, clamp

log = get_logger("forense")


class AuditBlacklist:
    """Persisted cache of tokens rejected with score=0.

    Tokens on the blacklist are skipped during audit, saving API calls.
    Entries expire after AUDIT_BLACKLIST_TTL seconds.
    """

    def __init__(self):
        self._data = {}  # {address_lower: {"reason", "timestamp", "chain"}}
        self._lock = threading.Lock()
        self._file = config.AUDIT_BLACKLIST_FILE
        self._ttl = config.AUDIT_BLACKLIST_TTL
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._file):
                with open(self._file, "r") as f:
                    self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Blacklist load error: {e}")
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            log.warning(f"Blacklist save error: {e}")

    def is_blacklisted(self, address: str) -> tuple[bool, str | None]:
        addr = address.lower()
        with self._lock:
            entry = self._data.get(addr)
            if entry and time.time() - entry["timestamp"] < self._ttl:
                return True, entry["reason"]
        return False, None

    def add(self, address: str, reason: str, chain: str = ""):
        addr = address.lower()
        with self._lock:
            self._data[addr] = {
                "reason": reason,
                "timestamp": time.time(),
                "chain": chain,
            }
            self._save()

    def cleanup(self):
        now = time.time()
        with self._lock:
            expired = [a for a, e in self._data.items() if now - e["timestamp"] >= self._ttl]
            if expired:
                for a in expired:
                    del self._data[a]
                self._save()
                log.info(f"  [Blacklist] Cleaned up {len(expired)} expired entries")


class Forense:
    """Audits tokens for safety. This is the most critical component.

    v3.0: Parallel audit, conditional GeckoTerminal calls, early-alert callback.
    """

    def __init__(self):
        self.gecko = GeckoTerminalClient()
        self.dex = DexScreenerClient()
        self.rugcheck = RugcheckClient()
        self.solana_rpc = SolanaRPCClient()
        self.blacklist = AuditBlacklist()
        self._dex_cache = {}  # address -> pair data
        self._dex_cache_lock = threading.Lock()
        self._gecko_trades_calls = 0
        self._gecko_trades_lock = threading.Lock()

    def audit(self, candidates: list[dict], on_pass_callback=None) -> list[dict]:
        """Audit all candidates in parallel. Returns only those with forense_score >= threshold.

        Args:
            candidates: List of token dicts from Scout.
            on_pass_callback: Optional callable(token) invoked immediately when a token
                              passes the audit. Used for early alerting.
        """
        log.info(f"=== THE FORENSE: Auditing {len(candidates)} candidates ===")
        t_start = time.monotonic()

        # v8.6: SOL Trend Filter — block ALL buys if SOL is bleeding
        sol_blocked = self._check_sol_trend()
        if sol_blocked:
            log.warning(f"=== THE FORENSE: ALL BUYS BLOCKED — SOL bleeding ({sol_blocked}) ===")
            return []

        # Filter blacklisted tokens before any API calls
        self.blacklist.cleanup()
        filtered = []
        blacklisted_count = 0
        for c in candidates:
            addr = c.get("address", "").lower()
            is_bl, reason = self.blacklist.is_blacklisted(addr)
            if is_bl:
                log.info(f"  [Forense] Token {addr[:12]}... saltado por Blacklist (Ahorrada 1 llamada) - {reason}")
                blacklisted_count += 1
            else:
                filtered.append(c)
        if blacklisted_count:
            log.info(f"  [Blacklist] Skipped {blacklisted_count} blacklisted tokens")
        candidates = filtered

        # Pre-fetch DexScreener data in batches (up to 30 per call, 300 req/min)
        self._batch_enrich_dexscreener(candidates)

        # v7.0: Respect cool-down mode — reduce workers if RPC is rate-limited
        from api_client import APIClient
        rpc_host = "api.mainnet-beta.solana.com"
        gecko_host = "api.geckoterminal.com"
        rpc_workers = APIClient.get_max_workers(rpc_host)
        gecko_workers = APIClient.get_max_workers(gecko_host)
        workers = min(getattr(config, 'AUDIT_PARALLEL_WORKERS', 8),
                      rpc_workers, gecko_workers)
        audited = []
        rejected = 0
        low_score = 0

        def _audit_one(token):
            result = self._audit_token(token)
            token.update(result)
            return token

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_audit_one, t): t for t in candidates}
            for future in as_completed(futures):
                token = future.result()
                if token["forense_score"] == 0:
                    log.info(f"  REJECTED: {token['name']} - {token['forense_reject_reason']}")
                    rejected += 1
                elif token["forense_score"] < config.AUDIT_PASS_SCORE:
                    log.info(f"  LOW SCORE: {token['name']} = {token['forense_score']:.1f}/10")
                    low_score += 1
                else:
                    log.info(f"  PASSED: {token['name']} = {token['forense_score']:.1f}/10")
                    audited.append(token)
                    # Early alert callback: notify immediately when token passes
                    if on_pass_callback:
                        try:
                            on_pass_callback(token)
                        except Exception as e:
                            log.warning(f"  Early alert callback error: {e}")

        elapsed = time.monotonic() - t_start
        log.info(f"=== THE FORENSE: {len(audited)}/{len(candidates)} passed audit "
                 f"({rejected} rejected, {low_score} low-score) in {elapsed:.1f}s ===")
        log.info(f"  DexScreener cache: {len(self._dex_cache)} tokens | "
                 f"GeckoTerminal trade calls: {self._gecko_trades_calls}")
        return audited

    def _batch_enrich_dexscreener(self, candidates: list[dict]):
        """Pre-fetch DexScreener pair data in batches to avoid per-token calls."""
        # Group by chain
        by_chain = {}
        for c in candidates:
            chain = c.get("chain", "")
            addr = c.get("address", "")
            if chain and addr:
                by_chain.setdefault(chain, []).append(addr)

        for chain, addresses in by_chain.items():
            # DexScreener tokens/v1 supports comma-separated addresses (up to 30)
            for i in range(0, len(addresses), 30):
                batch = addresses[i:i+30]
                pairs = self.dex.get_tokens_batch(chain, batch)
                for pair in pairs:
                    base_addr = pair.get("baseToken", {}).get("address", "")
                    if base_addr:
                        with self._dex_cache_lock:
                            self._dex_cache[base_addr.lower()] = pair

    def _audit_token(self, token: dict) -> dict:
        """Run all audit checks on a single token. Returns audit results.

        v3.0: Two-phase audit. Phase 1 uses only DexScreener data (fast, no rate limit issues).
        Phase 2 (GeckoTerminal trade analysis) only runs if Phase 1 score is promising.
        This avoids burning GeckoTerminal quota on tokens that would be rejected anyway.
        """
        checks = {
            "liquidity_check": 0,
            "honeypot_check": 0,
            "holder_concentration_check": 0,
            "age_check": 0,
            "tx_activity_check": 0,
            "volume_legitimacy_check": 0,
            "forense_score": 0,
            "forense_reject_reason": None,
            "forense_flags": [],
        }

        flags = []

        # ─── Enrich with DexScreener data (fast, 300 req/min) ──────────
        dex_pair = self._get_dex_pair(token)

        # ─── CHECK 0: Minimum Token Age (v8.0 anti-rug) ─────────────────
        pool_age_seconds = token.get("pool_age_days", 0) * 86400
        if dex_pair and dex_pair.get("pairCreatedAt"):
            age_ms = time.time() * 1000 - dex_pair["pairCreatedAt"]
            pool_age_seconds = max(pool_age_seconds, age_ms / 1000)

        min_age = config.AUDIT_MIN_TOKEN_AGE_SECONDS
        token_name = token.get("name") or token.get("address", "???")[:12]
        if 0 < pool_age_seconds < min_age:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = (
                f"Token too young: {pool_age_seconds:.0f}s (min {min_age}s)"
            )
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Age ({pool_age_seconds:.0f}s < {min_age}s)")
            self.blacklist.add(
                token.get("address", ""),
                checks["forense_reject_reason"],
                token.get("chain", ""),
            )
            return checks

        # ─── CHECK 1: Liquidity ──────────────────────────────────────────
        liquidity = token.get("liquidity_usd", 0)
        if dex_pair:
            liquidity = max(liquidity, safe_float(
                dex_pair.get("liquidity", {}).get("usd")))

        if liquidity < config.AUDIT_MIN_LIQUIDITY:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Liquidity too low: ${liquidity:,.0f}"
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Liquidity (${liquidity:,.0f} < ${config.AUDIT_MIN_LIQUIDITY:,.0f})")
            self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
            return checks

        # ─── CHECK 1b: Liquidity/Market Cap Ratio (v8.1 anti-fragility) ───
        # Tokens where liquidity is <10% of mcap are "paper castles":
        # a single seller can crash the price 90%. Plush Solana was this.
        mcap = safe_float(token.get("market_cap", 0))
        if dex_pair:
            mcap = max(mcap, safe_float(dex_pair.get("marketCap", 0)))
        if mcap > 0 and liquidity > 0:
            liq_mcap_ratio = liquidity / mcap
            if liq_mcap_ratio < config.AUDIT_MIN_LIQ_MCAP_RATIO:
                checks["forense_score"] = 0
                checks["forense_reject_reason"] = (
                    f"Fragile liquidity: liq/mcap={liq_mcap_ratio:.1%} "
                    f"(${liquidity:,.0f} / ${mcap:,.0f}, min {config.AUDIT_MIN_LIQ_MCAP_RATIO:.0%})"
                )
                log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Liquidity Ratio ({liq_mcap_ratio:.1%} < {config.AUDIT_MIN_LIQ_MCAP_RATIO:.0%})")
                self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
                return checks

        checks["liquidity_check"] = self._score_liquidity(liquidity)

        # ─── CHECK 2: Honeypot Detection (buy/sell ratio) ────────────────
        buys = token.get("buys_24h", 0)
        sells = token.get("sells_24h", 0)

        # Enrich from DexScreener pair data (preferred, fast API)
        if dex_pair:
            txns = dex_pair.get("txns", {}).get("h24", {})
            buys = max(buys, safe_int(txns.get("buys")))
            sells = max(sells, safe_int(txns.get("sells")))

        total_txns = buys + sells
        if total_txns > 0:
            buy_sell_ratio = buys / max(sells, 1)
            sell_buy_ratio = sells / max(buys, 1)

            # Honeypot: people can buy but can't sell
            if sell_buy_ratio < config.AUDIT_MIN_BUY_SELL_RATIO and total_txns > 20:
                checks["forense_score"] = 0
                checks["forense_reject_reason"] = f"Honeypot suspected: sell/buy ratio = {sell_buy_ratio:.2f}"
                log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Honeypot (sell/buy={sell_buy_ratio:.2f}, buys={buys}, sells={sells})")
                self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
                return checks

            # Suspicious if almost no sells
            if sells < 5 and buys > 50:
                flags.append("very_low_sells")

            checks["honeypot_check"] = self._score_buy_sell(buys, sells)
        else:
            flags.append("no_transaction_data")
            checks["honeypot_check"] = 3.0

        # ─── CHECK 4: Pool Age (moved before holder check - cheap filter) ──
        age_hours = token.get("pool_age_days", 0) * 24
        if age_hours < config.AUDIT_MIN_POOL_AGE_HOURS and age_hours > 0:
            flags.append("very_new_pool")
            checks["age_check"] = 3.0
        elif age_hours == 0:
            flags.append("unknown_age")
            checks["age_check"] = 4.0
        else:
            # Sweet spot: 12h - 5 days
            if 12 <= age_hours <= 120:
                checks["age_check"] = 9.0
            elif 6 <= age_hours < 12:
                checks["age_check"] = 6.0
            elif 120 < age_hours <= 168:
                checks["age_check"] = 7.0
            else:
                checks["age_check"] = 5.0

        # ─── CHECK 5: Transaction Activity ───────────────────────────────
        if total_txns < config.AUDIT_MIN_TX_COUNT:
            flags.append("low_tx_count")
            checks["tx_activity_check"] = max(2.0, total_txns / config.AUDIT_MIN_TX_COUNT * 5)
        else:
            checks["tx_activity_check"] = min(10.0, 5.0 + (total_txns / 500) * 5)

        # ─── CHECK 6: Volume Legitimacy ──────────────────────────────────
        volume = token.get("volume_24h", 0)
        if dex_pair:
            volume = max(volume, safe_float(dex_pair.get("volume", {}).get("h24")))
        if liquidity > 0 and volume > 0:
            vol_liq = volume / liquidity
            # Suspicious if volume >> liquidity (wash trading)
            if vol_liq > 5.0:
                flags.append("possible_wash_trading")
                checks["volume_legitimacy_check"] = 2.0
            elif vol_liq > 3.0:
                flags.append("high_vol_liq_ratio")
                checks["volume_legitimacy_check"] = 5.0
            else:
                checks["volume_legitimacy_check"] = 8.0
        else:
            checks["volume_legitimacy_check"] = 3.0

        # ─── PHASE 1 PRE-SCORE (DexScreener-only, no GeckoTerminal) ─────
        # Compute preliminary score from checks 1,2,4,5,6.
        # Only proceed to expensive GeckoTerminal trade analysis if promising.
        phase1_scores = [
            checks["liquidity_check"],
            checks["honeypot_check"],
            checks["age_check"],
            checks["tx_activity_check"],
            checks["volume_legitimacy_check"],
        ]
        phase1_weights = [0.25, 0.30, 0.15, 0.15, 0.15]
        phase1_score = sum(s * w for s, w in zip(phase1_scores, phase1_weights))
        phase1_score -= len(flags) * 0.5

        trade_check_min = getattr(config, 'AUDIT_TRADE_CHECK_MIN_SCORE', 5.0)
        if phase1_score < trade_check_min:
            # Token is unlikely to pass even with perfect holder distribution.
            # Skip the expensive GeckoTerminal call.
            checks["holder_concentration_check"] = 5.0  # neutral default
            flags.append("trade_analysis_skipped_low_phase1")
        else:
            # ─── CHECK 3: Holder Concentration (expensive - GeckoTerminal) ──
            concentration_score, concentration_flags = self._estimate_holder_concentration(token, None)
            checks["holder_concentration_check"] = concentration_score
            flags.extend(concentration_flags)

            # Auto-reject if extreme concentration detected
            if concentration_score == 0:
                checks["forense_score"] = 0
                checks["forense_reject_reason"] = "Extreme holder concentration detected"
                log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Holder Concentration (extreme, top3 trades >70% volume)")
                self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
                return checks

        # ─── PHASE 3: Rugcheck Security (Solana only, FREE) ─────────────
        rugcheck_score, rugcheck_flags = self._check_rugcheck(token)
        checks["rugcheck_check"] = rugcheck_score
        flags.extend(rugcheck_flags)

        if rugcheck_score == 0:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Rugcheck rejected: {', '.join(rugcheck_flags)}"
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Rugcheck ({', '.join(rugcheck_flags)})")
            self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
            return checks

        # ─── PHASE 3b: Top Holders Analysis (Rugcheck data + RPC fallback) ──
        holder_score, holder_flags = self._check_top_holders(token, token.get("_rugcheck_report"))
        checks["top_holders_check"] = holder_score
        flags.extend(holder_flags)

        if holder_score == 0:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Holder concentration: {', '.join(holder_flags)}"
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Top Holders ({', '.join(holder_flags)})")
            self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
            return checks

        # ─── PHASE 4: Bundled Supply Detection (Solana, v6.0) ─────────
        relevant_holders = []
        if token.get("_rugcheck_report") and token["_rugcheck_report"].get("topHolders"):
            relevant_holders = token["_rugcheck_report"]["topHolders"]
        bundled_score, bundled_flags = self._check_bundled_wallets(token, relevant_holders)
        checks["bundled_check"] = bundled_score
        flags.extend(bundled_flags)

        if bundled_score == 0:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Bundled supply: {', '.join(bundled_flags)}"
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Bundled Wallets ({', '.join(bundled_flags)})")
            self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
            return checks

        # ─── PHASE 5: Anti-Fomo Filter (v8.5) ──────────────────────────
        # Reject tokens that are overbought (RSI > 65) or have low h1 volume
        antifomo_score, antifomo_flags = self._check_anti_fomo(token, dex_pair)
        flags.extend(antifomo_flags)

        if antifomo_score == 0:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Anti-Fomo rejected: {', '.join(antifomo_flags)}"
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Anti-Fomo ({', '.join(antifomo_flags)})")
            self.blacklist.add(token.get("address", ""), checks["forense_reject_reason"], token.get("chain", ""))
            return checks

        # ─── Compute Final Score ─────────────────────────────────────────
        component_scores = [
            checks["liquidity_check"],              # 0.11
            checks["honeypot_check"],                # 0.16
            checks["holder_concentration_check"],    # 0.09 (trade-based, Phase 2)
            checks["age_check"],                     # 0.07
            checks["tx_activity_check"],             # 0.06
            checks["volume_legitimacy_check"],       # 0.09
            checks["rugcheck_check"],                # 0.18 (Rugcheck score)
            checks["top_holders_check"],             # 0.13 (on-chain holders)
            checks["bundled_check"],                 # 0.11 (bundled supply, v6.0)
        ]

        # Weighted average with emphasis on safety checks (9 components, v6.0)
        weights = [0.11, 0.16, 0.09, 0.07, 0.06, 0.09, 0.18, 0.13, 0.11]
        weighted = sum(s * w for s, w in zip(component_scores, weights))

        # Apply flag penalties
        penalty = len(flags) * 0.5
        final_score = clamp(weighted - penalty, 1, 10)

        checks["forense_score"] = round(final_score, 1)
        checks["forense_flags"] = flags

        return checks

    def _get_dex_pair(self, token: dict) -> dict | None:
        """Get DexScreener pair data from batch cache or direct lookup. Thread-safe."""
        addr = token.get("address", "").lower()
        with self._dex_cache_lock:
            if addr in self._dex_cache:
                return self._dex_cache[addr]
        # Fallback: direct DexScreener lookup (fast, 300 req/min)
        chain = token.get("chain", "")
        if chain and addr:
            pairs = self.dex.get_token_pairs(chain, addr)
            if pairs:
                with self._dex_cache_lock:
                    self._dex_cache[addr] = pairs[0]
                return pairs[0]
        return None

    def _get_pool_data(self, token: dict) -> dict | None:
        """Get additional pool data from GeckoTerminal (used only for trade analysis)."""
        network_id = token.get("network_id", "")
        pool_address = token.get("pool_address", "")
        if network_id and pool_address:
            return self.gecko.get_pool(network_id, pool_address)
        return None

    def _score_liquidity(self, liquidity: float) -> float:
        """Score liquidity. Sweet spot: $100k-$300k."""
        if liquidity >= 100_000 and liquidity <= 300_000:
            return 9.0
        elif liquidity >= 50_000 and liquidity < 100_000:
            return 6.0
        elif liquidity > 300_000:
            return 7.0
        return 3.0

    def _score_buy_sell(self, buys: int, sells: int) -> float:
        """Score buy/sell health. Healthy = balanced with slight buy pressure."""
        total = buys + sells
        if total == 0:
            return 3.0
        buy_pct = buys / total
        # Healthy range: 45-65% buys
        if 0.45 <= buy_pct <= 0.65:
            return 9.0
        elif 0.35 <= buy_pct <= 0.75:
            return 7.0
        elif buy_pct > 0.75:
            return 5.0  # Too one-sided, might dump
        elif buy_pct < 0.35:
            return 4.0  # Selling pressure
        return 5.0

    def _estimate_holder_concentration(self, token: dict, pool_data: dict | None) -> tuple[float, list]:
        """Estimate holder concentration from available data.

        Without direct on-chain access, we use heuristics:
        - Trade size distribution
        - Large transaction frequency
        """
        flags = []
        network_id = token.get("network_id", "")
        pool_address = token.get("pool_address", "")

        if not network_id or not pool_address:
            return 5.0, ["no_holder_data"]

        # Get recent trades to analyze distribution (expensive GeckoTerminal call)
        with self._gecko_trades_lock:
            self._gecko_trades_calls += 1
        trades = self.gecko.get_pool_trades(network_id, pool_address, 10)

        if not trades:
            return 5.0, ["no_trade_data"]

        # Analyze trade sizes
        trade_volumes = []
        unique_kinds = {"buy": 0, "sell": 0}
        for trade in trades:
            attrs = trade.get("attributes", {})
            vol = safe_float(attrs.get("volume_in_usd"))
            kind = attrs.get("kind", "")
            if vol > 0:
                trade_volumes.append(vol)
            if kind in unique_kinds:
                unique_kinds[kind] += 1

        if not trade_volumes:
            return 5.0, ["no_volume_data"]

        total_volume = sum(trade_volumes)
        avg_trade = total_volume / len(trade_volumes)
        max_trade = max(trade_volumes)

        # Flag if single trade is >20% of total volume (whale)
        if total_volume > 0 and max_trade / total_volume > 0.20:
            flags.append("whale_trade_detected")

        # Flag if top 3 trades are >50% of volume
        sorted_trades = sorted(trade_volumes, reverse=True)
        top3_volume = sum(sorted_trades[:3])
        if total_volume > 0 and top3_volume / total_volume > 0.50:
            flags.append("concentrated_trading")
            if top3_volume / total_volume > 0.70:
                return 0, ["extreme_concentration"]

        # Score based on trade distribution
        # More diverse trading = better
        num_trades = len(trade_volumes)
        if num_trades > 50:
            base_score = 8.0
        elif num_trades > 20:
            base_score = 6.0
        elif num_trades > 10:
            base_score = 5.0
        else:
            base_score = 3.0

        # Penalize concentration
        if "whale_trade_detected" in flags:
            base_score -= 1.5
        if "concentrated_trading" in flags:
            base_score -= 1.0

        return clamp(base_score, 1, 10), flags

    def _check_rugcheck(self, token: dict) -> tuple[float, list]:
        """Phase 3: Rugcheck security audit (Solana only, free API).

        Returns (score, flags). Score of 0 = auto-reject.
        Rugcheck score: lower = safer. >5000 = high risk.
        """
        flags = []
        chain = token.get("chain", "")
        if chain != "solana":
            return 7.0, []  # neutral for non-Solana chains

        address = token.get("address", "")
        if not address:
            return 5.0, ["no_address"]

        report = self.rugcheck.get_token_report(address)
        if not report:
            # v7.5: If Rugcheck is null/timeout, REJECT the token.
            # Better to miss a trade than buy an unverified potential rug.
            log.warning(f"  Rugcheck REJECT (null/timeout): {address[:20]}...")
            return 0, ["rugcheck_null_rejected"]

        # Store report for top holders analysis (Task 4)
        token["_rugcheck_report"] = report

        score = report.get("score", 0)
        risks = report.get("risks", [])

        # Auto-reject: score > 5000
        if score > config.RUGCHECK_MAX_SCORE:
            return 0, [f"rugcheck_score_{score}"]

        # Check for Danger-level risks
        danger_risks = [r for r in risks
                       if str(r.get("level", "")).lower() == "danger"]
        if danger_risks and config.RUGCHECK_REJECT_DANGER:
            danger_names = [r.get("name", "unknown") for r in danger_risks]
            return 0, [f"rugcheck_danger_{n}" for n in danger_names]

        # Score mapping: lower rugcheck score = better
        if score <= 100:
            return 9.0, []
        elif score <= 1000:
            return 7.0, ["rugcheck_moderate_risk"]
        elif score <= 3000:
            return 5.0, ["rugcheck_elevated_risk"]
        else:
            return 3.0, ["rugcheck_high_risk"]

    def _check_top_holders(self, token: dict, rugcheck_report: dict | None = None) -> tuple[float, list]:
        """Analyze top holder concentration using Rugcheck data or Solana RPC fallback.

        Returns (score, flags). Score of 0 = auto-reject (extreme concentration).
        v8.1: Added creator/dev wallet check (>15% insider = reject).
        """
        flags = []
        chain = token.get("chain", "")
        if chain != "solana":
            return 6.0, []  # skip for non-Solana

        # v8.1: Check if creator/dev holds >15% of supply (rug risk)
        if rugcheck_report:
            is_dev_heavy, insider_pct = check_creator_balance(
                rugcheck_report, config.AUDIT_MAX_CREATOR_PCT
            )
            if is_dev_heavy:
                token_name = token.get("name") or token.get("address", "???")[:12]
                log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Dev Wallet ({insider_pct:.1f}% > {config.AUDIT_MAX_CREATOR_PCT}%)")
                return 0, [f"LARGE_DEV_WALLET_{insider_pct:.1f}pct"]

        holders = []

        # Source 1: Rugcheck topHolders (already fetched in Phase 3)
        if rugcheck_report and rugcheck_report.get("topHolders"):
            holders = rugcheck_report["topHolders"]
        else:
            # Source 2: Solana RPC fallback (free)
            mint = token.get("address", "")
            if mint:
                raw = self.solana_rpc.get_token_largest_accounts(mint)
                if raw:
                    # Convert RPC format {amount, decimals, uiAmount, uiAmountString}
                    # to percentage-based (estimate from relative amounts)
                    total = sum(safe_float(h.get("uiAmount")) for h in raw)
                    if total > 0:
                        holders = [
                            {"address": h.get("address", ""), "pct": safe_float(h.get("uiAmount")) / total * 100}
                            for h in raw
                        ]

        if not holders:
            return 5.0, ["no_holder_data"]

        # Known addresses to exclude (LP pools, bonding curves, program accounts)
        EXCLUDE_OWNERS = {
            "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium LP v4
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
            "39azUYFWPz3VHgKCf3VChY6SkHCHvgmx4erxhBSGPTmp",  # Raydium CLMM
            "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",   # Raydium Launchpad
            "FRhB8L7Y9Qq41qZXYLtC2nw8An1RJfLLxRF2x9RwLLMo",  # Pump.fun fee account
        }

        # Calculate top 10 concentration excluding known pools
        relevant = [h for h in holders[:20]
                   if h.get("owner", h.get("address", "")) not in EXCLUDE_OWNERS
                   and not h.get("insider", False)]
        top10_pct = sum(safe_float(h.get("pct", 0)) for h in relevant[:10])

        if top10_pct > 50:
            return 0, ["EXTREME_CONCENTRATION_RISK"]
        elif top10_pct > 30:
            return 2.0, ["HIGH_CONCENTRATION_RISK"]
        elif top10_pct > 20:
            return 5.0, ["moderate_concentration"]
        elif top10_pct > 10:
            return 7.0, []
        else:
            return 9.0, ["well_distributed"]

    def _check_bundled_wallets(self, token: dict, holders: list) -> tuple[float, list]:
        """Phase 4: Detect bundled wallet manipulation (Solana only, v6.0).

        Checks if top holders were funded at the same time (within 5 minutes),
        indicating coordinated wallet creation to disguise concentrated supply.

        Uses free Solana RPC getSignaturesForAddress (limit=1) to get the
        first transaction timestamp for each holder.
        """
        flags = []
        chain = token.get("chain", "")
        if chain != "solana":
            return 7.0, []  # neutral for non-Solana

        if not holders or len(holders) < 3:
            return 7.0, ["insufficient_holder_data_for_bundle_check"]

        # Get first tx timestamps for top 10 holder addresses
        # Use the address field (token account) — for rugcheck data this is the holder address
        holder_addrs = []
        for h in holders[:10]:
            addr = h.get("address", "")
            if addr and len(addr) > 20:
                holder_addrs.append(addr)

        if len(holder_addrs) < 3:
            return 7.0, []

        first_tx_times = []
        for addr in holder_addrs:
            try:
                sigs = self.solana_rpc.get_signatures(addr, limit=1)
                if sigs and isinstance(sigs, list) and len(sigs) > 0:
                    block_time = sigs[0].get("blockTime")
                    if block_time and isinstance(block_time, (int, float)):
                        first_tx_times.append((addr[:8], block_time))
            except Exception:
                continue

        if len(first_tx_times) < 3:
            return 6.0, ["bundled_check_incomplete"]

        # Sort by timestamp and find clusters within 300 seconds (5 minutes)
        first_tx_times.sort(key=lambda x: x[1])
        max_cluster_size = 1
        cluster_start = 0

        for i in range(1, len(first_tx_times)):
            if first_tx_times[i][1] - first_tx_times[cluster_start][1] <= 300:
                cluster_size = i - cluster_start + 1
                max_cluster_size = max(max_cluster_size, cluster_size)
            else:
                cluster_start = i

        # 3+ holders with first tx within 5 minutes = HIGH manipulation risk
        if max_cluster_size >= 3:
            flags.append(f"BUNDLED_{max_cluster_size}_WALLETS")
            if max_cluster_size >= 5:
                return 0, flags  # auto-reject: 5+ bundled wallets
            return 2.0, flags  # 3-4 bundled wallets: heavy penalty

        # 2 holders close together: mild flag
        if max_cluster_size == 2:
            return 6.0, ["possible_bundled_pair"]

        return 8.0, ["no_bundling_detected"]

    def _check_anti_fomo(self, token: dict, dex_pair: dict | None) -> tuple[float, list]:
        """Phase 5: Anti-Fomo filter (v8.8 Sweet Spot).

        Three checks:
        1. Minimum h1 volume ($10k) — rejects truly dead tokens
        2. Vol_h1 / Liquidity ratio >= 0.3 — validates real traction
           (SPIRIT had ratio 1.13, dead tokens have 0.01)
        3. RSI(9) on 1-min candles > 70 = reject (buying the peak)
           (RSI period 9 for faster response on 1-min timeframe)

        v8.8: Relaxed from v8.5 ($50k→$10k, RSI 65→70, period 14→9).
        Added vol/liq ratio to distinguish "low volume because new" from
        "low volume because dead". This would have allowed SPIRIT (+1630%),
        SWAM (+1929%), CQ (+1794%) while still blocking GIRAFFLUNA (-98%).
        """
        flags = []
        token_name = token.get("name") or token.get("address", "???")[:12]

        # ─── Check 1: Minimum h1 volume from DexScreener ────────────────
        vol_h1 = 0
        if dex_pair:
            vol_h1 = safe_float(dex_pair.get("volume", {}).get("h1"))
        min_vol = getattr(config, 'AUDIT_MIN_VOLUME_H1', 10_000)
        if vol_h1 < min_vol:
            log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Anti-Fomo Volume "
                     f"(h1=${vol_h1:,.0f} < ${min_vol:,.0f})")
            return 0, [f"low_h1_volume_{vol_h1:.0f}"]

        # ─── Check 2: Vol_h1 / Liquidity ratio (v8.8 NEW) ───────────────
        # Fresh gems have vol/liq > 0.3 (SPIRIT: $35k/$31k = 1.13)
        # Dead tokens have vol/liq < 0.1 (random shitcoins)
        liquidity = safe_float(token.get("liquidity_usd", 0))
        if dex_pair:
            liquidity = max(liquidity, safe_float(
                dex_pair.get("liquidity", {}).get("usd")))
        min_ratio = getattr(config, 'AUDIT_MIN_VOL_LIQ_RATIO_H1', 0.3)
        if liquidity > 0 and vol_h1 > 0:
            vol_liq_ratio = vol_h1 / liquidity
            if vol_liq_ratio < min_ratio:
                log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Anti-Fomo Vol/Liq "
                         f"(ratio={vol_liq_ratio:.2f} < {min_ratio}, "
                         f"h1=${vol_h1:,.0f}, liq=${liquidity:,.0f})")
                return 0, [f"low_vol_liq_ratio_{vol_liq_ratio:.2f}"]
            flags.append(f"vol_liq_h1_{vol_liq_ratio:.2f}")

        # ─── Check 3: RSI on 1-min candles (v8.8: period 9, threshold 70) ──
        network_id = token.get("network_id", "")
        pool_address = token.get("pool_address", "")
        max_rsi = getattr(config, 'AUDIT_MAX_RSI_1M', 70)
        rsi_period = getattr(config, 'RSI_PERIOD_ANTIFOMO', 9)

        if network_id and pool_address:
            try:
                ohlcv = self.gecko.get_pool_ohlcv(
                    network_id, pool_address,
                    timeframe="minute", aggregate=1, limit=20
                )
                if ohlcv and len(ohlcv) >= rsi_period + 1:
                    closes = [safe_float(c[4]) for c in ohlcv if safe_float(c[4]) > 0]
                    rsi = self._calculate_rsi_simple(closes, rsi_period)
                    if rsi is not None and rsi > max_rsi:
                        log.info(f"  [SAFETY] REJECTED: {token_name} | Reason: Anti-Fomo RSI "
                                 f"(RSI({rsi_period})={rsi:.1f} > {max_rsi})")
                        return 0, [f"overbought_rsi_{rsi:.0f}"]
                    if rsi is not None:
                        flags.append(f"rsi_{rsi_period}m_{rsi:.0f}")
            except Exception as e:
                log.debug(f"  Anti-Fomo RSI check failed for {token_name}: {e}")

        return 7.0, flags

    def _check_sol_trend(self) -> str | None:
        """v8.6: Check if SOL dropped >2% in the last hour.

        Uses DexScreener to get the SOL/USDC pair's h1 price change.
        Returns a description string if buys should be blocked, None otherwise.
        """
        max_drop = getattr(config, 'SOL_TREND_MAX_DROP_PCT', -2.0)
        try:
            sol_mint = getattr(config, 'SOL_USDC_PAIR_ADDRESS',
                               'So11111111111111111111111111111111111111112')
            pairs = self.dex.get_token_pairs("solana", sol_mint)
            if not pairs:
                log.debug("SOL trend check: no pairs found, allowing buys")
                return None

            # Find the highest-liquidity SOL/USDC or SOL/USDT pair
            best_pair = None
            best_liq = 0
            for p in pairs:
                quote = p.get("quoteToken", {}).get("symbol", "").upper()
                if quote in ("USDC", "USDT"):
                    liq = safe_float(p.get("liquidity", {}).get("usd", 0))
                    if liq > best_liq:
                        best_liq = liq
                        best_pair = p

            if not best_pair:
                log.debug("SOL trend check: no SOL/USDC pair found, allowing buys")
                return None

            h1_change = safe_float(best_pair.get("priceChange", {}).get("h1", 0))
            log.info(f"[SOL TREND] SOL/USDC h1 change: {h1_change:+.2f}% "
                     f"(threshold: {max_drop}%)")

            if h1_change < max_drop:
                return f"SOL h1={h1_change:+.2f}% < {max_drop}%"

        except Exception as e:
            log.warning(f"SOL trend check failed: {e} — allowing buys")

        return None

    @staticmethod
    def _calculate_rsi_simple(closes: list[float], period: int = 14) -> float | None:
        """Calculate RSI using Wilder's smoothing method."""
        if len(closes) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))
        if len(gains) < period:
            return None
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
