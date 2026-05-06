"""
V5 Swing Watchlist Engine (ENGINE B)

Analyzes 4H candles for swing trade opportunities.
Does NOT instantly execute — stores setups in database watchlist.
Re-checks every cycle, executes only when confidence rises enough.

3 Swing Strategies:
  1. Trend Continuation — 4H strong trend + pullback to support
  2. Breakout Base — long consolidation near breakout + rising volume
  3. Major Reversal — exhaustion at key levels + structure shift

Swing setups require higher confidence than scalps to execute.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import numpy as np

from app.config import settings
from app.database import async_session
from app.models.trading import SwingWatchlist

from sqlalchemy import select, and_

logger = logging.getLogger(__name__)


@dataclass
class SwingSetup:
    symbol: str
    side: str              # BUY | SELL
    setup_type: str        # trend_continuation | breakout_base | major_reversal
    confidence: int        # 0-100
    trigger_price: float   # Price to trigger entry
    invalidation_price: float  # If price hits this, setup is invalid
    current_price: float
    reason: str
    regime: str = ""


class SwingEngine:
    """
    V5 Swing Watchlist Engine — delayed execution memory system.
    Scans 4H candles, stores promising setups, re-evaluates each cycle.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url

    # ─── Helpers ──────────────────────────────────────────────────────

    def _ema(self, values: np.ndarray, period: int) -> np.ndarray:
        k = 2 / (period + 1)
        result = np.zeros(len(values))
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i - 1] * (1 - k)
        return result

    def _rsi(self, closes: np.ndarray, period: int = 14) -> float:
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

    def _atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_list.append(tr)
        tr_arr = np.array(tr_list)
        if len(tr_arr) < period:
            return float(np.mean(tr_arr)) if len(tr_arr) > 0 else 0.0
        return float(np.mean(tr_arr[-period:]))

    async def _fetch_candles(self, symbol: str, interval: str = "4h", limit: int = 100) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    # ─── Strategy 1: Trend Continuation ───────────────────────────────

    def _score_trend_continuation(
        self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        volumes: np.ndarray, ema20: np.ndarray, ema50: np.ndarray
    ) -> Optional[SwingSetup]:
        """
        4H trend strong + pullback to support = continuation setup.
        LONG: EMA20 > EMA50, price pulled back near EMA20, now bouncing.
        SHORT: EMA20 < EMA50, price rallied to EMA20, now rejecting.
        """
        price = float(closes[-1])
        ema20_val = float(ema20[-1])
        ema50_val = float(ema50[-1])
        ema_dist = abs(ema20_val - ema50_val) / ema50_val * 100 if ema50_val > 0 else 0

        # Need clear trend (EMA separation > 0.5%)
        if ema_dist < 0.5:
            return None

        atr = self._atr(highs, lows, closes, 14)
        rsi = self._rsi(closes)
        avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))
        vol_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 1.0

        # LONG continuation
        if ema20_val > ema50_val and price > ema50_val:
            dist_to_ema = abs(price - ema20_val)
            if dist_to_ema < atr * 1.5 and price >= ema20_val * 0.995:
                score = 50
                if rsi > 40 and rsi < 65:
                    score += 10
                if ema_dist > 1.0:
                    score += 10
                if vol_ratio > 0.6:  # V17: lowered from 0.8
                    score += 5
                if closes[-1] > closes[-2]:
                    score += 10
                recent_low = min(float(lows[-3]), float(lows[-4]), float(lows[-5]))
                if recent_low < ema20_val:
                    score += 15

                swing_min = 70  # V17: lowered from settings.SWING_MIN_CONFIDENCE (80)
                if score >= swing_min:
                    return SwingSetup(
                        symbol="", side="BUY", setup_type="trend_continuation",
                        confidence=min(score, 98),
                        trigger_price=round(ema20_val * 1.002, 8),
                        invalidation_price=round(ema50_val * 0.99, 8),
                        current_price=price,
                        reason=f"4H uptrend pullback to EMA20 | EMA dist={ema_dist:.2f}% RSI={rsi:.0f}",
                    )

        # SHORT continuation
        if ema20_val < ema50_val and price < ema50_val:
            dist_to_ema = abs(price - ema20_val)
            # V17: widened from 1.005 to 1.012 — more SHORT setups qualify
            if dist_to_ema < atr * 1.5 and price <= ema20_val * 1.012:
                score = 50
                if rsi > 35 and rsi < 60:
                    score += 10
                if ema_dist > 1.0:
                    score += 10
                if vol_ratio > 0.6:  # V17: lowered from 0.8
                    score += 5
                if closes[-1] < closes[-2]:
                    score += 10
                recent_high = max(float(highs[-3]), float(highs[-4]), float(highs[-5]))
                if recent_high > ema20_val:
                    score += 15
                # V17: Lower-high detection bonus
                if len(highs) >= 6 and float(highs[-1]) < float(highs[-4]):
                    score += 8
                    logger.debug(f"  Swing SHORT: lower-high detected +8")

                swing_min = 70  # V17: lowered from settings.SWING_MIN_CONFIDENCE (80)
                if score >= swing_min:
                    return SwingSetup(
                        symbol="", side="SELL", setup_type="trend_continuation",
                        confidence=min(score, 98),
                        trigger_price=round(ema20_val * 0.998, 8),
                        invalidation_price=round(ema50_val * 1.01, 8),
                        current_price=price,
                        reason=f"4H downtrend rally rejection at EMA20 | EMA dist={ema_dist:.2f}% RSI={rsi:.0f}",
                    )

        return None

    # ─── Strategy 2: Breakout Base ────────────────────────────────────

    def _score_breakout_base(
        self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        volumes: np.ndarray
    ) -> Optional[SwingSetup]:
        """
        Long consolidation near breakout level + rising volume.
        Detects tight range (low ATR) with volume building.
        """
        if len(closes) < 30:
            return None

        price = float(closes[-1])
        atr = self._atr(highs, lows, closes, 14)
        atr_pct = (atr / price) * 100 if price > 0 else 0

        # Need consolidation (low recent ATR relative to historical)
        recent_atr = self._atr(highs[-15:], lows[-15:], closes[-15:], 10)
        historical_atr = self._atr(highs[-50:-15], lows[-50:-15], closes[-50:-15], 14) if len(closes) > 50 else atr
        if historical_atr <= 0:
            return None

        compression_ratio = recent_atr / historical_atr
        if compression_ratio > 0.7:  # Not enough compression
            return None

        # Find range boundaries
        recent_high = float(np.max(highs[-20:]))
        recent_low = float(np.min(lows[-20:]))
        range_pct = ((recent_high - recent_low) / recent_low * 100) if recent_low > 0 else 100

        if range_pct > 10:  # Range too wide for consolidation
            return None

        # Volume building (rising average volume)
        vol_early = float(np.mean(volumes[-20:-10]))
        vol_late = float(np.mean(volumes[-10:]))
        vol_building = vol_late > vol_early * 1.1

        # Score
        score = 55
        if compression_ratio < 0.5:
            score += 15
        if vol_building:
            score += 10
        if range_pct < 5:
            score += 10
        # Near top of range = likely upside breakout
        range_position = (price - recent_low) / (recent_high - recent_low) if (recent_high - recent_low) > 0 else 0.5

        if range_position > 0.7:
            side = "BUY"
            trigger = round(recent_high * 1.003, 8)
            invalidation = round(recent_low * 0.995, 8)
            score += 8
        elif range_position < 0.3:
            side = "SELL"
            trigger = round(recent_low * 0.997, 8)
            invalidation = round(recent_high * 1.005, 8)
            score += 8
        else:
            return None  # Mid-range, no clear direction

        if score >= settings.SWING_MIN_CONFIDENCE:
            return SwingSetup(
                symbol="", side=side, setup_type="breakout_base",
                confidence=min(score, 98),
                trigger_price=trigger,
                invalidation_price=invalidation,
                current_price=price,
                reason=f"4H consolidation breakout | range={range_pct:.1f}% compression={compression_ratio:.2f} vol_building={vol_building}",
            )
        return None

    # ─── Strategy 3: Major Reversal Zone ──────────────────────────────

    def _score_major_reversal(
        self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        volumes: np.ndarray, ema20: np.ndarray, ema50: np.ndarray
    ) -> Optional[SwingSetup]:
        """
        Exhaustion move at major support/resistance + structure shift.
        Needs strong rejection wick + volume surge.
        """
        if len(closes) < 30:
            return None

        price = float(closes[-1])
        rsi = self._rsi(closes)
        atr = self._atr(highs, lows, closes, 14)

        avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))
        vol_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 1.0

        # Check for exhaustion (RSI extreme + volume spike)
        # LONG reversal (oversold bounce)
        if rsi < 30 and vol_ratio > 1.5:
            # Rejection wick (long lower wick)
            last_body = abs(float(closes[-1]) - float(closes[-2]))
            last_low_wick = float(closes[-1]) - float(lows[-1]) if closes[-1] > lows[-1] else 0
            if last_low_wick > last_body * 1.5 and last_low_wick > atr * 0.5:
                score = 60
                if rsi < 25:
                    score += 10
                if vol_ratio > 2.0:
                    score += 10
                # Price near or below EMA50 (major level)
                if price < float(ema50[-1]) * 1.01:
                    score += 10

                if score >= settings.SWING_MIN_CONFIDENCE:
                    return SwingSetup(
                        symbol="", side="BUY", setup_type="major_reversal",
                        confidence=min(score, 98),
                        trigger_price=round(price * 1.005, 8),
                        invalidation_price=round(float(lows[-1]) * 0.995, 8),
                        current_price=price,
                        reason=f"4H oversold reversal | RSI={rsi:.0f} vol={vol_ratio:.1f}x rejection wick",
                    )

        # SHORT reversal (overbought rejection)
        if rsi > 70 and vol_ratio > 1.5:
            last_body = abs(float(closes[-1]) - float(closes[-2]))
            last_high_wick = float(highs[-1]) - float(closes[-1]) if highs[-1] > closes[-1] else 0
            if last_high_wick > last_body * 1.5 and last_high_wick > atr * 0.5:
                score = 60
                if rsi > 75:
                    score += 10
                if vol_ratio > 2.0:
                    score += 10
                if price > float(ema50[-1]) * 0.99:
                    score += 10

                if score >= settings.SWING_MIN_CONFIDENCE:
                    return SwingSetup(
                        symbol="", side="SELL", setup_type="major_reversal",
                        confidence=min(score, 98),
                        trigger_price=round(price * 0.995, 8),
                        invalidation_price=round(float(highs[-1]) * 1.005, 8),
                        current_price=price,
                        reason=f"4H overbought rejection | RSI={rsi:.0f} vol={vol_ratio:.1f}x rejection wick",
                    )

        return None

    # ─── Main Scan ────────────────────────────────────────────────────

    async def scan_symbol(self, symbol: str, regime: str = "") -> list[SwingSetup]:
        """Scan a single symbol for swing setups across all 3 strategies."""
        try:
            raw = await self._fetch_candles(symbol, "4h", 100)
            if len(raw) < 50:
                return []

            closes = np.array([float(k[4]) for k in raw])
            highs = np.array([float(k[2]) for k in raw])
            lows = np.array([float(k[3]) for k in raw])
            volumes = np.array([float(k[5]) for k in raw])

            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)

            setups = []

            # Strategy 1: Trend Continuation
            s1 = self._score_trend_continuation(closes, highs, lows, volumes, ema20, ema50)
            if s1:
                s1.symbol = symbol
                s1.regime = regime
                setups.append(s1)

            # Strategy 2: Breakout Base
            s2 = self._score_breakout_base(closes, highs, lows, volumes)
            if s2:
                s2.symbol = symbol
                s2.regime = regime
                setups.append(s2)

            # Strategy 3: Major Reversal
            s3 = self._score_major_reversal(closes, highs, lows, volumes, ema20, ema50)
            if s3:
                s3.symbol = symbol
                s3.regime = regime
                setups.append(s3)

            return setups

        except Exception as e:
            logger.warning(f"Swing scan failed for {symbol}: {e}")
            return []

    async def scan_multiple(self, symbols: list[str], regime: str = "") -> list[SwingSetup]:
        """Scan multiple symbols for swing setups concurrently."""
        logger.info(f"🔭 Swing scan: {len(symbols)} symbols...")
        semaphore = asyncio.Semaphore(5)

        async def scan_limited(symbol):
            async with semaphore:
                return await self.scan_symbol(symbol, regime)

        tasks = [scan_limited(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_setups = []
        for r in results:
            if isinstance(r, list):
                all_setups.extend(r)

        all_setups.sort(key=lambda x: x.confidence, reverse=True)
        logger.info(f"  Found {len(all_setups)} swing setups")
        return all_setups

    # ─── Watchlist Management ─────────────────────────────────────────

    async def save_to_watchlist(self, setups: list[SwingSetup]) -> int:
        """Save new swing setups to database watchlist."""
        saved = 0
        try:
            async with async_session() as session:
                # Check current watchlist count
                result = await session.execute(
                    select(SwingWatchlist).where(SwingWatchlist.status == "watching")
                )
                current_count = len(result.scalars().all())

                for setup in setups:
                    if current_count >= settings.SWING_WATCHLIST_MAX:
                        break

                    # Check if symbol already in watchlist with same side
                    existing = await session.execute(
                        select(SwingWatchlist).where(
                            and_(
                                SwingWatchlist.symbol == setup.symbol,
                                SwingWatchlist.side == setup.side,
                                SwingWatchlist.status == "watching",
                            )
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue  # Already watching this setup

                    entry = SwingWatchlist(
                        symbol=setup.symbol,
                        side=setup.side,
                        setup_type=setup.setup_type,
                        confidence=setup.confidence,
                        trigger_price=setup.trigger_price,
                        invalidation_price=setup.invalidation_price,
                        current_price=setup.current_price,
                        regime_at_creation=setup.regime,
                        notes=setup.reason,
                        status="watching",
                    )
                    session.add(entry)
                    saved += 1
                    current_count += 1

                await session.commit()
                logger.info(f"  📋 Saved {saved} new swing setups to watchlist")

        except Exception as e:
            logger.warning(f"Failed to save swing setups: {e}")

        return saved

    async def update_watchlist(self) -> list[dict]:
        """
        Re-evaluate all 'watching' setups.
        Returns list of setups ready to execute.
        """
        executable = []
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(SwingWatchlist).where(SwingWatchlist.status == "watching")
                )
                entries = result.scalars().all()

                if not entries:
                    return []

                logger.info(f"  🔄 Re-evaluating {len(entries)} swing watchlist entries...")

                for entry in entries:
                    try:
                        # Fetch current price
                        async with httpx.AsyncClient(timeout=10) as client:
                            resp = await client.get(
                                f"{self.base_url}/fapi/v1/ticker/price",
                                params={"symbol": entry.symbol},
                            )
                            resp.raise_for_status()
                            current_price = float(resp.json()["price"])

                        entry.current_price = current_price

                        # Check invalidation
                        if entry.side == "BUY" and current_price < entry.invalidation_price:
                            entry.status = "invalidated"
                            logger.info(f"    ❌ {entry.symbol} BUY invalidated: price {current_price} < {entry.invalidation_price}")
                            continue
                        if entry.side == "SELL" and current_price > entry.invalidation_price:
                            entry.status = "invalidated"
                            logger.info(f"    ❌ {entry.symbol} SELL invalidated: price {current_price} > {entry.invalidation_price}")
                            continue

                        # Check expiry
                        age_hours = (datetime.now(timezone.utc) - entry.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                        if age_hours > settings.SWING_EXPIRY_HOURS:
                            entry.status = "expired"
                            logger.info(f"    ⏰ {entry.symbol} expired after {age_hours:.0f}h")
                            continue

                        # Check trigger
                        triggered = False
                        if entry.side == "BUY" and current_price >= entry.trigger_price:
                            triggered = True
                        elif entry.side == "SELL" and current_price <= entry.trigger_price:
                            triggered = True

                        if triggered and entry.confidence >= settings.SWING_EXECUTE_CONFIDENCE:
                            entry.status = "triggered"
                            executable.append({
                                "symbol": entry.symbol,
                                "side": entry.side,
                                "setup_type": entry.setup_type,
                                "confidence": entry.confidence,
                                "trigger_price": entry.trigger_price,
                                "current_price": current_price,
                                "reason": entry.notes or f"Swing {entry.setup_type} triggered",
                                "strategy_type": f"swing_{entry.setup_type}",
                            })
                            logger.info(
                                f"    🎯 {entry.symbol} {entry.side} TRIGGERED: "
                                f"price={current_price} trigger={entry.trigger_price} conf={entry.confidence}"
                            )

                        # Boost confidence if setup strengthening
                        elif triggered and entry.confidence < settings.SWING_EXECUTE_CONFIDENCE:
                            entry.confidence = min(entry.confidence + 3, 98)
                            logger.info(f"    📈 {entry.symbol} approaching trigger, conf boosted to {entry.confidence}")

                    except Exception as e:
                        logger.warning(f"    Failed to update {entry.symbol}: {e}")

                await session.commit()

        except Exception as e:
            logger.warning(f"Watchlist update failed: {e}")

        logger.info(f"  📋 Watchlist: {len(executable)} setups ready to execute")
        return executable

    async def cleanup_old(self) -> int:
        """Remove invalidated and expired entries older than 7 days."""
        removed = 0
        try:
            async with async_session() as session:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                result = await session.execute(
                    select(SwingWatchlist).where(
                        and_(
                            SwingWatchlist.status.in_(["invalidated", "expired", "executed"]),
                            SwingWatchlist.created_at < cutoff,
                        )
                    )
                )
                old_entries = result.scalars().all()
                for entry in old_entries:
                    await session.delete(entry)
                    removed += 1
                await session.commit()
        except Exception as e:
            logger.warning(f"Watchlist cleanup failed: {e}")
        return removed
