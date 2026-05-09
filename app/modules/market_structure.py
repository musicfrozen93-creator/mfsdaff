"""
V15 Market Structure Analyzer

Provides:
  1. Swing point detection (HH, HL, LH, LL)
  2. Trend structure classification (uptrend, downtrend, ranging)
  3. Break of Structure (BOS) / Change of Character (CHoCH) detection
  4. Support/Resistance zone mapping
  5. Exhaustion / pre-reversal detection
  6. Liquidity sweep detection
  7. BTC relative strength analysis
  8. BTC dominance / alt rotation context
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SwingPoint:
    index: int
    price: float
    type: str  # "HH", "HL", "LH", "LL", "HIGH", "LOW"


@dataclass
class SRZone:
    price: float
    strength: int          # 1-5 (number of touches/tests)
    zone_type: str         # "support" | "resistance"
    width: float           # zone width (price units)


@dataclass
class StructureResult:
    # Trend structure
    trend: str                      # "UPTREND" | "DOWNTREND" | "RANGING"
    trend_strength: int             # 0-100
    swing_points: list              # recent swing points
    bos_detected: bool              # Break of Structure
    choch_detected: bool            # Change of Character
    bos_direction: str              # "BULLISH" | "BEARISH" | "NONE"

    # S/R zones
    support_zones: list             # SRZone list
    resistance_zones: list          # SRZone list
    nearest_support: float
    nearest_resistance: float
    distance_to_support_pct: float  # % distance to nearest support
    distance_to_resistance_pct: float

    # Exhaustion
    exhaustion_score: int           # 0-100 (higher = more exhausted)
    exhaustion_signals: list        # list of detected exhaustion reasons
    is_exhausted: bool

    # Liquidity
    liquidity_sweep_detected: bool
    sweep_direction: str            # "BULL_SWEEP" | "BEAR_SWEEP" | "NONE"

    # BTC relative strength
    btc_relative_strength: float    # >1 = outperforming BTC, <1 = underperforming
    btc_correlation: float          # -1 to 1

    # Pre-reversal
    reversal_risk: int              # 0-100
    reversal_signals: list


class MarketStructureAnalyzer:
    """V15 Market structure analysis for intelligent entry engineering."""

    def __init__(self):
        self.base_url = settings.binance_base_url

    # ─── Main Analysis ────────────────────────────────────────────────

    async def analyze(
        self,
        symbol: str,
        candles: list,
        btc_candles: list = None,
    ) -> StructureResult:
        """
        Full market structure analysis from pre-fetched candle data.
        candles: list of [time, open, high, low, close, volume, ...]
        """
        if len(candles) < 30:
            return self._empty_result()

        closes = np.array([float(c[4]) for c in candles])
        highs = np.array([float(c[2]) for c in candles])
        lows = np.array([float(c[3]) for c in candles])
        opens = np.array([float(c[1]) for c in candles])
        volumes = np.array([float(c[5]) for c in candles])
        current_price = closes[-1]

        # 1. Swing points
        swing_points = self._detect_swing_points(highs, lows, lookback=5)

        # 2. Trend structure
        trend, trend_strength = self._classify_trend(swing_points, closes)

        # 3. BOS / CHoCH
        bos, choch, bos_dir = self._detect_structure_breaks(swing_points, current_price)

        # 4. S/R zones
        support_zones, resistance_zones = self._map_sr_zones(
            highs, lows, closes, current_price
        )
        nearest_sup = support_zones[0].price if support_zones else current_price * 0.97
        nearest_res = resistance_zones[0].price if resistance_zones else current_price * 1.03
        dist_sup = abs(current_price - nearest_sup) / current_price * 100
        dist_res = abs(nearest_res - current_price) / current_price * 100

        # 5. Exhaustion detection
        exhaustion_score, exhaustion_signals = self._detect_exhaustion(
            closes, highs, lows, opens, volumes, current_price
        )

        # 6. Liquidity sweep
        sweep_detected, sweep_dir = self._detect_liquidity_sweep(
            highs, lows, closes, swing_points
        )

        # 7. BTC relative strength
        btc_rs, btc_corr = 1.0, 0.0
        if btc_candles and len(btc_candles) >= 20:
            btc_rs, btc_corr = self._calc_btc_relative_strength(closes, btc_candles)

        # 8. Pre-reversal risk
        reversal_risk, reversal_signals = self._assess_reversal_risk(
            exhaustion_score, exhaustion_signals, bos, choch, bos_dir,
            trend, dist_sup, dist_res, sweep_detected, sweep_dir
        )

        return StructureResult(
            trend=trend,
            trend_strength=trend_strength,
            swing_points=swing_points[-10:],
            bos_detected=bos,
            choch_detected=choch,
            bos_direction=bos_dir,
            support_zones=support_zones[:5],
            resistance_zones=resistance_zones[:5],
            nearest_support=nearest_sup,
            nearest_resistance=nearest_res,
            distance_to_support_pct=round(dist_sup, 3),
            distance_to_resistance_pct=round(dist_res, 3),
            exhaustion_score=exhaustion_score,
            exhaustion_signals=exhaustion_signals,
            is_exhausted=exhaustion_score >= 60,
            liquidity_sweep_detected=sweep_detected,
            sweep_direction=sweep_dir,
            btc_relative_strength=round(btc_rs, 4),
            btc_correlation=round(btc_corr, 4),
            reversal_risk=reversal_risk,
            reversal_signals=reversal_signals,
        )

    # ─── 1. Swing Point Detection ─────────────────────────────────────

    def _detect_swing_points(
        self, highs: np.ndarray, lows: np.ndarray, lookback: int = 5
    ) -> list[SwingPoint]:
        """Detect swing highs and swing lows using lookback window."""
        points = []
        n = len(highs)

        for i in range(lookback, n - lookback):
            # Swing high: highest in window
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                points.append(SwingPoint(index=i, price=float(highs[i]), type="HIGH"))
            # Swing low: lowest in window
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                points.append(SwingPoint(index=i, price=float(lows[i]), type="LOW"))

        # Classify as HH/HL/LH/LL
        swing_highs = [p for p in points if p.type == "HIGH"]
        swing_lows = [p for p in points if p.type == "LOW"]

        for i in range(1, len(swing_highs)):
            if swing_highs[i].price > swing_highs[i - 1].price:
                swing_highs[i].type = "HH"
            else:
                swing_highs[i].type = "LH"

        for i in range(1, len(swing_lows)):
            if swing_lows[i].price > swing_lows[i - 1].price:
                swing_lows[i].type = "HL"
            else:
                swing_lows[i].type = "LL"

        # Merge and sort by index
        all_points = swing_highs + swing_lows
        all_points.sort(key=lambda p: p.index)
        return all_points

    # ─── 2. Trend Classification ──────────────────────────────────────

    def _classify_trend(
        self, swing_points: list[SwingPoint], closes: np.ndarray
    ) -> tuple[str, int]:
        """Classify trend from swing structure."""
        if len(swing_points) < 4:
            return "RANGING", 50

        recent = swing_points[-8:]
        highs = [p for p in recent if p.type in ("HH", "LH", "HIGH")]
        lows = [p for p in recent if p.type in ("HL", "LL", "LOW")]

        hh_count = sum(1 for p in highs if p.type == "HH")
        lh_count = sum(1 for p in highs if p.type == "LH")
        hl_count = sum(1 for p in lows if p.type == "HL")
        ll_count = sum(1 for p in lows if p.type == "LL")

        bull_score = hh_count * 20 + hl_count * 15
        bear_score = ll_count * 20 + lh_count * 15

        if bull_score > bear_score and bull_score >= 30:
            strength = min(bull_score, 100)
            return "UPTREND", strength
        elif bear_score > bull_score and bear_score >= 30:
            strength = min(bear_score, 100)
            return "DOWNTREND", strength
        else:
            return "RANGING", max(50 - abs(bull_score - bear_score), 20)

    # ─── 3. BOS / CHoCH Detection ─────────────────────────────────────

    def _detect_structure_breaks(
        self, swing_points: list[SwingPoint], current_price: float
    ) -> tuple[bool, bool, str]:
        """
        BOS = Break of Structure (trend continuation)
        CHoCH = Change of Character (trend reversal signal)
        """
        if len(swing_points) < 4:
            return False, False, "NONE"

        recent_highs = [p for p in swing_points[-8:] if p.type in ("HH", "LH", "HIGH")]
        recent_lows = [p for p in swing_points[-8:] if p.type in ("HL", "LL", "LOW")]

        bos = False
        choch = False
        direction = "NONE"

        # Bullish BOS: price breaks above last swing high (in uptrend)
        if recent_highs:
            last_high = recent_highs[-1]
            if current_price > last_high.price and last_high.type in ("HH", "HIGH"):
                bos = True
                direction = "BULLISH"

        # Bearish BOS: price breaks below last swing low (in downtrend)
        if recent_lows:
            last_low = recent_lows[-1]
            if current_price < last_low.price and last_low.type in ("LL", "LOW"):
                bos = True
                direction = "BEARISH"

        # CHoCH: first LH after HH series, or first HL after LL series
        if len(recent_highs) >= 2:
            if recent_highs[-1].type == "LH" and recent_highs[-2].type == "HH":
                choch = True
                direction = "BEARISH"
        if len(recent_lows) >= 2:
            if recent_lows[-1].type == "HL" and recent_lows[-2].type == "LL":
                choch = True
                direction = "BULLISH"

        return bos, choch, direction

    # ─── 4. S/R Zone Mapping ──────────────────────────────────────────

    def _map_sr_zones(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        current_price: float,
        zone_pct: float = 0.003,
    ) -> tuple[list[SRZone], list[SRZone]]:
        """Map support and resistance zones from price clusters."""
        # Collect all swing points as potential S/R
        all_levels = []
        lookback = 3
        n = len(highs)

        for i in range(lookback, n - lookback):
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                all_levels.append(float(highs[i]))
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                all_levels.append(float(lows[i]))

        if not all_levels:
            return [], []

        # Cluster nearby levels
        all_levels.sort()
        clusters = []
        current_cluster = [all_levels[0]]

        for level in all_levels[1:]:
            if abs(level - current_cluster[-1]) / current_cluster[-1] < zone_pct:
                current_cluster.append(level)
            else:
                clusters.append(current_cluster)
                current_cluster = [level]
        clusters.append(current_cluster)

        # Build zones
        support_zones = []
        resistance_zones = []

        for cluster in clusters:
            avg_price = sum(cluster) / len(cluster)
            strength = min(len(cluster), 5)
            width = max(cluster) - min(cluster) if len(cluster) > 1 else avg_price * 0.001

            zone = SRZone(
                price=round(avg_price, 6),
                strength=strength,
                zone_type="support" if avg_price < current_price else "resistance",
                width=round(width, 6),
            )

            if avg_price < current_price:
                support_zones.append(zone)
            else:
                resistance_zones.append(zone)

        # Sort: support descending (nearest first), resistance ascending (nearest first)
        support_zones.sort(key=lambda z: z.price, reverse=True)
        resistance_zones.sort(key=lambda z: z.price)

        return support_zones, resistance_zones

    # ─── 5. Exhaustion Detection ──────────────────────────────────────

    def _detect_exhaustion(
        self,
        closes: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        opens: np.ndarray,
        volumes: np.ndarray,
        current_price: float,
    ) -> tuple[int, list[str]]:
        """
        Detect momentum exhaustion — higher score = more exhausted.
        """
        score = 0
        signals = []
        n = len(closes)
        if n < 20:
            return 0, []

        # ATR
        tr = np.maximum(highs[1:] - lows[1:],
                        np.maximum(np.abs(highs[1:] - closes[:-1]),
                                   np.abs(lows[1:] - closes[:-1])))
        atr = np.mean(tr[-14:])

        # 1. Candle body vs ATR (overextension)
        last_body = abs(closes[-1] - opens[-1])
        if atr > 0:
            body_atr_ratio = last_body / atr
            if body_atr_ratio > 2.5:
                score += 25
                signals.append(f"Body {body_atr_ratio:.1f}x ATR (extreme)")
            elif body_atr_ratio > 2.0:
                score += 15
                signals.append(f"Body {body_atr_ratio:.1f}x ATR (extended)")

        # 2. Consecutive candles in same direction
        direction_count = 0
        last_dir = 1 if closes[-1] > opens[-1] else -1
        for i in range(n - 2, max(n - 8, 0), -1):
            d = 1 if closes[i] > opens[i] else -1
            if d == last_dir:
                direction_count += 1
            else:
                break
        if direction_count >= 4:
            score += 20
            signals.append(f"{direction_count + 1} consecutive impulse candles")
        elif direction_count >= 3:
            score += 10
            signals.append(f"{direction_count + 1} consecutive candles")

        # 3. Volume climax (current vol > 2.5x average)
        avg_vol = np.mean(volumes[-20:])
        if avg_vol > 0:
            vol_ratio = volumes[-1] / avg_vol
            if vol_ratio > 3.0:
                score += 20
                signals.append(f"Volume climax {vol_ratio:.1f}x avg")
            elif vol_ratio > 2.5:
                score += 12
                signals.append(f"Volume spike {vol_ratio:.1f}x avg")

        # 4. Price distance from 21-period mean
        ema21 = self._calc_ema(closes, 21)
        if ema21 > 0:
            dist_pct = abs(current_price - ema21) / ema21 * 100
            if dist_pct > 2.0:
                score += 15
                signals.append(f"Price {dist_pct:.2f}% from EMA21")
            elif dist_pct > 1.2:
                score += 8
                signals.append(f"Price {dist_pct:.2f}% from EMA21")

        # 5. Wick rejection (long wick against direction)
        total_range = highs[-1] - lows[-1]
        if total_range > 0:
            upper_wick = highs[-1] - max(opens[-1], closes[-1])
            lower_wick = min(opens[-1], closes[-1]) - lows[-1]
            body = abs(closes[-1] - opens[-1])

            if body > 0:
                # Buying: long upper wick = exhaustion
                if closes[-1] > opens[-1] and upper_wick > body * 1.5:
                    score += 15
                    signals.append("Upper wick rejection (bull exhaustion)")
                # Selling: long lower wick = exhaustion
                elif closes[-1] < opens[-1] and lower_wick > body * 1.5:
                    score += 15
                    signals.append("Lower wick rejection (bear exhaustion)")

        # 6. RSI check (simple)
        rsi = self._calc_rsi(closes, 14)
        if rsi > 78:
            score += 12
            signals.append(f"RSI overbought {rsi:.0f}")
        elif rsi < 22:
            score += 12
            signals.append(f"RSI oversold {rsi:.0f}")

        return min(score, 100), signals

    # ─── 6. Liquidity Sweep Detection ─────────────────────────────────

    def _detect_liquidity_sweep(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        swing_points: list[SwingPoint],
    ) -> tuple[bool, str]:
        """
        Detect if price swept a swing high/low and reversed.
        Bull sweep: price dips below swing low then closes back above.
        Bear sweep: price spikes above swing high then closes back below.
        """
        if len(swing_points) < 2 or len(closes) < 3:
            return False, "NONE"

        recent_lows = [p for p in swing_points if p.type in ("HL", "LL", "LOW")]
        recent_highs = [p for p in swing_points if p.type in ("HH", "LH", "HIGH")]

        # Bull sweep: wick below recent swing low, close above it
        if recent_lows:
            last_swing_low = recent_lows[-1].price
            if lows[-1] < last_swing_low and closes[-1] > last_swing_low:
                return True, "BULL_SWEEP"
            if lows[-2] < last_swing_low and closes[-1] > last_swing_low:
                return True, "BULL_SWEEP"

        # Bear sweep: wick above recent swing high, close below it
        if recent_highs:
            last_swing_high = recent_highs[-1].price
            if highs[-1] > last_swing_high and closes[-1] < last_swing_high:
                return True, "BEAR_SWEEP"
            if highs[-2] > last_swing_high and closes[-1] < last_swing_high:
                return True, "BEAR_SWEEP"

        return False, "NONE"

    # ─── 7. BTC Relative Strength ─────────────────────────────────────

    def _calc_btc_relative_strength(
        self, coin_closes: np.ndarray, btc_candles: list
    ) -> tuple[float, float]:
        """
        Calculate coin's performance relative to BTC.
        RS > 1 = outperforming BTC, RS < 1 = underperforming.
        Also computes correlation.
        """
        btc_closes = np.array([float(c[4]) for c in btc_candles])

        min_len = min(len(coin_closes), len(btc_closes))
        if min_len < 10:
            return 1.0, 0.0

        coin_c = coin_closes[-min_len:]
        btc_c = btc_closes[-min_len:]

        # Returns over last N periods
        coin_returns = np.diff(coin_c) / coin_c[:-1]
        btc_returns = np.diff(btc_c) / btc_c[:-1]

        # Relative strength: coin cumulative return / BTC cumulative return
        lookback = min(20, len(coin_returns))
        coin_perf = (coin_c[-1] / coin_c[-lookback - 1]) - 1
        btc_perf = (btc_c[-1] / btc_c[-lookback - 1]) - 1

        if abs(btc_perf) < 0.0001:
            rs = 1.0 + coin_perf  # BTC flat, use coin performance directly
        else:
            rs = (1 + coin_perf) / (1 + btc_perf)

        # Correlation
        if len(coin_returns) >= 10 and len(btc_returns) >= 10:
            min_r = min(len(coin_returns), len(btc_returns))
            corr = float(np.corrcoef(coin_returns[-min_r:], btc_returns[-min_r:])[0, 1])
        else:
            corr = 0.0

        return float(rs), corr if not np.isnan(corr) else 0.0

    # ─── 8. Pre-Reversal Risk Assessment ──────────────────────────────

    def _assess_reversal_risk(
        self,
        exhaustion_score: int,
        exhaustion_signals: list,
        bos: bool,
        choch: bool,
        bos_dir: str,
        trend: str,
        dist_support_pct: float,
        dist_resistance_pct: float,
        sweep: bool,
        sweep_dir: str,
    ) -> tuple[int, list[str]]:
        """Composite reversal risk score."""
        risk = 0
        signals = []

        # Exhaustion contributes directly
        risk += int(exhaustion_score * 0.4)
        if exhaustion_score >= 60:
            signals.append(f"Exhaustion score {exhaustion_score}")

        # CHoCH is a strong reversal signal
        if choch:
            risk += 25
            signals.append(f"Change of Character detected ({bos_dir})")

        # Very close to S/R (about to hit wall)
        if dist_resistance_pct < 0.3:
            risk += 15
            signals.append(f"Near resistance ({dist_resistance_pct:.2f}%)")
        elif dist_resistance_pct < 0.6:
            risk += 8

        if dist_support_pct < 0.3:
            risk += 15
            signals.append(f"Near support ({dist_support_pct:.2f}%)")
        elif dist_support_pct < 0.6:
            risk += 8

        # Liquidity sweep (potential reversal catalyst)
        if sweep:
            risk += 10
            signals.append(f"Liquidity sweep: {sweep_dir}")

        return min(risk, 100), signals

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(data: np.ndarray, period: int) -> float:
        if len(data) < period:
            return float(np.mean(data)) if len(data) > 0 else 0.0
        multiplier = 2 / (period + 1)
        ema = float(data[0])
        for val in data[1:]:
            ema = (float(val) - ema) * multiplier + ema
        return ema

    @staticmethod
    def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))

    def _empty_result(self) -> StructureResult:
        return StructureResult(
            trend="RANGING", trend_strength=50, swing_points=[],
            bos_detected=False, choch_detected=False, bos_direction="NONE",
            support_zones=[], resistance_zones=[],
            nearest_support=0, nearest_resistance=0,
            distance_to_support_pct=5.0, distance_to_resistance_pct=5.0,
            exhaustion_score=0, exhaustion_signals=[], is_exhausted=False,
            liquidity_sweep_detected=False, sweep_direction="NONE",
            btc_relative_strength=1.0, btc_correlation=0.0,
            reversal_risk=0, reversal_signals=[],
        )

    # ─── Fetch BTC candles helper ─────────────────────────────────────

    async def fetch_btc_candles(
        self, interval: str = "5m", limit: int = 100
    ) -> list:
        """Fetch BTCUSDT candles for relative strength calculation."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/fapi/v1/klines",
                    params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning(f"BTC candle fetch failed: {e}")
            return []
"""V15 Market Structure Analyzer — end of module."""
