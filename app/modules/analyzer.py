"""
Technical Analysis Module
Calculates RSI, MACD, EMA, Bollinger Bands, ATR for a given symbol
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class IndicatorResult:
    symbol: str
    timeframe: str
    close_prices: list[float]

    # Trend
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    trend_direction: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL

    # Momentum
    rsi: float = 50.0
    rsi_signal: str = "NEUTRAL"  # OVERBOUGHT | OVERSOLD | NEUTRAL

    # MACD
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_crossover: str = "NONE"  # BULLISH | BEARISH | NONE

    # Bollinger Bands
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0
    bb_position: str = "MIDDLE"  # NEAR_UPPER | NEAR_LOWER | MIDDLE

    # Volatility
    atr: float = 0.0
    atr_pct: float = 0.0

    # Current price
    current_price: float = 0.0
    candles_used: int = 0


class TechnicalAnalyzer:
    """Computes technical indicators from raw OHLCV data."""

    def __init__(self):
        self.base_url = settings.binance_base_url

    async def fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 200) -> list:
        """Fetch OHLCV klines from Binance Futures"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    # ─── Indicator Calculations ────────────────────────────────────────

    def ema(self, values: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average"""
        k = 2 / (period + 1)
        result = np.zeros(len(values))
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i - 1] * (1 - k)
        return result

    def rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """Relative Strength Index"""
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

    def macd(self, closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
        """MACD Line, Signal Line, Histogram"""
        ema_fast = self.ema(closes, fast)
        ema_slow = self.ema(closes, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self.ema(macd_line, signal)
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def bollinger_bands(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
        """Bollinger Bands: upper, middle, lower"""
        middle = np.convolve(closes, np.ones(period) / period, mode="valid")
        std = np.array([np.std(closes[i:i + period]) for i in range(len(closes) - period + 1)])
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        return upper[-1], middle[-1], lower[-1]

    def atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        """Average True Range"""
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
            return float(np.mean(tr_arr))
        return float(np.mean(tr_arr[-period:]))

    # ─── Main Analysis ─────────────────────────────────────────────────

    async def analyze(self, symbol: str, interval: str = "4h") -> IndicatorResult:
        """Fetch candles and compute all indicators"""
        logger.info(f"📊 Analyzing {symbol} on {interval}...")

        raw = await self.fetch_candles(symbol, interval, limit=200)
        if len(raw) < 60:
            raise ValueError(f"Insufficient candles for {symbol}: {len(raw)}")

        opens  = np.array([float(k[1]) for k in raw])
        highs  = np.array([float(k[2]) for k in raw])
        lows   = np.array([float(k[3]) for k in raw])
        closes = np.array([float(k[4]) for k in raw])

        result = IndicatorResult(
            symbol=symbol,
            timeframe=interval,
            close_prices=closes[-20:].tolist(),
            current_price=closes[-1],
            candles_used=len(raw),
        )

        # EMAs
        result.ema_20  = round(float(self.ema(closes, 20)[-1]),  6)
        result.ema_50  = round(float(self.ema(closes, 50)[-1]),  6)
        result.ema_200 = round(float(self.ema(closes, 200)[-1]), 6)

        # Trend direction
        price = closes[-1]
        if price > result.ema_20 > result.ema_50 > result.ema_200:
            result.trend_direction = "BULLISH"
        elif price < result.ema_20 < result.ema_50 < result.ema_200:
            result.trend_direction = "BEARISH"
        else:
            result.trend_direction = "NEUTRAL"

        # RSI
        result.rsi = self.rsi(closes)
        if result.rsi >= 70:
            result.rsi_signal = "OVERBOUGHT"
        elif result.rsi <= 30:
            result.rsi_signal = "OVERSOLD"
        else:
            result.rsi_signal = "NEUTRAL"

        # MACD
        macd_line, signal_line, histogram = self.macd(closes)
        result.macd_line      = round(float(macd_line[-1]),  8)
        result.macd_signal    = round(float(signal_line[-1]), 8)
        result.macd_histogram = round(float(histogram[-1]),  8)

        # MACD crossover detection (last 2 bars)
        if histogram[-2] < 0 and histogram[-1] > 0:
            result.macd_crossover = "BULLISH"
        elif histogram[-2] > 0 and histogram[-1] < 0:
            result.macd_crossover = "BEARISH"
        else:
            result.macd_crossover = "NONE"

        # Bollinger Bands
        bb_upper, bb_mid, bb_lower = self.bollinger_bands(closes)
        result.bb_upper   = round(float(bb_upper), 6)
        result.bb_middle  = round(float(bb_mid),   6)
        result.bb_lower   = round(float(bb_lower),  6)
        result.bb_width   = round(float((bb_upper - bb_lower) / bb_mid * 100), 4)

        if price >= bb_upper * 0.99:
            result.bb_position = "NEAR_UPPER"
        elif price <= bb_lower * 1.01:
            result.bb_position = "NEAR_LOWER"
        else:
            result.bb_position = "MIDDLE"

        # ATR
        atr_val = self.atr(highs, lows, closes)
        result.atr     = round(atr_val, 8)
        result.atr_pct = round((atr_val / closes[-1]) * 100, 4)

        logger.info(
            f"  RSI={result.rsi} | Trend={result.trend_direction} | "
            f"MACD={result.macd_crossover} | ATR%={result.atr_pct}"
        )
        return result

    def to_dict(self, result: IndicatorResult) -> dict:
        """Serialize IndicatorResult to dict for API / AI prompt"""
        return {
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "current_price": result.current_price,
            "candles_used": result.candles_used,
            "trend": {
                "direction": result.trend_direction,
                "ema_20": result.ema_20,
                "ema_50": result.ema_50,
                "ema_200": result.ema_200,
            },
            "momentum": {
                "rsi": result.rsi,
                "rsi_signal": result.rsi_signal,
            },
            "macd": {
                "line": result.macd_line,
                "signal": result.macd_signal,
                "histogram": result.macd_histogram,
                "crossover": result.macd_crossover,
            },
            "bollinger_bands": {
                "upper": result.bb_upper,
                "middle": result.bb_middle,
                "lower": result.bb_lower,
                "width_pct": result.bb_width,
                "price_position": result.bb_position,
            },
            "volatility": {
                "atr": result.atr,
                "atr_pct": result.atr_pct,
            },
        }
