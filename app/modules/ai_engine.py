"""
V5 AI Decision Engine — Multi-Strategy Scalping + Regime Awareness

3 Scalp Sub-Strategies:
  1. Trend Pullback  — EMA trend + pullback to support (original V3 confluence)
  2. Breakout Momentum — resistance break + volume spike + strong close
  3. Range Reversal  — support bounce / resistance rejection at extremes

Layer 1: Technical Rules Engine (10-condition confluence per strategy)
Layer 2: OpenAI Verification (optional)

V5 Changes:
  - 3 independent scalp strategies scored separately
  - Best strategy selected per coin
  - strategy_type + regime fields for tracking
  - Regime-based confidence adjustment
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

from app.config import settings
from app.utils.serialization import clean_json_types

logger = logging.getLogger(__name__)

# ── Try importing OpenAI ─────────────────────────────────────────────
try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    logger.warning("openai package not installed — AI verification disabled")


@dataclass
class AIDecision:
    action: str          # BUY | SELL | HOLD
    confidence: int      # 0-100
    reason: str
    rsi: float = 50.0
    trend: str = "NEUTRAL"
    htf_trend: str = "NEUTRAL"
    atr: float = 0.0
    atr_pct: float = 0.0
    current_price: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    vwap: float = 0.0
    volume_spike: bool = False
    candle_type: str = "DOJI"
    is_choppy: bool = False
    # V3: New fields
    setup_grade: str = "C"       # A | B | C
    macd_crossover: str = "NONE" # BULLISH | BEARISH | NONE
    bb_position: str = "MID"     # UPPER | LOWER | MID
    is_pullback: bool = False
    is_chase: bool = False
    conditions_passed: int = 0
    conditions_total: int = 10
    # V5: Strategy + regime tracking
    strategy_type: str = "trend_pullback"  # trend_pullback | breakout_momentum | range_reversal
    regime: str = ""                        # TRENDING_BULL | SIDEWAYS_RANGE | etc.
    # AI logging
    ai_called: bool = False
    ai_tokens_used: int = 0
    ai_model: str = ""
    ai_latency_ms: int = 0
    ai_fallback: bool = False


class ScalpingEngine:
    """
    V5 Multi-Strategy Scalping Engine with 3 sub-strategies + regime awareness.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url
        self._openai_client = None

    def _get_openai_client(self):
        if self._openai_client is None and HAS_OPENAI and settings.OPENAI_API_KEY:
            self._openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._openai_client

    # ─── Data Fetching ────────────────────────────────────────────────

    async def fetch_candles(self, symbol: str, interval: str = "5m", limit: int = 100) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    # ─── Indicator Helpers ────────────────────────────────────────────

    def calc_ema(self, values: np.ndarray, period: int) -> np.ndarray:
        k = 2 / (period + 1)
        result = np.zeros(len(values))
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i - 1] * (1 - k)
        return result

    def calc_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    def calc_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_list.append(tr)
        tr_arr = np.array(tr_list)
        if len(tr_arr) < period:
            return float(np.mean(tr_arr)) if len(tr_arr) > 0 else 0.0
        return float(np.mean(tr_arr[-period:]))

    def calc_vwap(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> float:
        typical = (highs + lows + closes) / 3.0
        cum_tp_vol = np.cumsum(typical * volumes)
        cum_vol = np.cumsum(volumes)
        if cum_vol[-1] == 0:
            return closes[-1]
        return float((cum_tp_vol / cum_vol)[-1])

    def calc_macd(self, closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
        """MACD Line, Signal Line, Histogram"""
        ema_fast = self.calc_ema(closes, fast)
        ema_slow = self.calc_ema(closes, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self.calc_ema(macd_line, signal)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def calc_bollinger(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
        """Bollinger Bands: upper, middle, lower"""
        if len(closes) < period:
            mid = closes[-1]
            return mid, mid, mid
        middle = np.convolve(closes, np.ones(period) / period, mode="valid")
        std = np.array([np.std(closes[i:i + period]) for i in range(len(closes) - period + 1)])
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        return float(upper[-1]), float(middle[-1]), float(lower[-1])

    async def fetch_htf_trend(self, symbol: str) -> str:
        """15m higher timeframe trend via EMA 9/21."""
        try:
            raw = await self.fetch_candles(symbol, interval="15m", limit=100)
            if len(raw) < 30:
                return "NEUTRAL"
            closes = np.array([float(k[4]) for k in raw])
            ema9 = self.calc_ema(closes, 9)[-1]
            ema21 = self.calc_ema(closes, 21)[-1]
            price = closes[-1]
            if ema9 > ema21 and price > ema9:
                return "BULLISH"
            elif ema9 < ema21 and price < ema9:
                return "BEARISH"
            return "NEUTRAL"
        except Exception:
            return "NEUTRAL"

    # ─── V3: Pullback / Chase Detection ───────────────────────────────

    def detect_pullback(
        self, closes: np.ndarray, ema_fast: np.ndarray, ema_slow: np.ndarray
    ) -> bool:
        """
        V3: Detect pullback-to-EMA for quality entries.
        LONG: Price recently touched or went below EMA 9/21, now reclaiming above.
        """
        if len(closes) < 5:
            return False
        # Check if price was near/below EMA in last 3 candles, now above
        recent_low = min(closes[-3], closes[-4]) if len(closes) >= 4 else closes[-2]
        touched_ema = (recent_low <= ema_fast[-3] * 1.002) or (recent_low <= ema_slow[-3] * 1.002)
        now_above = closes[-1] > ema_fast[-1] and closes[-1] > ema_slow[-1]
        return touched_ema and now_above

    def detect_rejection(
        self, closes: np.ndarray, highs: np.ndarray, ema_fast: np.ndarray, ema_slow: np.ndarray
    ) -> bool:
        """
        V3: Detect rejection bounce for SHORT entries.
        SHORT: Price recently touched or went above EMA, rejected back down.
        """
        if len(closes) < 5:
            return False
        recent_high = max(highs[-3], highs[-4]) if len(highs) >= 4 else highs[-2]
        touched_ema = (recent_high >= ema_fast[-3] * 0.998) or (recent_high >= ema_slow[-3] * 0.998)
        now_below = closes[-1] < ema_fast[-1] and closes[-1] < ema_slow[-1]
        return touched_ema and now_below

    def detect_chase(self, body: float, atr: float) -> bool:
        """V3: Detect if last candle is a chase (body > 2×ATR)."""
        if atr <= 0:
            return False
        return body > atr * 2

    # ─── V3 Layer 1: Technical Rules Engine (10-Condition Confluence) ─

    def _evaluate_confluence(
        self,
        rsi: float,
        ema_fast: float,
        ema_slow: float,
        price: float,
        vwap: float,
        volume_spike: bool,
        spread_pct: float,
        atr_pct: float,
        candle_type: str,
        htf_trend: str,
        is_choppy: bool,
        macd_crossover: str,
        bb_position: str,
        is_pullback: bool,
        is_rejection: bool,
        is_chase: bool,
    ) -> tuple[str, int, str]:
        """
        V3 Layered confluence scoring — 10 conditions.
        Returns (action, confidence, reason).
        """
        # ── AVOID conditions (hard blocks) ───────────────────────────
        if is_choppy:
            return "HOLD", 0, "Sideways chop detected — EMAs too close"
        if spread_pct > settings.MAX_SPREAD_ENTRY_PCT:
            return "HOLD", 0, f"Spread too high: {spread_pct:.3f}% > {settings.MAX_SPREAD_ENTRY_PCT}%"
        if atr_pct > settings.MAX_VOLATILITY_PCT:
            return "HOLD", 0, f"Extreme volatility: ATR%={atr_pct:.2f}%"

        # ── LONG conditions (10 total) ───────────────────────────────
        long_conditions = {
            "ema_cross": ema_fast > ema_slow,
            "above_vwap": price > vwap,
            "rsi_range": 52 <= rsi <= 68,
            "volume_spike": volume_spike,
            "spread_ok": spread_pct < settings.MAX_SPREAD_ENTRY_PCT,
            "volatility_ok": atr_pct < settings.MAX_VOLATILITY_PCT,
            "candle_bullish": candle_type == "BULLISH",
            "htf_bullish": htf_trend == "BULLISH",
            "macd_bullish": macd_crossover == "BULLISH" or macd_crossover == "NONE",  # Not bearish
            "bb_favorable": bb_position != "UPPER",  # Not overbought at upper band
        }

        # ── SHORT conditions (10 total) ──────────────────────────────
        short_conditions = {
            "ema_cross": ema_fast < ema_slow,
            "below_vwap": price < vwap,
            "rsi_range": 32 <= rsi <= 48,
            "volume_spike": volume_spike,
            "spread_ok": spread_pct < settings.MAX_SPREAD_ENTRY_PCT,
            "volatility_ok": atr_pct < settings.MAX_VOLATILITY_PCT,
            "candle_bearish": candle_type == "BEARISH",
            "htf_bearish": htf_trend == "BEARISH",
            "macd_bearish": macd_crossover == "BEARISH" or macd_crossover == "NONE",
            "bb_favorable": bb_position != "LOWER",  # Not oversold at lower band
        }

        long_score = sum(1 for v in long_conditions.values() if v)
        short_score = sum(1 for v in short_conditions.values() if v)
        total_conditions = 10

        # V3: Minimum 6/10 conditions for signal (up from 5/8)
        min_conditions = 6

        # ── Decision logic ───────────────────────────────────────────
        if long_score >= min_conditions and long_score > short_score:
            confidence = int(50 + (long_score / total_conditions) * 50)
            passed = [k for k, v in long_conditions.items() if v]
            failed = [k for k, v in long_conditions.items() if not v]
            reason = f"LONG confluence {long_score}/{total_conditions}: {', '.join(passed)}"
            if failed:
                reason += f" | Missing: {', '.join(failed)}"

            # V3: Pullback bonus
            if is_pullback:
                confidence = min(confidence + 5, 98)
                reason += " | ✅ Pullback entry"

            # V3: Chase penalty
            if is_chase:
                confidence = max(confidence - 15, 50)
                reason += " | ⚠️ Chase detected"

            return "BUY", min(confidence, 98), reason

        elif short_score >= min_conditions and short_score > long_score:
            confidence = int(50 + (short_score / total_conditions) * 50)
            passed = [k for k, v in short_conditions.items() if v]
            failed = [k for k, v in short_conditions.items() if not v]
            reason = f"SHORT confluence {short_score}/{total_conditions}: {', '.join(passed)}"
            if failed:
                reason += f" | Missing: {', '.join(failed)}"

            # V3: Rejection bonus
            if is_rejection:
                confidence = min(confidence + 5, 98)
                reason += " | ✅ Rejection entry"

            # V3: Chase penalty
            if is_chase:
                confidence = max(confidence - 15, 50)
                reason += " | ⚠️ Chase detected"

            return "SELL", min(confidence, 98), reason

        else:
            best = max(long_score, short_score)
            return "HOLD", max(20, int(best / total_conditions * 40)), \
                f"Insufficient confluence: L={long_score}/10, S={short_score}/10"

    # ─── Layer 2: OpenAI Verification ─────────────────────────────────

    async def _openai_verify(
        self, symbol: str, indicators: dict, orderbook_summary: Optional[dict] = None
    ) -> Optional[dict]:
        """
        Call OpenAI for trade verification. Returns dict with action/confidence/reason
        or None if failed.
        """
        client = self._get_openai_client()
        if not client:
            return None

        try:
            prompt = self._build_ai_prompt(symbol, indicators, orderbook_summary)
            start_time = time.time()

            response = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a professional crypto futures scalping analyst. "
                            "Analyze the given indicators and return a JSON trading decision. "
                            "You MUST respond with ONLY valid JSON, no markdown, no explanation outside JSON. "
                            'Format: {"action": "BUY|SELL|HOLD", "confidence": 0-100, "reason": "brief explanation"}'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=200,
                response_format={"type": "json_object"},
            )

            latency_ms = int((time.time() - start_time) * 1000)
            content = response.choices[0].message.content.strip()
            tokens_used = response.usage.total_tokens if response.usage else 0

            result = json.loads(content)

            logger.info(
                f"  🤖 OpenAI response: action={result.get('action')} "
                f"conf={result.get('confidence')} | {tokens_used} tokens | {latency_ms}ms"
            )

            return {
                "action": result.get("action", "HOLD"),
                "confidence": int(result.get("confidence", 50)),
                "reason": result.get("reason", ""),
                "tokens_used": tokens_used,
                "model": settings.OPENAI_MODEL,
                "latency_ms": latency_ms,
            }

        except Exception as e:
            logger.warning(f"  OpenAI verification failed: {e}")
            return None

    def _build_ai_prompt(self, symbol: str, indicators: dict, orderbook: Optional[dict] = None) -> str:
        """Build structured prompt for OpenAI."""
        parts = [
            f"Symbol: {symbol}",
            f"Timeframe: 5m scalping",
            "",
            "=== INDICATORS ===",
            f"Price: ${indicators.get('current_price', 0)}",
            f"EMA 9: {indicators.get('ema_fast', 0)}",
            f"EMA 21: {indicators.get('ema_slow', 0)}",
            f"RSI(14): {indicators.get('rsi', 50)}",
            f"VWAP: {indicators.get('vwap', 0)}",
            f"ATR%: {indicators.get('atr_pct', 0)}%",
            f"Volume Spike: {indicators.get('volume_spike', False)}",
            f"Volume Ratio: {indicators.get('volume_ratio', 1.0)}x",
            f"Candle Type: {indicators.get('candle_type', 'DOJI')}",
            f"5m Trend (EMA): {indicators.get('trend', 'NEUTRAL')}",
            f"15m HTF Trend: {indicators.get('htf_trend', 'NEUTRAL')}",
            f"Choppy Market: {indicators.get('is_choppy', False)}",
            f"Spread: {indicators.get('spread_pct', 0)}%",
            f"MACD Crossover: {indicators.get('macd_crossover', 'NONE')}",
            f"BB Position: {indicators.get('bb_position', 'MID')}",
            f"Pullback Entry: {indicators.get('is_pullback', False)}",
            f"Chase Warning: {indicators.get('is_chase', False)}",
        ]

        if orderbook:
            parts.extend([
                "",
                "=== ORDER BOOK ===",
                f"Imbalance: {orderbook.get('imbalance_score', 0)}",
                f"Bias: {orderbook.get('bias', 'NEUTRAL')}",
                f"Support Zones: {orderbook.get('support_zones', [])}",
                f"Resistance Zones: {orderbook.get('resistance_zones', [])}",
            ])

        parts.extend([
            "",
            "Should I BUY, SELL, or HOLD? Respond with JSON only.",
        ])

        return "\n".join(parts)

    # ─── V3: Setup Grade ──────────────────────────────────────────────

    @staticmethod
    def determine_setup_grade(conditions_passed: int, volume_spike: bool) -> str:
        """
        A = Elite (8+/10 conditions, volume spike present)
        B = Strong (7/10 or 8+/10 without volume spike)
        C = Standard (6/10)
        """
        if conditions_passed >= 8 and volume_spike:
            return "A"
        elif conditions_passed >= 7:
            return "B"
        else:
            return "C"

    async def analyze(
        self, symbol: str, spread_pct: float = 0.0,
        orderbook_data: Optional[dict] = None,
        regime: str = "", regime_weights: Optional[dict] = None,
    ) -> AIDecision:
        """
        V5 Multi-strategy scalping analysis:
        1. Fetch 5m candles + 15m HTF trend
        2. Compute all indicators
        3. Run 3 sub-strategies: trend pullback, breakout momentum, range reversal
        4. Select best strategy adjusted by regime weights
        5. Layer 2: OpenAI verification (optional)
        """
        logger.info(f"🤖 V5 multi-strategy analysis for {symbol}...")
        weights = regime_weights or {
            "scalp_trend_pullback": 1.0,
            "scalp_breakout": 1.0,
            "scalp_range_reversal": 1.0,
        }

        try:
            # Fetch candles and HTF in parallel
            import asyncio
            raw_task = self.fetch_candles(symbol, interval="5m", limit=100)
            htf_task = self.fetch_htf_trend(symbol)
            raw, htf_trend = await asyncio.gather(raw_task, htf_task)

            if len(raw) < 30:
                return AIDecision(action="HOLD", confidence=0, reason="Insufficient candle data")

            # Parse OHLCV
            opens = np.array([float(k[1]) for k in raw])
            highs = np.array([float(k[2]) for k in raw])
            lows = np.array([float(k[3]) for k in raw])
            closes = np.array([float(k[4]) for k in raw])
            volumes = np.array([float(k[5]) for k in raw])

            current_price = float(closes[-1])
            rsi = float(self.calc_rsi(closes, period=14))
            atr = float(self.calc_atr(highs, lows, closes, period=14))
            atr_pct = float((atr / current_price) * 100) if current_price > 0 else 0.0

            ema_9 = self.calc_ema(closes, 9)
            ema_21 = self.calc_ema(closes, 21)
            ema_fast_val = float(ema_9[-1])
            ema_slow_val = float(ema_21[-1])

            vwap = float(self.calc_vwap(highs, lows, closes, volumes))

            # Volume spike
            avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))
            cur_vol = float(volumes[-1])
            volume_ratio = float(cur_vol / avg_vol) if avg_vol > 0 else 1.0
            volume_spike = bool(volume_ratio > 1.5)

            # Candle type
            o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
            body = abs(c - o)
            total_range = h - l
            if total_range == 0 or body / total_range < 0.15:
                candle_type = "DOJI"
            else:
                candle_type = "BULLISH" if c > o else "BEARISH"

            # Chop detection
            ema_dist = float(abs(ema_fast_val - ema_slow_val) / ema_slow_val * 100) if ema_slow_val > 0 else 0.0
            is_choppy = bool(ema_dist < 0.1)

            # Trend
            if ema_fast_val > ema_slow_val:
                trend = "BULLISH"
            elif ema_fast_val < ema_slow_val:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"

            # MACD crossover
            macd_line, signal_line, histogram = self.calc_macd(closes)
            if len(histogram) >= 2:
                if histogram[-2] < 0 and histogram[-1] > 0:
                    macd_crossover = "BULLISH"
                elif histogram[-2] > 0 and histogram[-1] < 0:
                    macd_crossover = "BEARISH"
                else:
                    macd_crossover = "NONE"
            else:
                macd_crossover = "NONE"

            # Bollinger Band position
            bb_upper, bb_mid, bb_lower = self.calc_bollinger(closes)
            if current_price >= bb_upper * 0.998:
                bb_position = "UPPER"
            elif current_price <= bb_lower * 1.002:
                bb_position = "LOWER"
            else:
                bb_position = "MID"

            # Pullback / rejection detection
            is_pullback = self.detect_pullback(closes, ema_9, ema_21)
            is_rejection = self.detect_rejection(closes, highs, ema_9, ema_21)
            is_chase = self.detect_chase(body, atr)

            # ══ V5: Run all 3 sub-strategies ══════════════════════════

            # Strategy 1: Trend Pullback (original V3 confluence)
            trend_result = self._evaluate_confluence(
                rsi=rsi, ema_fast=ema_fast_val, ema_slow=ema_slow_val,
                price=current_price, vwap=vwap, volume_spike=volume_spike,
                spread_pct=spread_pct, atr_pct=atr_pct, candle_type=candle_type,
                htf_trend=htf_trend, is_choppy=is_choppy,
                macd_crossover=macd_crossover, bb_position=bb_position,
                is_pullback=is_pullback, is_rejection=is_rejection, is_chase=is_chase,
            )

            # Strategy 2: Breakout Momentum
            breakout_result = self._score_breakout_momentum(
                price=current_price, highs=highs, closes=closes,
                volume_spike=volume_spike, volume_ratio=volume_ratio,
                macd_crossover=macd_crossover, bb_position=bb_position,
                htf_trend=htf_trend, spread_pct=spread_pct, atr_pct=atr_pct,
            )

            # Strategy 3: Range Reversal
            reversal_result = self._score_range_reversal(
                price=current_price, rsi=rsi, bb_position=bb_position,
                bb_upper=bb_upper, bb_lower=bb_lower, vwap=vwap,
                volume_spike=volume_spike, candle_type=candle_type,
                spread_pct=spread_pct, atr_pct=atr_pct,
            )

            # Select best strategy with regime weighting
            best_action, best_conf, best_reason, strategy_type = self._select_best_strategy(
                trend_result, breakout_result, reversal_result, weights,
            )

            # If no strategy triggered, fall back to trend pullback result
            if best_action == "HOLD":
                tech_action, tech_confidence, tech_reason = trend_result
                strategy_type = "trend_pullback"
            else:
                tech_action, tech_confidence, tech_reason = best_action, best_conf, best_reason

            # Count conditions for setup grade (from trend pullback)
            if tech_action in ("BUY", "SELL"):
                try:
                    parts = trend_result[2].split("/10")[0].split()
                    conditions_passed = int(parts[-1]) if parts else 6
                except Exception:
                    conditions_passed = 6
            else:
                conditions_passed = 0

            setup_grade = self.determine_setup_grade(conditions_passed, volume_spike)

            logger.info(
                f"  V5 Strategies: trend={trend_result[0]}/{trend_result[1]} "
                f"breakout={breakout_result[0]}/{breakout_result[1]} "
                f"reversal={reversal_result[0]}/{reversal_result[1]} "
                f"→ best={strategy_type} {tech_action}/{tech_confidence} grade={setup_grade}"
            )

            # Build decision
            decision = AIDecision(
                action=tech_action,
                confidence=tech_confidence,
                reason=tech_reason,
                rsi=rsi,
                trend=trend,
                htf_trend=htf_trend,
                atr=round(atr, 8),
                atr_pct=round(atr_pct, 4),
                current_price=current_price,
                ema_fast=round(float(ema_fast_val), 8),
                ema_slow=round(float(ema_slow_val), 8),
                vwap=round(vwap, 6),
                volume_spike=volume_spike,
                candle_type=candle_type,
                is_choppy=is_choppy,
                setup_grade=setup_grade,
                macd_crossover=macd_crossover,
                bb_position=bb_position,
                is_pullback=is_pullback,
                is_chase=is_chase,
                conditions_passed=conditions_passed,
                strategy_type=strategy_type,
                regime=regime,
            )

            # ── Layer 2: OpenAI Verification (only if Layer 1 produced a signal) ──
            if tech_action != "HOLD" and tech_confidence >= 60:
                indicators_for_ai = {
                    "current_price": current_price,
                    "ema_fast": ema_fast_val,
                    "ema_slow": ema_slow_val,
                    "rsi": rsi,
                    "vwap": vwap,
                    "atr_pct": round(atr_pct, 2),
                    "volume_spike": volume_spike,
                    "volume_ratio": round(volume_ratio, 2),
                    "candle_type": candle_type,
                    "trend": trend,
                    "htf_trend": htf_trend,
                    "is_choppy": is_choppy,
                    "spread_pct": spread_pct,
                    "macd_crossover": macd_crossover,
                    "bb_position": bb_position,
                    "is_pullback": is_pullback,
                    "is_chase": is_chase,
                    "strategy_type": strategy_type,
                }

                ai_result = await self._openai_verify(symbol, indicators_for_ai, orderbook_data)

                if ai_result:
                    decision.ai_called = True
                    decision.ai_tokens_used = ai_result.get("tokens_used", 0)
                    decision.ai_model = ai_result.get("model", "")
                    decision.ai_latency_ms = ai_result.get("latency_ms", 0)

                    ai_action = ai_result.get("action", "HOLD")
                    ai_confidence = ai_result.get("confidence", 50)
                    ai_reason = ai_result.get("reason", "")

                    if ai_action == tech_action:
                        combined_confidence = int((tech_confidence * 0.6) + (ai_confidence * 0.4))
                        decision.confidence = min(combined_confidence, 98)
                        decision.reason = f"{tech_reason} | AI confirms: {ai_reason}"
                    elif ai_action == "HOLD":
                        decision.confidence = max(tech_confidence - 15, 50)
                        decision.reason = f"{tech_reason} | AI cautious: {ai_reason}"
                    else:
                        decision.confidence = max(tech_confidence - 25, 40)
                        decision.reason = f"{tech_reason} | AI disagrees ({ai_action}): {ai_reason}"
                        if decision.confidence < settings.MIN_CONFIDENCE:
                            decision.action = "HOLD"
                else:
                    decision.ai_fallback = True
                    logger.info("  Layer2: OpenAI unavailable — using technical rules only")

            logger.info(
                f"  Final: {decision.action} conf={decision.confidence} "
                f"strategy={decision.strategy_type} grade={decision.setup_grade} regime={regime} | "
                f"AI={decision.ai_called} fallback={decision.ai_fallback}"
            )
            return decision

        except Exception as e:
            logger.error(f"Analysis failed for {symbol}: {e}")
            return AIDecision(
                action="HOLD", confidence=0,
                reason=f"Analysis error: {str(e)[:100]}"
            )

    # ─── V5: Breakout Momentum Sub-Strategy ─────────────────────────

    def _score_breakout_momentum(
        self, price: float, highs: np.ndarray, closes: np.ndarray,
        volume_spike: bool, volume_ratio: float, macd_crossover: str,
        bb_position: str, htf_trend: str, spread_pct: float, atr_pct: float,
    ) -> tuple[str, int, str]:
        """
        V5 Breakout momentum strategy — detects resistance breaks with volume.
        Best in BREAKOUT_EXPANSION regime.
        """
        if spread_pct > settings.MAX_SPREAD_ENTRY_PCT or atr_pct > settings.MAX_VOLATILITY_PCT:
            return "HOLD", 0, "Spread/volatility too high for breakout"

        # Find recent resistance (highest high in last 20 candles, excluding last 2)
        if len(highs) < 22:
            return "HOLD", 0, "Insufficient data for breakout"

        recent_resistance = float(np.max(highs[-22:-2]))
        recent_support = float(np.min(closes[-22:-2]))

        score = 0
        # LONG breakout: price above recent resistance
        if price > recent_resistance:
            score = 40
            if volume_spike:
                score += 15
            if volume_ratio > 2.0:
                score += 10
            if macd_crossover == "BULLISH":
                score += 10
            if htf_trend == "BULLISH":
                score += 10
            if closes[-1] > closes[-2] > closes[-3]:  # 3 green candles
                score += 10
            if bb_position == "UPPER":  # Breaking above BB = strong
                score += 5

            if score >= 65:
                return "BUY", min(score, 98), f"Breakout above {recent_resistance:.4f} | vol={volume_ratio:.1f}x"

        # SHORT breakout: price below recent support
        elif price < recent_support:
            score = 40
            if volume_spike:
                score += 15
            if volume_ratio > 2.0:
                score += 10
            if macd_crossover == "BEARISH":
                score += 10
            if htf_trend == "BEARISH":
                score += 10
            if closes[-1] < closes[-2] < closes[-3]:
                score += 10
            if bb_position == "LOWER":
                score += 5

            if score >= 65:
                return "SELL", min(score, 98), f"Breakdown below {recent_support:.4f} | vol={volume_ratio:.1f}x"

        return "HOLD", 0, "No breakout detected"

    # ─── V5: Range Reversal Sub-Strategy ───────────────────────────

    def _score_range_reversal(
        self, price: float, rsi: float, bb_position: str,
        bb_upper: float, bb_lower: float, vwap: float,
        volume_spike: bool, candle_type: str, spread_pct: float, atr_pct: float,
    ) -> tuple[str, int, str]:
        """
        V5 Range reversal strategy — buys support, sells resistance.
        Best in SIDEWAYS_RANGE regime.
        """
        if spread_pct > settings.MAX_SPREAD_ENTRY_PCT or atr_pct > settings.MAX_VOLATILITY_PCT:
            return "HOLD", 0, "Spread/volatility too high for reversal"

        # LONG reversal at lower BB / oversold
        if bb_position == "LOWER" and rsi < 35:
            score = 45
            if rsi < 25:
                score += 15
            elif rsi < 30:
                score += 10
            if candle_type == "BULLISH":  # Reversal candle
                score += 15
            if volume_spike:
                score += 10
            if price < vwap:
                score += 5

            if score >= 65:
                return "BUY", min(score, 98), f"Range reversal at lower BB | RSI={rsi:.0f}"

        # SHORT reversal at upper BB / overbought
        if bb_position == "UPPER" and rsi > 65:
            score = 45
            if rsi > 75:
                score += 15
            elif rsi > 70:
                score += 10
            if candle_type == "BEARISH":
                score += 15
            if volume_spike:
                score += 10
            if price > vwap:
                score += 5

            if score >= 65:
                return "SELL", min(score, 98), f"Range reversal at upper BB | RSI={rsi:.0f}"

        return "HOLD", 0, "No reversal setup"

    # ─── V5: Multi-Strategy Best Selection ─────────────────────────

    def _select_best_strategy(
        self,
        trend_result: tuple, breakout_result: tuple, reversal_result: tuple,
        regime_weights: dict,
    ) -> tuple[str, int, str, str]:
        """
        Pick the best scoring strategy, apply regime weight adjustment.
        Returns (action, confidence, reason, strategy_type).
        """
        strategies = [
            ("trend_pullback", trend_result, regime_weights.get("scalp_trend_pullback", 1.0)),
            ("breakout_momentum", breakout_result, regime_weights.get("scalp_breakout", 1.0)),
            ("range_reversal", reversal_result, regime_weights.get("scalp_range_reversal", 1.0)),
        ]

        best = None
        for name, (action, conf, reason), weight in strategies:
            if action == "HOLD" or conf < 60:
                continue
            adjusted_conf = int(conf * weight)
            adjusted_conf = max(50, min(adjusted_conf, 98))
            if best is None or adjusted_conf > best[1]:
                best = (action, adjusted_conf, f"[{name}] {reason}", name)

        if best:
            return best
        return "HOLD", 0, "No strategy met minimum threshold", "none"

    def to_dict(self, decision: AIDecision) -> dict:
        raw = {
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "rsi": decision.rsi,
            "trend": decision.trend,
            "htf_trend": decision.htf_trend,
            "atr": decision.atr,
            "atr_pct": decision.atr_pct,
            "current_price": decision.current_price,
            "vwap": decision.vwap,
            "volume_spike": decision.volume_spike,
            "candle_type": decision.candle_type,
            "is_choppy": decision.is_choppy,
            # V3
            "setup_grade": decision.setup_grade,
            "macd_crossover": decision.macd_crossover,
            "bb_position": decision.bb_position,
            "is_pullback": decision.is_pullback,
            "is_chase": decision.is_chase,
            "conditions_passed": decision.conditions_passed,
            "conditions_total": decision.conditions_total,
            # V5
            "strategy_type": decision.strategy_type,
            "regime": decision.regime,
            # AI
            "ai_called": decision.ai_called,
            "ai_tokens_used": decision.ai_tokens_used,
            "ai_model": decision.ai_model,
            "ai_latency_ms": decision.ai_latency_ms,
            "ai_fallback": decision.ai_fallback,
        }
        return clean_json_types(raw)
