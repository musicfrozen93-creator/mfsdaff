"""
V7 Confidence Engine — Weighted Normalized Scoring (0-100 Hard Cap)

Replaces the old percentage-based scoring with a 6-pillar weighted system.
Every engine (Scalp, Swing, Sniper) uses this module for consistent scoring.

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
  70-79  = Tradable
  80-89  = Strong
  90-100 = Elite

Entry Quality Filters:
  Hard-reject conditions that block bad entries regardless of score.
"""

import logging
from dataclasses import dataclass, field

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

        # 1. Candle overextended (body > 4×ATR — relaxed from 2.5x for scalp compatibility)
        if atr > 0 and body > atr * 4.0:
            return False, f"Overextended candle: body={body:.4f} > 4×ATR={atr*4.0:.4f}", 0

        # 2. Spread too wide (> 0.20% — relaxed from 0.12% to allow more scalp opportunities)
        if spread_pct > 0.20:
            return False, f"Spread too wide: {spread_pct:.3f}% > 0.20%", 0

        # 3. Extreme volatility
        if atr_pct > 5.0:
            return False, f"Extreme volatility: ATR%={atr_pct:.2f}% > 5%", 0

        # 4. Fake pump detection (huge wick > 3×body in trade direction)
        if side == "BUY" and body > 0:
            if upper_wick > body * 3:
                return False, "Fake pump: massive upper wick (rejection)", 0
        elif side == "SELL" and body > 0:
            if lower_wick > body * 3:
                return False, "Fake dump: massive lower wick (bounce)", 0

        # 5. Low volume move (volume < 0.3x average — relaxed from 0.5x)
        if volume_ratio < 0.3:
            return False, f"Very low volume: {volume_ratio:.1f}x < 0.3x avg", 0

        # 6. Sideways chop (EMA distance < 0.02% — tightened from 0.05% to reduce over-rejection)
        if ema_dist_pct < 0.02:
            return False, f"Extreme chop: EMA dist={ema_dist_pct:.3f}% < 0.02%", 0

        # ── Soft Penalties (reduce confidence but don't reject) ───────

        penalty = 0

        # 7. Wick trap against side
        if side == "BUY" and upper_wick > body * 2:
            penalty += 8
        elif side == "SELL" and lower_wick > body * 2:
            penalty += 8

        # 8. Moderate overextension (body > 1.8×ATR but < 2.5×ATR)
        if atr > 0 and body > atr * 1.8:
            penalty += 5

        # 9. Spread warning zone (0.08% - 0.12%)
        if 0.08 < spread_pct <= 0.12:
            penalty += 3

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
    ) -> ConfidenceResult:
        """
        V7: Calculate weighted confidence score from 6 pillars.
        
        Returns ConfidenceResult with:
          - score: 0-100 (HARD CLAMPED, never exceeds 100)
          - tier: NO_TRADE | WEAK | TRADABLE | STRONG | ELITE
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

        # ── Step 6: HARD CLAMP to 0-100 ──────────────────────────────
        final_score = max(0, min(int(round(raw_score)), 100))

        # ── Step 7: Determine tier ────────────────────────────────────
        if final_score >= 90:
            tier = "ELITE"
        elif final_score >= 80:
            tier = "STRONG"
        elif final_score >= 70:
            tier = "TRADABLE"
        elif final_score >= 60:
            tier = "WEAK"
        else:
            tier = "NO_TRADE"

        # ── Step 8: Determine action ──────────────────────────────────
        action = side if final_score >= 60 else "HOLD"

        # ── Build reason string ───────────────────────────────────────
        reason_parts = [
            f"V7 confidence={final_score} [{tier}]",
            f"T:{trend_score:.0f}/25",
            f"V:{volume_score:.0f}/20",
            f"M:{momentum_score:.0f}/15",
            f"S:{structure_score:.0f}/15",
            f"B:{btc_score:.0f}/15",
            f"L:{spread_score:.0f}/10",
        ]
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
        }

        logger.debug(
            f"  V7 Confidence: {final_score} [{tier}] | "
            f"T={trend_score:.0f} V={volume_score:.0f} M={momentum_score:.0f} "
            f"S={structure_score:.0f} B={btc_score:.0f} L={spread_score:.0f} "
            f"bonus={bonus} penalty={eq_penalty}"
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


# Singleton
confidence_engine = ConfidenceEngine()
