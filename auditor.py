"""
THE FORENSE - Scam Detection Auditor (CRITICAL SAFETY LAYER)
Analyzes tokens for rug pull indicators, honeypots, and other red flags.
Score of 0 = auto-reject. Minimum score of 7 to proceed.

v2.0: DexScreener-first strategy to reduce GeckoTerminal load.
      GeckoTerminal reserved for trade distribution analysis only.
"""
import config
from api_client import DexScreenerClient, GeckoTerminalClient
from utils import get_logger, safe_float, safe_int, clamp

log = get_logger("forense")


class Forense:
    """Audits tokens for safety. This is the most critical component."""

    def __init__(self):
        self.gecko = GeckoTerminalClient()
        self.dex = DexScreenerClient()
        self._dex_cache = {}  # address -> pair data

    def audit(self, candidates: list[dict]) -> list[dict]:
        """Audit all candidates. Returns only those with forense_score >= threshold."""
        log.info(f"=== THE FORENSE: Auditing {len(candidates)} candidates ===")

        # Pre-fetch DexScreener data in batches (up to 30 per call, 300 req/min)
        self._batch_enrich_dexscreener(candidates)

        audited = []
        for token in candidates:
            result = self._audit_token(token)
            token.update(result)

            if token["forense_score"] == 0:
                log.info(f"  REJECTED: {token['name']} - {token['forense_reject_reason']}")
            elif token["forense_score"] < config.AUDIT_PASS_SCORE:
                log.info(f"  LOW SCORE: {token['name']} = {token['forense_score']:.1f}/10")
            else:
                log.info(f"  PASSED: {token['name']} = {token['forense_score']:.1f}/10")
                audited.append(token)

        log.info(f"=== THE FORENSE: {len(audited)}/{len(candidates)} passed audit ===")
        log.info(f"  API calls saved: DexScreener batch enrichment used for {len(self._dex_cache)} tokens")
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
                        self._dex_cache[base_addr.lower()] = pair

    def _audit_token(self, token: dict) -> dict:
        """Run all audit checks on a single token. Returns audit results."""
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

        # ─── CHECK 1: Liquidity ──────────────────────────────────────────
        liquidity = token.get("liquidity_usd", 0)
        if dex_pair:
            liquidity = max(liquidity, safe_float(
                dex_pair.get("liquidity", {}).get("usd")))

        if liquidity < config.AUDIT_MIN_LIQUIDITY:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = f"Liquidity too low: ${liquidity:,.0f}"
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
                return checks

            # Suspicious if almost no sells
            if sells < 5 and buys > 50:
                flags.append("very_low_sells")

            checks["honeypot_check"] = self._score_buy_sell(buys, sells)
        else:
            flags.append("no_transaction_data")
            checks["honeypot_check"] = 3.0

        # ─── CHECK 3: Holder Concentration ───────────────────────────────
        # Only call GeckoTerminal for trade analysis if basic checks passed
        concentration_score, concentration_flags = self._estimate_holder_concentration(token, None)
        checks["holder_concentration_check"] = concentration_score
        flags.extend(concentration_flags)

        # Auto-reject if extreme concentration detected
        if concentration_score == 0:
            checks["forense_score"] = 0
            checks["forense_reject_reason"] = "Extreme holder concentration detected"
            return checks

        # ─── CHECK 4: Pool Age ───────────────────────────────────────────
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

        # ─── Compute Final Score ─────────────────────────────────────────
        component_scores = [
            checks["liquidity_check"],
            checks["honeypot_check"],
            checks["holder_concentration_check"],
            checks["age_check"],
            checks["tx_activity_check"],
            checks["volume_legitimacy_check"],
        ]

        # Weighted average with emphasis on safety checks
        weights = [0.20, 0.25, 0.25, 0.10, 0.10, 0.10]
        weighted = sum(s * w for s, w in zip(component_scores, weights))

        # Apply flag penalties
        penalty = len(flags) * 0.5
        final_score = clamp(weighted - penalty, 1, 10)

        checks["forense_score"] = round(final_score, 1)
        checks["forense_flags"] = flags

        return checks

    def _get_dex_pair(self, token: dict) -> dict | None:
        """Get DexScreener pair data from batch cache or direct lookup."""
        addr = token.get("address", "").lower()
        if addr in self._dex_cache:
            return self._dex_cache[addr]
        # Fallback: direct DexScreener lookup (fast, 300 req/min)
        chain = token.get("chain", "")
        if chain and addr:
            pairs = self.dex.get_token_pairs(chain, addr)
            if pairs:
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

        # Get recent trades to analyze distribution
        trades = self.gecko.get_pool_trades(network_id, pool_address, 100)

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
