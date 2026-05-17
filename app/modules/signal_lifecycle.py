"""
V18 Signal Lifecycle Engine — Local rule-based price monitoring.

Core principle: AI generates setup ONCE → local engine tracks everything.

This module provides:
  1. SignalLifecycleEngine — determines state transitions based on price
  2. SignalMonitor — async background task that polls prices and updates states
  3. NO AI API CALLS — purely deterministic price comparisons

Lifecycle flow:
  WATCHING → ENTRY_HIT → ACTIVE → TP1_HIT → TP2_HIT → TP3_HIT (done)
                                           → SL_HIT (done)
             → EXPIRED (entry never hit within TTL)
             → INVALIDATED (price broke invalidation level)
"""

import asyncio
import logging
import time
from typing import Optional, Tuple

import httpx

from app.config import settings
from app.modules.signal_store import (
    SignalStore, StoredSignal, SignalState, signal_store,
)

logger = logging.getLogger(__name__)

# ── Price fetch (lightweight, no AI) ──────────────────────────────────

REST_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
REST_TEST_URL = "https://testnet.binancefuture.com/fapi/v1/ticker/price"


async def fetch_prices_bulk(symbols: list[str], testnet: bool = False) -> dict[str, float]:
    """
    Fetch current prices for multiple symbols via Binance REST.
    NO AI. NO analysis. Just price numbers.
    Returns {symbol: price} dict.
    """
    if not symbols:
        return {}

    url = REST_TEST_URL if testnet else REST_PRICE_URL
    prices = {}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        symbol_set = set(s.upper() for s in symbols)
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol", "")
                if sym in symbol_set:
                    price = float(item.get("price", 0))
                    if price > 0:
                        prices[sym] = price
    except Exception as e:
        logger.warning(f"[LIFECYCLE] Price fetch error: {e}")

    return prices


# ── Signal Lifecycle Engine — deterministic state machine ─────────────

class SignalLifecycleEngine:
    """
    Determines state transitions based on current price vs signal levels.
    NO AI calls. Purely rule-based.
    """

    @staticmethod
    def check_stale_before_watch(
        signal: StoredSignal,
        current_price: float,
    ) -> Tuple[bool, str]:
        """
        Check if a signal is already too late to send as WATCH.
        Called BEFORE emitting the WATCH signal.

        Returns (is_stale, reason).
        """
        if current_price <= 0 or signal.entry_price <= 0:
            return False, ""

        side = signal.side.upper()

        # Check 1: Price already past ideal entry toward TP
        if signal.ideal_entry > 0 and signal.tp1_price > 0:
            if side == "BUY":
                # For longs: stale if price already above ideal entry toward TP
                total_dist = abs(signal.tp1_price - signal.ideal_entry)
                traveled = max(0, current_price - signal.ideal_entry)
            else:
                # For shorts: stale if price already below ideal entry toward TP
                total_dist = abs(signal.ideal_entry - signal.tp1_price)
                traveled = max(0, signal.ideal_entry - current_price)

            if total_dist > 0:
                move_pct = (traveled / total_dist) * 100
                max_move = (
                    settings.V18_SCALP_MAX_STALE_PCT
                    if signal.signal_type != "swing"
                    else settings.V18_SWING_MAX_STALE_PCT
                )
                if move_pct > max_move:
                    return True, (
                        f"Price already {move_pct:.0f}% toward TP1 "
                        f"(max {max_move}% for {signal.signal_type})"
                    )

        # Check 2: Price already past entry zone entirely
        if signal.entry_zone_low > 0 and signal.entry_zone_high > 0:
            if side == "BUY" and current_price > signal.entry_zone_high * 1.005:
                overshoot = ((current_price - signal.entry_zone_high) / signal.entry_zone_high) * 100
                if overshoot > 0.5:
                    return True, f"Price {overshoot:.1f}% above entry zone (BUY setup)"
            elif side == "SELL" and current_price < signal.entry_zone_low * 0.995:
                overshoot = ((signal.entry_zone_low - current_price) / signal.entry_zone_low) * 100
                if overshoot > 0.5:
                    return True, f"Price {overshoot:.1f}% below entry zone (SELL setup)"

        return False, ""

    @staticmethod
    def evaluate(
        signal: StoredSignal,
        current_price: float,
    ) -> Optional[SignalState]:
        """
        Evaluate whether a signal should transition to a new state.
        Returns new state or None if no change.

        This is the CORE of the lifecycle engine — pure price comparison.
        NO AI CALLS.
        """
        if current_price <= 0:
            return None

        now = time.time()
        side = signal.side.upper()
        state = signal.state

        # ── WATCHING → check entry, expiry, invalidation ──────────────
        if state == SignalState.WATCHING.value:
            # Check expiry first
            if signal.expiry_time > 0 and now >= signal.expiry_time:
                return SignalState.EXPIRED

            # Check invalidation
            if signal.invalidation_price > 0:
                if side == "BUY" and current_price < signal.invalidation_price:
                    return SignalState.INVALIDATED
                elif side == "SELL" and current_price > signal.invalidation_price:
                    return SignalState.INVALIDATED

            # Check entry zone hit
            if signal.entry_zone_low > 0 and signal.entry_zone_high > 0:
                if _price_in_zone(current_price, signal.entry_zone_low, signal.entry_zone_high):
                    return SignalState.ENTRY_HIT
                # Also trigger if price passed through zone (gap move)
                if side == "BUY" and current_price <= signal.entry_zone_high:
                    return SignalState.ENTRY_HIT
                if side == "SELL" and current_price >= signal.entry_zone_low:
                    return SignalState.ENTRY_HIT
            elif signal.ideal_entry > 0:
                # No zone, use ideal entry with 0.3% tolerance
                tol = signal.ideal_entry * 0.003
                if abs(current_price - signal.ideal_entry) <= tol:
                    return SignalState.ENTRY_HIT
                # Passed through
                if side == "BUY" and current_price <= signal.ideal_entry:
                    return SignalState.ENTRY_HIT
                if side == "SELL" and current_price >= signal.ideal_entry:
                    return SignalState.ENTRY_HIT
            else:
                # No entry zone defined — use entry_price with tolerance
                if signal.entry_price > 0:
                    tol = signal.entry_price * 0.003
                    if abs(current_price - signal.entry_price) <= tol:
                        return SignalState.ENTRY_HIT

            return None

        # ── ENTRY_HIT → auto-promote to ACTIVE after brief confirmation ─
        if state == SignalState.ENTRY_HIT.value:
            # Auto-promote after 5 seconds of confirmation
            if signal.entry_hit_at > 0 and (now - signal.entry_hit_at) >= 5:
                return SignalState.ACTIVE
            return None

        # ── ACTIVE → check TP1 and SL ─────────────────────────────────
        if state == SignalState.ACTIVE.value:
            # Check SL first (priority)
            if signal.stop_loss > 0:
                if side == "BUY" and current_price <= signal.stop_loss:
                    return SignalState.SL_HIT
                elif side == "SELL" and current_price >= signal.stop_loss:
                    return SignalState.SL_HIT

            # Check TP1
            if signal.tp1_price > 0:
                if side == "BUY" and current_price >= signal.tp1_price:
                    return SignalState.TP1_HIT
                elif side == "SELL" and current_price <= signal.tp1_price:
                    return SignalState.TP1_HIT

            return None

        # ── TP1_HIT → check TP2 and SL ────────────────────────────────
        if state == SignalState.TP1_HIT.value:
            # SL check (may have moved to breakeven)
            if signal.stop_loss > 0:
                if side == "BUY" and current_price <= signal.stop_loss:
                    return SignalState.SL_HIT
                elif side == "SELL" and current_price >= signal.stop_loss:
                    return SignalState.SL_HIT

            # TP2
            if signal.tp2_price > 0:
                if side == "BUY" and current_price >= signal.tp2_price:
                    return SignalState.TP2_HIT
                elif side == "SELL" and current_price <= signal.tp2_price:
                    return SignalState.TP2_HIT

            return None

        # ── TP2_HIT → check TP3 and SL ────────────────────────────────
        if state == SignalState.TP2_HIT.value:
            # SL check
            if signal.stop_loss > 0:
                if side == "BUY" and current_price <= signal.stop_loss:
                    return SignalState.SL_HIT
                elif side == "SELL" and current_price >= signal.stop_loss:
                    return SignalState.SL_HIT

            # TP3
            if signal.tp3_price > 0:
                if side == "BUY" and current_price >= signal.tp3_price:
                    return SignalState.TP3_HIT
                elif side == "SELL" and current_price <= signal.tp3_price:
                    return SignalState.TP3_HIT

            return None

        return None


def _price_in_zone(price: float, low: float, high: float) -> bool:
    """Check if price is within the entry zone (inclusive)."""
    return low <= price <= high


# ── Signal Monitor — background async task ────────────────────────────

class SignalMonitor:
    """
    Background monitor that polls prices and drives lifecycle transitions.

    Runs every V18_MONITOR_INTERVAL_SEC (default 15s).
    NO AI API CALLS — only Binance price REST.
    """

    def __init__(self, store: SignalStore = None):
        self._store = store or signal_store
        self._engine = SignalLifecycleEngine()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._telegram = None  # Lazy init to avoid circular imports
        self._last_save = 0.0
        self._cycle_count = 0

    @property
    def telegram(self):
        """Lazy-init TelegramNotifier to avoid circular imports."""
        if self._telegram is None:
            from app.modules.telegram import TelegramNotifier
            self._telegram = TelegramNotifier()
        return self._telegram

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            logger.warning("[LIFECYCLE MONITOR] Already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("🔄 [LIFECYCLE MONITOR] Started — monitoring active signals")

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("🔄 [LIFECYCLE MONITOR] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _monitor_loop(self) -> None:
        """Main monitoring loop — runs every V18_MONITOR_INTERVAL_SEC."""
        interval = getattr(settings, 'V18_MONITOR_INTERVAL_SEC', 15)

        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LIFECYCLE MONITOR] Error in tick: {e}", exc_info=True)

            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        """Single monitoring cycle. Returns list of events for n8n routing."""
        self._cycle_count += 1
        events = await self._evaluate_signals()

        # Periodic save (every 4 cycles = ~1 minute at 15s interval)
        if self._cycle_count % 4 == 0:
            self._store.periodic_save()

        # Periodic cleanup (every 240 cycles = ~1 hour)
        if self._cycle_count % 240 == 0:
            self._store.cleanup_old_signals(max_age_hours=24)

        return events

    async def _evaluate_signals(self) -> list:
        """
        Evaluate all active signals and collect lifecycle events.
        Returns list of event dicts for n8n routing.
        NO AI CALLS — only price comparisons.
        """
        events = []
        active_signals = self._store.get_active_signals()

        if not active_signals:
            # Periodic cleanup even when idle
            if self._cycle_count % 60 == 0:  # Every ~15 minutes
                self._store.cleanup_old_signals(max_age_hours=24)
            return events

        # Get unique symbols to fetch
        symbols = list(set(s.symbol for s in active_signals))

        # Fetch prices (NO AI — just REST price query)
        testnet = getattr(settings, 'BINANCE_TESTNET', False)
        prices = await fetch_prices_bulk(symbols, testnet=testnet)

        if not prices:
            logger.debug("[LIFECYCLE MONITOR] No prices fetched this cycle")
            return events

        # Evaluate each signal
        transitions = 0
        for sig in active_signals:
            price = prices.get(sig.symbol)
            if price is None or price <= 0:
                continue

            # Update cached price
            self._store.update_price(sig.signal_id, price)

            # Check for state transition
            new_state = self._engine.evaluate(sig, price)
            if new_state is not None:
                old_state = sig.state
                updated = self._store.update_state(sig.signal_id, new_state, price)
                if updated:
                    transitions += 1
                    logger.info(
                        f"📊 [LIFECYCLE] {sig.symbol} {sig.side}: "
                        f"{old_state} → {new_state.value} @ ${price:.6f}"
                    )

                    # DEDUP CHECK: Only notify if not already notified for this state
                    is_first = self._store.mark_notified(
                        sig.signal_id, new_state.value
                    )

                    if is_first:
                        # Send Telegram notification for this transition
                        await self._notify_transition(updated, old_state, price)

                        # Build event data for n8n routing
                        event = self._build_event(updated, old_state, new_state, price)
                        events.append(event)
                    else:
                        logger.debug(
                            f"[LIFECYCLE] Skipping duplicate notification: "
                            f"{sig.symbol} {new_state.value}"
                        )

                    # After TP1 hit, move SL to breakeven
                    if new_state == SignalState.TP1_HIT:
                        self._move_sl_to_breakeven(sig)

        if transitions > 0:
            logger.info(
                f"[LIFECYCLE MONITOR] Cycle #{self._cycle_count}: "
                f"{transitions} transitions, {len(active_signals)} active signals"
            )

        return events

    def _build_event(
        self, signal: StoredSignal, old_state: str,
        new_state: SignalState, price: float
    ) -> dict:
        """
        Build structured event dict for n8n routing.
        Contains all data needed for Telegram message formatting.
        NO AI CALLS.
        """
        from datetime import datetime, timezone

        entry_ref = signal.ideal_entry or signal.entry_price

        # Calculate current ROI
        roi_pct = 0.0
        if entry_ref > 0:
            if signal.side == "BUY":
                roi_pct = ((price - entry_ref) / entry_ref) * 100
            else:
                roi_pct = ((entry_ref - price) / entry_ref) * 100

        # TP progress tracker
        tp_progress = "NONE"
        if new_state in (SignalState.TP3_HIT,):
            tp_progress = "TP3_COMPLETE"
        elif new_state in (SignalState.TP2_HIT,):
            tp_progress = "TP2_HIT"
        elif new_state in (SignalState.TP1_HIT,):
            tp_progress = "TP1_HIT"
        elif signal.state in (SignalState.TP2_HIT.value,):
            tp_progress = "TP2_HIT"
        elif signal.state in (SignalState.TP1_HIT.value,):
            tp_progress = "TP1_HIT"
        elif signal.state in (SignalState.ACTIVE.value, SignalState.ENTRY_HIT.value):
            tp_progress = "ACTIVE"

        return {
            "event_type": new_state.value,
            "previous_state": old_state,
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "side": signal.side,
            "entry_price": entry_ref,
            "current_price": price,
            "current_status": new_state.value,
            "tp1_price": signal.tp1_price,
            "tp2_price": signal.tp2_price,
            "tp3_price": signal.tp3_price,
            "stop_loss": signal.stop_loss,
            "entry_zone_low": signal.entry_zone_low,
            "entry_zone_high": signal.entry_zone_high,
            "strategy_type": signal.strategy_type,
            "signal_type": signal.signal_type,
            "confidence": signal.confidence,
            "leverage": signal.leverage,
            "risk_reward": signal.risk_reward,
            "quality_tier": signal.quality_tier,
            "roi_pct": round(roi_pct, 2),
            "tp_progress": tp_progress,
            "age_seconds": round(signal.age_seconds, 0),
            "expiry_seconds": signal.expiry_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "notified_states": signal.notified_states.copy()
                if isinstance(signal.notified_states, list) else [],
        }

    async def trigger_check_for_n8n(self) -> dict:
        """
        N8n-specific lifecycle check. Triggers one evaluation cycle
        and returns structured data for n8n event routing.
        NO AI CALLS.
        """
        events = await self._evaluate_signals()
        active_count = self._store.count_active()

        # Get current summary of all active signals for context
        active_signals = self._store.get_active_signals()
        signal_summary = []
        for s in active_signals:
            signal_summary.append({
                "signal_id": s.signal_id,
                "symbol": s.symbol,
                "side": s.side,
                "state": s.state,
                "last_price": s.last_price,
                "age_seconds": round(s.age_seconds, 0),
            })

        return {
            "status": "ok",
            "active_signals": active_count,
            "has_events": len(events) > 0,
            "event_count": len(events),
            "events": events,
            "signal_summary": signal_summary,
        }

    def _move_sl_to_breakeven(self, signal: StoredSignal) -> None:
        """After TP1 hit, move SL to entry price (breakeven)."""
        if signal.ideal_entry > 0:
            be_price = signal.ideal_entry
        elif signal.entry_price > 0:
            be_price = signal.entry_price
        else:
            return

        # Add small buffer for fees
        fee_buffer = be_price * 0.001  # 0.1%
        if signal.side == "BUY":
            new_sl = be_price + fee_buffer
        else:
            new_sl = be_price - fee_buffer

        old_sl = signal.stop_loss
        signal.stop_loss = new_sl
        logger.info(
            f"🛡️ [LIFECYCLE] {signal.symbol}: SL moved to breakeven "
            f"${old_sl:.6f} → ${new_sl:.6f}"
        )

    async def _notify_transition(
        self, signal: StoredSignal, old_state: str, price: float
    ) -> None:
        """Send Telegram notification for a lifecycle transition."""
        try:
            new_state = signal.state

            if new_state == SignalState.ENTRY_HIT.value:
                await self.telegram.send_entry_hit(
                    symbol=signal.symbol,
                    side=signal.side,
                    confidence=signal.confidence,
                    entry_price=price,
                    entry_zone_low=signal.entry_zone_low,
                    entry_zone_high=signal.entry_zone_high,
                    strategy_type=signal.strategy_type,
                    leverage=signal.leverage,
                    tp1_price=signal.tp1_price,
                    stop_loss=signal.stop_loss,
                )

            elif new_state == SignalState.ACTIVE.value:
                await self.telegram.send_trade_active(
                    symbol=signal.symbol,
                    side=signal.side,
                    confidence=signal.confidence,
                    entry_price=price,
                    strategy_type=signal.strategy_type,
                    leverage=signal.leverage,
                    tp1_price=signal.tp1_price,
                    tp2_price=signal.tp2_price,
                    tp3_price=signal.tp3_price,
                    stop_loss=signal.stop_loss,
                    risk_reward=signal.risk_reward,
                    quality_tier=signal.quality_tier,
                )

            elif new_state == SignalState.TP1_HIT.value:
                await self.telegram.send_tp_hit(
                    symbol=signal.symbol,
                    side=signal.side,
                    tp_level="TP1",
                    tp_price=signal.tp1_price,
                    entry_price=signal.ideal_entry or signal.entry_price,
                    current_price=price,
                    strategy_type=signal.strategy_type,
                    new_sl=signal.stop_loss,  # After BE move
                )

            elif new_state == SignalState.TP2_HIT.value:
                await self.telegram.send_tp_hit(
                    symbol=signal.symbol,
                    side=signal.side,
                    tp_level="TP2",
                    tp_price=signal.tp2_price,
                    entry_price=signal.ideal_entry or signal.entry_price,
                    current_price=price,
                    strategy_type=signal.strategy_type,
                )

            elif new_state == SignalState.TP3_HIT.value:
                await self.telegram.send_tp_hit(
                    symbol=signal.symbol,
                    side=signal.side,
                    tp_level="TP3",
                    tp_price=signal.tp3_price,
                    entry_price=signal.ideal_entry or signal.entry_price,
                    current_price=price,
                    strategy_type=signal.strategy_type,
                )

            elif new_state == SignalState.SL_HIT.value:
                await self.telegram.send_sl_hit(
                    symbol=signal.symbol,
                    side=signal.side,
                    sl_price=signal.stop_loss,
                    entry_price=signal.ideal_entry or signal.entry_price,
                    current_price=price,
                    strategy_type=signal.strategy_type,
                )

            elif new_state == SignalState.EXPIRED.value:
                await self.telegram.send_signal_expired(
                    symbol=signal.symbol,
                    side=signal.side,
                    strategy_type=signal.strategy_type,
                    age_seconds=signal.age_seconds,
                    expiry_seconds=signal.expiry_seconds,
                )

            elif new_state == SignalState.INVALIDATED.value:
                await self.telegram.send_signal_invalidated(
                    symbol=signal.symbol,
                    side=signal.side,
                    invalidation_price=signal.invalidation_price,
                    current_price=price,
                    strategy_type=signal.strategy_type,
                )

        except Exception as e:
            logger.error(
                f"[LIFECYCLE] Telegram notification failed for "
                f"{signal.symbol} {old_state}→{signal.state}: {e}"
            )


# ── Singleton ─────────────────────────────────────────────────────────────────
signal_monitor = SignalMonitor()
