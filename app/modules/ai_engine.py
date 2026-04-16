"""
V2 AI Decision Engine — Layered Confluence + OpenAI Verification

Layer 1: Technical Rules Engine (confluence logic)
  - All conditions must align for BUY/SELL signal
  - Confidence scored by number of passing conditions

Layer 2: OpenAI Verification (optional)
  - Sends indicator + orderbook summary
  - Expects strict JSON response
  - Adjusts confidence by averaging with technical score
  - Falls back to Layer 1 if OpenAI fails

Full logging: AI called, tokens, model, latency, fallback status.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

from app.config import settings

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
    # AI logging
    ai_called: bool = False
    ai_tokens_used: int = 0
    ai_model: str = ""
    ai_latency_ms: int = 0
    ai_fallback: bool = False


class ScalpingEngine:
    """
    V2 Scalping Decision Engine with layered confluence + optional OpenAI.
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

    # ─── Layer 1: Technical Rules Engine (Confluence) ─────────────────

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
    ) -> tuple[str, int, str]:
        """
        Layered confluence scoring.
        Returns (action, confidence, reason).
        """
        # ── AVOID conditions (hard blocks) ───────────────────────────
        if is_choppy:
            return "HOLD", 0, "Sideways chop detected — EMAs too close"
        if spread_pct > 0.15:
            return "HOLD", 0, f"Spread too high: {spread_pct:.3f}%"
        if atr_pct > settings.MAX_VOLATILITY_PCT:
            return "HOLD", 0, f"Extreme volatility: ATR%={atr_pct:.2f}%"
        if not volume_spike:
            # Not a hard block, but reduces confidence significantly
            pass

        # ── LONG conditions ──────────────────────────────────────────
        long_conditions = {
            "ema_cross": ema_fast > ema_slow,
            "above_vwap": price > vwap,
            "rsi_range": 52 <= rsi <= 68,
            "volume_spike": volume_spike,
            "spread_ok": spread_pct < 0.15,
            "volatility_ok": atr_pct < settings.MAX_VOLATILITY_PCT,
            "candle_bullish": candle_type == "BULLISH",
            "htf_bullish": htf_trend == "BULLISH",
        }

        # ── SHORT conditions ─────────────────────────────────────────
        short_conditions = {
            "ema_cross": ema_fast < ema_slow,
            "below_vwap": price < vwap,
            "rsi_range": 32 <= rsi <= 48,
            "volume_spike": volume_spike,
            "spread_ok": spread_pct < 0.15,
            "volatility_ok": atr_pct < settings.MAX_VOLATILITY_PCT,
            "candle_bearish": candle_type == "BEARISH",
            "htf_bearish": htf_trend == "BEARISH",
        }

        long_score = sum(1 for v in long_conditions.values() if v)
        short_score = sum(1 for v in short_conditions.values() if v)
        total_conditions = 8

        # ── Decision logic ───────────────────────────────────────────
        # Need at least 5/8 conditions for a signal
        min_conditions = 5

        if long_score >= min_conditions and long_score > short_score:
            confidence = int(50 + (long_score / total_conditions) * 50)
            passed = [k for k, v in long_conditions.items() if v]
            failed = [k for k, v in long_conditions.items() if not v]
            reason = f"LONG confluence {long_score}/{total_conditions}: {', '.join(passed)}"
            if failed:
                reason += f" | Missing: {', '.join(failed)}"
            return "BUY", min(confidence, 98), reason

        elif short_score >= min_conditions and short_score > long_score:
            confidence = int(50 + (short_score / total_conditions) * 50)
            passed = [k for k, v in short_conditions.items() if v]
            failed = [k for k, v in short_conditions.items() if not v]
            reason = f"SHORT confluence {short_score}/{total_conditions}: {', '.join(passed)}"
            if failed:
                reason += f" | Missing: {', '.join(failed)}"
            return "SELL", min(confidence, 98), reason

        else:
            best = max(long_score, short_score)
            return "HOLD", max(20, int(best / total_conditions * 40)), f"Insufficient confluence: L={long_score}/8, S={short_score}/8"

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

    # ─── Main Analysis ────────────────────────────────────────────────

    async def analyze(self, symbol: str, spread_pct: float = 0.0, orderbook_data: Optional[dict] = None) -> AIDecision:
        """
        Full scalping analysis:
        1. Fetch 5m candles + 15m HTF trend
        2. Compute all indicators
        3. Layer 1: Technical confluence scoring
        4. Layer 2: OpenAI verification (optional)
        5. Combine results
        """
        logger.info(f"🤖 V2 scalping analysis for {symbol}...")

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

            current_price = closes[-1]
            rsi = self.calc_rsi(closes, period=14)
            atr = self.calc_atr(highs, lows, closes, period=14)
            atr_pct = (atr / current_price) * 100 if current_price > 0 else 0

            ema_9 = self.calc_ema(closes, 9)
            ema_21 = self.calc_ema(closes, 21)
            ema_fast_val = ema_9[-1]
            ema_slow_val = ema_21[-1]

            vwap = self.calc_vwap(highs, lows, closes, volumes)

            # Volume spike
            avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))
            cur_vol = float(volumes[-1])
            volume_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
            volume_spike = volume_ratio > 1.5

            # Candle type
            o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
            body = abs(c - o)
            total_range = h - l
            if total_range == 0 or body / total_range < 0.15:
                candle_type = "DOJI"
            else:
                candle_type = "BULLISH" if c > o else "BEARISH"

            # Chop detection
            ema_dist = abs(ema_fast_val - ema_slow_val) / ema_slow_val * 100 if ema_slow_val > 0 else 0
            is_choppy = ema_dist < 0.1

            # Trend
            if ema_fast_val > ema_slow_val:
                trend = "BULLISH"
            elif ema_fast_val < ema_slow_val:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"

            # ── Layer 1: Technical Confluence ─────────────────────────
            tech_action, tech_confidence, tech_reason = self._evaluate_confluence(
                rsi=rsi,
                ema_fast=ema_fast_val,
                ema_slow=ema_slow_val,
                price=current_price,
                vwap=vwap,
                volume_spike=volume_spike,
                spread_pct=spread_pct,
                atr_pct=atr_pct,
                candle_type=candle_type,
                htf_trend=htf_trend,
                is_choppy=is_choppy,
            )

            logger.info(f"  Layer1: {tech_action} conf={tech_confidence} | {tech_reason}")

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

                    # Combine: if AI agrees, boost confidence. If disagrees, reduce.
                    if ai_action == tech_action:
                        combined_confidence = int((tech_confidence * 0.6) + (ai_confidence * 0.4))
                        decision.confidence = min(combined_confidence, 98)
                        decision.reason = f"{tech_reason} | AI confirms: {ai_reason}"
                    elif ai_action == "HOLD":
                        # AI says hold but tech says trade — reduce confidence
                        decision.confidence = max(tech_confidence - 15, 50)
                        decision.reason = f"{tech_reason} | AI cautious: {ai_reason}"
                    else:
                        # AI disagrees on direction — big penalty
                        decision.confidence = max(tech_confidence - 25, 40)
                        decision.reason = f"{tech_reason} | AI disagrees ({ai_action}): {ai_reason}"
                        if decision.confidence < settings.MIN_CONFIDENCE:
                            decision.action = "HOLD"
                else:
                    # OpenAI failed — fallback to technical only
                    decision.ai_fallback = True
                    logger.info("  Layer2: OpenAI unavailable — using technical rules only")

            logger.info(
                f"  Final: {decision.action} conf={decision.confidence} | "
                f"AI={decision.ai_called} fallback={decision.ai_fallback}"
            )
            return decision

        except Exception as e:
            logger.error(f"Analysis failed for {symbol}: {e}")
            return AIDecision(
                action="HOLD", confidence=0,
                reason=f"Analysis error: {str(e)[:100]}"
            )

    def to_dict(self, decision: AIDecision) -> dict:
        return {
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
            "ai_called": decision.ai_called,
            "ai_tokens_used": decision.ai_tokens_used,
            "ai_model": decision.ai_model,
            "ai_latency_ms": decision.ai_latency_ms,
            "ai_fallback": decision.ai_fallback,
        }
