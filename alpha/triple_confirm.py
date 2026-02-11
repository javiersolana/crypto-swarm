#!/usr/bin/env python3
"""
Triple Confirmation Strategy Engine (v7.0)

Only alerts when 3+ independent signals align:
  1. Smart wallet buying
  2. Social/news momentum
  3. Technical/on-chain signals (from existing pipeline)
  4. GitHub/dev activity
  5. Volume building without pump

v7.0 changes:
  - Case-insensitive, whitespace-cleaned address matching
  - Active DexScreener lookup for whale-bought tokens not in scanner candidates
  - Signal accumulation awareness (30min window)

Usage:
  from alpha.triple_confirm import TripleConfirmation
  tc = TripleConfirmation()
  boosted = tc.evaluate(candidates, wallet_signals)
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import config_alpha
from utils import get_logger, now_utc, safe_float, clamp

log = get_logger("triple_confirm")


def _normalize_address(addr: str) -> str:
    """Normalize token address: strip whitespace, lowercase."""
    return addr.strip().lower() if addr else ""


class TripleConfirmation:
    """
    Multi-signal confirmation engine.
    Evaluates tokens against multiple independent alpha signals
    and produces a boosted score only when signals converge.

    v7.0: Active DexScreener lookup for whale-bought tokens missing from
    scanner candidates. Ensures high-score whale buys generate alerts even
    when the token wasn't on GeckoTerminal trending/new pools.
    """

    def __init__(self):
        self.weights = config_alpha.ALPHA_WEIGHTS
        self.alert_threshold = config_alpha.ALPHA_ALERT_THRESHOLD

    def evaluate(self, candidates: list[dict],
                 wallet_signals: list[dict] = None) -> list[dict]:
        """
        Evaluate candidates against multiple alpha signals.
        Adds alpha_score and alpha_signals to each candidate.

        v7.0: Also injects whale-bought tokens not present in candidates
        by actively looking them up on DexScreener.
        """
        wallet_signals = wallet_signals or []
        wallet_token_map = self._build_wallet_map(wallet_signals)

        log.info(f"Triple confirmation: evaluating {len(candidates)} candidates "
                 f"against {len(wallet_signals)} wallet signals")

        # v7.0: Find whale-bought tokens NOT in scanner candidates → active lookup
        candidate_addresses = set(_normalize_address(c.get("address", "")) for c in candidates)
        whale_only_tokens = self._inject_whale_tokens(
            wallet_token_map, candidate_addresses, candidates
        )
        if whale_only_tokens:
            log.info(f"  Injected {len(whale_only_tokens)} whale-only tokens via DexScreener")
            candidates = candidates + whale_only_tokens

        evaluated = []
        for token in candidates:
            try:
                token = self._evaluate_token(token, wallet_token_map)
            except Exception as e:
                log.warning(f"Evaluation failed for {token.get('name', '?')}: {e}")
                token["alpha_score"] = 0
                token["alpha_signals"] = []
                token["alpha_signal_count"] = 0
            evaluated.append(token)

        # Sort by alpha_score descending
        evaluated.sort(key=lambda x: x.get("alpha_score", 0), reverse=True)

        # Log high-scoring candidates
        high_alpha = [t for t in evaluated if t.get("alpha_score", 0) >= self.alert_threshold]
        if high_alpha:
            log.info(f"HIGH ALPHA CANDIDATES ({len(high_alpha)}):")
            for t in high_alpha[:5]:
                log.info(f"  {t.get('name', '?')}: alpha={t['alpha_score']:.1f}, "
                         f"signals={t.get('alpha_signals', [])}")

        return evaluated

    # v8.0: Whale-bought tokens require $15k minimum liquidity.
    # Below this threshold, even smart wallet buys are likely manipulation.
    WHALE_MIN_LIQUIDITY = 15_000  # $15k (raised from $10k in v7.5)

    def _inject_whale_tokens(self, wallet_token_map: dict,
                             candidate_addresses: set,
                             candidates: list[dict]) -> list[dict]:
        """v7.1: Aggressively look up whale-bought tokens missing from scanner.

        Changes from v7.0:
          - Lowered liquidity threshold to $10k (was $30k)
          - Rugcheck safety gate for Solana tokens (rejects danger-level)
          - Higher default forense_score (8.0) for whale-bought tokens
          - Detailed logging for every token examined
        """
        from api_client import DexScreenerClient

        missing_addrs = []
        for addr, signals in wallet_token_map.items():
            if addr not in candidate_addresses and addr:
                unique_wallets = set(s.get("wallet_address", "").lower() for s in signals)
                chain = signals[0].get("chain", "solana")
                missing_addrs.append((addr, chain, signals, len(unique_wallets)))

        if not missing_addrs:
            log.info("[WHALE INJECT] No whale-only tokens to inject (all already in candidates)")
            return []

        log.info(f"[WHALE INJECT] {len(missing_addrs)} whale-bought tokens NOT in scanner candidates:")
        for addr, chain, sigs, n_wallets in missing_addrs:
            labels = [s.get("wallet_label", "?") for s in sigs[:3]]
            log.info(f"  {addr[:20]}... ({chain}) — {n_wallets} wallet(s): {', '.join(labels)}")

        injected = []
        dex = DexScreenerClient()

        # Group by chain for batch lookup
        by_chain = {}
        for addr, chain, sigs, n_wallets in missing_addrs:
            dex_chain = config.DEXSCREENER_CHAINS.get(chain, chain)
            by_chain.setdefault(dex_chain, []).append((addr, sigs, n_wallets))

        for chain, items in by_chain.items():
            addrs = [item[0] for item in items]
            sig_map = {item[0]: item[1] for item in items}
            wallet_counts = {item[0]: item[2] for item in items}

            # Batch lookup (up to 30 per call)
            for i in range(0, len(addrs), 30):
                batch = addrs[i:i + 30]
                try:
                    pairs = dex.get_tokens_batch(chain, batch)
                    if not pairs:
                        log.warning(f"[WHALE INJECT] DexScreener returned 0 pairs for {len(batch)} tokens on {chain}")
                        continue

                    for pair in pairs:
                        base = pair.get("baseToken", {})
                        token_addr = _normalize_address(base.get("address", ""))
                        if not token_addr:
                            continue

                        liq = safe_float(pair.get("liquidity", {}).get("usd"))
                        if liq < self.WHALE_MIN_LIQUIDITY:
                            log.debug(f"[WHALE INJECT] SKIP {base.get('name','?')}: "
                                      f"liq=${liq:,.0f} < ${self.WHALE_MIN_LIQUIDITY:,}")
                            continue

                        price = safe_float(pair.get("priceUsd"))
                        volume = safe_float(pair.get("volume", {}).get("h24"))
                        change = safe_float(pair.get("priceChange", {}).get("h24"))
                        mcap = safe_float(pair.get("marketCap") or pair.get("fdv"))

                        created = pair.get("pairCreatedAt")
                        pool_age_days = 0
                        if created:
                            age_ms = time.time() * 1000 - created
                            pool_age_days = age_ms / (86400 * 1000)

                        n_wallets = wallet_counts.get(token_addr, 1)
                        # Multi-wallet buy = stronger conviction = higher default scores
                        base_forense = 8.5 if n_wallets >= 2 else 8.0

                        token_dict = {
                            "name": base.get("name", "?"),
                            "symbol": base.get("symbol", "?"),
                            "address": token_addr,
                            "pool_address": pair.get("pairAddress", ""),
                            "network": chain,
                            "chain": chain,
                            "liquidity_usd": liq,
                            "volume_24h": volume,
                            "price_usd": price,
                            "price_change_24h": change,
                            "mcap": mcap,
                            "pool_age_days": pool_age_days,
                            "dex_url": pair.get("url", ""),
                            "source": "whale_inject",
                            "whale_wallets": n_wallets,
                            "forense_score": base_forense,
                            "narrator_score": 5.0,
                            "quant_score": 5.0,
                            "scout_score": 7.0,  # boosted: whale buy is a strong scout signal
                        }

                        injected.append(token_dict)
                        log.info(f"  WHALE INJECT: {base.get('name', '?')} "
                                 f"({chain}) liq=${liq:,.0f} vol=${volume:,.0f} "
                                 f"age={pool_age_days:.1f}d — "
                                 f"bought by {n_wallets} wallet(s)")
                except Exception as e:
                    log.warning(f"[WHALE INJECT] DexScreener batch failed ({chain}): {e}")

        # ── Rugcheck safety gate for Solana whale tokens ──
        if injected:
            try:
                from api_client import RugcheckClient
                rc = RugcheckClient()
                safe_injected = []
                for token in injected:
                    if token.get("network") == "solana":
                        try:
                            report = rc.get_token_report(token["address"])
                            if report:
                                score = report.get("score", 0)
                                has_danger = any(
                                    r.get("level", "").lower() == "danger"
                                    for r in report.get("risks", [])
                                )
                                if has_danger or score > config.RUGCHECK_MAX_SCORE:
                                    log.info(f"  WHALE REJECT (Rugcheck): {token['name']} "
                                             f"score={score}, danger={has_danger}")
                                    continue
                                token["rugcheck_score"] = score
                                log.info(f"  WHALE PASS (Rugcheck): {token['name']} score={score}")
                        except Exception as e:
                            log.debug(f"  Rugcheck skip for {token['name']}: {e}")
                    safe_injected.append(token)
                injected = safe_injected
            except ImportError:
                log.debug("RugcheckClient not available, skipping safety check")

        return injected

    def _build_wallet_map(self, wallet_signals: list[dict]) -> dict:
        """Build a map of token_address -> [wallet_signals] for quick lookup.

        v7.0: Case-insensitive, whitespace-cleaned addresses.
        """
        token_map = {}
        for sig in wallet_signals:
            addr = _normalize_address(sig.get("token_address", ""))
            if addr:
                token_map.setdefault(addr, []).append(sig)
        return token_map

    def _evaluate_token(self, token: dict, wallet_map: dict) -> dict:
        """Evaluate a single token against all alpha signals."""
        alpha_score = 0.0
        alpha_signals = []
        signal_count = 0

        address = _normalize_address(token.get("address", ""))

        # ─── Signal 1: Smart Wallet Buying ────────────────────────────
        wallet_buys = wallet_map.get(address, [])
        if wallet_buys:
            signal_count += 1
            alpha_score += self.weights["smart_wallet_buying"]
            labels = [w.get("wallet_label", "?") for w in wallet_buys]
            alpha_signals.append(f"smart_wallet_buy({','.join(labels[:3])})")

            # Bonus for multiple smart wallets buying same token
            unique_wallets = set(w.get("wallet_address", "") for w in wallet_buys)
            if len(unique_wallets) >= 2:
                alpha_score += self.weights["multiple_smart_wallets"]
                alpha_signals.append(f"multi_wallet_buy({len(unique_wallets)})")

        # ─── Signal 2: GitHub/Dev Activity ────────────────────────────
        github_score = token.get("github_score", 0)
        if github_score >= 3:
            signal_count += 1
            weight = self.weights["github_active"]
            # Scale weight by GitHub score
            alpha_score += weight * (github_score / 10.0)
            alpha_signals.append(f"github_active({github_score:.0f})")

        # ─── Signal 3: News/Social Momentum ───────────────────────────
        news_sentiment = token.get("news_sentiment")
        social_intel_score = token.get("social_intel_score", 0)
        news_count = token.get("news_count", 0)

        if news_count >= 2:
            signal_count += 1
            if news_sentiment and news_sentiment.get("score", 5) >= 7:
                alpha_score += self.weights["news_positive"]
                alpha_signals.append(f"news_bullish({news_count})")
            if news_count >= 5:
                alpha_score += self.weights["news_trending"]
                alpha_signals.append("news_trending")

        # ─── Signal 4: Volume Building (from Quant analysis) ──────────
        quant_signals = token.get("quant_signals", [])
        accumulation = token.get("accumulation_detected", False)

        if accumulation:
            signal_count += 1
            alpha_score += self.weights["volume_building"]
            alpha_signals.append("accumulation_detected")
        elif any("volume_surging" in s for s in quant_signals):
            signal_count += 1
            alpha_score += self.weights["volume_building"] * 0.7
            alpha_signals.append("volume_surging")
        elif any("volume_increasing" in s for s in quant_signals):
            alpha_score += self.weights["volume_building"] * 0.4
            alpha_signals.append("volume_increasing")

        # ─── Signal 5: Fresh Token (Early Entry) ─────────────────────
        pool_age_hours = token.get("pool_age_days", 1) * 24
        early_signals = token.get("early_entry_signals", [])

        if "pre_pump_entry" in early_signals:
            signal_count += 1
            alpha_score += self.weights["fresh_token"]
            alpha_signals.append("early_entry")
        elif pool_age_hours < 6:
            alpha_score += self.weights["fresh_token"] * 0.5
            alpha_signals.append("very_fresh_token")

        # ─── Signal 6: Holder Growth ─────────────────────────────────
        buys_24h = token.get("buys_24h", 0)
        sells_24h = token.get("sells_24h", 0)
        if buys_24h > 100 and buys_24h > sells_24h * 1.5:
            signal_count += 1
            alpha_score += self.weights["holder_growth"]
            alpha_signals.append(f"holder_growth(buys={buys_24h})")

        # ─── Signal 7: Social Intel Score ─────────────────────────────
        social_signals = token.get("social_intel_signals", [])
        if social_signals:
            for sig in social_signals:
                if "github_strong" in sig:
                    alpha_score += 1.0
                if "news_bullish" in sig:
                    alpha_score += 0.5

        # ─── Penalties ────────────────────────────────────────────────

        # Bearish divergence = strong negative signal
        if any("BEARISH_DIVERGENCE" in s for s in quant_signals):
            alpha_score -= 3.0
            alpha_signals.append("PENALTY_bearish_divergence")

        # Coordinated pump
        if "coordinated_pump" in early_signals:
            alpha_score -= 4.0
            alpha_signals.append("PENALTY_coordinated_pump")

        # Already pumped significantly
        price_change = safe_float(token.get("price_change_24h"))
        if price_change > 200:
            alpha_score -= 2.0
            alpha_signals.append(f"PENALTY_already_pumped_{price_change:.0f}pct")
        elif price_change > 100:
            alpha_score -= 1.0
            alpha_signals.append(f"PENALTY_pump_{price_change:.0f}pct")

        # Very low liquidity
        liq = safe_float(token.get("liquidity_usd"))
        if 0 < liq < 30000:
            alpha_score -= 1.5
            alpha_signals.append("PENALTY_low_liquidity")

        # Honeypot indicators from forense
        forense_flags = token.get("forense_flags", [])
        if "possible_honeypot" in forense_flags:
            alpha_score -= 5.0
            alpha_signals.append("PENALTY_honeypot")

        # ─── Compute Final Alpha Score ────────────────────────────────

        # Normalize to 0-10 scale
        # Max possible raw: ~18 (all signals firing)
        # Practical max: ~12 (3-4 strong signals)
        normalized_alpha = clamp(alpha_score * (10.0 / 12.0), 0, 10)

        # Triple confirmation bonus: require 3+ independent signals
        if signal_count >= 3:
            normalized_alpha = min(10, normalized_alpha * 1.2)
            alpha_signals.append(f"TRIPLE_CONFIRMED({signal_count}_signals)")
        elif signal_count >= 2:
            alpha_signals.append(f"double_confirmed({signal_count}_signals)")
        elif signal_count <= 1 and normalized_alpha > 5:
            # Single signal = reduce confidence
            normalized_alpha *= 0.7
            alpha_signals.append("single_signal_penalty")

        token["alpha_score"] = round(normalized_alpha, 2)
        token["alpha_signals"] = alpha_signals
        token["alpha_signal_count"] = signal_count

        return token

    def compute_enhanced_composite(self, token: dict) -> float:
        """
        Compute an enhanced composite score that incorporates alpha signals.
        Blends the original composite with alpha_score.

        Original composite:
          0.15*scout + 0.30*forense + 0.15*narrator + 0.20*quant + 0.20*executor

        Enhanced: adds alpha as a blend factor
        """
        original_composite = safe_float(token.get("composite_score"))
        alpha = safe_float(token.get("alpha_score"))

        if alpha <= 0:
            return original_composite

        # Alpha influence: up to 30% of final score can come from alpha signals
        # If triple confirmed, alpha has stronger influence
        if token.get("alpha_signal_count", 0) >= 3:
            alpha_weight = 0.30
        elif token.get("alpha_signal_count", 0) >= 2:
            alpha_weight = 0.20
        else:
            alpha_weight = 0.10

        enhanced = (1 - alpha_weight) * original_composite + alpha_weight * alpha
        return round(enhanced, 2)

    def get_high_priority_alerts(self, candidates: list[dict]) -> list[dict]:
        """
        Filter for tokens that pass triple confirmation.
        These are the highest-conviction signals.
        """
        return [
            t for t in candidates
            if t.get("alpha_score", 0) >= self.alert_threshold
            and t.get("alpha_signal_count", 0) >= 3
        ]

    def format_alpha_alert(self, token: dict) -> str:
        """Format a high-priority alpha alert for Telegram."""
        name = token.get("name", "Unknown")
        chain = token.get("network", token.get("chain", "?")).upper()
        address = token.get("address", "")
        alpha_score = token.get("alpha_score", 0)
        composite = token.get("composite_score", 0)
        enhanced = self.compute_enhanced_composite(token)
        signals = token.get("alpha_signals", [])
        signal_count = token.get("alpha_signal_count", 0)
        price = safe_float(token.get("price_usd"))
        change = safe_float(token.get("price_change_24h"))
        liq = safe_float(token.get("liquidity_usd"))

        # Priority emoji
        if alpha_score >= 8 and signal_count >= 3:
            priority = "MAXIMUM ALPHA"
        elif alpha_score >= 7:
            priority = "HIGH ALPHA"
        else:
            priority = "ALPHA SIGNAL"

        dex_url = f"https://dexscreener.com/{token.get('chain', 'solana')}/{address}"

        # Format signals nicely
        signal_lines = []
        for s in signals:
            if s.startswith("TRIPLE"):
                signal_lines.append(f"  {s}")
            elif s.startswith("PENALTY"):
                signal_lines.append(f"  {s}")
            elif "smart_wallet" in s:
                signal_lines.append(f"  {s}")
            elif "github" in s:
                signal_lines.append(f"  {s}")
            elif "news" in s:
                signal_lines.append(f"  {s}")
            else:
                signal_lines.append(f"  {s}")

        signal_text = "\n".join(signal_lines[:8])

        msg = (
            f"<b>{priority}</b>\n\n"
            f"<b>{name}</b> ({chain})\n"
            f"Alpha Score: <b>{alpha_score:.1f}/10</b> ({signal_count} signals)\n"
            f"Pipeline Score: {composite:.1f}/10\n"
            f"Enhanced Score: <b>{enhanced:.1f}/10</b>\n\n"
            f"Price: ${price:.8f} ({change:+.1f}%)\n"
            f"Liquidity: ${liq:,.0f}\n\n"
            f"<b>Signals:</b>\n{signal_text}\n\n"
            f"<code>{address}</code>\n"
            f"\n<a href=\"{dex_url}\">DexScreener</a>"
        )
        return msg
