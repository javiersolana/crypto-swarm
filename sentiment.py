"""
THE NARRATOR - Social Momentum & Sentiment Analysis
Evaluates social signals and detects if a token's pump has already occurred.
"""
import re

import config
from api_client import CoinGeckoClient, DexScreenerClient
from utils import get_logger, safe_float, clamp, score_range

log = get_logger("narrator")


class Narrator:
    """Analyzes social momentum and sentiment for tokens."""

    def __init__(self):
        self.coingecko = CoinGeckoClient()
        self.dex = DexScreenerClient()

    def analyze(self, candidates: list[dict]) -> list[dict]:
        """Analyze sentiment for all candidates. Returns scored list."""
        log.info(f"=== THE NARRATOR: Analyzing sentiment for {len(candidates)} tokens ===")
        analyzed = []

        for token in candidates:
            result = self._analyze_token(token)
            token.update(result)
            log.info(f"  {token['name']}: narrator_score={token['narrator_score']:.1f}/10")
            analyzed.append(token)

        # Sort by narrator_score
        analyzed.sort(key=lambda x: x["narrator_score"], reverse=True)
        log.info(f"=== THE NARRATOR: Analysis complete ===")
        return analyzed

    def _analyze_token(self, token: dict) -> dict:
        """Analyze a single token's social signals."""
        result = {
            "narrator_score": 5.0,
            "narrator_signals": [],
            "social_mentions": 0,
            "pump_already_occurred": False,
            "discourse_quality": "neutral",
        }

        signals = []
        scores = []

        # ─── Signal 1: DexScreener Profile & Socials ─────────────────────
        dex_score, dex_signals = self._check_dexscreener_profile(token)
        scores.append(dex_score)
        signals.extend(dex_signals)

        # ─── Signal 2: CoinGecko Community Data ──────────────────────────
        cg_score, cg_signals = self._check_coingecko_community(token)
        scores.append(cg_score)
        signals.extend(cg_signals)

        # ─── Signal 3: Price Action vs Social (Pump Detection) ───────────
        pump_score, pump_detected, pump_signals = self._detect_pump_already_occurred(token)
        scores.append(pump_score)
        signals.extend(pump_signals)
        result["pump_already_occurred"] = pump_detected

        # ─── Signal 4: Token Name/Symbol Quality ────────────────────────
        name_score, name_signals = self._analyze_token_name(token)
        scores.append(name_score)
        signals.extend(name_signals)

        # ─── Compute Final Score ─────────────────────────────────────────
        if scores:
            avg_score = sum(scores) / len(scores)
        else:
            avg_score = 5.0

        # Penalty if pump already occurred
        if pump_detected:
            avg_score = min(avg_score, 4.0)

        result["narrator_score"] = round(clamp(avg_score, 1, 10), 1)
        result["narrator_signals"] = signals
        result["social_mentions"] = len([s for s in signals if "mention" in s.lower()])
        result["discourse_quality"] = self._classify_discourse(signals)

        return result

    def _check_dexscreener_profile(self, token: dict) -> tuple[float, list]:
        """Check if token has a DexScreener profile with social links."""
        signals = []
        address = token.get("address", "")
        chain = token.get("chain", "")

        if not address or not chain:
            return 4.0, ["no_dex_profile_data"]

        pairs = self.dex.get_token_pairs(chain, address)
        if not pairs:
            return 3.0, ["no_dexscreener_pairs"]

        pair = pairs[0]
        info = pair.get("info", {})
        socials = info.get("socials", [])
        websites = info.get("websites", [])

        score = 4.0

        if websites:
            score += 1.5
            signals.append("has_website")

        if socials:
            for social in socials:
                platform = social.get("type", "").lower()
                if platform == "twitter":
                    score += 1.5
                    signals.append("has_twitter")
                elif platform == "telegram":
                    score += 1.0
                    signals.append("has_telegram")
                elif platform == "discord":
                    score += 0.5
                    signals.append("has_discord")

        # Check if token has header/icon (effort put into branding)
        if info.get("imageUrl"):
            score += 0.5
            signals.append("has_branding")

        if not socials and not websites:
            signals.append("no_social_presence")
            score = 2.0

        return clamp(score, 1, 10), signals

    def _check_coingecko_community(self, token: dict) -> tuple[float, list]:
        """Check CoinGecko for community data if available."""
        signals = []
        cg_id = token.get("coingecko_id", "")

        if not cg_id:
            # Try to find by name
            name = token.get("name", "").lower()
            if not name:
                return 5.0, ["no_coingecko_data"]

            # Search DexScreener which may have CoinGecko links
            return 5.0, []

        coin_data = self.coingecko.get_coin_data(cg_id)
        if not coin_data:
            return 5.0, ["coingecko_lookup_failed"]

        score = 5.0
        community = coin_data.get("community_data", {})

        twitter_followers = safe_float(community.get("twitter_followers"))
        telegram_members = safe_float(community.get("telegram_channel_user_count"))
        reddit_subs = safe_float(community.get("reddit_subscribers"))

        if twitter_followers > 10000:
            score += 2.0
            signals.append(f"twitter_{int(twitter_followers)}followers")
        elif twitter_followers > 1000:
            score += 1.0
            signals.append(f"twitter_{int(twitter_followers)}followers")
        elif twitter_followers > 0:
            score += 0.5

        if telegram_members > 5000:
            score += 1.5
            signals.append(f"telegram_{int(telegram_members)}members")
        elif telegram_members > 500:
            score += 0.5

        if reddit_subs > 1000:
            score += 0.5
            signals.append(f"reddit_{int(reddit_subs)}subs")

        # Sentiment score from CoinGecko
        sentiment_up = safe_float(coin_data.get("sentiment_votes_up_percentage"))
        if sentiment_up > 70:
            score += 1.0
            signals.append("positive_sentiment")
        elif sentiment_up < 30:
            score -= 1.0
            signals.append("negative_sentiment")

        return clamp(score, 1, 10), signals

    def _detect_pump_already_occurred(self, token: dict) -> tuple[float, bool, list]:
        """Detect if the token has already pumped (too late to enter).

        v2.0: Cross-references pool age with price change for smarter detection.
        A 200% pump on a 2-day-old token is different from a 200% pump on a 2-hour-old token.
        """
        signals = []
        price_change = token.get("price_change_24h", 0)
        age_hours = token.get("pool_age_days", 0) * 24

        # Incorporate early entry signals from scanner
        early_signals = token.get("early_entry_signals", [])
        has_late_entry = any("late_entry" in s for s in early_signals)
        has_coordinated = any("coordinated_pump" in s for s in early_signals)

        if has_coordinated:
            signals.append("coordinated_pump_detected")
            return 2.0, True, signals

        if price_change > config.SENTIMENT_MAX_PUMP_PRICE_CHANGE:
            signals.append(f"massive_pump_{price_change:.0f}pct")
            return 2.0, True, signals

        # v2.0: Age-adjusted pump detection
        # Old pool + big pump = definitely too late
        if has_late_entry and price_change > 100:
            signals.append(f"late_entry_pump_{price_change:.0f}pct_{age_hours:.0f}h")
            return 3.0, True, signals

        if price_change > 200:
            signals.append(f"large_pump_{price_change:.0f}pct")
            return 4.0, True, signals

        # New pool + moderate pump might still have room
        if price_change > 100:
            if age_hours < 6:
                signals.append(f"early_pump_{price_change:.0f}pct_{age_hours:.1f}h")
                return 5.5, False, signals  # More forgiving for very new pools
            signals.append(f"significant_pump_{price_change:.0f}pct")
            return 5.0, False, signals

        if price_change > 50:
            signals.append(f"moderate_pump_{price_change:.0f}pct")
            return 6.0, False, signals

        if price_change > 0:
            signals.append("positive_momentum")
            return 7.0, False, signals

        if price_change < -30:
            signals.append(f"major_dip_{price_change:.0f}pct")
            return 5.0, False, signals

        if price_change < -10:
            signals.append("minor_dip")
            return 6.0, False, signals

        return 6.0, False, signals

    def _analyze_token_name(self, token: dict) -> tuple[float, list]:
        """Analyze token name quality. Scam tokens often have telltale names."""
        signals = []
        name = token.get("name", "").lower()

        if not name:
            return 4.0, ["no_name"]

        score = 6.0

        # Red flags in names
        scam_patterns = [
            r"\belon\b", r"\bmusk\b", r"\btrump\b", r"\bmoon\b",
            r"\brocket\b", r"\b1000x\b", r"\bsafe\b", r"\bbaby\b",
            r"\binu\b", r"\bdoge\b", r"\bshib\b", r"\bpepe\b",
            r"\bfloki\b", r"\bcum\b", r"\bass\b", r"\btits\b",
        ]

        meme_count = 0
        for pattern in scam_patterns:
            if re.search(pattern, name):
                meme_count += 1

        if meme_count >= 3:
            score = 2.0
            signals.append("highly_meme_name")
        elif meme_count >= 2:
            score = 4.0
            signals.append("meme_name")
        elif meme_count == 1:
            score = 5.0
            signals.append("meme_adjacent_name")

        # Positive: technical/project-sounding names
        tech_patterns = [r"\bswap\b", r"\bfi\b", r"\bprotocol\b", r"\bnet\b", r"\bchain\b",
                         r"\bbridge\b", r"\blayer\b", r"\bvault\b"]
        for pattern in tech_patterns:
            if re.search(pattern, name):
                score += 0.5
                signals.append("tech_name")
                break

        return clamp(score, 1, 10), signals

    def _classify_discourse(self, signals: list) -> str:
        """Classify overall discourse quality."""
        tech_signals = sum(1 for s in signals if "tech" in s or "protocol" in s)
        hype_signals = sum(1 for s in signals if "meme" in s or "pump" in s or "moon" in s)

        if tech_signals > hype_signals:
            return "technical"
        elif hype_signals > tech_signals:
            return "hype"
        return "neutral"
