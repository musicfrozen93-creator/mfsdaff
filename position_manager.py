#!/usr/bin/env python3
"""
V9 Position Manager Bot — 24/7 Trade Exit Engine

Runs as a standalone process (separate from the main FastAPI bot).
Monitors all open positions and closes them when TP/SL/trailing triggers.

Architecture:
  - WebSocket price feed (PriceStream) for near-real-time prices
  - Position loop runs every CHECK_INTERVAL_SECONDS
  - Per-account multi-support: 10 users × any symbol, all tracked independently
  - Reads open_positions table written by main bot at trade open
  - Uses CloseEngine for market close (correct precision + hedge mode)
  - Updates trades table and open_positions table on close
  - Sends Telegram notifications on every close

Strategy-aware:
  - TP/SL prices stored at trade open by RiskEngine — NOT re-calculated
  - Trailing stop: activates after trailing_trigger_pct profit
  - Trailing SL moves with peak price (scalp: tighter, swing: wider)

Startup / Recovery:
  - On restart, loads ALL open_positions with status='open' from DB
  - Resumes monitoring immediately — no position is ever orphaned

Usage:
  python position_manager.py

Docker:
  CMD ["python", "position_manager.py"]
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Path setup (when run directly from root) ─────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

# ── App imports ───────────────────────────────────────────────────────
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

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("position_manager")

# ── Configuration ─────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS = 1.0        # Main loop tick (≈ 1 per second)
PRICE_STALE_MAX_SECONDS = 10.0      # If price older than this, use REST
SYMBOL_REFRESH_INTERVAL = 30        # Refresh symbol list every N ticks
TRAILING_SL_PCT_SCALP = 0.4        # Trailing SL = 40% of TP distance behind peak (scalp)
TRAILING_SL_PCT_SWING = 0.5        # Trailing SL = 50% of TP distance behind peak (swing)
POSITION_MANAGER_VERSION = "V9"


# ── Database session factory ──────────────────────────────────────────
engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _duration_minutes(opened_at: datetime) -> int:
    """Calculate how many minutes a trade has been open."""
    try:
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - opened_at
        return max(0, int(delta.total_seconds() / 60))
    except Exception:
        return 0


def _strategy_display(strategy_type: str) -> str:
    """Convert strategy_type to human-readable label."""
    if not strategy_type:
        return "Unknown"
    if strategy_type.startswith("swing"):
        return "🌊 Swing"
    elif strategy_type.startswith("sniper"):
        return "🎯 Sniper"
    return "⚡ Scalp"


def _is_swing(strategy_type: str) -> bool:
    return (strategy_type or "").startswith("swing")


# ─────────────────────────────────────────────────────────────────────
# Position Manager Class
# ─────────────────────────────────────────────────────────────────────

class PositionManager:
    """
    V9 24/7 Position Manager.
    Main loop: load open positions → check prices → close on trigger → notify.
    """

    def __init__(self):
        self.telegram = TelegramNotifier()
        self.price_stream = PriceStream(testnet=settings.BINANCE_TESTNET)
        self._close_engines: dict[int, CloseEngine] = {}   # account_id → CloseEngine
        self._account_credentials: dict[int, dict] = {}    # account_id → {api_key, secret}
        self._running = False
        self._tick = 0

    # ── Account credential management ────────────────────────────────

    async def _load_account_credentials(self) -> None:
        """
        V10: Load all active accounts' decrypted credentials into memory.

        Uses a direct JOIN instead of ORM lazy-load (which breaks in async context).
        Mirrors the pattern used in app/routers/executor.py.
        """
        try:
            from sqlalchemy import join, text
            from app.models.user import ApiConnection

            async with AsyncSessionFactory() as session:
                # Direct join: accounts + api_connections (same as executor.py)
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
                acc_id = acc.id
                if not conn.api_key_encrypted or not conn.api_secret_encrypted:
                    logger.warning(f"  Account {acc_id}: missing encrypted keys — skipped")
                    continue
                try:
                    api_key = decrypt_api_key(conn.api_key_encrypted)
                    api_secret = decrypt_api_key(conn.api_secret_encrypted)
                    self._account_credentials[acc_id] = {
                        "api_key": api_key,
                        "api_secret": api_secret,
                    }
                    loaded += 1
                except Exception as e:
                    logger.warning(f"  Failed to decrypt creds for account {acc_id}: {e}")

            # Fallback: master account from .env (id=0) if no DB accounts
            if settings.BINANCE_API_KEY and 0 not in self._account_credentials:
                self._account_credentials[0] = {
                    "api_key": settings.BINANCE_API_KEY,
                    "api_secret": settings.BINANCE_SECRET_KEY,
                }

            logger.info(
                f"  [PM] Credentials loaded: {loaded} DB accounts + "
                f"{'1 master' if 0 in self._account_credentials else '0 master'} fallback"
            )

        except Exception as e:
            logger.error(f"[PM] Failed to load account credentials: {e}")

    def _get_close_engine(self, account_id: int) -> Optional[CloseEngine]:
        """Return (cached) CloseEngine for this account, or None if no creds."""
        if account_id not in self._close_engines:
            creds = self._account_credentials.get(account_id)
            if not creds:
                # Try master account as fallback
                creds = self._account_credentials.get(0)
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
        """Load all open positions from DB (survived restarts)."""
        try:
            async with AsyncSessionFactory() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.status == "open")
                )
                positions = result.scalars().all()
            return positions
        except Exception as e:
            logger.error(f"Failed to load open positions: {e}")
            return []

    async def _get_tracked_symbols(self) -> list[str]:
        """Return unique list of symbols currently being tracked."""
        positions = await self._load_open_positions()
        return list(set(p.symbol for p in positions))

    # ── Trailing Stop Logic ───────────────────────────────────────────

    def _update_trailing(
        self, pos: OpenPosition, current_price: float
    ) -> tuple[bool, Optional[float]]:
        """
        Update trailing stop state for a position.

        Returns (should_close: bool, new_trailing_sl: Optional[float])

        Trailing Logic:
        1. Activate trailing when ROI >= trailing_trigger_pct
        2. Trailing SL moves with peak price (never moves backward)
        3. Trailing distance = tp_distance × TRAILING_SL_PCT
        4. Close when price falls below trailing SL
        """
        entry = pos.entry_price
        if entry <= 0 or current_price <= 0:
            return False, None

        is_long = pos.side == "BUY"
        swing = _is_swing(pos.strategy_type or "")

        # Calculate current ROI %
        if is_long:
            roi_pct = (current_price - entry) / entry * 100
        else:
            roi_pct = (entry - current_price) / entry * 100

        # Update peak price
        new_highest = pos.highest_price or entry
        new_lowest = pos.lowest_price or entry

        if is_long:
            new_highest = max(new_highest, current_price)
        else:
            new_lowest = min(new_lowest, current_price)

        trigger_pct = pos.trailing_trigger_pct or settings.BREAK_EVEN_TRIGGER_PCT

        # Activate trailing if not already active and profit threshold met
        if not pos.trailing_active and roi_pct >= trigger_pct:
            pos.trailing_active = True
            logger.info(
                f"  🔄 [{pos.symbol}] Trailing activated: "
                f"roi={roi_pct:.2f}% >= trigger={trigger_pct}%"
            )

        # Update trailing SL if trailing is active
        new_trailing_sl = pos.trailing_sl_price
        if pos.trailing_active:
            # Distance = TP distance × trailing multiplier
            tp_dist = abs(pos.tp_price - entry)
            trail_mult = TRAILING_SL_PCT_SWING if swing else TRAILING_SL_PCT_SCALP
            trail_distance = tp_dist * trail_mult

            if is_long:
                candidate_sl = new_highest - trail_distance
                # Only move SL up (never backward)
                if new_trailing_sl is None or candidate_sl > new_trailing_sl:
                    new_trailing_sl = candidate_sl
                # Close if price fell below trailing SL
                if current_price <= new_trailing_sl:
                    logger.info(
                        f"  📈 [{pos.symbol}] TRAILING EXIT triggered: "
                        f"price={current_price} <= trail_sl={new_trailing_sl:.6f}"
                    )
                    pos.highest_price = new_highest
                    pos.lowest_price = new_lowest
                    pos.trailing_sl_price = new_trailing_sl
                    return True, new_trailing_sl
            else:
                candidate_sl = new_lowest + trail_distance
                # Only move SL down (never backward)
                if new_trailing_sl is None or candidate_sl < new_trailing_sl:
                    new_trailing_sl = candidate_sl
                # Close if price rose above trailing SL
                if current_price >= new_trailing_sl:
                    logger.info(
                        f"  📈 [{pos.symbol}] TRAILING EXIT triggered: "
                        f"price={current_price} >= trail_sl={new_trailing_sl:.6f}"
                    )
                    pos.highest_price = new_highest
                    pos.lowest_price = new_lowest
                    pos.trailing_sl_price = new_trailing_sl
                    return True, new_trailing_sl

        # Update state (no close)
        pos.highest_price = new_highest
        pos.lowest_price = new_lowest
        pos.trailing_sl_price = new_trailing_sl
        return False, new_trailing_sl

    # ── TP/SL Check ───────────────────────────────────────────────────

    def _check_tp_sl(
        self, pos: OpenPosition, current_price: float
    ) -> Optional[str]:
        """
        Check if TP or SL is hit.
        Returns: "tp_hit" | "sl_hit" | None
        Uses stored tp_price / sl_price (set by RiskEngine at open time).
        """
        is_long = pos.side == "BUY"
        tp = pos.tp_price
        sl = pos.sl_price

        if tp <= 0 or sl <= 0:
            return None

        if is_long:
            if current_price >= tp:
                return "tp_hit"
            if current_price <= sl:
                return "sl_hit"
        else:
            if current_price <= tp:
                return "tp_hit"
            if current_price >= sl:
                return "sl_hit"

        return None

    # ── Position Close Handler ────────────────────────────────────────

    async def _close_position(
        self,
        pos: OpenPosition,
        close_reason: str,
        current_price: float,
    ) -> None:
        """
        Execute close, update DB, send Telegram.
        Handles all strategies (scalp / swing / sniper) uniformly.
        """
        logger.info(
            f"  🔒 Closing position: {pos.symbol} {pos.side} "
            f"reason={close_reason} price={current_price}"
        )

        close_engine = self._get_close_engine(pos.account_id)
        if not close_engine:
            logger.error(
                f"  ❌ No CloseEngine for account {pos.account_id} — "
                f"cannot close {pos.symbol}"
            )
            await self.telegram.close_failed_manual(
                symbol=pos.symbol,
                side=pos.side,
                reason=close_reason,
                error=f"No API credentials for account {pos.account_id}",
            )
            return

        # Execute market close
        close_result: CloseResult = await close_engine.market_close(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            close_reason=close_reason,
            is_hedge_mode=pos.is_hedge_mode,
            position_side=pos.position_side or "BOTH",
        )

        duration_mins = _duration_minutes(pos.opened_at)

        if close_result.success:
            logger.info(
                f"  ✅ Closed {pos.symbol}: "
                f"price={close_result.close_price} "
                f"pnl={close_result.pnl_usdt:+.4f} USDT "
                f"({close_result.pnl_pct:+.2f}%)"
            )

            # Update DB
            await self._mark_closed(
                pos=pos,
                close_price=close_result.close_price,
                pnl_usdt=close_result.pnl_usdt,
                pnl_pct=close_result.pnl_pct,
                close_reason=close_reason,
            )

            # Send Telegram notification
            await self._send_close_notification(
                pos=pos,
                close_reason=close_reason,
                close_price=close_result.close_price,
                pnl_usdt=close_result.pnl_usdt,
                pnl_pct=close_result.pnl_pct,
                duration_mins=duration_mins,
            )
        else:
            logger.error(
                f"  ❌ Close FAILED for {pos.symbol}: {close_result.error}"
            )
            await self.telegram.close_failed_manual(
                symbol=pos.symbol,
                side=pos.side,
                reason=close_reason,
                error=close_result.error or "Unknown close error",
            )

    # ── DB Updates ────────────────────────────────────────────────────

    async def _mark_closed(
        self,
        pos: OpenPosition,
        close_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        close_reason: str,
    ) -> None:
        """Update open_positions AND trades table when a position is closed."""
        now = datetime.now(timezone.utc)
        try:
            async with AsyncSessionFactory() as session:
                # Update open_positions
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

                # Update trades table (link via trade_id)
                # V10: Also update protection_status -> CLOSED and managed_by
                if pos.trade_id:
                    db_trade = await session.get(Trade, pos.trade_id)
                    if db_trade:
                        db_trade.status = "closed"
                        db_trade.close_price = close_price
                        db_trade.pnl = pnl_usdt
                        db_trade.pnl_pct = pnl_pct
                        db_trade.close_reason = close_reason
                        db_trade.closed_at = now
                        # V10: Protection Engine lifecycle
                        db_trade.protection_status = "CLOSED"
                        db_trade.managed_by = "external_engine"

                await session.commit()
                logger.info(
                    f"  [PM] DB updated: {pos.symbol} closed "
                    f"pnl={pnl_usdt:+.4f} reason={close_reason} "
                    f"[protection_status=CLOSED]"
                )
        except Exception as e:
            logger.error(f"  [PM] Failed to update DB for {pos.symbol}: {e}")

    async def _update_position_price(
        self, pos: OpenPosition, current_price: float
    ) -> None:
        """Update last_price, check_count, and set protection_status=ACTIVE in DB."""
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

                # V10: Also mark trades.protection_status = ACTIVE on first pick-up
                if pos.trade_id and db_pos:
                    db_trade = await session.get(Trade, pos.trade_id)
                    if db_trade and getattr(db_trade, 'protection_status', None) == 'PENDING':
                        db_trade.protection_status = 'ACTIVE'
                        await session.commit()
                        logger.debug(
                            f"  [PM] {pos.symbol} trade_id={pos.trade_id}: "
                            f"protection_status PENDING -> ACTIVE"
                        )
        except Exception:
            pass  # Best-effort, don't interrupt main loop

    # ── Telegram Notifications ────────────────────────────────────────

    async def _send_close_notification(
        self,
        pos: OpenPosition,
        close_reason: str,
        close_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        duration_mins: int,
    ) -> None:
        """Send the appropriate Telegram close notification."""
        strategy = pos.strategy_type or ""
        confidence = pos.confidence or 0

        if close_reason == "tp_hit":
            await self.telegram.trade_closed_tp(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                close_price=close_price,
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                strategy_type=strategy,
                confidence=confidence,
                tp_price=pos.tp_price,
                duration_minutes=duration_mins,
            )
        elif close_reason == "sl_hit":
            await self.telegram.trade_closed_sl(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                close_price=close_price,
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                strategy_type=strategy,
                confidence=confidence,
                sl_price=pos.sl_price,
                duration_minutes=duration_mins,
            )
        elif close_reason == "trailing_exit":
            await self.telegram.trade_closed_trailing(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                close_price=close_price,
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                peak_price=pos.highest_price or 0.0,
                strategy_type=strategy,
                duration_minutes=duration_mins,
            )

    # ── Main Loop ─────────────────────────────────────────────────────

    async def _process_position(self, pos: OpenPosition) -> None:
        """
        Process a single open position:
        1. Get live price from WebSocket cache (fallback: REST)
        2. Check TP/SL trigger
        3. Check trailing stop
        4. If trigger → close
        5. Otherwise → update DB price fields
        """
        symbol = pos.symbol

        # Get price from WebSocket cache
        current_price = self.price_stream.get_price(symbol)

        # If price is stale or missing, use REST fallback
        if current_price is None or self.price_stream.is_stale(symbol, PRICE_STALE_MAX_SECONDS):
            close_engine = self._get_close_engine(pos.account_id)
            if close_engine:
                try:
                    current_price = await close_engine.get_market_price(symbol)
                    logger.debug(f"  [{symbol}] REST price fallback: {current_price}")
                except Exception as e:
                    logger.warning(f"  [{symbol}] Cannot get price: {e}")
                    return

        if not current_price or current_price <= 0:
            return

        # ── 1. Check hard TP/SL ───────────────────────────────────────
        trigger = self._check_tp_sl(pos, current_price)
        if trigger:
            await self._close_position(pos, trigger, current_price)
            return

        # ── 2. Check trailing stop ────────────────────────────────────
        if settings.BREAK_EVEN_ENABLED:
            should_trail_close, _ = self._update_trailing(pos, current_price)
            if should_trail_close:
                await self._close_position(pos, "trailing_exit", current_price)
                return

        # ── 3. Update price in DB (best-effort, every 10 ticks) ──────
        if self._tick % 10 == 0:
            await self._update_position_price(pos, current_price)

    async def run(self) -> None:
        """Main entry point. Loads positions, starts streams, runs loop forever."""
        logger.info("=" * 60)
        logger.info(f"🤖 Position Manager {POSITION_MANAGER_VERSION} starting...")
        logger.info("=" * 60)

        self._running = True

        # Send startup Telegram
        try:
            await self.telegram.position_manager_started(POSITION_MANAGER_VERSION)
        except Exception:
            pass

        # Load account credentials
        await self._load_account_credentials()

        # Prime price stream with currently tracked symbols
        symbols = await self._get_tracked_symbols()
        if symbols:
            logger.info(f"  Resuming monitoring: {len(symbols)} symbols → {symbols[:10]}")
        await self.price_stream.start(symbols or [])

        last_symbol_refresh = 0

        while self._running:
            loop_start = time.time()
            self._tick += 1

            try:
                # ── Refresh symbol list periodically ─────────────────
                if self._tick - last_symbol_refresh >= SYMBOL_REFRESH_INTERVAL:
                    symbols = await self._get_tracked_symbols()
                    await self.price_stream.update_symbols(symbols)
                    await self._load_account_credentials()
                    last_symbol_refresh = self._tick

                # ── Load open positions ───────────────────────────────
                open_positions = await self._load_open_positions()

                if not open_positions:
                    # V10: No open positions -- sleep quietly to avoid DB hammering
                    logger.debug(f"[PM] No open positions -- sleeping 5s (tick={self._tick})")
                    await asyncio.sleep(5.0)
                    continue

                # ── Process each position ─────────────────────────────
                # Run concurrently but cap concurrency to avoid flooding Binance
                semaphore = asyncio.Semaphore(10)

                async def process_guarded(p: OpenPosition):
                    async with semaphore:
                        try:
                            await self._process_position(p)
                        except Exception as e:
                            logger.error(
                                f"  Error processing {p.symbol} (id={p.id}): {e}",
                                exc_info=False,
                            )

                tasks = [process_guarded(p) for p in open_positions]
                await asyncio.gather(*tasks, return_exceptions=True)

                # Log summary every 60 ticks
                if self._tick % 60 == 0:
                    logger.info(
                        f"[PM] Monitoring {len(open_positions)} positions | "
                        f"accounts={len(self._account_credentials)} | "
                        f"tick={self._tick} | "
                        f"prices_cached={len(self.price_stream.all_prices())}"
                    )

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                try:
                    await self.telegram.position_manager_error(
                        error=str(e),
                        context="Main monitoring loop",
                    )
                except Exception:
                    pass
                await asyncio.sleep(5.0)
                continue

            # ── Sleep remainder of interval ───────────────────────────
            elapsed = time.time() - loop_start
            sleep_time = max(0.0, CHECK_INTERVAL_SECONDS - elapsed)
            await asyncio.sleep(sleep_time)


# ─────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────

async def main():
    manager = PositionManager()
    try:
        await manager.run()
    except KeyboardInterrupt:
        logger.info("🛑 Position Manager stopped by user")
    except Exception as e:
        logger.critical(f"🔥 Fatal error: {e}", exc_info=True)
        try:
            telegram = TelegramNotifier()
            await telegram.position_manager_error(
                error=str(e),
                context="Fatal crash — Position Manager offline!",
            )
        except Exception:
            pass
        raise
    finally:
        await manager.price_stream.stop()
        await engine.dispose()


if __name__ == "__main__":
    # V10: Kill switch — set PYTHON_PM_ENABLED=false in .env once n8n PM is stable
    from app.config import settings as _cfg
    if not _cfg.PYTHON_PM_ENABLED:
        logger.warning("🛑 PYTHON_PM_ENABLED=false — Python Position Manager is DISABLED.")
        logger.warning("   The n8n Position Manager workflow is the active exit engine.")
        sys.exit(0)
    asyncio.run(main())
