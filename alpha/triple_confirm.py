#!/usr/bin/env python3
"""
Triple Confirmation Strategy Engine

Only alerts when 3+ independent signals align:
  1. Smart wallet buying
  2. Social/news momentum
  3. Technical/on-chain signals (from existing pipeline)
  4. GitHub/dev activity
  5. Volume building without pump

Produces a unified "alpha score" that boosts or penalizes candidates
from the main pipeline.

Usage:
  # Integrated into swarm_v2.py pipeline
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


class TripleConfirmation:
    """
    Multi-signal confirmation engine.
    Evaluates tokens against multiple independent alpha signals
    and produces a boosted score only when signals converge.
    """

    def __init__(self):
        self.weights = config_alpha.ALPHA_WEIGHTS
        self.alert_threshold = config_alpha.ALPHA_ALERT_THRESHOLD

    def evaluate(self, candidates: list[dict],
                 wallet_signals: list[dict] = None) -> list[dict]:
        """
        Evaluate candidates against multiple alpha signals.
        Adds alpha_score and alpha_signals to each candidate.

        Args:
            candidates: Token candidates from the main pipeline (post-audit)
            wallet_signals: Recent smart wallet buy signals

        Returns:
            Candidates enriched with alpha_score and alpha_signals
        """
        wallet_signals = wallet_signals or []
        wallet_token_map = self._build_wallet_map(wallet_signals)

        log.info(f"Triple confirmation: evaluating {len(candidates)} candidates "
                 f"against {len(wallet_signals)} wallet signals")

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

    def _build_wallet_map(self, wallet_signals: list[dict]) -> dict:
        """Build a map of token_address -> [wallet_signals] for quick lookup."""
        token_map = {}
        for sig in wallet_signals:
            addr = sig.get("token_address", "").lower()
            if addr:
                token_map.setdefault(addr, []).append(sig)
        return token_map

    def _evaluate_token(self, token: dict, wallet_map: dict) -> dict:
        """Evaluate a single token against all alpha signals."""
        alpha_score = 0.0
        alpha_signals = []
        signal_count = 0

        address = token.get("address", "").lower()

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
