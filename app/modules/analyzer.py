"""
V2 Technical Analysis Module — Scalping Confluence
Calculates EMA 9/21, VWAP, RSI, ATR, Volume Spike,
Candle Confirmation, Chop Detection, and 15m HTF Trend.
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
class IndicatorResult:
    symbol: str
    timeframe: str
    close_prices: list[float]

    # Current price
    current_price: float = 0.0
    candles_used: int = 0

    # Scalping EMAs
    ema_9: float = 0.0
    ema_21: float = 0.0

    # Legacy EMAs (kept for compatibility)
    ema_20: float = 0.0
    ema_50: float = 0.0

    # Trend
    trend_direction: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL

    # VWAP
    vwap: float = 0.0
    price_vs_vwap: str = "AT"  # ABOVE | BELOW | AT

    # Momentum
    rsi: float = 50.0
    rsi_signal: str = "NEUTRAL"

    # Volume
    volume_spike: bool = False
    volume_ratio: float = 1.0
    current_volume: float = 0.0
    avg_volume: float = 0.0

    # Candle Confirmation
    candle_type: str = "DOJI"  # BULLISH | BEARISH | DOJI

    # Chop Detection
    is_choppy: bool = False
    ema_distance_pct: float = 0.0

    # Volatility
    atr: float = 0.0
    atr_pct: float = 0.0

    # Bollinger Bands
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0

    # MACD
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_crossover: str = "NONE"

    # Spread
    spread_pct: float = 0.0

    # Higher Timeframe
    htf_trend: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL


class TechnicalAnalyzer:
    """
    V2 Technical Analyzer — optimized for scalping with layered confluence.
    Primary timeframe: 5m (configurable to 1m/3m).
    Higher timeframe confirmation: 15m.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url

    async def fetch_candles(self, symbol: str, interval: str = "5m", limit: int = 200) -> list:
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
        if len(closes) < period:
            mid = closes[-1]
            return mid, mid, mid
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
            return float(np.mean(tr_arr)) if len(tr_arr) > 0 else 0.0
        return float(np.mean(tr_arr[-period:]))

    def calc_vwap(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> float:
        """Volume Weighted Average Price — intraday approximation."""
        typical_prices = (highs + lows + closes) / 3.0
        cumulative_tp_vol = np.cumsum(typical_prices * volumes)
        cumulative_vol = np.cumsum(volumes)
        if cumulative_vol[-1] == 0:
            return closes[-1]
        vwap_arr = cumulative_tp_vol / cumulative_vol
        return float(vwap_arr[-1])

    def detect_candle_type(self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> str:
        """Detect last candle type: BULLISH, BEARISH, or DOJI."""
        o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
        body = abs(c - o)
        total_range = h - l
        if total_range == 0:
            return "DOJI"
        body_ratio = body / total_range
        if body_ratio < 0.15:
            return "DOJI"
        return "BULLISH" if c > o else "BEARISH"

    def detect_chop(self, ema_fast: float, ema_slow: float, price: float) -> tuple[bool, float]:
        """
        Detect sideways chop by measuring EMA convergence.
        If EMAs are within 0.1% of each other, market is likely choppy.
        """
        if ema_slow == 0:
            return False, 0.0
        distance_pct = abs(ema_fast - ema_slow) / ema_slow * 100
        is_choppy = distance_pct < 0.1
        return is_choppy, round(distance_pct, 4)

    def detect_volume_spike(self, volumes: np.ndarray, lookback: int = 20) -> tuple[bool, float]:
        """Check if current volume is above average. Returns (is_spike, ratio)."""
        if len(volumes) < lookback + 1:
            return False, 1.0
        avg_vol = np.mean(volumes[-(lookback + 1):-1])
        current_vol = volumes[-1]
        if avg_vol == 0:
            return False, 1.0
        ratio = current_vol / avg_vol
        return ratio > 1.5, round(ratio, 2)

    # ─── Higher Timeframe Trend ────────────────────────────────────────

    async def analyze_htf(self, symbol: str, interval: str = "15m") -> str:
        """
        Fetch higher timeframe candles and determine trend using EMA 9 vs EMA 21.
        Returns: BULLISH | BEARISH | NEUTRAL
        """
        try:
            raw = await self.fetch_candles(symbol, interval=interval, limit=100)
            if len(raw) < 30:
                return "NEUTRAL"

            closes = np.array([float(k[4]) for k in raw])
            ema_9 = self.ema(closes, 9)
            ema_21 = self.ema(closes, 21)

            fast = ema_9[-1]
            slow = ema_21[-1]
            price = closes[-1]

            if fast > slow and price > fast:
                return "BULLISH"
            elif fast < slow and price < fast:
                return "BEARISH"
            return "NEUTRAL"
        except Exception as e:
            logger.warning(f"HTF analysis failed for {symbol}: {e}")
            return "NEUTRAL"

    # ─── Main Analysis ─────────────────────────────────────────────────

    async def analyze(self, symbol: str, interval: str = "5m") -> IndicatorResult:
        """
        Full technical analysis with scalping confluence indicators.
        Fetches primary timeframe + 15m HTF in parallel.
        """
        logger.info(f"📊 Analyzing {symbol} on {interval}...")

        # Fetch primary and HTF candles in parallel
        primary_task = self.fetch_candles(symbol, interval, limit=200)
        htf_task = self.analyze_htf(symbol, interval="15m")
        raw, htf_trend = await asyncio.gather(primary_task, htf_task)

        if len(raw) < 60:
            raise ValueError(f"Insufficient candles for {symbol}: {len(raw)}")

        opens = np.array([float(k[1]) for k in raw])
        highs = np.array([float(k[2]) for k in raw])
        lows = np.array([float(k[3]) for k in raw])
        closes = np.array([float(k[4]) for k in raw])
        volumes = np.array([float(k[5]) for k in raw])

        result = IndicatorResult(
            symbol=symbol,
            timeframe=interval,
            close_prices=closes[-20:].tolist(),
            current_price=closes[-1],
            candles_used=len(raw),
            htf_trend=htf_trend,
        )

        # ── Scalping EMAs (9 / 21) ───────────────────────────────────
        ema_9_arr = self.ema(closes, 9)
        ema_21_arr = self.ema(closes, 21)
        result.ema_9 = round(float(ema_9_arr[-1]), 6)
        result.ema_21 = round(float(ema_21_arr[-1]), 6)

        # Legacy EMAs
        result.ema_20 = round(float(self.ema(closes, 20)[-1]), 6)
        result.ema_50 = round(float(self.ema(closes, 50)[-1]), 6)

        # ── Trend direction (EMA 9 vs EMA 21) ────────────────────────
        price = closes[-1]
        if result.ema_9 > result.ema_21:
            result.trend_direction = "BULLISH"
        elif result.ema_9 < result.ema_21:
            result.trend_direction = "BEARISH"
        else:
            result.trend_direction = "NEUTRAL"

        # ── VWAP ─────────────────────────────────────────────────────
        result.vwap = round(self.calc_vwap(highs, lows, closes, volumes), 6)
        if price > result.vwap * 1.001:
            result.price_vs_vwap = "ABOVE"
        elif price < result.vwap * 0.999:
            result.price_vs_vwap = "BELOW"
        else:
            result.price_vs_vwap = "AT"

        # ── RSI ──────────────────────────────────────────────────────
        result.rsi = self.rsi(closes)
        if result.rsi >= 70:
            result.rsi_signal = "OVERBOUGHT"
        elif result.rsi <= 30:
            result.rsi_signal = "OVERSOLD"
        else:
            result.rsi_signal = "NEUTRAL"

        # ── Volume Spike ─────────────────────────────────────────────
        result.volume_spike, result.volume_ratio = self.detect_volume_spike(volumes)
        result.current_volume = float(volumes[-1])
        result.avg_volume = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))

        # ── Candle Confirmation ──────────────────────────────────────
        result.candle_type = self.detect_candle_type(opens, highs, lows, closes)

        # ── Chop Detection ───────────────────────────────────────────
        result.is_choppy, result.ema_distance_pct = self.detect_chop(result.ema_9, result.ema_21, price)

        # ── MACD ─────────────────────────────────────────────────────
        macd_line, signal_line, histogram = self.macd(closes)
        result.macd_line = round(float(macd_line[-1]), 8)
        result.macd_signal = round(float(signal_line[-1]), 8)
        result.macd_histogram = round(float(histogram[-1]), 8)
        if histogram[-2] < 0 and histogram[-1] > 0:
            result.macd_crossover = "BULLISH"
        elif histogram[-2] > 0 and histogram[-1] < 0:
            result.macd_crossover = "BEARISH"
        else:
            result.macd_crossover = "NONE"

        # ── Bollinger Bands ──────────────────────────────────────────
        bb_upper, bb_mid, bb_lower = self.bollinger_bands(closes)
        result.bb_upper = round(float(bb_upper), 6)
        result.bb_middle = round(float(bb_mid), 6)
        result.bb_lower = round(float(bb_lower), 6)
        result.bb_width = round(float((bb_upper - bb_lower) / bb_mid * 100), 4) if bb_mid > 0 else 0.0

        # ── ATR ──────────────────────────────────────────────────────
        atr_val = self.atr(highs, lows, closes)
        result.atr = round(atr_val, 8)
        result.atr_pct = round((atr_val / closes[-1]) * 100, 4) if closes[-1] > 0 else 0.0

        logger.info(
            f"  EMA9={result.ema_9} EMA21={result.ema_21} | RSI={result.rsi} | "
            f"VWAP={result.price_vs_vwap} | VolSpike={result.volume_spike} | "
            f"Candle={result.candle_type} | Choppy={result.is_choppy} | "
            f"HTF={result.htf_trend} | ATR%={result.atr_pct}"
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
                "ema_9": result.ema_9,
                "ema_21": result.ema_21,
                "htf_trend": result.htf_trend,
            },
            "vwap": {
                "value": result.vwap,
                "price_position": result.price_vs_vwap,
            },
            "momentum": {
                "rsi": result.rsi,
                "rsi_signal": result.rsi_signal,
            },
            "volume": {
                "spike": result.volume_spike,
                "ratio": result.volume_ratio,
                "current": result.current_volume,
                "average": result.avg_volume,
            },
            "candle": {
                "type": result.candle_type,
            },
            "chop": {
                "is_choppy": result.is_choppy,
                "ema_distance_pct": result.ema_distance_pct,
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
            },
            "volatility": {
                "atr": result.atr,
                "atr_pct": result.atr_pct,
            },
        }
