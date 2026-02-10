"""
THE QUANT - Technical Analysis Engine
Calculates RSI, support/resistance, detects accumulation patterns.
NEVER recommends buy at ATH.
"""
import config
from api_client import GeckoTerminalClient
from utils import get_logger, safe_float, clamp, score_range

log = get_logger("quant")


class Quant:
    """Technical analysis for token candidates."""

    def __init__(self):
        self.gecko = GeckoTerminalClient()
        self._ohlcv_calls = 0
        self._ohlcv_budget = getattr(config, 'GECKO_OHLCV_BUDGET', 30)

    def analyze(self, candidates: list[dict]) -> list[dict]:
        """Run technical analysis on candidates. Returns scored list.

        Respects OHLCV API budget to avoid GeckoTerminal rate limits.
        """
        log.info(f"=== THE QUANT: Analyzing {len(candidates)} tokens (budget: {self._ohlcv_budget} OHLCV calls) ===")
        analyzed = []

        for token in candidates:
            result = self._analyze_token(token)
            token.update(result)
            log.info(
                f"  {token['name']}: quant_score={token['quant_score']:.1f}/10, "
                f"RSI={token.get('rsi', 'N/A')}, entry=${token.get('entry_price', 0):.6f}"
            )
            analyzed.append(token)

        analyzed.sort(key=lambda x: x["quant_score"], reverse=True)
        log.info(f"=== THE QUANT: Analysis complete ({self._ohlcv_calls} OHLCV calls used) ===")
        return analyzed

    def _analyze_token(self, token: dict) -> dict:
        """Full technical analysis for a single token."""
        result = {
            "quant_score": 5.0,
            "rsi": None,
            "support_price": None,
            "resistance_price": None,
            "entry_price": token.get("price_usd", 0),
            "at_ath": False,
            "accumulation_detected": False,
            "quant_signals": [],
        }

        signals = []
        scores = []

        # Get OHLCV data
        candles = self._get_candles(token)

        if not candles or len(candles) < 10:
            signals.append("insufficient_data")
            result["quant_score"] = 5.0
            result["quant_signals"] = signals
            return result

        closes = [c[4] for c in candles]  # [timestamp, open, high, low, close, volume]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        volumes = [c[5] for c in candles]
        current_price = closes[-1] if closes else token.get("price_usd", 0)

        # ─── RSI Analysis ────────────────────────────────────────────────
        rsi = self._calculate_rsi(closes, config.RSI_PERIOD)
        if rsi is not None:
            result["rsi"] = round(rsi, 1)
            rsi_score, rsi_signals = self._score_rsi(rsi)
            scores.append(rsi_score)
            signals.extend(rsi_signals)

        # ─── Support & Resistance ────────────────────────────────────────
        support = self._find_support(lows, current_price)
        resistance = self._find_resistance(highs, current_price)
        result["support_price"] = support
        result["resistance_price"] = resistance

        if support and resistance and current_price > 0:
            sr_score, sr_signals = self._score_support_resistance(
                current_price, support, resistance)
            scores.append(sr_score)
            signals.extend(sr_signals)

        # ─── ATH Check ───────────────────────────────────────────────────
        ath = max(highs) if highs else 0
        if ath > 0 and current_price > 0:
            pct_from_ath = ((ath - current_price) / ath) * 100
            if pct_from_ath < config.ATH_PROXIMITY_PCT:
                result["at_ath"] = True
                signals.append("AT_ATH_DO_NOT_BUY")
                scores.append(1.0)
            elif pct_from_ath < 20:
                signals.append("near_ath")
                scores.append(3.0)
            elif pct_from_ath > 50:
                signals.append("well_below_ath")
                scores.append(7.0)
            else:
                scores.append(6.0)

        # ─── Accumulation Detection ──────────────────────────────────────
        accum, accum_signals = self._detect_accumulation(closes, volumes)
        result["accumulation_detected"] = accum
        signals.extend(accum_signals)
        if accum:
            scores.append(8.5)
        else:
            scores.append(5.0)

        # ─── Volume Trend ────────────────────────────────────────────────
        vol_score, vol_signals = self._analyze_volume_trend(volumes)
        scores.append(vol_score)
        signals.extend(vol_signals)

        # ─── Volume-Price Divergence Detection (v2.0) ────────────────────
        div_score, div_signals = self._detect_volume_divergence(closes, volumes)
        if div_signals:
            scores.append(div_score)
            signals.extend(div_signals)

        # ─── Entry Price Recommendation ──────────────────────────────────
        result["entry_price"] = self._recommend_entry(
            current_price, support, resistance, rsi)

        # ─── Final Score ─────────────────────────────────────────────────
        if scores:
            avg = sum(scores) / len(scores)
        else:
            avg = 5.0

        # Hard penalty if at ATH
        if result["at_ath"]:
            avg = min(avg, 2.0)

        result["quant_score"] = round(clamp(avg, 1, 10), 1)
        result["quant_signals"] = signals
        return result

    def _get_candles(self, token: dict) -> list:
        """Get OHLCV candles for a token. Respects API budget."""
        network_id = token.get("network_id", "")
        pool_address = token.get("pool_address", "")

        if not network_id or not pool_address:
            return []

        # Check budget before making expensive OHLCV calls
        if self._ohlcv_calls >= self._ohlcv_budget:
            log.warning(f"OHLCV budget exhausted ({self._ohlcv_budget}), skipping {token.get('name', '?')}")
            return []

        # Try hourly candles first (7 days = 168 candles)
        self._ohlcv_calls += 1
        candles = self.gecko.get_pool_ohlcv(
            network_id, pool_address,
            timeframe="hour", aggregate=1, limit=168
        )

        if candles and len(candles) >= 10:
            return candles

        # Fallback to 15-minute candles (costs another call)
        if self._ohlcv_calls < self._ohlcv_budget:
            self._ohlcv_calls += 1
            candles = self.gecko.get_pool_ohlcv(
                network_id, pool_address,
                timeframe="minute", aggregate=15, limit=100
            )
            return candles or []
        return []

    def _calculate_rsi(self, closes: list[float], period: int = 14) -> float | None:
        """Calculate RSI (Relative Strength Index)."""
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

        # Smoothed RSI using Wilder's method
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _score_rsi(self, rsi: float) -> tuple[float, list]:
        """Score based on RSI value."""
        signals = []
        if rsi < config.RSI_OVERSOLD:
            signals.append(f"oversold_rsi_{rsi:.0f}")
            return 8.0, signals  # Good buy opportunity
        elif rsi > config.RSI_OVERBOUGHT:
            signals.append(f"overbought_rsi_{rsi:.0f}")
            return 3.0, signals  # Bad time to buy
        elif rsi < 45:
            signals.append("rsi_favorable")
            return 7.0, signals
        elif rsi > 60:
            signals.append("rsi_elevated")
            return 5.0, signals
        else:
            signals.append("rsi_neutral")
            return 6.0, signals

    def _find_support(self, lows: list[float], current_price: float) -> float | None:
        """Find nearest support level below current price."""
        if not lows:
            return None
        support_candidates = [l for l in lows if l < current_price and l > 0]
        if not support_candidates:
            return None
        # Use recent local minimums
        recent = support_candidates[-20:]  # last 20 candles
        if recent:
            return min(recent)
        return None

    def _find_resistance(self, highs: list[float], current_price: float) -> float | None:
        """Find nearest resistance level above current price."""
        if not highs:
            return None
        resistance_candidates = [h for h in highs if h > current_price]
        if not resistance_candidates:
            return None
        recent = resistance_candidates[-20:]
        if recent:
            return max(recent)
        return None

    def _score_support_resistance(self, price: float, support: float,
                                   resistance: float) -> tuple[float, list]:
        """Score position relative to support/resistance."""
        signals = []
        range_size = resistance - support
        if range_size <= 0:
            return 5.0, []

        position = (price - support) / range_size  # 0 = at support, 1 = at resistance

        if position < 0.3:
            signals.append("near_support")
            return 8.0, signals  # Good entry near support
        elif position > 0.8:
            signals.append("near_resistance")
            return 3.0, signals  # Bad entry near resistance
        elif position < 0.5:
            signals.append("lower_half_range")
            return 6.5, signals
        else:
            signals.append("upper_half_range")
            return 5.0, signals

    def _detect_accumulation(self, closes: list[float], volumes: list[float]) -> tuple[bool, list]:
        """Detect accumulation: volume increasing while price relatively stable."""
        signals = []
        if len(closes) < 20 or len(volumes) < 20:
            return False, []

        recent_closes = closes[-10:]
        older_closes = closes[-20:-10]
        recent_volumes = volumes[-10:]
        older_volumes = volumes[-20:-10]

        if not older_volumes or not older_closes:
            return False, []

        avg_recent_vol = sum(recent_volumes) / len(recent_volumes)
        avg_older_vol = sum(older_volumes) / len(older_volumes)
        avg_recent_price = sum(recent_closes) / len(recent_closes)
        avg_older_price = sum(older_closes) / len(older_closes)

        if avg_older_vol == 0 or avg_older_price == 0:
            return False, []

        vol_change = avg_recent_vol / avg_older_vol
        price_change = abs(avg_recent_price - avg_older_price) / avg_older_price

        # Accumulation: volume up significantly, price stable (<15% change)
        if vol_change >= config.ACCUMULATION_VOL_INCREASE and price_change < 0.15:
            signals.append("accumulation_pattern")
            return True, signals

        if vol_change >= 1.2 and price_change < 0.10:
            signals.append("mild_accumulation")
            return False, signals

        return False, signals

    def _analyze_volume_trend(self, volumes: list[float]) -> tuple[float, list]:
        """Analyze volume trend direction."""
        signals = []
        if len(volumes) < 6:
            return 5.0, ["insufficient_volume_data"]

        recent = volumes[-3:]
        older = volumes[-6:-3]

        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)

        if avg_older == 0:
            return 5.0, []

        trend = avg_recent / avg_older

        if trend > 2.0:
            signals.append("volume_surging")
            return 8.0, signals
        elif trend > 1.3:
            signals.append("volume_increasing")
            return 7.0, signals
        elif trend > 0.8:
            signals.append("volume_stable")
            return 5.5, signals
        else:
            signals.append("volume_declining")
            return 4.0, signals

    def _detect_volume_divergence(self, closes: list[float], volumes: list[float]) -> tuple[float, list]:
        """Detect bearish volume-price divergence.

        Bearish divergence: price rising but volume declining = unsustainable pump.
        This catches tokens where artificial buying has dried up but price
        hasn't corrected yet (about to dump).
        """
        signals = []
        if len(closes) < 12 or len(volumes) < 12:
            return 5.0, []

        # Compare two periods: recent (last 6 candles) vs prior (6 before that)
        recent_closes = closes[-6:]
        prior_closes = closes[-12:-6]
        recent_vols = volumes[-6:]
        prior_vols = volumes[-12:-6]

        avg_recent_price = sum(recent_closes) / len(recent_closes)
        avg_prior_price = sum(prior_closes) / len(prior_closes)
        avg_recent_vol = sum(recent_vols) / len(recent_vols)
        avg_prior_vol = sum(prior_vols) / len(prior_vols)

        if avg_prior_price == 0 or avg_prior_vol == 0:
            return 5.0, []

        price_change_pct = ((avg_recent_price - avg_prior_price) / avg_prior_price) * 100
        vol_ratio = avg_recent_vol / avg_prior_vol

        price_up_thresh = getattr(config, 'VOL_DIVERGENCE_PRICE_UP_PCT', 10)
        vol_down_thresh = getattr(config, 'VOL_DIVERGENCE_VOL_DOWN_RATIO', 0.7)

        # Bearish divergence: price up but volume down
        if price_change_pct > price_up_thresh and vol_ratio < vol_down_thresh:
            signals.append(f"BEARISH_DIVERGENCE_price+{price_change_pct:.0f}pct_vol{vol_ratio:.1f}x")
            return 2.0, signals

        # Mild bearish divergence
        if price_change_pct > 5 and vol_ratio < 0.85:
            signals.append("mild_bearish_divergence")
            return 4.0, signals

        # Bullish confirmation: price up AND volume up
        if price_change_pct > 5 and vol_ratio > 1.3:
            signals.append("bullish_volume_confirmation")
            return 8.0, signals

        return 5.0, []

    def _recommend_entry(self, current_price: float, support: float | None,
                          resistance: float | None, rsi: float | None) -> float:
        """Recommend an entry price based on technical analysis."""
        if current_price <= 0:
            return 0

        # If oversold and near support, current price is fine
        if rsi and rsi < config.RSI_OVERSOLD and support:
            return current_price

        # If we have support, recommend slightly above support
        if support and support > 0:
            # Entry at 5% above support
            entry = support * 1.05
            # But not higher than current price
            return min(entry, current_price)

        # Default: suggest 5% below current (limit order)
        return current_price * 0.95
