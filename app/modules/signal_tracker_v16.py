"""
V16 Signal Tracker — Virtual Trade State Machine

Tracks every signal as a virtual trade after it is generated.
No Binance orders. Pure price-monitoring for signal performance.

State lifecycle:
  PENDING     → signal generated, waiting for price to enter entry zone
  ENTRY_HIT   → price touched entry zone, virtual trade "opened"
  DRAWDOWN_10 → virtual trade in -10% drawdown
  DRAWDOWN_20 → virtual trade in -20% drawdown
  TP_HIT      → take-profit price reached → signal WIN
  SL_HIT      → stop-loss price reached → signal LOSS
  INVALIDATED → opposite signal appeared for same coin
  CANCELLED   → timeout expired or bad market condition

Usage:
  signal_tracker = SignalTracker()              # singleton at module level
  sig_id = await signal_tracker.register(...)  # call from /signal endpoint
  await signal_tracker.update_all(prices)      # call from monitor loop
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.database import async_session
from app.models.trading import Signal

logger = logging.getLogger(__name__)

# ── Timeouts ───────────────────────────────────────────────────────────────────────
_SCALP_TIMEOUT_MINUTES  = 15     # Scalp signal expires after 15 min without entry
_SWING_TIMEOUT_MINUTES  = 240    # Swing signal expires after 4 hours

# ── Drawdown thresholds (strategy-specific) ────────────────────────────────────────────
# SCALP: warn at -3%, close at -5% (tight SL)
_SCALP_DRAWDOWN_WARN = -3.0    # Scalp: first warning at -3%
_SCALP_DRAWDOWN_SL   = -5.0    # Scalp: SL trigger at -5% (force close)
# SWING: warn at -10%, danger at -20%
_SWING_DRAWDOWN_WARN = -10.0   # Swing: first warning at -10%
_SWING_DRAWDOWN_CRIT = -20.0   # Swing: critical warning at -20%

# ── Capacity limits ───────────────────────────────────────────────────────────────────────
MAX_ACTIVE_SIGNALS = 5    # Max total active signals at one time
MAX_PER_COIN       = 2    # Max active signals per coin (same symbol)

# ── Opposite signal confidence threshold ────────────────────────────────────────────
OPPOSITE_MIN_CONFIDENCE = 80   # Only invalidate if new signal confidence ≥ 80


# ── Internal Signal State ────────────────────────────────────────────

@dataclass
class SignalState:
    signal_id:    int
    signal_number: int
    symbol:       str
    side:         str          # BUY | SELL
    entry_price:  float        # Initial signal price (centre of entry zone)
    entry_zone_low:  float
    entry_zone_high: float
    tp_price:     float
    sl_price:     float
    tp_pct:       float
    sl_pct:       float
    strategy_type: str
    confidence:   int
    status:       str = "PENDING"
    created_at:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Runtime tracking
    peak_price:   float = 0.0
    trough_price: float = 0.0
    drawdown_pct: float = 0.0
    entry_hit_at: Optional[datetime] = None
    # Drawdown alert flags — scalp uses 3/5 thresholds, swing uses 10/20
    drawdown_warn_sent:  bool = False   # Scalp: -3% / Swing: -10%
    drawdown_crit_sent:  bool = False   # Scalp: -5% (SL) / Swing: -20%
    # Legacy aliases kept for compat
    drawdown_10_sent: bool = False
    drawdown_20_sent: bool = False

    @property
    def is_swing(self) -> bool:
        return self.strategy_type.startswith("swing")

    @property
    def timeout_minutes(self) -> int:
        return _SWING_TIMEOUT_MINUTES if self.is_swing else _SCALP_TIMEOUT_MINUTES

    @property
    def age_minutes(self) -> float:
        delta = datetime.now(timezone.utc) - self.created_at
        return delta.total_seconds() / 60

    @property
    def is_timed_out(self) -> bool:
        return self.status == "PENDING" and self.age_minutes >= self.timeout_minutes

    def is_active(self) -> bool:
        return self.status in (
            "PENDING", "ENTRY_HIT",
            "DRAWDOWN_3", "DRAWDOWN_5",    # scalp states
            "DRAWDOWN_10", "DRAWDOWN_20",  # swing states
        )


# ── Signal Tracker Singleton ─────────────────────────────────────────

class SignalTracker:
    """
    V16 in-memory + DB-backed signal tracker.
    Holds all active signals and evaluates each against live prices.
    Emits Telegram notifications on state transitions.
    """

    def __init__(self):
        self._signals: dict[int, SignalState] = {}   # signal_id → state
        self._lock = asyncio.Lock()

    # ── Public: Register a new signal ────────────────────────────────

    async def register(
        self,
        signal_id:    int,
        signal_number: int,
        symbol:       str,
        side:         str,
        entry_price:  float,
        entry_zone_low:  float,
        entry_zone_high: float,
        tp_price:     float,
        sl_price:     float,
        tp_pct:       float,
        sl_pct:       float,
        strategy_type: str,
        confidence:   int,
    ) -> None:
        """Register a new signal for virtual tracking."""
        state = SignalState(
            signal_id=signal_id,
            signal_number=signal_number,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            tp_price=tp_price,
            sl_price=sl_price,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            strategy_type=strategy_type,
            confidence=confidence,
        )
        async with self._lock:
            self._signals[signal_id] = state
        logger.info(
            f"📡 [SignalTracker] Registered #{signal_number:03d} "
            f"{symbol} {side} entry={entry_price:.4f} "
            f"TP={tp_price:.4f} SL={sl_price:.4f}"
        )

    # ── Public: Update all signals with latest prices ─────────────────

    async def update_all(self, prices: dict[str, float], telegram=None) -> None:
        """
        Called every minute by the background monitor loop.
        prices: {symbol: current_price}
        Evaluates each active signal and triggers state transitions.
        """
        async with self._lock:
            to_close: list[int] = []

            for sig_id, state in self._signals.items():
                if not state.is_active():
                    to_close.append(sig_id)
                    continue

                price = prices.get(state.symbol)
                if not price or price <= 0:
                    continue

                # Timeout check (PENDING only)
                if state.is_timed_out:
                    logger.info(
                        f"  ⏱️ Signal #{state.signal_number:03d} {state.symbol} TIMED OUT "
                        f"after {state.age_minutes:.0f}m"
                    )
                    await self._close_signal(state, "CANCELLED", "timeout", price, telegram)
                    to_close.append(sig_id)
                    continue

                # ── PENDING: watch for entry zone hit ──────────────────
                if state.status == "PENDING":
                    in_zone = state.entry_zone_low <= price <= state.entry_zone_high
                    if in_zone:
                        state.status = "ENTRY_HIT"
                        state.entry_hit_at = datetime.now(timezone.utc)
                        state.peak_price   = price
                        state.trough_price = price
                        logger.info(
                            f"  ✅ Signal #{state.signal_number:03d} ENTRY HIT "
                            f"{state.symbol} @ {price:.4f}"
                        )
                        await self._update_db(state, "ENTRY_HIT")
                        if telegram:
                            try:
                                await telegram.send_signal_entry_triggered(
                                    signal_number=state.signal_number,
                                    symbol=state.symbol,
                                    side=state.side,
                                    entry_price=price,
                                    tp_price=state.tp_price,
                                    sl_price=state.sl_price,
                                )
                            except Exception as te:
                                logger.warning(f"Telegram entry trigger failed: {te}")
                    continue  # Don't check TP/SL until entry is hit

                # ── ENTRY_HIT / DRAWDOWN: watch TP, SL, drawdown ───────
                if state.side == "BUY":
                    # Update peak/trough
                    if price > state.peak_price:
                        state.peak_price = price
                    if price < state.trough_price or state.trough_price == 0:
                        state.trough_price = price

                    # Drawdown from entry
                    dd_pct = (price - state.entry_price) / state.entry_price * 100
                    state.drawdown_pct = round(dd_pct, 2)

                    # TP hit
                    if price >= state.tp_price:
                        logger.info(f"  🎯 Signal #{state.signal_number:03d} TP HIT {state.symbol} @ {price:.4f}")
                        await self._close_signal(state, "TP_HIT", "TP", price, telegram)
                        to_close.append(sig_id)
                        continue

                    # SL hit
                    if price <= state.sl_price:
                        logger.info(f"  ❌ Signal #{state.signal_number:03d} SL HIT {state.symbol} @ {price:.4f}")
                        await self._close_signal(state, "SL_HIT", "SL", price, telegram)
                        to_close.append(sig_id)
                        continue

                    # Drawdown alerts
                    await self._check_drawdown(state, dd_pct, telegram)

                else:  # SELL
                    if price < state.trough_price or state.trough_price == 0:
                        state.trough_price = price
                    if price > state.peak_price:
                        state.peak_price = price

                    dd_pct = (state.entry_price - price) / state.entry_price * 100
                    state.drawdown_pct = round(dd_pct, 2)

                    # TP hit (price goes DOWN for shorts)
                    if price <= state.tp_price:
                        logger.info(f"  🎯 Signal #{state.signal_number:03d} TP HIT {state.symbol} @ {price:.4f}")
                        await self._close_signal(state, "TP_HIT", "TP", price, telegram)
                        to_close.append(sig_id)
                        continue

                    # SL hit (price goes UP for shorts)
                    if price >= state.sl_price:
                        logger.info(f"  ❌ Signal #{state.signal_number:03d} SL HIT {state.symbol} @ {price:.4f}")
                        await self._close_signal(state, "SL_HIT", "SL", price, telegram)
                        to_close.append(sig_id)
                        continue

                    await self._check_drawdown(state, dd_pct, telegram)

            # Remove closed signals
            for sig_id in to_close:
                self._signals.pop(sig_id, None)

    # ── Public: Opposite signal detection ────────────────────────────

    async def check_and_invalidate_opposite(
        self,
        symbol: str,
        new_side: str,
        new_signal_id: int,
        new_confidence: int = 0,
        telegram=None,
    ) -> list[int]:
        """
        V16: Invalidate existing opposite signals for same coin.
        ONLY invalidates if new_confidence >= OPPOSITE_MIN_CONFIDENCE (80).
        Prevents aggressive invalidation from weak counter-signals.
        """
        if new_confidence < OPPOSITE_MIN_CONFIDENCE:
            logger.debug(
                f"  [SignalTracker] Opposite check skipped: confidence={new_confidence} "
                f"< threshold={OPPOSITE_MIN_CONFIDENCE}"
            )
            return []

        invalidated_numbers = []
        async with self._lock:
            for sig_id, state in list(self._signals.items()):
                if state.symbol != symbol:
                    continue
                if not state.is_active():
                    continue
                # Opposite direction?
                if (state.side == "BUY" and new_side == "SELL") or \
                   (state.side == "SELL" and new_side == "BUY"):
                    logger.info(
                        f"  ⚠️ Reversal: #{state.signal_number:03d} {symbol} {state.side} "
                        f"invalidated by new {new_side} signal (conf={new_confidence})"
                    )
                    await self._close_signal(state, "INVALIDATED", "reversal", 0.0, telegram)
                    invalidated_numbers.append(state.signal_number)
                    self._signals.pop(sig_id, None)

        return invalidated_numbers

    # ── Public: Capacity check ──────────────────────────────────────────────────────────

    def capacity_check(self, symbol: str) -> tuple[bool, str]:
        """
        V16: Check if a new signal can be accepted.
        Returns (allowed: bool, reason: str).
        Enforces MAX_ACTIVE_SIGNALS=5 and MAX_PER_COIN=2.
        """
        active = [s for s in self._signals.values() if s.is_active()]
        if len(active) >= MAX_ACTIVE_SIGNALS:
            return False, f"Max active signals reached ({MAX_ACTIVE_SIGNALS})"
        coin_count = sum(1 for s in active if s.symbol == symbol)
        if coin_count >= MAX_PER_COIN:
            return False, f"Max signals per coin reached ({MAX_PER_COIN}) for {symbol}"
        return True, ""

    # ── Public: Get next signal number ────────────────────────────────

    async def get_next_signal_number(self) -> int:
        """
        Returns next daily sequential signal number.
        Increments the signal_counter table row for today.
        Thread-safe via DB advisory lock via RETURNING.
        """
        from datetime import date as _date
        today = _date.today().strftime("%Y-%m-%d")

        try:
            async with async_session() as session:
                from sqlalchemy import text
                # Upsert: insert today if missing, then increment
                result = await session.execute(
                    text("""
                        INSERT INTO signal_counter (date, last_number)
                        VALUES (:today, 1)
                        ON CONFLICT (date) DO UPDATE
                            SET last_number = signal_counter.last_number + 1
                        RETURNING last_number
                    """),
                    {"today": today},
                )
                row = result.fetchone()
                await session.commit()
                return int(row[0]) if row else 1
        except Exception as e:
            logger.error(f"[SignalTracker] get_next_signal_number failed: {e}")
            return 1

    # ── Public: Active signal count ───────────────────────────────────

    def active_count(self) -> int:
        return sum(1 for s in self._signals.values() if s.is_active())

    def active_count_for_coin(self, symbol: str) -> int:
        return sum(1 for s in self._signals.values() if s.is_active() and s.symbol == symbol)

    def get_active_signals(self) -> list[dict]:
        """Return active signals as list of dicts for API response."""
        result = []
        for s in self._signals.values():
            if s.is_active():
                result.append({
                    "signal_id":       s.signal_id,
                    "signal_number":   s.signal_number,
                    "symbol":          s.symbol,
                    "side":            s.side,
                    "status":          s.status,
                    "strategy_type":   s.strategy_type,
                    "confidence":      s.confidence,
                    # Prices — all needed by n8n tracking workflow
                    "entry_price":     s.entry_price,
                    "entry_zone_low":  s.entry_zone_low,
                    "entry_zone_high": s.entry_zone_high,
                    "tp_price":        s.tp_price,
                    "sl_price":        s.sl_price,
                    "tp_pct":          s.tp_pct,
                    "sl_pct":          s.sl_pct,
                    # Runtime tracking
                    "drawdown_pct":    s.drawdown_pct,
                    "age_minutes":     round(s.age_minutes, 1),
                })
        return result

    # ── Internal: Drawdown alerts ─────────────────────────────────────

    async def _check_drawdown(self, state: SignalState, dd_pct: float, telegram) -> None:
        """
        Strategy-aware drawdown alerts:
          Scalp: DRAWDOWN_3 at -3%, force SL at -5%
          Swing: DRAWDOWN_10 at -10%, DRAWDOWN_20 at -20%
        """
        if state.is_swing:
            # ── Swing drawdown ──────────────────────────────────────────────────────────
            if dd_pct <= _SWING_DRAWDOWN_CRIT and not state.drawdown_crit_sent:
                state.drawdown_crit_sent = True
                state.drawdown_20_sent   = True
                state.status = "DRAWDOWN_20"
                logger.warning(f"  🚨 Signal #{state.signal_number:03d} {state.symbol} DRAWDOWN -20%")
                await self._update_db(state, "DRAWDOWN_20")
                if telegram:
                    try:
                        await telegram.send_signal_status_update(
                            signal_number=state.signal_number, symbol=state.symbol,
                            side=state.side, status="DRAWDOWN_20",
                            current_price=state.trough_price if state.side == "BUY" else state.peak_price,
                            entry_price=state.entry_price, drawdown_pct=dd_pct,
                        )
                    except Exception as te:
                        logger.warning(f"Telegram drawdown-20 failed: {te}")

            elif dd_pct <= _SWING_DRAWDOWN_WARN and not state.drawdown_warn_sent:
                state.drawdown_warn_sent = True
                state.drawdown_10_sent   = True
                state.status = "DRAWDOWN_10"
                logger.info(f"  ⚠️ Signal #{state.signal_number:03d} {state.symbol} DRAWDOWN -10%")
                await self._update_db(state, "DRAWDOWN_10")
                if telegram:
                    try:
                        await telegram.send_signal_status_update(
                            signal_number=state.signal_number, symbol=state.symbol,
                            side=state.side, status="DRAWDOWN_10",
                            current_price=state.trough_price if state.side == "BUY" else state.peak_price,
                            entry_price=state.entry_price, drawdown_pct=dd_pct,
                        )
                    except Exception as te:
                        logger.warning(f"Telegram drawdown-10 failed: {te}")

        else:
            # ── Scalp drawdown ──────────────────────────────────────────────────────────
            if dd_pct <= _SCALP_DRAWDOWN_SL and not state.drawdown_crit_sent:
                # -5% on scalp = immediate SL-level alert (will be SL'd by price check above)
                state.drawdown_crit_sent = True
                state.status = "DRAWDOWN_5"
                logger.warning(f"  🚨 Scalp #{state.signal_number:03d} {state.symbol} DRAWDOWN -5% (SL zone)")
                await self._update_db(state, "DRAWDOWN_5")
                if telegram:
                    try:
                        await telegram.send_signal_status_update(
                            signal_number=state.signal_number, symbol=state.symbol,
                            side=state.side, status="DRAWDOWN_5",
                            current_price=state.trough_price if state.side == "BUY" else state.peak_price,
                            entry_price=state.entry_price, drawdown_pct=dd_pct,
                        )
                    except Exception as te:
                        logger.warning(f"Telegram scalp drawdown-5 failed: {te}")

            elif dd_pct <= _SCALP_DRAWDOWN_WARN and not state.drawdown_warn_sent:
                state.drawdown_warn_sent = True
                state.status = "DRAWDOWN_3"
                logger.info(f"  ⚠️ Scalp #{state.signal_number:03d} {state.symbol} DRAWDOWN -3%")
                await self._update_db(state, "DRAWDOWN_3")
                if telegram:
                    try:
                        await telegram.send_signal_status_update(
                            signal_number=state.signal_number, symbol=state.symbol,
                            side=state.side, status="DRAWDOWN_3",
                            current_price=state.trough_price if state.side == "BUY" else state.peak_price,
                            entry_price=state.entry_price, drawdown_pct=dd_pct,
                        )
                    except Exception as te:
                        logger.warning(f"Telegram scalp drawdown-3 failed: {te}")

    # ── Internal: Close signal + update DB ───────────────────────────

    async def _close_signal(
        self,
        state: SignalState,
        final_status: str,
        result: str,
        close_price: float,
        telegram,
    ) -> None:
        """Update state + persist to DB + send Telegram result."""
        state.status = final_status
        closed_at = datetime.now(timezone.utc)

        # Calculate duration
        if state.entry_hit_at:
            duration_minutes = int((closed_at - state.entry_hit_at).total_seconds() / 60)
        else:
            duration_minutes = int((closed_at - state.created_at).total_seconds() / 60)

        # Persist to DB
        try:
            async with async_session() as session:
                db_signal = await session.get(Signal, state.signal_id)
                if db_signal:
                    db_signal.status     = final_status
                    db_signal.result     = result
                    db_signal.peak_price   = state.peak_price
                    db_signal.trough_price = state.trough_price
                    db_signal.drawdown_pct = state.drawdown_pct
                    db_signal.closed_at    = closed_at
                    if close_price > 0:
                        pass  # close_price stored via status/result
                    await session.commit()
        except Exception as e:
            logger.error(f"[SignalTracker] DB close failed for #{state.signal_number:03d}: {e}")

        # Send Telegram result
        if telegram and close_price > 0:
            try:
                if final_status == "TP_HIT":
                    await telegram.send_signal_result(
                        signal_number=state.signal_number,
                        symbol=state.symbol,
                        side=state.side,
                        result="TP",
                        entry_price=state.entry_price,
                        close_price=close_price,
                        tp_pct=state.tp_pct,
                        sl_pct=state.sl_pct,
                        duration_minutes=duration_minutes,
                    )
                elif final_status == "SL_HIT":
                    await telegram.send_signal_result(
                        signal_number=state.signal_number,
                        symbol=state.symbol,
                        side=state.side,
                        result="SL",
                        entry_price=state.entry_price,
                        close_price=close_price,
                        tp_pct=state.tp_pct,
                        sl_pct=state.sl_pct,
                        duration_minutes=duration_minutes,
                    )
                elif final_status == "INVALIDATED":
                    await telegram.send_reversal_warning(
                        invalidated_number=state.signal_number,
                        symbol=state.symbol,
                        old_side=state.side,
                    )
                elif final_status == "CANCELLED":
                    pass  # quiet cancel — no spam
            except Exception as te:
                logger.warning(f"[SignalTracker] Telegram result send failed: {te}")

    async def _update_db(self, state: SignalState, new_status: str) -> None:
        """Persist status change to DB."""
        try:
            async with async_session() as session:
                db_signal = await session.get(Signal, state.signal_id)
                if db_signal:
                    db_signal.status = new_status
                    if new_status == "ENTRY_HIT":
                        db_signal.entry_hit_at = state.entry_hit_at
                    db_signal.drawdown_pct = state.drawdown_pct
                    await session.commit()
        except Exception as e:
            logger.warning(f"[SignalTracker] DB update failed: {e}")


# ── Module-level singleton ────────────────────────────────────────────
signal_tracker_v16 = SignalTracker()
