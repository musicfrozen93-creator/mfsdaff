"""
Scalping AI Decision Engine
Fast RSI-based logic — no external API needed.
RSI < 30 → BUY, RSI > 70 → SELL, else → HOLD
Less strict thresholds for more trades.
"""

import logging
from dataclasses import dataclass
from typing import Optional
import numpy as np
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ScalpDecision:
    action: str         # BUY | SELL | HOLD
    confidence: int     # 0-100
    reason: str
    rsi: float = 50.0
    trend: str = "NEUTRAL"
    atr: float = 0.0
    atr_pct: float = 0.0
    current_price: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0


class ScalpingEngine:
    """
    Fast scalping decision engine using RSI + short-term trend.
    No OpenAI dependency. Pure technical logic.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url

    async def fetch_candles(self, symbol: str, interval: str = "5m", limit: int = 100) -> list:
        """Fetch short-term candles for scalping"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

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
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)
        tr_arr = np.array(tr_list)
        if len(tr_arr) < period:
            return float(np.mean(tr_arr)) if len(tr_arr) > 0 else 0.0
        return float(np.mean(tr_arr[-period:]))

    async def analyze(self, symbol: str) -> ScalpDecision:
        """
        Scalping analysis:
        - RSI < 30 → BUY
        - RSI > 70 → SELL
        - Else → HOLD
        Uses short-term EMA trend for confidence boost.
        """
        logger.info(f"🤖 Scalping analysis for {symbol}...")

        try:
            raw = await self.fetch_candles(symbol, interval="5m", limit=100)

            if len(raw) < 30:
                return ScalpDecision(
                    action="HOLD", confidence=0,
                    reason="Insufficient candle data"
                )

            highs = np.array([float(k[2]) for k in raw])
            lows = np.array([float(k[3]) for k in raw])
            closes = np.array([float(k[4]) for k in raw])

            current_price = closes[-1]
            rsi = self.calc_rsi(closes, period=14)
            atr = self.calc_atr(highs, lows, closes, period=14)
            atr_pct = (atr / current_price) * 100 if current_price > 0 else 0

            # Short-term EMAs for trend
            ema_9 = self.calc_ema(closes, 9)
            ema_21 = self.calc_ema(closes, 21)

            ema_fast_val = ema_9[-1]
            ema_slow_val = ema_21[-1]

            # Determine short-term trend
            if ema_fast_val > ema_slow_val:
                trend = "UP"
            elif ema_fast_val < ema_slow_val:
                trend = "DOWN"
            else:
                trend = "NEUTRAL"

            # --- SCALPING DECISION LOGIC ---
            action = "HOLD"
            confidence = 0
            reason = ""

            if rsi < 30:
                action = "BUY"
                # Base confidence from RSI extremity
                rsi_strength = (30 - rsi) / 30 * 100
                confidence = int(min(65 + rsi_strength * 0.35, 95))

                if trend == "UP":
                    confidence = min(confidence + 10, 98)
                    reason = f"RSI={rsi:.1f} oversold + uptrend confirmed"
                elif trend == "DOWN":
                    confidence = max(confidence - 5, 60)
                    reason = f"RSI={rsi:.1f} oversold but downtrend — cautious buy"
                else:
                    reason = f"RSI={rsi:.1f} oversold — bounce expected"

            elif rsi > 70:
                action = "SELL"
                rsi_strength = (rsi - 70) / 30 * 100
                confidence = int(min(65 + rsi_strength * 0.35, 95))

                if trend == "DOWN":
                    confidence = min(confidence + 10, 98)
                    reason = f"RSI={rsi:.1f} overbought + downtrend confirmed"
                elif trend == "UP":
                    confidence = max(confidence - 5, 60)
                    reason = f"RSI={rsi:.1f} overbought but uptrend — cautious sell"
                else:
                    reason = f"RSI={rsi:.1f} overbought — pullback expected"

            elif rsi < 40 and trend == "UP":
                action = "BUY"
                confidence = int(55 + (40 - rsi) * 0.8)
                confidence = min(confidence, 78)
                reason = f"RSI={rsi:.1f} approaching oversold + uptrend"

            elif rsi > 60 and trend == "DOWN":
                action = "SELL"
                confidence = int(55 + (rsi - 60) * 0.8)
                confidence = min(confidence, 78)
                reason = f"RSI={rsi:.1f} approaching overbought + downtrend"

            else:
                action = "HOLD"
                confidence = max(20, int(50 - abs(rsi - 50)))
                reason = f"RSI={rsi:.1f} neutral zone — no clear signal"

            logger.info(f"  Action={action} | Confidence={confidence} | RSI={rsi} | Trend={trend}")

            return ScalpDecision(
                action=action,
                confidence=confidence,
                reason=reason,
                rsi=rsi,
                trend=trend,
                atr=round(atr, 8),
                atr_pct=round(atr_pct, 4),
                current_price=current_price,
                ema_fast=round(float(ema_fast_val), 8),
                ema_slow=round(float(ema_slow_val), 8),
            )

        except Exception as e:
            logger.error(f"Scalping analysis failed for {symbol}: {e}")
            return ScalpDecision(
                action="HOLD", confidence=0,
                reason=f"Analysis error: {str(e)[:100]}"
            )

    def to_dict(self, decision: ScalpDecision) -> dict:
        return {
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "rsi": decision.rsi,
            "trend": decision.trend,
            "atr": decision.atr,
            "atr_pct": decision.atr_pct,
            "current_price": decision.current_price,
        }
