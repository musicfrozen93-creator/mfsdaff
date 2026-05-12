"""
V15 Confidence Engine — Weighted Normalized Scoring (0-100 Hard Cap)

V15 Upgrades over V7:
  - Anti-momentum-chase penalties (exhaustion, late breakout, volume climax)
  - Entry quality score integration (30% weight on final confidence)
  - BTC relative strength integration (reduces long bias)
  - Market structure integration (S/R proximity, reversal risk)
  - Pre-reversal detection (CHoCH, exhaustion candles)

Scoring Pillars:
  1. Trend Strength     → 25 pts max  (EMA alignment, distance, slope)
  2. Volume Quality     → 20 pts max  (ratio, spike, consistency)
  3. Momentum           → 15 pts max  (RSI position, MACD, candle structure)
  4. Structure / S&R    → 15 pts max  (BB position, pullback, HTF alignment)
  5. BTC Market Align   → 15 pts max  (BTC trend vs trade direction)
  6. Spread / Liquidity → 10 pts max  (spread %, volume depth)

Total = 100 max. NEVER exceeds 100.

Confidence Tiers:
  0-59   = NO TRADE (return HOLD)
  60-69  = Weak (tradable only if nothing better)
  70-79  = Normal (tradable)
  80-89  = Strong
  90-100 = Elite

Entry Quality Filters:
  Hard-reject conditions that block bad entries regardless of score.
"""

import logging
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceResult:
    """Result from the confidence engine."""
    score: int                       # 0-100, hard-clamped
    tier: str                        # "NO_TRADE" | "WEAK" | "TRADABLE" | "STRONG" | "ELITE"
    action: str                      # BUY | SELL | HOLD
    reason: str                      # Human-readable explanation
    breakdown: dict = field(default_factory=dict)   # Per-pillar scores
    rejected: bool = False           # True if entry quality filter blocked
    reject_reason: str = ""          # Why it was rejected
    bonus_applied: list = field(default_factory=list)  # Entry pattern bonuses
    penalty_applied: list = field(default_factory=list) # Penalties


class ConfidenceEngine:
    """
    V7 Weighted Confidence Calculator.
    Used by ScalpingEngine, SwingEngine, SniperEngine.
    """

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 1: Trend Strength (25 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_trend(
        ema_fast: float, ema_slow: float, price: float,
        side: str, ema_dist_pct: float,
    ) -> tuple[float, str]:
        """
        Score trend alignment and strength.
        Max: 25 points.
        """
        score = 0.0
        details = []

        if side == "BUY":
            # EMA alignment: fast > slow
            if ema_fast > ema_slow:
                score += 8.0
                details.append("EMA aligned")
            else:
                details.append("EMA against")



            # Price above both EMAs
            if price > ema_fast and price > ema_slow:
                score += 5.0
                details.append("price>EMAs")
            elif price > ema_slow:
                score += 2.0
                details.append("price>slowEMA")

            # EMA distance (trend strength)
            if ema_dist_pct > 0.5:
                score += 7.0
                details.append(f"strong trend {ema_dist_pct:.2f}%")
            elif ema_dist_pct > 0.2:
                score += 4.0
                details.append(f"moderate trend {ema_dist_pct:.2f}%")
            elif ema_dist_pct > 0.08:
                score += 1.5
                details.append(f"weak trend {ema_dist_pct:.2f}%")

            # EMA slope (is trend accelerating?)
            # Approximated by ema_dist > some threshold
            if ema_dist_pct > 0.3 and ema_fast > ema_slow:
                score += 5.0
                details.append("trend momentum")
            elif ema_dist_pct > 0.15:
                score += 2.5

        else:  # SELL
            if ema_fast < ema_slow:
                score += 8.0
                details.append("EMA aligned")
            else:
                details.append("EMA against")

            if price < ema_fast and price < ema_slow:
                score += 5.0
                details.append("price<EMAs")
            elif price < ema_slow:
                score += 2.0
                details.append("price<slowEMA")

            if ema_dist_pct > 0.5:
                score += 7.0
                details.append(f"strong trend {ema_dist_pct:.2f}%")
            elif ema_dist_pct > 0.2:
                score += 4.0
            elif ema_dist_pct > 0.08:
                score += 1.5

            if ema_dist_pct > 0.3 and ema_fast < ema_slow:
                score += 5.0
                details.append("trend momentum")
            elif ema_dist_pct > 0.15:
                score += 2.5

        return min(score, 25.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 2: Volume Quality (20 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_volume(
        volume_ratio: float, volume_spike: bool,
    ) -> tuple[float, str]:
        """
        Score volume quality and confirmation.
        Max: 20 points.
        """
        score = 0.0
        details = []

        # Volume spike present
        if volume_spike:
            score += 8.0
            details.append("volume spike")

        # Volume ratio scoring
        if volume_ratio >= 2.5:
            score += 8.0
            details.append(f"extreme vol {volume_ratio:.1f}x")
        elif volume_ratio >= 1.5:
            score += 6.0
            details.append(f"high vol {volume_ratio:.1f}x")
        elif volume_ratio >= 1.0:
            score += 3.0
            details.append(f"normal vol {volume_ratio:.1f}x")
        elif volume_ratio >= 0.7:
            score += 1.0
            details.append(f"low vol {volume_ratio:.1f}x")
        else:
            details.append(f"very low vol {volume_ratio:.1f}x")

        # Volume consistency bonus (if ratio is steady above 1.0)
        if volume_ratio >= 1.2:
            score += 4.0
            details.append("consistent flow")

        return min(score, 20.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 3: Momentum (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_momentum(
        rsi: float, macd_crossover: str, candle_type: str, side: str,
    ) -> tuple[float, str]:
        """
        Score momentum indicators.
        Max: 15 points.
        """
        score = 0.0
        details = []

        if side == "BUY":
            # RSI in ideal range for longs
            if 45 <= rsi <= 65:
                score += 5.0
                details.append(f"RSI ideal {rsi:.0f}")
            elif 35 <= rsi < 45:
                score += 3.0
                details.append(f"RSI recovering {rsi:.0f}")
            elif 65 < rsi <= 75:
                score += 2.0
                details.append(f"RSI high {rsi:.0f}")
            elif rsi > 75:
                score += 0.0
                details.append(f"RSI overbought {rsi:.0f}")
            elif rsi < 30:
                score += 1.0
                details.append(f"RSI oversold {rsi:.0f}")

            # MACD alignment
            if macd_crossover == "BULLISH":
                score += 5.0
                details.append("MACD bullish cross")
            elif macd_crossover == "NONE":
                score += 2.0  # Neutral is OK

            # Candle confirmation
            if candle_type == "BULLISH":
                score += 5.0
                details.append("bullish candle")
            elif candle_type == "DOJI":
                score += 1.0

        else:  # SELL
            if 35 <= rsi <= 55:
                score += 5.0
                details.append(f"RSI ideal {rsi:.0f}")
            elif 55 < rsi <= 65:
                score += 3.0
            elif 25 <= rsi < 35:
                score += 2.0
                details.append(f"RSI low {rsi:.0f}")
            elif rsi < 25:
                score += 0.0
                details.append(f"RSI oversold {rsi:.0f}")
            elif rsi > 70:
                score += 1.0
                details.append(f"RSI overbought {rsi:.0f}")

            if macd_crossover == "BEARISH":
                score += 5.0
                details.append("MACD bearish cross")
            elif macd_crossover == "NONE":
                score += 2.0

            if candle_type == "BEARISH":
                score += 5.0
                details.append("bearish candle")
            elif candle_type == "DOJI":
                score += 1.0

        return min(score, 15.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 4: Structure / Support & Resistance (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_structure(
        bb_position: str, is_pullback: bool, is_rejection: bool,
        vwap: float, price: float, side: str,
    ) -> tuple[float, str]:
        """
        Score structural setup quality.
        Max: 15 points.
        """
        score = 0.0
        details = []

        if side == "BUY":
            # VWAP position
            if price > vwap:
                score += 4.0
                details.append("above VWAP")
            elif price > vwap * 0.998:
                score += 2.0
                details.append("near VWAP")

            # Bollinger position
            if bb_position == "LOWER":
                score += 4.0
                details.append("at BB lower (bounce zone)")
            elif bb_position == "MID":
                score += 3.0
                details.append("BB mid")
            # UPPER = 0 for longs (already extended)

            # Pullback quality
            if is_pullback:
                score += 5.0
                details.append("pullback entry")

            # Rejection (wrong for longs)
            if is_rejection:
                score -= 2.0
                details.append("rejection (bad for long)")

        else:  # SELL
            if price < vwap:
                score += 4.0
                details.append("below VWAP")
            elif price < vwap * 1.002:
                score += 2.0
                details.append("near VWAP")

            if bb_position == "UPPER":
                score += 4.0
                details.append("at BB upper (rejection zone)")
            elif bb_position == "MID":
                score += 3.0
                details.append("BB mid")

            if is_rejection:
                score += 5.0
                details.append("rejection entry")

            if is_pullback:
                score -= 2.0
                details.append("pullback (bad for short)")

        return max(0.0, min(score, 15.0)), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 5: BTC Market Alignment (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_btc_alignment(
        htf_trend: str, btc_trend: str, side: str,
    ) -> tuple[float, str]:
        """
        Score alignment with higher timeframe and BTC market trend.
        Max: 15 points.
        """
        score = 0.0
        details = []

        # HTF (15m) alignment
        if side == "BUY":
            if htf_trend == "BULLISH":
                score += 8.0
                details.append("HTF bullish ✓")
            elif htf_trend == "NEUTRAL":
                score += 3.0
                details.append("HTF neutral")
            else:
                details.append("HTF bearish ✗")

            # BTC trend alignment
            if btc_trend in ("BULLISH", "TRENDING_BULL"):
                score += 7.0
                details.append("BTC bullish ✓")
            elif btc_trend in ("NEUTRAL", "SIDEWAYS_RANGE", ""):
                score += 3.0
                details.append("BTC neutral")
            else:
                details.append("BTC bearish ✗")

        else:  # SELL
            if htf_trend == "BEARISH":
                score += 8.0
                details.append("HTF bearish ✓")
            elif htf_trend == "NEUTRAL":
                score += 3.0
                details.append("HTF neutral")
            else:
                details.append("HTF bullish ✗")

            if btc_trend in ("BEARISH", "TRENDING_BEAR"):
                score += 7.0
                details.append("BTC bearish ✓")
            elif btc_trend in ("NEUTRAL", "SIDEWAYS_RANGE", ""):
                score += 3.0
                details.append("BTC neutral")
            else:
                details.append("BTC bullish ✗")

        return min(score, 15.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 6: Spread / Liquidity (10 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_spread(spread_pct: float) -> tuple[float, str]:
        """
        Score spread quality (lower = better).
        Max: 10 points.
        """
        if spread_pct <= 0.02:
            return 10.0, "excellent spread"
        elif spread_pct <= 0.05:
            return 8.0, "good spread"
        elif spread_pct <= 0.08:
            return 6.0, "acceptable spread"
        elif spread_pct <= 0.12:
            return 3.0, "wide spread"
        else:
            return 0.0, f"spread too wide {spread_pct:.3f}%"

    # ═══════════════════════════════════════════════════════════════════
    # ENTRY QUALITY FILTERS (Hard-reject conditions)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def check_entry_quality(
        body: float,
        atr: float,
        spread_pct: float,
        high: float,
        low: float,
        open_price: float,
        close_price: float,
        volume_ratio: float,
        ema_dist_pct: float,
        side: str,
        atr_pct: float = 0.0,
    ) -> tuple[bool, str, int]:
        """
        V7 Entry Quality Filters — reject bad entries before scoring.
        
        Returns: (passed, reject_reason, confidence_penalty)
          - passed=False → hard reject (HOLD)
          - confidence_penalty > 0 → reduce score by this amount
        """
        total_range = high - low if high > low else 0.001
        upper_wick = high - max(open_price, close_price)
        lower_wick = min(open_price, close_price) - low

        # ── Hard Rejects ─────────────────────────────────────────────

        # 1. Candle overextended (body > 4×ATR)
        if atr > 0 and body > atr * 4.0:
            return False, f"Overextended candle: body={body:.4f} > 4×ATR={atr*4.0:.4f}", 0

        # 2. Spread too wide (> 0.20% — relaxed from 0.12% to allow more scalp opportunities)
        if spread_pct > 0.20:
            return False, f"Spread too wide: {spread_pct:.3f}% > 0.20%", 0

        # 3. Extreme volatility
        if atr_pct > 5.0:
            return False, f"Extreme volatility: ATR%={atr_pct:.2f}% > 5%", 0

        # 4. V13: Fake pump/dump detection (wick > 2.5×body — tightened from 3x)
        if side == "BUY" and body > 0:
            if upper_wick > body * 2.5:
                return False, "Fake pump: upper wick > 2.5× body (rejection)", 0
        elif side == "SELL" and body > 0:
            if lower_wick > body * 2.5:
                return False, "Fake dump: lower wick > 2.5× body (bounce)", 0

        # 5. Low volume move (volume < 0.3x average — relaxed from 0.5x)
        if volume_ratio < 0.3:
            return False, f"Very low volume: {volume_ratio:.1f}x < 0.3x avg", 0

        # 6. V13: Sideways chop (tightened to 0.03%)
        if ema_dist_pct < 0.03:
            return False, f"Extreme chop: EMA dist={ema_dist_pct:.3f}% < 0.03%", 0

        # ── Soft Penalties (reduce confidence but don't reject) ───────

        penalty = 0

        # 7. Wick trap against side
        if side == "BUY" and upper_wick > body * 2:
            penalty += 8
        elif side == "SELL" and lower_wick > body * 2:
            penalty += 8

        # 8. Moderate overextension (body > 1.8×ATR)
        if atr > 0 and body > atr * 1.8:
            penalty += 5

        # 9. Spread warning zone (0.08% - 0.12%)
        if 0.08 < spread_pct <= 0.12:
            penalty += 3

        # 10. V13: Thin-body fakeout penalty — body < 30% of total range signals indecision
        if total_range > 0 and body > 0 and (body / total_range) < 0.30:
            penalty += 10

        # 11. V13: Wide spread during potential breakout — spreads hurt fill quality
        if spread_pct > 0.10 and ema_dist_pct > 0.3:
            penalty += 5

        return True, "", penalty

    # ═══════════════════════════════════════════════════════════════════
    # ENTRY PATTERN BONUSES
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def check_entry_patterns(
        is_pullback: bool, is_rejection: bool,
        htf_trend: str, volume_spike: bool,
        side: str,
    ) -> tuple[int, list[str]]:
        """
        V7 Entry pattern bonuses — reward high-quality setups.
        Returns: (bonus_points, [descriptions])
        """
        bonus = 0
        descriptions = []

        if side == "BUY":
            # Pullback to EMA + bounce
            if is_pullback:
                bonus += 5
                descriptions.append("pullback entry +5")

            # HTF alignment bonus
            if htf_trend == "BULLISH":
                bonus += 3
                descriptions.append("HTF aligned +3")

            # Volume confirmation
            if volume_spike:
                bonus += 2
                descriptions.append("volume confirmed +2")

        else:  # SELL
            # Rejection at resistance
            if is_rejection:
                bonus += 5
                descriptions.append("rejection entry +5")

            if htf_trend == "BEARISH":
                bonus += 3
                descriptions.append("HTF aligned +3")

            if volume_spike:
                bonus += 2
                descriptions.append("volume confirmed +2")

        return bonus, descriptions

    # ═══════════════════════════════════════════════════════════════════
    # V15: ANTI-MOMENTUM-CHASE PENALTIES
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _calc_anti_chase_penalty(
        side: str,
        rsi: float,
        body: float,
        atr: float,
        price: float,
        ema_slow: float,
        vwap: float,
        volume_ratio: float,
        open_price: float,
        close_price: float,
        candles_data: list = None,
    ) -> tuple[int, list[str]]:
        """
        V15: Detect and penalize momentum-chasing entries.
        Returns (penalty_points, [reason_strings])
        """
        penalty = 0
        reasons = []

        # 1. Body > 2.2x ATR (overextended candle)
        if atr > 0:
            body_atr = body / atr
            if body_atr > settings.V15_MAX_BODY_ATR_RATIO:
                p = min(int((body_atr - 2.0) * 12), 20)
                penalty += p
                reasons.append(f"Overextended candle {body_atr:.1f}x ATR (-{p})")

        # 2. Price too far from EMA21 (chasing)
        if ema_slow > 0:
            ema_dist = abs(price - ema_slow) / ema_slow * 100
            if ema_dist > settings.V15_MAX_EMA_DISTANCE_PCT:
                p = min(int((ema_dist - 1.5) * 8), 15)
                penalty += p
                reasons.append(f"Far from EMA21 {ema_dist:.2f}% (-{p})")

        # 3. RSI exhaustion on entry
        if side == "BUY" and rsi > settings.V15_RSI_OVERBOUGHT_LONG:
            p = min(int((rsi - 70) * 1.2), 15)
            penalty += p
            reasons.append(f"RSI overbought {rsi:.0f} on LONG (-{p})")
        elif side == "SELL" and rsi < settings.V15_RSI_OVERSOLD_SHORT:
            p = min(int((30 - rsi) * 1.2), 15)
            penalty += p
            reasons.append(f"RSI oversold {rsi:.0f} on SHORT (-{p})")

        # 4. Distance from VWAP too large
        if vwap > 0:
            vwap_dist = abs(price - vwap) / vwap * 100
            if vwap_dist > settings.V15_MAX_VWAP_DISTANCE_PCT:
                p = min(int((vwap_dist - 1.0) * 6), 12)
                penalty += p
                reasons.append(f"Far from VWAP {vwap_dist:.2f}% (-{p})")

        # 5. Volume climax (exhaustion signal)
        if volume_ratio > settings.V15_VOLUME_CLIMAX_RATIO:
            p = min(int((volume_ratio - 2.0) * 5), 10)
            penalty += p
            reasons.append(f"Volume climax {volume_ratio:.1f}x (-{p})")

        # 6. Consecutive impulse candles (detect via candles_data if available)
        if candles_data and len(candles_data) >= 5:
            impulse_count = 0
            for c in candles_data[-5:-1]:  # last 4 candles before current
                c_open = float(c[1])
                c_close = float(c[4])
                if side == "BUY" and c_close > c_open:
                    impulse_count += 1
                elif side == "SELL" and c_close < c_open:
                    impulse_count += 1
                else:
                    break
            if impulse_count >= settings.V15_MAX_IMPULSE_CANDLES:
                p = min(impulse_count * 5, 15)
                penalty += p
                reasons.append(f"{impulse_count} impulse candles before entry (-{p})")

        return penalty, reasons

    # ═══════════════════════════════════════════════════════════════════
    # MAIN SCORING METHOD
    # ═══════════════════════════════════════════════════════════════════

    def calculate(
        self,
        side: str,
        # Pillar 1: Trend
        ema_fast: float,
        ema_slow: float,
        price: float,
        ema_dist_pct: float,
        # Pillar 2: Volume
        volume_ratio: float,
        volume_spike: bool,
        # Pillar 3: Momentum
        rsi: float,
        macd_crossover: str,
        candle_type: str,
        # Pillar 4: Structure
        bb_position: str,
        is_pullback: bool,
        is_rejection: bool,
        vwap: float,
        # Pillar 5: BTC
        htf_trend: str,
        btc_trend: str = "",
        # Pillar 6: Spread
        spread_pct: float = 0.0,
        # Entry quality filter inputs
        body: float = 0.0,
        atr: float = 0.0,
        atr_pct: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        open_price: float = 0.0,
        close_price: float = 0.0,
        # V15: New inputs
        entry_quality_score: int = -1,      # -1 = not computed yet
        exhaustion_score: int = 0,
        reversal_risk: int = 0,
        btc_relative_strength: float = 1.0,
        distance_to_support_pct: float = 5.0,
        distance_to_resistance_pct: float = 5.0,
        candles_data: list = None,
    ) -> ConfidenceResult:
        """
        V15: Calculate weighted confidence score from 6 pillars
        + entry quality integration + anti-chase penalties.
        
        Returns ConfidenceResult with:
          - score: 0-100 (HARD CLAMPED, never exceeds 100)
          - tier: NO_TRADE | WEAK | NORMAL | STRONG | ELITE
          - breakdown: per-pillar scores
        """

        # ── Step 1: Entry quality filter (hard reject) ────────────────
        eq_passed, eq_reason, eq_penalty = self.check_entry_quality(
            body=body, atr=atr, spread_pct=spread_pct,
            high=high, low=low, open_price=open_price,
            close_price=close_price, volume_ratio=volume_ratio,
            ema_dist_pct=ema_dist_pct, side=side, atr_pct=atr_pct,
        )

        if not eq_passed:
            return ConfidenceResult(
                score=0, tier="NO_TRADE", action="HOLD",
                reason=f"Entry quality rejected: {eq_reason}",
                rejected=True, reject_reason=eq_reason,
            )

        # ── V15: Hard reject on extreme exhaustion ────────────────────
        if settings.V15_EXHAUSTION_PENALTY_ENABLED and exhaustion_score >= settings.V15_EXHAUSTION_HARD_REJECT:
            return ConfidenceResult(
                score=0, tier="NO_TRADE", action="HOLD",
                reason=f"V15 Exhaustion hard reject: score={exhaustion_score}",
                rejected=True, reject_reason=f"Momentum exhaustion {exhaustion_score}/100",
            )

        # ── V15: Hard reject on S/R collision ─────────────────────────
        if side == "BUY" and distance_to_resistance_pct < 0.15:
            return ConfidenceResult(
                score=0, tier="NO_TRADE", action="HOLD",
                reason=f"V15 LONG into resistance: {distance_to_resistance_pct:.2f}% away",
                rejected=True, reject_reason="LONG directly into resistance",
            )
        if side == "SELL" and distance_to_support_pct < 0.15:
            return ConfidenceResult(
                score=0, tier="NO_TRADE", action="HOLD",
                reason=f"V15 SHORT into support: {distance_to_support_pct:.2f}% away",
                rejected=True, reject_reason="SHORT directly into support",
            )

        # ── Step 2: Calculate 6 pillars ───────────────────────────────
        trend_score, trend_detail = self._score_trend(
            ema_fast, ema_slow, price, side, ema_dist_pct,
        )
        volume_score, volume_detail = self._score_volume(
            volume_ratio, volume_spike,
        )
        momentum_score, momentum_detail = self._score_momentum(
            rsi, macd_crossover, candle_type, side,
        )
        structure_score, structure_detail = self._score_structure(
            bb_position, is_pullback, is_rejection, vwap, price, side,
        )
        btc_score, btc_detail = self._score_btc_alignment(
            htf_trend, btc_trend, side,
        )
        spread_score, spread_detail = self._score_spread(spread_pct)

        # ── Step 3: Sum raw score ─────────────────────────────────────
        raw_score = (
            trend_score + volume_score + momentum_score
            + structure_score + btc_score + spread_score
        )

        # ── Step 4: Apply entry pattern bonuses ───────────────────────
        bonus, bonus_descriptions = self.check_entry_patterns(
            is_pullback, is_rejection, htf_trend, volume_spike, side,
        )
        raw_score += bonus

        # ── Step 5: Apply entry quality penalty ───────────────────────
        penalty_descriptions = []
        if eq_penalty > 0:
            raw_score -= eq_penalty
            penalty_descriptions.append(f"entry quality penalty -{eq_penalty}")

        # ── V15 Step 5b: Anti-momentum-chase penalties ────────────────
        if settings.V15_EXHAUSTION_PENALTY_ENABLED:
            chase_penalty, chase_reasons = self._calc_anti_chase_penalty(
                side=side, rsi=rsi, body=body, atr=atr,
                price=price, ema_slow=ema_slow, vwap=vwap,
                volume_ratio=volume_ratio,
                open_price=open_price, close_price=close_price,
                candles_data=candles_data,
            )
            if chase_penalty > 0:
                raw_score -= chase_penalty
                penalty_descriptions.extend(chase_reasons)
                logger.info(
                    f"  [V15 ANTI-CHASE] {side}: total penalty={chase_penalty} | "
                    f"{'; '.join(chase_reasons)}"
                )

        # ── V15 Step 5c: Exhaustion / reversal risk penalty ───────────
        if exhaustion_score >= 40:
            exh_penalty = int(exhaustion_score * 0.15)
            raw_score -= exh_penalty
            penalty_descriptions.append(f"exhaustion penalty -{exh_penalty} (score={exhaustion_score})")

        if reversal_risk >= settings.V15_REVERSAL_RISK_THRESHOLD:
            rev_penalty = int(reversal_risk * 0.12)
            raw_score -= rev_penalty
            penalty_descriptions.append(f"reversal risk -{rev_penalty} (risk={reversal_risk})")

        # ── V15 Step 5d: BTC Relative Strength penalty ────────────────
        if settings.V15_BTC_RS_ENABLED:
            if side == "BUY" and btc_relative_strength < settings.V15_BTC_RS_LONG_MIN:
                rs_penalty = min(int((1.0 - btc_relative_strength) * 40), 12)
                raw_score -= rs_penalty
                penalty_descriptions.append(
                    f"Weak vs BTC RS={btc_relative_strength:.3f} (-{rs_penalty})"
                )
            elif side == "SELL" and btc_relative_strength > settings.V15_BTC_RS_SHORT_MAX:
                rs_penalty = min(int((btc_relative_strength - 1.0) * 40), 12)
                raw_score -= rs_penalty
                penalty_descriptions.append(
                    f"Strong vs BTC on SHORT RS={btc_relative_strength:.3f} (-{rs_penalty})"
                )

        # ── Step 6: HARD CLAMP to 0-100 (pillar score) ────────────────
        pillar_score = max(0, min(int(round(raw_score)), 100))

        # ── V15 Step 6b: Blend with entry quality score ───────────────
        if settings.V15_ENTRY_ENGINE_ENABLED and entry_quality_score >= 0:
            eq_weight = settings.V15_ENTRY_QUALITY_WEIGHT  # 0.30
            final_score = int(
                pillar_score * (1 - eq_weight) + entry_quality_score * eq_weight
            )
            final_score = max(0, min(final_score, 100))
            bonus_descriptions.append(f"EQ={entry_quality_score} blend@{eq_weight:.0%}")
        else:
            final_score = pillar_score

        # ── Step 7: Determine tier ────────────────────────────────────
        if final_score >= settings.V15_TIER_ELITE_MIN:
            tier = "ELITE"
        elif final_score >= settings.V15_TIER_STRONG_MIN:
            tier = "STRONG"
        elif final_score >= settings.V15_TIER_NORMAL_MIN:
            tier = "NORMAL"
        elif final_score >= settings.V15_TIER_WEAK_MIN:
            tier = "WEAK"
        else:
            tier = "NO_TRADE"

        # ── Step 8: Determine action ──────────────────────────────────
        action = side if final_score >= 60 else "HOLD"

        # ── Build reason string ───────────────────────────────────────
        reason_parts = [
            f"V15 confidence={final_score} [{tier}]",
            f"T:{trend_score:.0f}/25",
            f"V:{volume_score:.0f}/20",
            f"M:{momentum_score:.0f}/15",
            f"S:{structure_score:.0f}/15",
            f"B:{btc_score:.0f}/15",
            f"L:{spread_score:.0f}/10",
        ]
        if entry_quality_score >= 0:
            reason_parts.append(f"EQ:{entry_quality_score}/100")
        if bonus_descriptions:
            reason_parts.append(f"bonus: {', '.join(bonus_descriptions)}")
        if penalty_descriptions:
            reason_parts.append(f"penalty: {', '.join(penalty_descriptions)}")

        breakdown = {
            "trend": round(trend_score, 1),
            "volume": round(volume_score, 1),
            "momentum": round(momentum_score, 1),
            "structure": round(structure_score, 1),
            "btc_alignment": round(btc_score, 1),
            "spread": round(spread_score, 1),
            "bonus": bonus,
            "penalty": eq_penalty,
            "raw_total": round(raw_score, 1),
            "entry_quality": entry_quality_score,
            "exhaustion": exhaustion_score,
            "reversal_risk": reversal_risk,
            "btc_rs": round(btc_relative_strength, 3),
            "pillar_score": pillar_score,
        }

        logger.info(
            f"  [V15] Confidence: {final_score} [{tier}] | "
            f"T={trend_score:.0f} V={volume_score:.0f} M={momentum_score:.0f} "
            f"S={structure_score:.0f} B={btc_score:.0f} L={spread_score:.0f} "
            f"EQ={entry_quality_score} exh={exhaustion_score} rev={reversal_risk} "
            f"BTC_RS={btc_relative_strength:.3f} bonus={bonus} penalty={eq_penalty}"
        )

        return ConfidenceResult(
            score=final_score,
            tier=tier,
            action=action,
            reason=" | ".join(reason_parts),
            breakdown=breakdown,
            bonus_applied=bonus_descriptions,
            penalty_applied=penalty_descriptions,
        )

    # ═══════════════════════════════════════════════════════════════════
    # V17: EARLY SETUP DETECTION — Pre-breakout condition scoring
    # Identifies setups BEFORE full expansion triggers.
    # Used to emit WATCH signals for setups that are forming.
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def detect_early_setup(
        ema9: float = 0.0,
        ema21: float = 0.0,
        price: float = 0.0,
        vwap: float = 0.0,
        volume_ratio: float = 1.0,
        rsi: float = 50.0,
        bos_detected: bool = False,
        choch_detected: bool = False,
        liquidity_sweep: bool = False,
        sweep_direction: str = "NONE",
        side: str = "BUY",
        ema_distance_pct: float = 0.0,
    ) -> dict:
        """
        V17: Score pre-breakout conditions that indicate a setup is forming.

        Returns:
            {
                "early_score": int (0-100),
                "signals": list[str],
                "is_watch_worthy": bool,
            }

        Scoring:
          - EMA compression releasing (0.05-0.20% distance): +15
          - Volume pre-build (1.3-1.8x avg): +10
          - Volume building (1.0-1.3x with momentum): +5
          - Liquidity sweep detected: +15
          - VWAP interaction (price near VWAP): +10
          - BOS/CHoCH forming: +10
          - RSI positioned well (not extended): +5
        """
        score = 0
        signals = []

        if not settings.V17_ANTICIPATION_ENABLED:
            return {"early_score": 0, "signals": [], "is_watch_worthy": False}

        # 1. EMA compression release detection
        # Very tight EMAs that are starting to separate = setup forming
        if ema9 > 0 and ema21 > 0:
            dist = ema_distance_pct if ema_distance_pct > 0 else (
                abs(ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0
            )
            if 0.03 <= dist <= 0.20:
                # Tight compression — check if curling in right direction
                if side == "BUY" and ema9 >= ema21:
                    score += 15
                    signals.append(f"EMA compression releasing bullish ({dist:.2f}%)")
                elif side == "SELL" and ema9 <= ema21:
                    score += 15
                    signals.append(f"EMA compression releasing bearish ({dist:.2f}%)")
                else:
                    # Compression exists but direction not confirmed yet
                    score += 8
                    signals.append(f"EMA compression detected ({dist:.2f}%)")

        # 2. Volume pre-build (building but not spiking yet)
        if volume_ratio >= 1.3 and volume_ratio <= 1.8:
            score += 10
            signals.append(f"Volume pre-building {volume_ratio:.1f}x avg")
        elif volume_ratio >= 1.1 and volume_ratio < 1.3:
            score += 5
            signals.append(f"Volume building {volume_ratio:.1f}x avg")

        # 3. Liquidity sweep detection (institutional catalyst)
        if liquidity_sweep:
            if (side == "BUY" and sweep_direction == "BULL_SWEEP") or \
               (side == "SELL" and sweep_direction == "BEAR_SWEEP"):
                score += 15
                signals.append(f"Liquidity sweep: {sweep_direction}")
            elif sweep_direction != "NONE":
                score += 8
                signals.append(f"Liquidity sweep (counter): {sweep_direction}")

        # 4. VWAP interaction (price touching/crossing VWAP)
        if vwap > 0 and price > 0:
            vwap_dist = abs(price - vwap) / vwap * 100
            if vwap_dist <= 0.15:
                score += 10
                signals.append(f"Price at VWAP ({vwap_dist:.2f}%)")
            elif vwap_dist <= 0.30:
                score += 5
                signals.append(f"Price near VWAP ({vwap_dist:.2f}%)")

        # 5. BOS/CHoCH forming (market structure shift)
        if bos_detected:
            score += 10
            signals.append("Break of Structure detected")
        elif choch_detected:
            score += 10
            signals.append("Change of Character detected")

        # 6. RSI positioned well (not overextended)
        if side == "BUY" and 35 <= rsi <= 55:
            score += 5
            signals.append(f"RSI well-positioned for LONG ({rsi:.0f})")
        elif side == "SELL" and 45 <= rsi <= 65:
            score += 5
            signals.append(f"RSI well-positioned for SHORT ({rsi:.0f})")

        is_watch = score >= settings.V17_WATCH_MIN_EARLY_SCORE

        if is_watch:
            logger.info(
                f"  [V17 EARLY] {side} early_score={score} | "
                f"{', '.join(signals)}"
            )

        return {
            "early_score": min(score, 100),
            "signals": signals,
            "is_watch_worthy": is_watch,
        }


# Singleton
confidence_engine = ConfidenceEngine()

