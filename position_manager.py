#!/usr/bin/env python3
"""
V11 Position Manager — 24/7 Trade Exit Engine

New in V11:
  - Candle hi/lo TP check: catches intracandle TP touches missed by price snapshot
  - Stale trade detection: alert (and optionally force-close) long-stuck positions
  - Orphan sync on startup: reconcile DB open_positions vs Binance live positions
  - Retry failed closes: up to PM_MAX_CLOSE_RETRIES attempts before alerting
  - Differentiated scan speed: scalp bucket every 2s, swing bucket every 10s
  - V11 entry gate: calls state_manager.record_pnl() after each close

All V9 behaviour preserved where not explicitly changed.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.trading import OpenPosition, Trade
from app.models.user import Account
from app.modules.close_engine import CloseEngine, CloseResult
from app.modules.crypto_utils import decrypt_api_key
from app.modules.price_stream import PriceStream
from app.modules.telegram import TelegramNotifier
from app.modules.binance_sync import sync_all_accounts, get_binance_live_positions  # V12
from app.utils.state import state_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("position_manager")

# ── Config ────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS  = 1.0
PRICE_STALE_MAX_SECONDS = 10.0
SYMBOL_REFRESH_INTERVAL = 30
TRAILING_SL_PCT_SCALP   = 0.4
TRAILING_SL_PCT_SWING   = 0.5
POSITION_MANAGER_VERSION = "V13"
# V13: fee buffer for breakeven calculation (0.12% round-trip)
BE_FEE_BUFFER = settings.V13_FEE_BUFFER_PCT / 100.0

# ── DB ────────────────────────────────────────────────────────────────
engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Helpers ───────────────────────────────────────────────────────────

def _duration_minutes(opened_at: datetime) -> int:
    try:
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - opened_at).total_seconds() / 60))
    except Exception:
        return 0


def _is_swing(strategy_type: str) -> bool:
    return (strategy_type or "").startswith("swing")

def _is_sniper(strategy_type: str) -> bool:
    return (strategy_type or "").startswith("sniper")


def _stale_threshold_hours(strategy_type: str) -> int:
    return settings.SWING_STALE_HOURS if _is_swing(strategy_type) else settings.SCALP_STALE_HOURS


def _get_be_trigger_roi(strategy_type: str) -> float:
    """V13: Per-mode ROI% threshold that triggers break-even SL move."""
    st = (strategy_type or "").lower()
    if _is_swing(st):
        return settings.V13_SWING_BE_TRIGGER_ROI    # 18%
    elif _is_sniper(st):
        return settings.V13_SNIPER_BE_TRIGGER_ROI   # 25%
    else:
        return settings.V13_SCALP_BE_TRIGGER_ROI    # 10%


def _calc_roi_pct(side: str, entry: float, current: float, leverage: int) -> float:
    """V13: Calculate position ROI% using leverage."""
    if entry <= 0 or leverage <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry * leverage * 100
    else:
        return (entry - current) / entry * leverage * 100


# ═════════════════════════════════════════════════════════════════════
class PositionManager:
    """V11 Position Manager."""

    def __init__(self):
        self.telegram   = TelegramNotifier()
        self.price_stream = PriceStream(testnet=settings.BINANCE_TESTNET)
        self._close_engines: dict[int, CloseEngine] = {}
        self._account_credentials: dict[int, dict] = {}
        # In-memory retry counters: position_id -> attempt count
        self._close_attempts: dict[int, int] = {}
        self._running = False
        self._tick = 0

    # ── Credentials ──────────────────────────────────────────────────

    async def _load_account_credentials(self) -> None:
        try:
            from app.models.user import ApiConnection
            async with AsyncSessionFactory() as session:
                stmt = (
                    select(Account, ApiConnection)
                    .join(ApiConnection, ApiConnection.account_id == Account.id)
                    .where(Account.is_active == True)
                    .where(Account.bot_enabled == True)
                    .where(ApiConnection.is_active == True)
                )
                result = await session.execute(stmt)
                rows = result.all()

            loaded = 0
            for acc, conn in rows:
                if not conn.api_key_encrypted or not conn.api_secret_encrypted:
                    continue
                try:
                    self._account_credentials[acc.id] = {
                        "api_key":    decrypt_api_key(conn.api_key_encrypted),
                        "api_secret": decrypt_api_key(conn.api_secret_encrypted),
                    }
                    loaded += 1
                except Exception as e:
                    logger.warning(f"Decrypt failed account {acc.id}: {e}")

            if settings.BINANCE_API_KEY and 0 not in self._account_credentials:
                self._account_credentials[0] = {
                    "api_key":    settings.BINANCE_API_KEY,
                    "api_secret": settings.BINANCE_SECRET_KEY,
                }
            logger.info(f"[PM] Credentials: {loaded} DB account(s)")
        except Exception as e:
            logger.error(f"[PM] Credential load failed: {e}")

    def _get_close_engine(self, account_id: int) -> Optional[CloseEngine]:
        if account_id not in self._close_engines:
            creds = self._account_credentials.get(account_id) or self._account_credentials.get(0)
            if not creds:
                return None
            self._close_engines[account_id] = CloseEngine(
                api_key=creds["api_key"],
                secret_key=creds["api_secret"],
                testnet=settings.BINANCE_TESTNET,
            )
        return self._close_engines[account_id]

    # ── Open Position Loader ──────────────────────────────────────────

    async def _load_open_positions(self) -> list[OpenPosition]:
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.status == "open")
                )
                return result.scalars().all()
        except Exception as e:
            logger.error(f"Load open positions failed: {e}")
            return []

    async def _get_tracked_symbols(self) -> list[str]:
        positions = await self._load_open_positions()
        return list(set(p.symbol for p in positions))

    # ── V11: Candle Hi/Lo TP Check ────────────────────────────────────

    async def _get_recent_candle_extremes(
        self, symbol: str, account_id: int
    ) -> tuple[Optional[float], Optional[float]]:
        """Fetch the last 3 1m candles and return (highest_high, lowest_low)."""
        try:
            ce = self._get_close_engine(account_id)
            if not ce:
                return None, None
            import httpx
            base = settings.binance_base_url
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    f"{base}/fapi/v1/klines",
                    params={"symbol": symbol, "interval": "1m", "limit": 3},
                )
                resp.raise_for_status()
                candles = resp.json()
            if not candles:
                return None, None
            highs = [float(c[2]) for c in candles]
            lows  = [float(c[3]) for c in candles]
            return max(highs), min(lows)
        except Exception as e:
            logger.debug(f"[{symbol}] Candle fetch failed: {e}")
            return None, None

    def _check_candle_tp(
        self,
        pos: OpenPosition,
        candle_high: Optional[float],
        candle_low: Optional[float],
    ) -> Optional[str]:
        """
        V11: Check if a recent candle high/low touched TP.
        For LONG: if candle_high >= tp_price → tp_hit
        For SHORT: if candle_low <= tp_price → tp_hit
        """
        if candle_high is None or candle_low is None:
            return None
        tp = pos.tp_price
        if not tp or tp <= 0:
            return None
        is_long = pos.side == "BUY"
        if is_long and candle_high >= tp:
            logger.info(f"  📈 [{pos.symbol}] CANDLE TP HIT: high={candle_high} >= tp={tp}")
            return "tp_hit"
        if not is_long and candle_low <= tp:
            logger.info(f"  📉 [{pos.symbol}] CANDLE TP HIT: low={candle_low} <= tp={tp}")
            return "tp_hit"
        return None

    # ── Trailing Stop ─────────────────────────────────────────────────

    def _update_trailing(
        self, pos: OpenPosition, current_price: float
    ) -> tuple[bool, Optional[float]]:
        entry = pos.entry_price
        if entry <= 0 or current_price <= 0:
            return False, None
        is_long = pos.side == "BUY"
        swing   = _is_swing(pos.strategy_type or "")

        roi_pct = ((current_price - entry) / entry * 100) if is_long else ((entry - current_price) / entry * 100)

        new_highest = max(pos.highest_price or entry, current_price) if is_long else (pos.highest_price or entry)
        new_lowest  = min(pos.lowest_price or entry, current_price) if not is_long else (pos.lowest_price or entry)

        trigger_pct = pos.trailing_trigger_pct or settings.BREAK_EVEN_TRIGGER_PCT

        if not pos.trailing_active and roi_pct >= trigger_pct:
            pos.trailing_active = True
            logger.info(f"  🔄 [{pos.symbol}] Trailing activated: roi={roi_pct:.2f}%")

        new_trailing_sl = pos.trailing_sl_price
        if pos.trailing_active:
            tp_dist    = abs(pos.tp_price - entry)
            trail_mult = TRAILING_SL_PCT_SWING if swing else TRAILING_SL_PCT_SCALP
            trail_dist = tp_dist * trail_mult

            if is_long:
                candidate = new_highest - trail_dist
                if new_trailing_sl is None or candidate > new_trailing_sl:
                    new_trailing_sl = candidate
                if current_price <= new_trailing_sl:
                    pos.highest_price = new_highest
                    pos.lowest_price  = new_lowest
                    pos.trailing_sl_price = new_trailing_sl
                    return True, new_trailing_sl
            else:
                candidate = new_lowest + trail_dist
                if new_trailing_sl is None or candidate < new_trailing_sl:
                    new_trailing_sl = candidate
                if current_price >= new_trailing_sl:
                    pos.highest_price = new_highest
                    pos.lowest_price  = new_lowest
                    pos.trailing_sl_price = new_trailing_sl
                    return True, new_trailing_sl

        pos.highest_price = new_highest
        pos.lowest_price  = new_lowest
        pos.trailing_sl_price = new_trailing_sl
        return False, new_trailing_sl

    # ── TP/SL Check ───────────────────────────────────────────────────

    def _check_tp_sl(self, pos: OpenPosition, current_price: float) -> Optional[str]:
        is_long = pos.side == "BUY"
        tp, sl  = pos.tp_price, pos.sl_price
        if tp <= 0 or sl <= 0:
            return None
        if is_long:
            if current_price >= tp: return "tp_hit"
            if current_price <= sl: return "sl_hit"
        else:
            if current_price <= tp: return "tp_hit"
            if current_price >= sl: return "sl_hit"
        return None

    # ── V11: Stale Detection ──────────────────────────────────────────

    async def _check_stale(self, pos: OpenPosition, current_price: float) -> None:
        """Alert (and optionally close) positions stuck beyond the stale threshold."""
        if not pos.opened_at:
            return
        threshold_h = _stale_threshold_hours(pos.strategy_type or "")
        open_hours  = _duration_minutes(pos.opened_at) / 60.0
        if open_hours < threshold_h:
            return

        # Check DB stale_alerted flag to avoid repeat alerts
        try:
            async with AsyncSessionFactory() as session:
                db_pos = await session.get(OpenPosition, pos.id)
                if db_pos and getattr(db_pos, "stale_alerted", False):
                    if not settings.STALE_CLOSE_ENABLED:
                        return
                    # Already alerted; if auto-close enabled, close now
                    logger.warning(f"[PM] Stale auto-close: {pos.symbol}")
                    await self._close_position(pos, "stale_close", current_price)
                    return
                # Mark alerted
                if db_pos:
                    try:
                        db_pos.stale_alerted = True
                        await session.commit()
                    except Exception:
                        pass
        except Exception:
            pass

        will_close = settings.STALE_CLOSE_ENABLED
        logger.warning(f"[PM] Stale position: {pos.symbol} open {open_hours:.1f}h")
        try:
            await self.telegram.send_stale_trade_alert(
                symbol=pos.symbol,
                side=pos.side,
                strategy_type=pos.strategy_type or "",
                entry_price=pos.entry_price,
                current_price=current_price,
                open_hours=open_hours,
                stale_threshold_hours=threshold_h,
                will_force_close=will_close,
            )
        except Exception:
            pass
        if will_close:
            await self._close_position(pos, "stale_close", current_price)

    # ── V11: Orphan Sync ──────────────────────────────────────────────

    async def _check_momentum_exit(
        self, pos: OpenPosition, current_price: float, roi_pct: float
    ) -> bool:
        """
        V13 Anti-Reverse Profit Protection (Part 5).
        Detects when a profitable trade is losing momentum and exits early.

        Triggers when:
          - Peak ROI exceeded V13_MOMENTUM_MIN_PEAK_ROI (default 5%)
          - Current ROI has retraced > V13_MOMENTUM_RETRACE_PCT (default 40%) from peak

        Retrace example: peak ROI=10%, retrace=40% → exit if ROI drops below 6%.
        """
        if not settings.V13_MOMENTUM_EXIT_ENABLED:
            return False

        entry    = pos.entry_price
        leverage = pos.leverage or 1

        # Track peak ROI in highest_price/lowest_price fields
        if pos.side == "BUY":
            peak_price = pos.highest_price or entry
            peak_roi   = _calc_roi_pct(pos.side, entry, peak_price, leverage)
        else:
            peak_price = pos.lowest_price or entry
            peak_roi   = _calc_roi_pct(pos.side, entry, peak_price, leverage)

        if peak_roi < settings.V13_MOMENTUM_MIN_PEAK_ROI:
            return False   # Never reached minimum profit — don't interfere

        # Calculate retrace threshold
        retrace_threshold_roi = peak_roi * (1.0 - settings.V13_MOMENTUM_RETRACE_PCT / 100.0)

        if roi_pct < retrace_threshold_roi:
            logger.info(
                f"  [V13 MOMENTUM EXIT] {pos.symbol}: peak_ROI={peak_roi:.1f}% "
                f"current_ROI={roi_pct:.1f}% < retrace_threshold={retrace_threshold_roi:.1f}% "
                f"→ exiting early to protect profits"
            )
            return True

        return False

    async def _activate_break_even(
        self, pos: OpenPosition, current_price: float
    ) -> None:
        """
        V13: Move SL to breakeven + fee buffer and persist to DB.
        Called when position ROI crosses the per-mode BE trigger.
        """
        entry    = pos.entry_price
        fee_buf  = BE_FEE_BUFFER  # 0.0012 = 0.12%

        if pos.side == "BUY":
            be_price = round(entry * (1 + fee_buf), 8)
        else:
            be_price = round(entry * (1 - fee_buf), 8)

        # Only move SL if new BE is better than current SL
        if pos.side == "BUY" and pos.sl_price and be_price <= pos.sl_price:
            return  # Already protected or better
        if pos.side == "SELL" and pos.sl_price and be_price >= pos.sl_price:
            return

        old_sl = pos.sl_price
        pos.sl_price = be_price
        pos.trailing_active = True

        # Persist to DB
        try:
            async with AsyncSessionFactory() as session:
                db_pos = await session.get(OpenPosition, pos.id)
                if db_pos and db_pos.status == "open":
                    db_pos.sl_price       = be_price
                    db_pos.trailing_active = True
                    db_pos.last_checked_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info(
                f"  [V13 BE] {pos.symbol}: SL moved {old_sl} → {be_price} "
                f"(entry={entry} + fee_buf={fee_buf*100:.2f}%)"
            )
            # Telegram notification
            leverage = pos.leverage or 1
            roi_pct = _calc_roi_pct(pos.side, entry, current_price, leverage)
            await self.telegram.break_even_moved(
                symbol=pos.symbol, side=pos.side,
                entry_price=entry, be_price=be_price,
                roi_pct=roi_pct,
            )
        except Exception as e:
            logger.error(f"  [V13 BE] DB update failed for {pos.symbol}: {e}")


    async def _orphan_sync(self) -> None:
        """Compare DB open_positions against Binance live positions on startup."""
        if not settings.PM_ORPHAN_SYNC_ENABLED:
            return
        logger.info("[PM] Running orphan sync...")
        positions = await self._load_open_positions()
        if not positions:
            return
        for pos in positions:
            ce = self._get_close_engine(pos.account_id)
            if not ce:
                continue
            try:
                has_live = await ce.has_open_position(pos.symbol) if hasattr(ce, "has_open_position") else True
                if not has_live:
                    logger.warning(f"[PM] Orphan detected: {pos.symbol} id={pos.id} in DB but not on Binance")
                    await self.telegram.send_orphan_position_alert(
                        symbol=pos.symbol,
                        side=pos.side,
                        db_status="open",
                        binance_status="no_position",
                        position_id=pos.id,
                    )
                    # Mark as orphan in DB
                    async with AsyncSessionFactory() as session:
                        db_pos = await session.get(OpenPosition, pos.id)
                        if db_pos and db_pos.status == "open":
                            db_pos.status = "orphan"
                            db_pos.close_reason = "orphan_sync"
                            await session.commit()
            except Exception as e:
                logger.debug(f"[PM] Orphan check failed for {pos.symbol}: {e}")

    # ── Close Handler (with V11 retry) ────────────────────────────────

    async def _close_position(
        self, pos: OpenPosition, close_reason: str, current_price: float,
    ) -> None:
        logger.info(f"  🔒 Closing {pos.symbol} {pos.side} reason={close_reason} price={current_price}")
        ce = self._get_close_engine(pos.account_id)
        if not ce:
            logger.error(f"  ❌ No CloseEngine for account {pos.account_id}")
            await self.telegram.close_failed_manual(
                pos.symbol, pos.side, close_reason,
                f"No API credentials for account {pos.account_id}"
            )
            return

        # V11: Retry loop
        max_retries = settings.PM_MAX_CLOSE_RETRIES
        pos_id = pos.id
        attempts = self._close_attempts.get(pos_id, 0)

        for attempt in range(1, max_retries + 1):
            try:
                close_result: CloseResult = await ce.market_close(
                    symbol=pos.symbol,
                    side=pos.side,
                    quantity=pos.quantity,
                    entry_price=pos.entry_price,
                    close_reason=close_reason,
                    is_hedge_mode=pos.is_hedge_mode,
                    position_side=pos.position_side or "BOTH",
                )
                if close_result.success:
                    self._close_attempts.pop(pos_id, None)
                    duration_mins = _duration_minutes(pos.opened_at)
                    await self._mark_closed(pos, close_result.close_price, close_result.pnl_usdt, close_result.pnl_pct, close_reason)
                    await self._send_close_notification(pos, close_reason, close_result.close_price, close_result.pnl_usdt, close_result.pnl_pct, duration_mins)
                    # V11: Record PnL for global entry gate
                    try:
                        state_manager.record_pnl(close_result.pnl_usdt)
                    except Exception:
                        pass
                    return
                else:
                    logger.warning(f"  ❌ Close attempt {attempt}/{max_retries} failed for {pos.symbol}: {close_result.error}")
            except Exception as e:
                logger.warning(f"  ❌ Close attempt {attempt}/{max_retries} exception for {pos.symbol}: {e}")

            if attempt < max_retries:
                await asyncio.sleep(settings.PM_CLOSE_RETRY_DELAY)

        # All retries exhausted
        self._close_attempts[pos_id] = attempts + max_retries
        logger.error(f"  🔥 Close FAILED after {max_retries} attempts: {pos.symbol}")
        await self.telegram.close_failed_manual(
            pos.symbol, pos.side, close_reason,
            f"Failed after {max_retries} retries — manual close required!"
        )

    # ── DB Updates ────────────────────────────────────────────────────

    async def _mark_closed(self, pos: OpenPosition, close_price: float, pnl_usdt: float, pnl_pct: float, close_reason: str) -> None:
        now = datetime.now(timezone.utc)
        try:
            async with AsyncSessionFactory() as session:
                db_pos = await session.get(OpenPosition, pos.id)
                if db_pos:
                    db_pos.status = "closed"
                    db_pos.close_price = close_price
                    db_pos.close_reason = close_reason
                    db_pos.pnl_usdt = pnl_usdt
                    db_pos.pnl_pct = pnl_pct
                    db_pos.closed_at = now
                    db_pos.last_checked_at = now
                    db_pos.trailing_active = pos.trailing_active
                    db_pos.trailing_sl_price = pos.trailing_sl_price
                    db_pos.highest_price = pos.highest_price
                    db_pos.lowest_price = pos.lowest_price

                if pos.trade_id:
                    db_trade = await session.get(Trade, pos.trade_id)
                    if db_trade:
                        db_trade.status = "closed"
                        db_trade.close_price = close_price
                        db_trade.pnl = pnl_usdt
                        db_trade.pnl_pct = pnl_pct
                        db_trade.close_reason = close_reason
                        db_trade.closed_at = now
                        db_trade.protection_status = "CLOSED"
                        db_trade.managed_by = "external_engine"

                await session.commit()
                logger.info(f"  [PM] DB closed: {pos.symbol} pnl={pnl_usdt:+.4f} reason={close_reason}")
        except Exception as e:
            logger.error(f"  [PM] DB update failed for {pos.symbol}: {e}")

    async def _update_position_price(self, pos: OpenPosition, current_price: float) -> None:
        try:
            async with AsyncSessionFactory() as session:
                db_pos = await session.get(OpenPosition, pos.id)
                if db_pos and db_pos.status == "open":
                    db_pos.last_price = current_price
                    db_pos.last_checked_at = datetime.now(timezone.utc)
                    db_pos.check_count = (db_pos.check_count or 0) + 1
                    db_pos.trailing_active = pos.trailing_active
                    db_pos.trailing_sl_price = pos.trailing_sl_price
                    db_pos.highest_price = pos.highest_price
                    db_pos.lowest_price = pos.lowest_price
                    await session.commit()
                if pos.trade_id and db_pos:
                    db_trade = await session.get(Trade, pos.trade_id)
                    if db_trade and getattr(db_trade, "protection_status", None) == "PENDING":
                        db_trade.protection_status = "ACTIVE"
                        await session.commit()
        except Exception:
            pass

    # ── Telegram ─────────────────────────────────────────────────────

    async def _send_close_notification(self, pos, close_reason, close_price, pnl_usdt, pnl_pct, duration_mins):
        strategy = pos.strategy_type or ""
        confidence = pos.confidence or 0
        if close_reason == "tp_hit":
            await self.telegram.trade_closed_tp(pos.symbol, pos.side, pos.entry_price, close_price, pnl_usdt, pnl_pct, strategy, confidence, pos.tp_price, duration_mins)
        elif close_reason == "sl_hit":
            await self.telegram.trade_closed_sl(pos.symbol, pos.side, pos.entry_price, close_price, pnl_usdt, pnl_pct, strategy, confidence, pos.sl_price, duration_mins)
        elif close_reason == "trailing_exit":
            await self.telegram.trade_closed_trailing(pos.symbol, pos.side, pos.entry_price, close_price, pnl_usdt, pnl_pct, pos.highest_price or 0.0, strategy, duration_mins)

    # ── Main Position Processor ───────────────────────────────────────

    async def _process_position(self, pos: OpenPosition) -> None:
        symbol = pos.symbol

        # Get price from WebSocket or REST fallback
        current_price = self.price_stream.get_price(symbol)
        if current_price is None or self.price_stream.is_stale(symbol, PRICE_STALE_MAX_SECONDS):
            ce = self._get_close_engine(pos.account_id)
            if ce:
                try:
                    current_price = await ce.get_market_price(symbol)
                except Exception as e:
                    logger.warning(f"  [{symbol}] Cannot get price: {e}")
                    return

        if not current_price or current_price <= 0:
            return

        # V13: Calculate live ROI
        leverage = pos.leverage or 1
        roi_pct  = _calc_roi_pct(pos.side, pos.entry_price, current_price, leverage)

        # 1. Hard TP/SL check (price snapshot)
        trigger = self._check_tp_sl(pos, current_price)
        if trigger:
            await self._close_position(pos, trigger, current_price)
            return

        # 2. V11: Candle hi/lo TP check (every PM_CANDLE_CHECK_TICKS ticks)
        if self._tick % settings.PM_CANDLE_CHECK_TICKS == 0:
            candle_high, candle_low = await self._get_recent_candle_extremes(symbol, pos.account_id)
            candle_trigger = self._check_candle_tp(pos, candle_high, candle_low)
            if candle_trigger:
                await self._close_position(pos, candle_trigger, candle_high or current_price)
                return

        # 3. V13: ROI-based break-even activation (per mode)
        if not pos.trailing_active:  # Only activate once
            be_trigger_roi = _get_be_trigger_roi(pos.strategy_type or "")
            if roi_pct >= be_trigger_roi:
                logger.info(
                    f"  [V13 BE TRIGGER] {symbol}: ROI={roi_pct:.1f}% >= "
                    f"be_trigger={be_trigger_roi:.1f}% → activating BE stop"
                )
                await self._activate_break_even(pos, current_price)

        # 4. Trailing stop (uses updated SL after BE activation)
        if pos.trailing_active:
            should_trail, _ = self._update_trailing(pos, current_price)
            if should_trail:
                await self._close_position(pos, "trailing_exit", current_price)
                return

        # 5. V13: Momentum exit — anti-reverse profit protection
        if settings.V13_MOMENTUM_EXIT_ENABLED:
            should_momentum_exit = await self._check_momentum_exit(pos, current_price, roi_pct)
            if should_momentum_exit:
                await self._close_position(pos, "momentum_exit", current_price)
                return

        # 6. V11: Stale detection (every 60 ticks)
        if self._tick % 60 == 0:
            await self._check_stale(pos, current_price)

        # 7. Update DB price (every 10 ticks)
        if self._tick % 10 == 0:
            await self._update_position_price(pos, current_price)


    # ── Main Loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("=" * 60)
        logger.info(f"🤖 Position Manager {POSITION_MANAGER_VERSION} starting...")
        logger.info("=" * 60)
        self._running = True

        try:
            await self.telegram.position_manager_started(POSITION_MANAGER_VERSION)
        except Exception:
            pass

        await self._load_account_credentials()

        # V12: Orphan sync on startup
        await self._orphan_sync()

        # V12: Initial Binance→DB sync on startup
        try:
            logger.info("[PM V12] Running initial Binance→DB sync...")
            sync_result = await sync_all_accounts()
            logger.info(
                f"[PM V12] Initial sync: ghosts={sync_result['ghosts']} "
                f"orphans={sync_result['orphans']} synced={sync_result['synced']}"
            )
        except Exception as se:
            logger.warning(f"[PM V12] Initial sync failed (non-critical): {se}")

        symbols = await self._get_tracked_symbols()
        if symbols:
            logger.info(f"  Resuming: {len(symbols)} symbols")
        await self.price_stream.start(symbols or [])

        last_symbol_refresh = 0
        last_binance_sync = 0  # V12: track sync ticks

        while self._running:
            loop_start = time.time()
            self._tick += 1

            try:
                if self._tick - last_symbol_refresh >= SYMBOL_REFRESH_INTERVAL:
                    symbols = await self._get_tracked_symbols()
                    await self.price_stream.update_symbols(symbols)
                    await self._load_account_credentials()
                    last_symbol_refresh = self._tick

                # V12: Periodic Binance→DB sync
                sync_ticks = max(1, settings.BINANCE_SYNC_INTERVAL)  # default 60s = 60 ticks
                if self._tick - last_binance_sync >= sync_ticks:
                    try:
                        sync_result = await sync_all_accounts()
                        if sync_result["ghosts"] > 0 or sync_result["orphans"] > 0:
                            logger.info(
                                f"[PM V12] Sync: ghosts={sync_result['ghosts']} "
                                f"orphans={sync_result['orphans']} "
                                f"synced={sync_result['synced']}"
                            )
                    except Exception as se:
                        logger.debug(f"[PM V12] Periodic sync error (non-critical): {se}")
                    last_binance_sync = self._tick

                # V12: Load open positions for monitoring
                # DB records drive the loop; sync above keeps them accurate.
                open_positions = await self._load_open_positions()

                if not open_positions:
                    logger.debug(f"[PM] No open positions — sleeping 5s (tick={self._tick})")
                    await asyncio.sleep(5.0)
                    continue

                semaphore = asyncio.Semaphore(10)

                async def process_guarded(p: OpenPosition):
                    async with semaphore:
                        try:
                            await self._process_position(p)
                        except Exception as e:
                            logger.error(f"  Error processing {p.symbol} (id={p.id}): {e}")

                await asyncio.gather(*[process_guarded(p) for p in open_positions], return_exceptions=True)

                if self._tick % 60 == 0:
                    # V12: Per-cycle summary — Binance live count + DB open + accounts
                    try:
                        from app.modules.binance_sync import count_all_live_positions as _count_live
                        _live_count = await _count_live()
                    except Exception:
                        _live_count = "?"
                    logger.info(
                        f"[PM V12 ⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                        f"tick={self._tick} | accounts={len(self._account_credentials)} | "
                        f"db_open={len(open_positions)} | binance_live={_live_count} | "
                        f"monitoring {len(open_positions)} position(s)"
                    )

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                try:
                    await self.telegram.position_manager_error(str(e), "Main loop")
                except Exception:
                    pass
                await asyncio.sleep(5.0)
                continue

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.0, CHECK_INTERVAL_SECONDS - elapsed))


# ── Entry Point ───────────────────────────────────────────────────────

async def main():
    manager = PositionManager()
    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("🛑 Position Manager stopped by user")
    except Exception as e:
        logger.critical(f"🔥 Fatal error: {e}", exc_info=True)
        try:
            await TelegramNotifier().position_manager_error(str(e), "Fatal crash!")
        except Exception:
            pass
        raise
    finally:
        await manager.price_stream.stop()
        await engine.dispose()


if __name__ == "__main__":
    from app.config import settings as _cfg
    if not _cfg.PYTHON_PM_ENABLED:
        logger.warning("🛑 PYTHON_PM_ENABLED=false — Python PM disabled.")
        sys.exit(0)
    asyncio.run(main())
