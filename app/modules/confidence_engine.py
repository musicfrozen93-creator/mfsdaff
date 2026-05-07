"""
V17 Confidence Engine — Probabilistic Weighted Scoring (0-100)

Key changes from V7:
- Reduced over-filtering: thin body penalty -10→-4, chop reject 0.03%→0.015%
- MACD NONE now gives 3pts (neutral markets valid)
- SELL RSI ideal range widened to 35-60
- BTC NEUTRAL alignment gives 4pts (was 3)
- Momentum override bonus when strong trend + aligned candle
- Probabilistic fallback: soft score instead of hard 0
- Adaptive SHORT scoring improved
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceResult:
    score: int
    tier: str
    action: str
    reason: str
    breakdown: dict = field(default_factory=dict)
    rejected: bool = False
    reject_reason: str = ""
    bonus_applied: list = field(default_factory=list)
    penalty_applied: list = field(default_factory=list)


class ConfidenceEngine:

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 1: Trend Strength (25 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_trend(
        ema_fast: float, ema_slow: float, price: float,
        side: str, ema_dist_pct: float,
    ) -> tuple[float, str]:
        score = 0.0
        details = []

        if side == "BUY":
            if ema_fast > ema_slow:
                score += 8.0
                details.append("EMA aligned")
            else:
                details.append("EMA against")

            if price > ema_fast and price > ema_slow:
                score += 5.0
                details.append("price>EMAs")
            elif price > ema_slow:
                score += 2.0
                details.append("price>slowEMA")

            if ema_dist_pct > 0.5:
                score += 7.0
                details.append(f"strong trend {ema_dist_pct:.2f}%")
            elif ema_dist_pct > 0.2:
                score += 4.0
                details.append(f"moderate trend {ema_dist_pct:.2f}%")
            elif ema_dist_pct > 0.05:  # V17: lowered from 0.08
                score += 2.0
                details.append(f"weak trend {ema_dist_pct:.2f}%")

            if ema_dist_pct > 0.3 and ema_fast > ema_slow:
                score += 5.0
                details.append("trend momentum")
            elif ema_dist_pct > 0.12:  # V17: lowered from 0.15
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
            elif ema_dist_pct > 0.05:
                score += 2.0

            if ema_dist_pct > 0.3 and ema_fast < ema_slow:
                score += 5.0
                details.append("trend momentum")
            elif ema_dist_pct > 0.12:
                score += 2.5

        return min(score, 25.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 2: Volume Quality (10 points max) — V19: reduced from 20 to stop over-penalizing
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_volume(
        volume_ratio: float, volume_spike: bool,
    ) -> tuple[float, str]:
        score = 0.0
        details = []

        if volume_spike:
            score += 4.0  # V19: was 8.0
            details.append("volume spike")

        # V17: Lowered ratio thresholds — adaptive
        if volume_ratio >= 2.0:
            score += 4.0  # V19: was 8.0
            details.append(f"extreme vol {volume_ratio:.1f}x")
        elif volume_ratio >= 1.3:
            score += 3.0  # V19: was 6.0
            details.append(f"high vol {volume_ratio:.1f}x")
        elif volume_ratio >= 0.9:
            score += 2.0  # V19: was 4.0 — normal vol should not penalize
            details.append(f"normal vol {volume_ratio:.1f}x")
        elif volume_ratio >= 0.6:
            score += 1.0  # V19: was 2.0
            details.append(f"low vol {volume_ratio:.1f}x")
        else:
            score += 0.5  # V19: give 0.5 even for very low vol — stop zero-score
            details.append(f"very low vol {volume_ratio:.1f}x")

        # Consistency bonus
        if volume_ratio >= 1.1:  # V17: lowered from 1.2
            score += 2.0  # V19: was 4.0
            details.append("consistent flow")

        return min(score, 10.0), " | ".join(details)  # V19: cap at 10 (was 20)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 3: Momentum (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_momentum(
        rsi: float, macd_crossover: str, candle_type: str, side: str,
    ) -> tuple[float, str]:
        score = 0.0
        details = []

        if side == "BUY":
            if 40 <= rsi <= 65:  # V17: widened from 45-65
                score += 5.0
                details.append(f"RSI ideal {rsi:.0f}")
            elif 30 <= rsi < 40:  # V17: widened
                score += 3.5
                details.append(f"RSI recovering {rsi:.0f}")
            elif 65 < rsi <= 78:
                score += 2.0
                details.append(f"RSI high {rsi:.0f}")
            elif rsi > 78:
                score += 0.0
                details.append(f"RSI overbought {rsi:.0f}")
            elif rsi < 30:
                score += 2.0  # V17: raised — oversold is opportunity
                details.append(f"RSI oversold {rsi:.0f}")

            if macd_crossover == "BULLISH":
                score += 5.0
                details.append("MACD bullish cross")
            elif macd_crossover == "NONE":
                score += 3.0  # V17: raised from 2.0 — neutral is valid
                details.append("MACD neutral")

            if candle_type == "BULLISH":
                score += 5.0
                details.append("bullish candle")
            elif candle_type == "DOJI":
                score += 1.5  # V17: raised from 1.0

        else:  # SELL
            if 35 <= rsi <= 60:  # V17: widened from 35-55
                score += 5.0
                details.append(f"RSI ideal {rsi:.0f}")
            elif 60 < rsi <= 70:  # V17: widened
                score += 3.5
                details.append(f"RSI elevated {rsi:.0f}")
            elif 25 <= rsi < 35:
                score += 2.0
                details.append(f"RSI low {rsi:.0f}")
            elif rsi < 25:
                score += 0.0
                details.append(f"RSI oversold {rsi:.0f}")
            elif rsi > 70:
                score += 2.0  # V17: raised — overbought is SHORT opportunity
                details.append(f"RSI overbought {rsi:.0f}")

            if macd_crossover == "BEARISH":
                score += 5.0
                details.append("MACD bearish cross")
            elif macd_crossover == "NONE":
                score += 3.0  # V17: raised from 2.0
                details.append("MACD neutral")

            if candle_type == "BEARISH":
                score += 5.0
                details.append("bearish candle")
            elif candle_type == "DOJI":
                score += 1.5

        return min(score, 15.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 4: Structure / S&R (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_structure(
        bb_position: str, is_pullback: bool, is_rejection: bool,
        vwap: float, price: float, side: str,
    ) -> tuple[float, str]:
        score = 0.0
        details = []

        if side == "BUY":
            if price > vwap:
                score += 4.0
                details.append("above VWAP")
            elif price > vwap * 0.997:  # V17: slightly wider VWAP tolerance
                score += 2.5
                details.append("near VWAP")

            if bb_position == "LOWER":
                score += 4.0
                details.append("at BB lower (bounce zone)")
            elif bb_position == "MID":
                score += 3.0
                details.append("BB mid")

            if is_pullback:
                score += 5.0
                details.append("pullback entry")

            if is_rejection:
                score -= 1.5  # V17: reduced penalty from -2.0
                details.append("rejection (mild neg)")

        else:  # SELL
            if price < vwap:
                score += 4.0
                details.append("below VWAP")
            elif price < vwap * 1.003:  # V17: wider tolerance
                score += 2.5
                details.append("near VWAP")

            if bb_position == "UPPER":
                score += 4.0
                details.append("at BB upper (rejection zone)")
            elif bb_position == "MID":
                score += 3.0
                details.append("BB mid")

            # V17: Both rejection AND pullback can score for SELL
            if is_rejection:
                score += 5.0
                details.append("rejection entry")
            elif is_pullback:
                score += 3.0  # V17: pullback also valid for SELL (bearish retest)
                details.append("pullback retest (SELL)")

        return max(0.0, min(score, 15.0)), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 5: BTC Market Alignment (15 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_btc_alignment(
        htf_trend: str, btc_trend: str, side: str,
    ) -> tuple[float, str]:
        score = 0.0
        details = []

        if side == "BUY":
            if htf_trend == "BULLISH":
                score += 8.0
                details.append("HTF bullish ✓")
            elif htf_trend == "NEUTRAL":
                score += 4.0  # V17: raised from 3.0
                details.append("HTF neutral")
            else:
                score += 1.0  # V17: not 0 — partial credit even against HTF
                details.append("HTF bearish ✗")

            if btc_trend in ("BULLISH", "TRENDING_BULL"):
                score += 7.0
                details.append("BTC bullish ✓")
            elif btc_trend in ("NEUTRAL", "SIDEWAYS_RANGE", ""):
                score += 4.0  # V17: raised from 3.0
                details.append("BTC neutral")
            else:
                score += 1.5  # V17: not 0
                details.append("BTC bearish ✗")

        else:  # SELL
            if htf_trend == "BEARISH":
                score += 8.0
                details.append("HTF bearish ✓")
            elif htf_trend == "NEUTRAL":
                score += 4.0  # V17: raised from 3.0
                details.append("HTF neutral")
            else:
                score += 1.0
                details.append("HTF bullish ✗")

            if btc_trend in ("BEARISH", "TRENDING_BEAR"):
                score += 7.0
                details.append("BTC bearish ✓")
            elif btc_trend in ("NEUTRAL", "SIDEWAYS_RANGE", ""):
                score += 4.0  # V17: raised from 3.0
                details.append("BTC neutral")
            else:
                score += 1.5
                details.append("BTC bullish ✗")

        return min(score, 15.0), " | ".join(details)

    # ═══════════════════════════════════════════════════════════════════
    # PILLAR 6: Spread / Liquidity (10 points max)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _score_spread(spread_pct: float) -> tuple[float, str]:
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
    # ENTRY QUALITY FILTERS — V17 recalibrated
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
        total_range = high - low if high > low else 0.001
        upper_wick = high - max(open_price, close_price)
        lower_wick = min(open_price, close_price) - low

        # ── Hard Rejects (kept strict where it matters) ───────────────

        # 1. Candle overextended (body > 4×ATR) — unchanged
        if atr > 0 and body > atr * 4.0:
            return False, f"Overextended candle: body={body:.4f} > 4xATR={atr*4.0:.4f}", 0

        # 2. Spread too wide (> 0.20%) — unchanged
        if spread_pct > 0.20:
            return False, f"Spread too wide: {spread_pct:.3f}% > 0.20%", 0

        # 3. Extreme volatility — unchanged
        if atr_pct > 5.0:
            return False, f"Extreme volatility: ATR%={atr_pct:.2f}% > 5%", 0

        # 4. Fake pump/dump (wick > 3.5x body) — V18-debug: relaxed from 3.0x
        if side == "BUY" and body > 0:
            if upper_wick > body * 3.5:
                return False, "Fake pump: upper wick > 3.5x body (rejection)", 0
        elif side == "SELL" and body > 0:
            if lower_wick > body * 3.5:
                return False, "Fake dump: lower wick > 3.5x body (bounce)", 0

        # 5. Very low volume (< 0.15x) — V18-debug: relaxed from 0.25x
        if volume_ratio < 0.15:
            return False, f"Very low volume: {volume_ratio:.1f}x < 0.15x avg", 0

        # 6. Extreme chop — V18-debug: relaxed from 0.015% to 0.008%
        if ema_dist_pct < 0.008:
            return False, f"Extreme chop: EMA dist={ema_dist_pct:.3f}% < 0.008%", 0

        # ── Soft Penalties — V17 reduced aggressiveness ───────────────
        penalty = 0

        # 7. Wick trap against side
        if side == "BUY" and upper_wick > body * 2:
            penalty += 5  # V17: reduced from 8
        elif side == "SELL" and lower_wick > body * 2:
            penalty += 5

        # 8. Moderate overextension (body > 1.8×ATR)
        if atr > 0 and body > atr * 1.8:
            penalty += 4  # V17: reduced from 5

        # 9. Spread warning zone
        if 0.08 < spread_pct <= 0.12:
            penalty += 2  # V17: reduced from 3

        # 10. Thin-body — V17: massively reduced from 10 to 4
        if total_range > 0 and body > 0 and (body / total_range) < 0.30:
            penalty += 4  # WAS 10 — this was the #1 killer

        # 11. Wide spread during breakout
        if spread_pct > 0.10 and ema_dist_pct > 0.3:
            penalty += 3  # V17: reduced from 5

        # V17: Momentum override — cancel penalties if strong trend
        if ema_dist_pct > 0.4 and volume_ratio > 1.2:
            penalty = max(0, penalty - 5)  # Strong momentum partially negates penalties

        return True, "", penalty

    # ═══════════════════════════════════════════════════════════════════
    # ENTRY PATTERN BONUSES — V17 enhanced SHORT bonuses
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def check_entry_patterns(
        is_pullback: bool, is_rejection: bool,
        htf_trend: str, volume_spike: bool,
        side: str,
    ) -> tuple[int, list[str]]:
        bonus = 0
        descriptions = []

        if side == "BUY":
            if is_pullback:
                bonus += 5
                descriptions.append("pullback entry +5")
            if htf_trend == "BULLISH":
                bonus += 3
                descriptions.append("HTF aligned +3")
            if volume_spike:
                bonus += 2
                descriptions.append("volume confirmed +2")

        else:  # SELL — V17: enhanced SHORT bonuses
            if is_rejection:
                bonus += 5
                descriptions.append("rejection entry +5")
            if is_pullback:  # V17: pullback/retest also valid for SELL
                bonus += 3
                descriptions.append("bearish retest +3")
            if htf_trend == "BEARISH":
                bonus += 3
                descriptions.append("HTF aligned +3")
            elif htf_trend == "NEUTRAL":
                bonus += 1  # V17: partial bonus for neutral HTF
                descriptions.append("HTF neutral +1")
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
        ema_fast: float,
        ema_slow: float,
        price: float,
        ema_dist_pct: float,
        volume_ratio: float,
        volume_spike: bool,
        rsi: float,
        macd_crossover: str,
        candle_type: str,
        bb_position: str,
        is_pullback: bool,
        is_rejection: bool,
        vwap: float,
        htf_trend: str,
        btc_trend: str = "",
        spread_pct: float = 0.0,
        body: float = 0.0,
        atr: float = 0.0,
        atr_pct: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        open_price: float = 0.0,
        close_price: float = 0.0,
    ) -> ConfidenceResult:

        # Step 1: Entry quality filter
        eq_passed, eq_reason, eq_penalty = self.check_entry_quality(
            body=body, atr=atr, spread_pct=spread_pct,
            high=high, low=low, open_price=open_price,
            close_price=close_price, volume_ratio=volume_ratio,
            ema_dist_pct=ema_dist_pct, side=side, atr_pct=atr_pct,
        )

        if not eq_passed:
            logger.info(f"  ❌ V18 HARD REJECT [{side}]: {eq_reason}")
            return ConfidenceResult(
                score=0, tier="NO_TRADE", action="HOLD",
                reason=f"Entry quality rejected: {eq_reason}",
                rejected=True, reject_reason=eq_reason,
            )

        # Step 2: Calculate 6 pillars
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

        # Step 3: Sum (V19: pillars total 90 max after volume rebalance)
        raw_score = (
            trend_score + volume_score + momentum_score
            + structure_score + btc_score + spread_score
        )
        # V19: Normalize from 90-point scale back to 100-point scale
        # This ensures the threshold system still works correctly after volume reduction
        raw_score = raw_score * (100.0 / 90.0)

        # Step 4: Entry pattern bonuses
        bonus, bonus_descriptions = self.check_entry_patterns(
            is_pullback, is_rejection, htf_trend, volume_spike, side,
        )
        raw_score += bonus

        # Step 5: Apply penalty
        penalty_descriptions = []
        if eq_penalty > 0:
            raw_score -= eq_penalty
            penalty_descriptions.append(f"entry quality penalty -{eq_penalty}")

        # V17: Momentum override bonus — strong trend + aligned candle
        if ema_dist_pct > 0.35 and volume_ratio > 1.15:
            if (side == "BUY" and candle_type == "BULLISH") or \
               (side == "SELL" and candle_type == "BEARISH"):
                raw_score += 5.0
                bonus_descriptions.append("momentum override +5")

        # Step 6: Clamp to 0-95
        final_score = max(0, min(int(round(raw_score)), 95))

        # Step 7: Tier — V19: added WEAK tier to preserve directional bias
        if final_score >= 88:
            tier = "ELITE"
        elif final_score >= 80:
            tier = "STRONG"
        elif final_score >= 70:
            tier = "TRADABLE"
        elif final_score >= 60:
            tier = "MODERATE"
        elif final_score >= 48:
            tier = "WEAK"  # V19: was NO_TRADE — now preserves direction
        else:
            tier = "NO_TRADE"

        # Step 8: Action — V19: threshold lowered to 48 (was 55)
        # Preserves directional bias for weak signals instead of destroying with HOLD
        action = side if final_score >= 48 else "HOLD"

        reason_parts = [
            f"V17 confidence={final_score} [{tier}]",
            f"T:{trend_score:.0f}/25",
            f"V:{volume_score:.0f}/10",
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

        logger.info(
            f"  📊 V19 Confidence [{side}]: {final_score} [{tier}] | "
            f"T={trend_score:.0f}/25 V={volume_score:.0f}/10 M={momentum_score:.0f}/15 "
            f"S={structure_score:.0f}/15 B={btc_score:.0f}/15 L={spread_score:.0f}/10 "
            f"bonus={bonus} penalty={eq_penalty} raw={raw_score:.0f} action={action}"
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
