"""
V18 Signal Store — JSON-based persistent signal storage.

Stores active signals locally so the lifecycle monitor can track them
WITHOUT requiring AI calls. Signals survive workflow/server restarts.

Storage: JSON file on disk (lightweight, no DB dependency for hot path).
Fallback: in-memory dict if file I/O fails.
"""

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)

# ── Signal lifecycle states ───────────────────────────────────────────

class SignalState(str, Enum):
    WATCHING     = "WATCHING"       # Setup exists but entry not hit
    ENTRY_HIT    = "ENTRY_HIT"     # Entry zone touched
    ACTIVE       = "ACTIVE"         # Trade active (confirmed)
    TP1_HIT      = "TP1_HIT"       # First target hit
    TP2_HIT      = "TP2_HIT"       # Second target hit
    TP3_HIT      = "TP3_HIT"       # Extended target hit
    SL_HIT       = "SL_HIT"        # Stop loss hit
    EXPIRED      = "EXPIRED"        # Entry never triggered in time
    INVALIDATED  = "INVALIDATED"    # Setup structurally broken


# ── Stored signal data ────────────────────────────────────────────────

@dataclass
class StoredSignal:
    """All data needed to track a signal lifecycle WITHOUT AI."""
    # Identity
    signal_id: str = ""              # Unique ID: SYMBOL_SIDE_TIMESTAMP
    symbol: str = ""
    side: str = ""                   # BUY | SELL

    # Entry zone
    entry_price: float = 0.0         # Market price at signal creation
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    ideal_entry: float = 0.0

    # Targets
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp3_price: float = 0.0
    stop_loss: float = 0.0
    invalidation_price: float = 0.0

    # Signal metadata
    signal_type: str = ""            # scalp | swing | sniper
    strategy_type: str = ""
    confidence: int = 0
    leverage: int = 1
    risk_reward: float = 0.0
    setup_grade: str = ""
    quality_tier: str = ""
    regime: str = ""
    reason: str = ""

    # Lifecycle
    state: str = SignalState.WATCHING.value
    previous_state: str = ""          # Previous state before last transition
    created_at: float = 0.0          # Unix timestamp
    entry_hit_at: float = 0.0        # When entry was touched
    active_at: float = 0.0           # When confirmed active
    tp1_hit_at: float = 0.0
    tp2_hit_at: float = 0.0
    tp3_hit_at: float = 0.0
    sl_hit_at: float = 0.0
    expired_at: float = 0.0
    invalidated_at: float = 0.0
    last_checked_at: float = 0.0
    last_price: float = 0.0

    # Notification tracking — prevents duplicate Telegram spam
    notified_states: list = field(default_factory=list)  # States already sent to Telegram

    # Validity
    expiry_time: float = 0.0         # Unix timestamp when signal expires
    expiry_seconds: int = 0          # Original TTL in seconds

    # TP/SL ROI for display
    tp_roi_pct: float = 0.0
    sl_roi_pct: float = 0.0
    tp_pct: float = 0.0
    sl_pct: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # Ensure notified_states is always a list (survives JSON round-trip)
        if not isinstance(d.get('notified_states'), list):
            d['notified_states'] = []
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "StoredSignal":
        # Filter to only known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        # Ensure notified_states is a list
        if 'notified_states' in filtered and not isinstance(filtered['notified_states'], list):
            filtered['notified_states'] = []
        return cls(**filtered)

    @property
    def is_terminal(self) -> bool:
        """Signal is in a final state — no more monitoring needed."""
        return self.state in (
            SignalState.TP3_HIT.value,
            SignalState.SL_HIT.value,
            SignalState.EXPIRED.value,
            SignalState.INVALIDATED.value,
        )

    @property
    def is_active_trade(self) -> bool:
        """Signal is in an active trade state (entry was hit)."""
        return self.state in (
            SignalState.ENTRY_HIT.value,
            SignalState.ACTIVE.value,
            SignalState.TP1_HIT.value,
            SignalState.TP2_HIT.value,
        )

    @property
    def age_seconds(self) -> float:
        """How old is this signal."""
        return time.time() - self.created_at if self.created_at > 0 else 0

    def generate_id(self) -> str:
        """Generate unique signal ID."""
        ts = int(self.created_at * 1000)
        return f"{self.symbol}_{self.side}_{ts}"


# ── Persistent signal store ───────────────────────────────────────────

# Default path for signal store — inside the app directory
_DEFAULT_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "active_signals.json",
)


class SignalStore:
    """
    Thread-safe JSON-based signal store.
    Signals persist across restarts.
    """

    def __init__(self, store_path: str = ""):
        self._store_path = store_path or _DEFAULT_STORE_PATH
        self._signals: Dict[str, StoredSignal] = {}
        self._lock = threading.Lock()
        self._load()

    # ── CRUD Operations ───────────────────────────────────────────────

    def add_signal(self, signal: StoredSignal) -> str:
        """Add a new signal to the store. Returns signal_id."""
        if not signal.signal_id:
            if signal.created_at <= 0:
                signal.created_at = time.time()
            signal.signal_id = signal.generate_id()

        with self._lock:
            self._signals[signal.signal_id] = signal
            self._save()

        logger.info(
            f"📦 [STORE] Signal saved: {signal.signal_id} "
            f"{signal.symbol} {signal.side} state={signal.state}"
        )
        return signal.signal_id

    def get_signal(self, signal_id: str) -> Optional[StoredSignal]:
        """Get a signal by ID."""
        with self._lock:
            return self._signals.get(signal_id)

    def update_state(
        self, signal_id: str, new_state: SignalState, price: float = 0.0
    ) -> Optional[StoredSignal]:
        """Update a signal's state. Returns updated signal or None."""
        with self._lock:
            sig = self._signals.get(signal_id)
            if not sig:
                return None

            old_state = sig.state

            # STATE CHANGE PROTECTION: Only transition if state actually changed
            if old_state == new_state.value:
                return None

            sig.previous_state = old_state
            sig.state = new_state.value
            now = time.time()

            # Set state-specific timestamps
            if new_state == SignalState.ENTRY_HIT:
                sig.entry_hit_at = now
            elif new_state == SignalState.ACTIVE:
                sig.active_at = now
            elif new_state == SignalState.TP1_HIT:
                sig.tp1_hit_at = now
            elif new_state == SignalState.TP2_HIT:
                sig.tp2_hit_at = now
            elif new_state == SignalState.TP3_HIT:
                sig.tp3_hit_at = now
            elif new_state == SignalState.SL_HIT:
                sig.sl_hit_at = now
            elif new_state == SignalState.EXPIRED:
                sig.expired_at = now
            elif new_state == SignalState.INVALIDATED:
                sig.invalidated_at = now

            if price > 0:
                sig.last_price = price

            sig.last_checked_at = now
            self._save()

        logger.info(
            f"🔄 [STORE] State change: {signal_id} "
            f"{old_state} → {new_state.value} "
            f"price={price:.6f}" if price > 0 else ""
        )
        return sig

    def mark_notified(self, signal_id: str, state: str) -> bool:
        """
        Mark a state as notified to prevent duplicate Telegram messages.
        Returns True if this is the FIRST notification for this state.
        Returns False if already notified (duplicate — skip sending).
        """
        with self._lock:
            sig = self._signals.get(signal_id)
            if not sig:
                return False

            # Initialize notified_states if needed
            if not isinstance(sig.notified_states, list):
                sig.notified_states = []

            # DUPLICATE CHECK: already notified for this state
            if state in sig.notified_states:
                return False

            # Mark as notified
            sig.notified_states.append(state)
            self._save()
            return True

    def is_notified(self, signal_id: str, state: str) -> bool:
        """Check if a state has already been notified."""
        with self._lock:
            sig = self._signals.get(signal_id)
            if not sig:
                return False
            if not isinstance(sig.notified_states, list):
                return False
            return state in sig.notified_states

    def update_price(self, signal_id: str, price: float) -> None:
        """Update last known price for a signal."""
        with self._lock:
            sig = self._signals.get(signal_id)
            if sig:
                sig.last_price = price
                sig.last_checked_at = time.time()
                # Don't save on every price update — too frequent
                # Save is done periodically or on state changes

    def remove_signal(self, signal_id: str) -> bool:
        """Remove a signal from the store."""
        with self._lock:
            if signal_id in self._signals:
                del self._signals[signal_id]
                self._save()
                return True
        return False

    # ── Query Methods ─────────────────────────────────────────────────

    def get_active_signals(self) -> List[StoredSignal]:
        """Get all non-terminal signals (need monitoring)."""
        with self._lock:
            return [
                s for s in self._signals.values()
                if not s.is_terminal
            ]

    def get_watching_signals(self) -> List[StoredSignal]:
        """Get signals in WATCHING state (waiting for entry)."""
        with self._lock:
            return [
                s for s in self._signals.values()
                if s.state == SignalState.WATCHING.value
            ]

    def get_active_trades(self) -> List[StoredSignal]:
        """Get signals that are in active trade states."""
        with self._lock:
            return [
                s for s in self._signals.values()
                if s.is_active_trade
            ]

    def get_all_signals(self) -> List[StoredSignal]:
        """Get all signals including terminal."""
        with self._lock:
            return list(self._signals.values())

    def count_active(self) -> int:
        """Count non-terminal signals."""
        with self._lock:
            return sum(1 for s in self._signals.values() if not s.is_terminal)

    def has_active_for_symbol(self, symbol: str, side: str = "") -> bool:
        """Check if there's already an active signal for this symbol+side."""
        with self._lock:
            for s in self._signals.values():
                if s.is_terminal:
                    continue
                if s.symbol == symbol:
                    if not side or s.side == side:
                        return True
        return False

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup_old_signals(self, max_age_hours: int = 24) -> int:
        """Remove terminal signals older than max_age_hours. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0
        with self._lock:
            to_remove = [
                sid for sid, sig in self._signals.items()
                if sig.is_terminal and sig.created_at < cutoff
            ]
            for sid in to_remove:
                del self._signals[sid]
                removed += 1
            if removed > 0:
                self._save()
        if removed > 0:
            logger.info(f"🧹 [STORE] Cleaned up {removed} old terminal signals")
        return removed

    def periodic_save(self) -> None:
        """Force a save to disk (call periodically to persist price updates)."""
        with self._lock:
            self._save()

    # ── Persistence ───────────────────────────────────────────────────

    def _save(self) -> None:
        """Save all signals to disk as JSON."""
        try:
            os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
            data = {sid: sig.to_dict() for sid, sig in self._signals.items()}
            with open(self._store_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[STORE] Failed to save signals: {e}")

    def _load(self) -> None:
        """Load signals from disk."""
        if not os.path.exists(self._store_path):
            logger.info(f"📦 [STORE] No existing signal store at {self._store_path}")
            return
        try:
            with open(self._store_path, "r") as f:
                data = json.load(f)
            for sid, sig_data in data.items():
                self._signals[sid] = StoredSignal.from_dict(sig_data)
            logger.info(
                f"📦 [STORE] Loaded {len(self._signals)} signals from disk "
                f"({sum(1 for s in self._signals.values() if not s.is_terminal)} active)"
            )
        except Exception as e:
            logger.error(f"[STORE] Failed to load signals: {e}")
            self._signals = {}


# ── Singleton ─────────────────────────────────────────────────────────
signal_store = SignalStore()
