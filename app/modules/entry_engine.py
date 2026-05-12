"""
V15 Entry Engine — Intelligent Limit Entry Zone Calculator

Replaces market-price entries with engineered limit entry zones.

Flow:
  1. Determine directional bias (from AI engine)
  2. Calculate optimal retracement entry zone
  3. Score entry quality (0-100)
  4. Output: entry_zone_low, entry_zone_high, ideal_entry, invalidation_price

Entry Zone Sources (LONG):
  - EMA21 retest zone
  - VWAP reclaim zone
  - Nearest support zone
  - Fibonacci 0.382/0.618 retracement
  - Fair value gap (FVG)
  - Liquidity sweep zone

Entry Zone Sources (SHORT):
  - EMA21 rejection zone
  - VWAP rejection zone
  - Nearest resistance zone
  - Fibonacci retracement
  - Fair value gap
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EntryZone:
    """Computed limit entry zone with quality scoring."""
    zone_low: float               # Bottom of entry zone
    zone_high: float              # Top of entry zone
    ideal_entry: float            # Best limit entry within zone
    invalidation: float           # Price that invalidates the setup
    current_price: float          # Market price at time of analysis
    entry_quality_score: int      # 0-100
    zone_width_pct: float         # Zone width as % of price
    zone_sources: list            # What created this zone
    distance_from_market_pct: float  # How far ideal entry is from market price
    rr_at_ideal: float            # R:R if entering at ideal entry
    side: str                     # BUY | SELL


@dataclass
class EntryQualityResult:
    """Detailed entry quality breakdown."""
    total_score: int              # 0-100
    ema_distance_score: int       # 0-15
    vwap_distance_score: int      # 0-15
    sr_proximity_score: int       # 0-15
    rr_quality_score: int         # 0-15
    pullback_quality_score: int   # 0-10
    candle_extension_score: int   # 0-10
    atr_extension_score: int      # 0-10
    wick_rejection_score: int     # 0-10
    penalties: list               # Applied penalties
    bonuses: list                 # Applied bonuses


class EntryEngine:
    """
    V15 Limit Entry Zone Calculator.
    Generates intelligent entry zones instead of market-price entries.
    """

    def calculate_entry_zone(
        self,
        side: str,
        current_price: float,
        ema9: float,
        ema21: float,
        vwap: float,
        atr: float,
        nearest_support: float,
        nearest_resistance: float,
        bb_lower: float = 0.0,
        bb_upper: float = 0.0,
        swing_low: float = 0.0,
        swing_high: float = 0.0,
        tp_price: float = 0.0,
        sl_price: float = 0.0,
    ) -> EntryZone:
        """
        Calculate optimal limit entry zone based on technical levels.

        For LONG: entry zone is BELOW current price (buy on pullback)
        For SHORT: entry zone is ABOVE current price (sell on rally)
        """
        if side == "BUY":
            return self._calc_long_zone(
                current_price, ema9, ema21, vwap, atr,
                nearest_support, nearest_resistance,
                bb_lower, swing_low, tp_price, sl_price,
            )
        else:
            return self._calc_short_zone(
                current_price, ema9, ema21, vwap, atr,
                nearest_support, nearest_resistance,
                bb_upper, swing_high, tp_price, sl_price,
            )

    def _ensure_atr(self, price: float, atr: float) -> float:
        """V16: Ensure ATR is never zero/tiny. Synthesize from price if needed."""
        if atr > 0 and (atr / price * 100) > 0.05:
            return atr
        # Synthesize ATR from config fallback percentage
        return price * settings.V16_FALLBACK_ATR_PCT / 100.0

    def _enforce_zone_constraints(
        self, ideal: float, zone_low: float, zone_high: float,
        invalidation: float, price: float, atr: float, side: str,
    ) -> tuple:
        """V16: Enforce minimum zone width, distance from market, and invalidation distance."""
        min_width = atr * settings.V16_MIN_ZONE_WIDTH_ATR
        min_dist = atr * settings.V16_MIN_ENTRY_DISTANCE_ATR
        min_inv_dist = atr * settings.V16_MIN_INVALIDATION_ATR

        # Enforce minimum zone width
        current_width = zone_high - zone_low
        if current_width < min_width:
            expand = (min_width - current_width) / 2
            zone_low -= expand
            zone_high += expand

        if side == "BUY":
            # Entry zone must be BELOW market price by at least min_dist
            if ideal > price - min_dist:
                ideal = price - min_dist
                zone_high = min(zone_high, price - min_dist * 0.5)
                zone_low = min(zone_low, ideal - (zone_high - ideal))
            # Invalidation must be below zone_low by min_inv_dist
            if invalidation > zone_low - min_inv_dist:
                invalidation = zone_low - min_inv_dist
        else:
            # Entry zone must be ABOVE market price by at least min_dist
            if ideal < price + min_dist:
                ideal = price + min_dist
                zone_low = max(zone_low, price + min_dist * 0.5)
                zone_high = max(zone_high, ideal + (ideal - zone_low))
            # Invalidation must be above zone_high by min_inv_dist
            if invalidation < zone_high + min_inv_dist:
                invalidation = zone_high + min_inv_dist

        return ideal, zone_low, zone_high, invalidation

    def _calc_long_zone(
        self,
        price: float, ema9: float, ema21: float, vwap: float, atr: float,
        support: float, resistance: float,
        bb_lower: float, swing_low: float,
        tp_price: float, sl_price: float,
    ) -> EntryZone:
        """V16: Calculate LONG entry zone (below market price) with real depth."""
        atr = self._ensure_atr(price, atr)
        candidates = []
        sources = []

        # 1. EMA21 retest zone
        if ema21 > 0 and ema21 < price:
            candidates.append(ema21 + atr * 0.1)
            sources.append("EMA21 retest")

        # 2. EMA9 retest (shallower pullback)
        if ema9 > 0 and ema9 < price:
            candidates.append(ema9 + atr * 0.05)
            sources.append("EMA9 retest")

        # 3. VWAP reclaim zone
        if vwap > 0 and vwap < price:
            candidates.append(vwap + atr * 0.1)
            sources.append("VWAP reclaim")

        # 4. Support zone
        if support > 0 and support < price:
            candidates.append(support + atr * 0.2)
            sources.append("Support zone")

        # 5. Fibonacci 0.382 / 0.618 retracement
        if swing_low > 0 and swing_low < price:
            fib_range = price - swing_low
            fib_382 = price - fib_range * 0.382
            fib_618 = price - fib_range * 0.618
            candidates.append(fib_382)
            sources.append("Fib 0.382")
            if fib_618 > support * 0.99 if support > 0 else True:
                candidates.append(fib_618)
                sources.append("Fib 0.618")

        # 6. Bollinger Band lower zone
        if bb_lower > 0 and bb_lower < price:
            candidates.append(bb_lower + atr * 0.2)
            sources.append("BB lower zone")

        # 7. V16: Fair Value Gap zone (pullback to 50% of recent impulse)
        fvg_zone = price - atr * 1.0
        if fvg_zone > 0 and fvg_zone < price * 0.99:
            candidates.append(fvg_zone)
            sources.append("FVG pullback")

        # V16: If no real candidates, generate ATR-based retracement levels
        if not candidates:
            candidates.append(price - atr * 1.2)
            sources.append("ATR retracement 1.2x")
            candidates.append(price - atr * 0.8)
            sources.append("ATR retracement 0.8x")

        # Filter: only levels below current price
        valid = [(c, s) for c, s in zip(candidates, sources) if c < price]
        if not valid:
            ideal = price - atr * 0.8
            zone_low = ideal - atr * 0.3
            zone_high = ideal + atr * 0.3
            invalidation = zone_low - atr * 1.2
            ideal, zone_low, zone_high, invalidation = self._enforce_zone_constraints(
                ideal, zone_low, zone_high, invalidation, price, atr, "BUY"
            )
            return EntryZone(
                zone_low=round(zone_low, 6), zone_high=round(zone_high, 6),
                ideal_entry=round(ideal, 6), invalidation=round(invalidation, 6),
                current_price=price, entry_quality_score=30,
                zone_width_pct=round(((zone_high - zone_low) / price) * 100, 3),
                zone_sources=["ATR offset (no levels found)"],
                distance_from_market_pct=round(((price - ideal) / price) * 100, 3),
                rr_at_ideal=0.0, side="BUY",
            )

        # Cluster nearby valid levels
        valid_prices = [v[0] for v in valid]
        valid_sources = [v[1] for v in valid]
        combined = sorted(zip(valid_prices, valid_sources), key=lambda x: -x[0])

        cluster = [combined[0]]
        for p, s in combined[1:]:
            if abs(p - cluster[0][0]) < atr * 1.5:
                cluster.append((p, s))

        cluster_prices = [c[0] for c in cluster]
        cluster_sources = [c[1] for c in cluster]

        ideal = sum(cluster_prices) / len(cluster_prices)
        zone_low = min(cluster_prices) - atr * 0.15
        zone_high = max(cluster_prices) + atr * 0.15
        invalidation = zone_low - atr * 1.2

        # V16: Enforce constraints
        ideal, zone_low, zone_high, invalidation = self._enforce_zone_constraints(
            ideal, zone_low, zone_high, invalidation, price, atr, "BUY"
        )

        # R:R at ideal entry
        rr = 0.0
        if sl_price > 0 and tp_price > 0 and ideal > sl_price:
            sl_dist = ideal - sl_price
            tp_dist = tp_price - ideal
            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

        dist_pct = ((price - ideal) / price) * 100
        width_pct = ((zone_high - zone_low) / price) * 100

        return EntryZone(
            zone_low=round(zone_low, 6), zone_high=round(zone_high, 6),
            ideal_entry=round(ideal, 6), invalidation=round(invalidation, 6),
            current_price=price, entry_quality_score=0,
            zone_width_pct=round(width_pct, 3), zone_sources=cluster_sources,
            distance_from_market_pct=round(dist_pct, 3),
            rr_at_ideal=rr, side="BUY",
        )

    def _calc_short_zone(
        self,
        price: float, ema9: float, ema21: float, vwap: float, atr: float,
        support: float, resistance: float,
        bb_upper: float, swing_high: float,
        tp_price: float, sl_price: float,
    ) -> EntryZone:
        """V16: Calculate SHORT entry zone (above market price) with real depth."""
        atr = self._ensure_atr(price, atr)
        candidates = []
        sources = []

        if ema21 > 0 and ema21 > price:
            candidates.append(ema21 - atr * 0.1)
            sources.append("EMA21 rejection")
        if ema9 > 0 and ema9 > price:
            candidates.append(ema9 - atr * 0.05)
            sources.append("EMA9 rejection")
        if vwap > 0 and vwap > price:
            candidates.append(vwap - atr * 0.1)
            sources.append("VWAP rejection")
        if resistance > 0 and resistance > price:
            candidates.append(resistance - atr * 0.2)
            sources.append("Resistance zone")
        if swing_high > 0 and swing_high > price:
            fib_range = swing_high - price
            fib_382 = price + fib_range * 0.382
            candidates.append(fib_382)
            sources.append("Fib 0.382")
        if bb_upper > 0 and bb_upper > price:
            candidates.append(bb_upper - atr * 0.2)
            sources.append("BB upper zone")

        # V16: Fair Value Gap zone (rally to 50% of recent impulse)
        fvg_zone = price + atr * 1.0
        if fvg_zone > price * 1.01:
            candidates.append(fvg_zone)
            sources.append("FVG rally")

        # V16: ATR-based fallback
        if not candidates:
            candidates.append(price + atr * 1.2)
            sources.append("ATR rally 1.2x")
            candidates.append(price + atr * 0.8)
            sources.append("ATR rally 0.8x")

        valid = [(c, s) for c, s in zip(candidates, sources) if c > price]
        if not valid:
            ideal = price + atr * 0.8
            zone_low = ideal - atr * 0.3
            zone_high = ideal + atr * 0.3
            invalidation = zone_high + atr * 1.2
            ideal, zone_low, zone_high, invalidation = self._enforce_zone_constraints(
                ideal, zone_low, zone_high, invalidation, price, atr, "SELL"
            )
            return EntryZone(
                zone_low=round(zone_low, 6), zone_high=round(zone_high, 6),
                ideal_entry=round(ideal, 6), invalidation=round(invalidation, 6),
                current_price=price, entry_quality_score=30,
                zone_width_pct=round(((zone_high - zone_low) / price) * 100, 3),
                zone_sources=["ATR offset (no levels found)"],
                distance_from_market_pct=round(((ideal - price) / price) * 100, 3),
                rr_at_ideal=0.0, side="SELL",
            )

        combined = sorted(zip([v[0] for v in valid], [v[1] for v in valid]),
                          key=lambda x: x[0])
        cluster = [combined[0]]
        for p, s in combined[1:]:
            if abs(p - cluster[0][0]) < atr * 1.5:
                cluster.append((p, s))

        cluster_prices = [c[0] for c in cluster]
        cluster_sources = [c[1] for c in cluster]
        ideal = sum(cluster_prices) / len(cluster_prices)
        zone_low = min(cluster_prices) - atr * 0.15
        zone_high = max(cluster_prices) + atr * 0.15
        invalidation = zone_high + atr * 1.2

        # V16: Enforce constraints
        ideal, zone_low, zone_high, invalidation = self._enforce_zone_constraints(
            ideal, zone_low, zone_high, invalidation, price, atr, "SELL"
        )

        rr = 0.0
        if sl_price > 0 and tp_price > 0 and sl_price > ideal:
            sl_dist = sl_price - ideal
            tp_dist = ideal - tp_price
            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

        dist_pct = ((ideal - price) / price) * 100
        width_pct = ((zone_high - zone_low) / price) * 100

        return EntryZone(
            zone_low=round(zone_low, 6), zone_high=round(zone_high, 6),
            ideal_entry=round(ideal, 6), invalidation=round(invalidation, 6),
            current_price=price, entry_quality_score=0,
            zone_width_pct=round(width_pct, 3), zone_sources=cluster_sources,
            distance_from_market_pct=round(dist_pct, 3),
            rr_at_ideal=rr, side="SELL",
        )

    # ─── Entry Quality Scoring ────────────────────────────────────────

    def score_entry_quality(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        ema9: float,
        ema21: float,
        vwap: float,
        atr: float,
        atr_pct: float,
        rsi: float,
        nearest_support: float,
        nearest_resistance: float,
        candle_body: float,
        upper_wick: float,
        lower_wick: float,
        volume_ratio: float,
        tp_price: float = 0.0,
        sl_price: float = 0.0,
        exhaustion_score: int = 0,
        reversal_risk: int = 0,
        btc_relative_strength: float = 1.0,
    ) -> EntryQualityResult:
        """
        Score entry quality on 0-100 scale.
        This score heavily impacts final confidence.
        """
        penalties = []
        bonuses = []

        # ── 1. EMA Distance Score (0-15) ──────────────────────────────
        ema_score = 0
        if ema21 > 0:
            ema_dist_pct = abs(current_price - ema21) / ema21 * 100
            if side == "BUY":
                if current_price <= ema21 * 1.003:  # At or below EMA21
                    ema_score = 15
                    bonuses.append("At EMA21 retest (+15)")
                elif ema_dist_pct < 0.3:
                    ema_score = 12
                elif ema_dist_pct < 0.8:
                    ema_score = 8
                elif ema_dist_pct < 1.5:
                    ema_score = 4
                else:
                    ema_score = 0
                    penalties.append(f"Far from EMA21: {ema_dist_pct:.2f}%")
            else:  # SELL
                if current_price >= ema21 * 0.997:
                    ema_score = 15
                    bonuses.append("At EMA21 rejection (+15)")
                elif ema_dist_pct < 0.3:
                    ema_score = 12
                elif ema_dist_pct < 0.8:
                    ema_score = 8
                elif ema_dist_pct < 1.5:
                    ema_score = 4
                else:
                    ema_score = 0
                    penalties.append(f"Far from EMA21: {ema_dist_pct:.2f}%")

        # ── 2. VWAP Distance Score (0-15) ─────────────────────────────
        vwap_score = 0
        if vwap > 0:
            vwap_dist_pct = abs(current_price - vwap) / vwap * 100
            if side == "BUY":
                if current_price <= vwap * 1.002:
                    vwap_score = 15
                    bonuses.append("At VWAP reclaim (+15)")
                elif vwap_dist_pct < 0.3:
                    vwap_score = 10
                elif vwap_dist_pct < 0.8:
                    vwap_score = 6
                else:
                    vwap_score = 2
            else:
                if current_price >= vwap * 0.998:
                    vwap_score = 15
                    bonuses.append("At VWAP rejection (+15)")
                elif vwap_dist_pct < 0.3:
                    vwap_score = 10
                elif vwap_dist_pct < 0.8:
                    vwap_score = 6
                else:
                    vwap_score = 2

        # ── 3. S/R Proximity Score (0-15) ─────────────────────────────
        sr_score = 0
        if side == "BUY":
            if nearest_support > 0:
                dist_sup = abs(current_price - nearest_support) / current_price * 100
                if dist_sup < 0.3:
                    sr_score = 15
                    bonuses.append("Near support (+15)")
                elif dist_sup < 0.8:
                    sr_score = 10
                elif dist_sup < 1.5:
                    sr_score = 5
            if nearest_resistance > 0:
                dist_res = abs(nearest_resistance - current_price) / current_price * 100
                if dist_res < 0.3:
                    sr_score = max(sr_score - 10, 0)
                    penalties.append("LONG into resistance (-10)")
        else:  # SELL
            if nearest_resistance > 0:
                dist_res = abs(nearest_resistance - current_price) / current_price * 100
                if dist_res < 0.3:
                    sr_score = 15
                    bonuses.append("Near resistance (+15)")
                elif dist_res < 0.8:
                    sr_score = 10
                elif dist_res < 1.5:
                    sr_score = 5
            if nearest_support > 0:
                dist_sup = abs(current_price - nearest_support) / current_price * 100
                if dist_sup < 0.3:
                    sr_score = max(sr_score - 10, 0)
                    penalties.append("SHORT into support (-10)")

        # ── 4. R:R Quality Score (0-15) ───────────────────────────────
        rr_score = 0
        if tp_price > 0 and sl_price > 0 and entry_price > 0:
            if side == "BUY":
                tp_dist = tp_price - entry_price
                sl_dist = entry_price - sl_price
            else:
                tp_dist = entry_price - tp_price
                sl_dist = sl_price - entry_price
            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            if rr >= 3.0:
                rr_score = 15
            elif rr >= 2.5:
                rr_score = 12
            elif rr >= 2.0:
                rr_score = 10
            elif rr >= 1.5:
                rr_score = 6
            elif rr >= 1.0:
                rr_score = 3
            else:
                rr_score = 0
                penalties.append(f"Poor R:R {rr:.2f}")

        # ── 5. Pullback Quality (0-10) ────────────────────────────────
        pullback_score = 0
        if side == "BUY" and ema9 > 0 and ema21 > 0:
            if ema9 > ema21 and current_price <= ema9 * 1.003:
                pullback_score = 10
                bonuses.append("Clean pullback to EMA9 in uptrend (+10)")
            elif ema9 > ema21 and current_price <= ema21 * 1.005:
                pullback_score = 8
                bonuses.append("Deep pullback to EMA21 in uptrend (+8)")
        elif side == "SELL" and ema9 > 0 and ema21 > 0:
            if ema9 < ema21 and current_price >= ema9 * 0.997:
                pullback_score = 10
                bonuses.append("Clean rally to EMA9 in downtrend (+10)")
            elif ema9 < ema21 and current_price >= ema21 * 0.995:
                pullback_score = 8

        # ── 6. Candle Extension Penalty (0-10) ────────────────────────
        candle_score = 10  # Start full, reduce for bad candles
        if atr > 0:
            body_atr = candle_body / atr if atr > 0 else 0
            if body_atr > 2.5:
                candle_score = 0
                penalties.append(f"Extreme candle {body_atr:.1f}x ATR")
            elif body_atr > 2.0:
                candle_score = 3
                penalties.append(f"Extended candle {body_atr:.1f}x ATR")
            elif body_atr > 1.5:
                candle_score = 6

        # ── 7. ATR Extension Penalty (0-10) ───────────────────────────
        atr_score = 10
        if atr_pct > 4.0:
            atr_score = 0
            penalties.append(f"Extreme volatility ATR%={atr_pct:.2f}%")
        elif atr_pct > 3.0:
            atr_score = 3
        elif atr_pct > 2.0:
            atr_score = 6

        # ── 8. Wick Rejection Quality (0-10) ──────────────────────────
        wick_score = 5  # Neutral default
        total_range = candle_body + upper_wick + lower_wick
        if total_range > 0:
            if side == "BUY":
                # Good: lower wick rejection (demand)
                if lower_wick > candle_body * 1.5 and upper_wick < candle_body * 0.5:
                    wick_score = 10
                    bonuses.append("Strong lower wick rejection (+10)")
                # Bad: upper wick rejection (supply)
                elif upper_wick > candle_body * 2.0:
                    wick_score = 0
                    penalties.append("Upper wick rejection on LONG")
            else:
                if upper_wick > candle_body * 1.5 and lower_wick < candle_body * 0.5:
                    wick_score = 10
                    bonuses.append("Strong upper wick rejection (+10)")
                elif lower_wick > candle_body * 2.0:
                    wick_score = 0
                    penalties.append("Lower wick rejection on SHORT")

        # ── Total ─────────────────────────────────────────────────────
        total = (ema_score + vwap_score + sr_score + rr_score +
                 pullback_score + candle_score + atr_score + wick_score)
        total = max(0, min(100, total))

        # ── Global penalties ──────────────────────────────────────────
        # Exhaustion penalty
        if exhaustion_score >= 60:
            penalty = int(exhaustion_score * 0.2)
            total = max(total - penalty, 10)
            penalties.append(f"Exhaustion penalty -{penalty}")

        # Reversal risk penalty
        if reversal_risk >= 50:
            penalty = int(reversal_risk * 0.15)
            total = max(total - penalty, 10)
            penalties.append(f"Reversal risk penalty -{penalty}")

        # BTC relative strength penalty/bonus
        if side == "BUY" and btc_relative_strength < 0.95:
            penalty = int((1.0 - btc_relative_strength) * 30)
            total = max(total - penalty, 10)
            penalties.append(f"Weak vs BTC ({btc_relative_strength:.3f}) -{penalty}")
        elif side == "SELL" and btc_relative_strength > 1.05:
            penalty = int((btc_relative_strength - 1.0) * 30)
            total = max(total - penalty, 10)
            penalties.append(f"Strong vs BTC on SHORT ({btc_relative_strength:.3f}) -{penalty}")
        elif side == "BUY" and btc_relative_strength > 1.05:
            bonus = min(int((btc_relative_strength - 1.0) * 20), 5)
            total = min(total + bonus, 100)
            bonuses.append(f"Outperforming BTC +{bonus}")

        # RSI exhaustion penalty
        if side == "BUY" and rsi > 72:
            penalty = int((rsi - 72) * 1.5)
            total = max(total - penalty, 10)
            penalties.append(f"RSI overbought {rsi:.0f} on LONG -{penalty}")
        elif side == "SELL" and rsi < 28:
            penalty = int((28 - rsi) * 1.5)
            total = max(total - penalty, 10)
            penalties.append(f"RSI oversold {rsi:.0f} on SHORT -{penalty}")

        return EntryQualityResult(
            total_score=total,
            ema_distance_score=ema_score,
            vwap_distance_score=vwap_score,
            sr_proximity_score=sr_score,
            rr_quality_score=rr_score,
            pullback_quality_score=pullback_score,
            candle_extension_score=candle_score,
            atr_extension_score=atr_score,
            wick_rejection_score=wick_score,
            penalties=penalties,
            bonuses=bonuses,
        )

    # ═══════════════════════════════════════════════════════════════════
    # V17: MOVE COMPLETION FILTER
    # Rejects signals where the move has already traveled too far
    # toward TP, or price is overextended from key MAs.
    # ═══════════════════════════════════════════════════════════════════

    def check_move_completion(
        self,
        side: str,
        current_price: float,
        ideal_entry: float,
        tp_price: float,
        ema21: float = 0.0,
        vwap: float = 0.0,
        strategy_type: str = "",
    ) -> 'MoveCompletionResult':
        """
        V17: Check if a setup is exhausted (move already completed).

        Returns MoveCompletionResult with:
          - is_exhausted: True if signal should be rejected
          - traveled_pct: how much of the TP distance is already gone
          - rejection_reason: human-readable reason

        Rejection triggers:
          1. Price already >35% (scalp) / >40% (swing) toward TP from ideal entry
          2. Price extended >2.0% from EMA21
          3. Price extended >1.5% from VWAP
        """
        reasons = []
        traveled_pct = 0.0
        ema_extension_pct = 0.0
        vwap_extension_pct = 0.0
        is_exhausted = False

        if not settings.V17_ANTICIPATION_ENABLED:
            return MoveCompletionResult(
                is_exhausted=False, traveled_pct=0.0,
                ema_extension_pct=0.0, vwap_extension_pct=0.0,
                rejection_reason="",
            )

        is_swing = strategy_type.startswith("swing")
        max_move_pct = settings.V17_SWING_MAX_MOVE_PCT if is_swing else settings.V17_SCALP_MAX_MOVE_PCT

        # ── Check 1: TP progress (how much of the move is done) ──────
        ref_entry = ideal_entry if ideal_entry > 0 else current_price
        if tp_price > 0 and ref_entry > 0:
            total_tp_dist = abs(tp_price - ref_entry)
            if total_tp_dist > 0:
                if side == "BUY":
                    already_traveled = max(current_price - ref_entry, 0)
                else:
                    already_traveled = max(ref_entry - current_price, 0)

                traveled_pct = (already_traveled / total_tp_dist) * 100

                if traveled_pct > max_move_pct:
                    is_exhausted = True
                    reasons.append(
                        f"Price already {traveled_pct:.0f}% toward TP "
                        f"(max {max_move_pct:.0f}%)"
                    )

        # ── Check 2: EMA21 extension ─────────────────────────────────
        if ema21 > 0 and current_price > 0:
            ema_extension_pct = abs(current_price - ema21) / ema21 * 100
            max_ema = settings.V17_MAX_EMA_EXTENSION_PCT

            # Only flag as extended if price is on the WRONG side of EMA
            # (for BUY: extended above EMA, for SELL: extended below EMA)
            if side == "BUY" and current_price > ema21 and ema_extension_pct > max_ema:
                is_exhausted = True
                reasons.append(
                    f"Price {ema_extension_pct:.1f}% above EMA21 "
                    f"(max {max_ema:.1f}%)"
                )
            elif side == "SELL" and current_price < ema21 and ema_extension_pct > max_ema:
                is_exhausted = True
                reasons.append(
                    f"Price {ema_extension_pct:.1f}% below EMA21 "
                    f"(max {max_ema:.1f}%)"
                )

        # ── Check 3: VWAP extension ──────────────────────────────────
        if vwap > 0 and current_price > 0:
            vwap_extension_pct = abs(current_price - vwap) / vwap * 100
            max_vwap = settings.V17_MAX_VWAP_EXTENSION_PCT

            if side == "BUY" and current_price > vwap and vwap_extension_pct > max_vwap:
                is_exhausted = True
                reasons.append(
                    f"Price {vwap_extension_pct:.1f}% above VWAP "
                    f"(max {max_vwap:.1f}%)"
                )
            elif side == "SELL" and current_price < vwap and vwap_extension_pct > max_vwap:
                is_exhausted = True
                reasons.append(
                    f"Price {vwap_extension_pct:.1f}% below VWAP "
                    f"(max {max_vwap:.1f}%)"
                )

        rejection_reason = " | ".join(reasons) if reasons else ""

        if is_exhausted:
            logger.info(
                f"  [V17 MOVE FILTER] {side}: EXHAUSTED — {rejection_reason} | "
                f"tp_progress={traveled_pct:.1f}% ema_ext={ema_extension_pct:.1f}% "
                f"vwap_ext={vwap_extension_pct:.1f}%"
            )

        return MoveCompletionResult(
            is_exhausted=is_exhausted,
            traveled_pct=round(traveled_pct, 1),
            ema_extension_pct=round(ema_extension_pct, 1),
            vwap_extension_pct=round(vwap_extension_pct, 1),
            rejection_reason=rejection_reason,
        )


@dataclass
class MoveCompletionResult:
    """V17: Result of move completion check."""
    is_exhausted: bool           # True = reject this signal
    traveled_pct: float          # % of TP distance already traveled
    ema_extension_pct: float     # % distance from EMA21
    vwap_extension_pct: float    # % distance from VWAP
    rejection_reason: str        # Human-readable rejection reason

