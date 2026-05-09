"""
V11 State Manager — DB-backed with in-memory cache.
Tracks trade limits, cooldowns, and rate limiting.
Designed for multi-account operation.

V4 Changes:
  - UTC timezone for daily reset (was local time — caused mismatch with daily_guard)
  - Removed duplicate daily P&L tracking (per-account daily_guard handles it)
  - Kept: hourly rate limits, coin cooldowns, trade counts (global, valid)
  - Simplified check_daily_limits to only check trade count limits
V11 Changes:
  - Added daily_pnl_pct tracking for global entry gate
  - Added check_v11_entry_gate() — blocks new entries at daily loss/profit limits
  - Preserves per-account daily_guard (not replaced)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TradeState:
    # Daily tracking (UTC timezone)
    daily_date: str = ""
    daily_trades: int = 0
    trading_paused: bool = False
    pause_reason: str = ""

    # Per-coin cooldown: { "XRPUSDT": [timestamp1, timestamp2], ... }
    coin_trade_times: Dict[str, List[float]] = field(default_factory=dict)

    # V10: Per-coin post-close cooldown: { "XRPUSDT": unix_timestamp_until }
    # Separate from open-trade cooldown so scalp/swing can have different durations
    post_close_cooldown_until: Dict[str, float] = field(default_factory=dict)

    # Hourly rate limit: list of unix timestamps of recent trades
    hourly_trade_timestamps: List[float] = field(default_factory=list)

    # Consecutive loss tracking (global — per-account is in daily_guard)
    consecutive_losses: int = 0
    loss_cooldown_until: float = 0.0  # Unix timestamp

    # V11: Global daily P&L gate (aggregated across accounts — rough estimate)
    # Per-account precision is still handled by daily_guard. This is a safety net.
    daily_pnl_usdt: float = 0.0          # Running today's realised P&L in USDT
    daily_starting_equity: float = 0.0   # Set on first trade of the day
    v11_entry_blocked: bool = False       # Set True when daily gate fires
    v11_block_reason: str = ""           # Reason string for the block

    # V14: Signal dedup memory — prevents duplicate Telegram broadcasts
    # Key = "SYMBOL:SIDE:strategy_prefix" → { "sent_at": timestamp, "confidence": int }
    signal_fingerprints: Dict[str, dict] = field(default_factory=dict)

    # Stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_starting_balance: float = 0.0
    ai_usage_count: int = 0
    skipped_trades_today: int = 0


class StateManager:
    """
    V4 In-memory state manager.
    Handles rate limiting and global trade counts.
    Per-account daily P&L and guards are handled by daily_guard module.
    """

    def __init__(self):
        self.state = TradeState()

    # ─── Daily Reset (V4: UTC timezone) ──────────────────────────────

    def _check_daily_reset(self, balance: float = 0.0):
        # V4: Use UTC to match daily_guard timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.daily_date != today:
            logger.info(f"📅 New trading day (UTC): {today} — resetting daily counters")
            self.state.daily_date = today
            self.state.daily_trades = 0
            self.state.daily_pnl = 0.0
            self.state.daily_starting_balance = balance if balance > 0 else self.state.daily_starting_balance
            self.state.trading_paused = False
            self.state.pause_reason = ""
            self.state.consecutive_losses = 0
            self.state.loss_cooldown_until = 0.0
            self.state.skipped_trades_today = 0
            # V11: Reset daily P&L gate
            self.state.daily_pnl_usdt = 0.0
            self.state.v11_entry_blocked = False
            self.state.v11_block_reason = ""
            # V14: Prune old signal fingerprints
            self._prune_stale_fingerprints()

    # ─── Daily Limits Check ───────────────────────────────────────────

    def check_daily_limits(self, current_balance: float) -> dict:
        """
        V4: Check global daily trading limits (trade count only).
        Per-account P&L limits are handled by daily_guard module.
        """
        self._check_daily_reset(current_balance)

        if self.state.trading_paused:
            return {"allowed": False, "reason": self.state.pause_reason}

        # Check max daily trades (global)
        if self.state.daily_trades >= settings.DAILY_MAX_TRADES:
            self.state.trading_paused = True
            self.state.pause_reason = f"Daily trade limit: {self.state.daily_trades} >= {settings.DAILY_MAX_TRADES}"
            return {"allowed": False, "reason": self.state.pause_reason}

        return {"allowed": True, "reason": ""}

    # ─── Hourly Rate Limit ────────────────────────────────────────────

    def is_hourly_limit_reached(self) -> tuple[bool, int]:
        now = time.time()
        one_hour_ago = now - 3600
        self.state.hourly_trade_timestamps = [
            ts for ts in self.state.hourly_trade_timestamps if ts > one_hour_ago
        ]
        count = len(self.state.hourly_trade_timestamps)
        return count >= settings.HOURLY_MAX_TRADES, count

    # ─── Per-Coin Cooldown ────────────────────────────────────────────

    def is_coin_on_cooldown(self, symbol: str) -> tuple[bool, int]:
        """Check if coin was traded too recently or too many times this hour."""
        now = time.time()
        cooldown_seconds = settings.COIN_COOLDOWN_MINUTES * 60
        one_hour_ago = now - 3600

        times = self.state.coin_trade_times.get(symbol, [])
        # Clean old entries
        times = [t for t in times if t > one_hour_ago]
        self.state.coin_trade_times[symbol] = times

        # Check repeats per hour
        if len(times) >= settings.MAX_COIN_REPEATS_PER_HOUR:
            return True, int(3600 - (now - times[0])) if times else 0

        # Check cooldown since last trade
        if times:
            last_time = times[-1]
            remaining = cooldown_seconds - (now - last_time)
            if remaining > 0:
                return True, int(remaining)

        return False, 0

    # ─── Loss Cooldown ────────────────────────────────────────────────

    def is_loss_cooldown_active(self) -> tuple[bool, int]:
        """Check if consecutive loss cooldown is active."""
        now = time.time()
        if self.state.loss_cooldown_until > now:
            remaining = int(self.state.loss_cooldown_until - now)
            return True, remaining
        return False, 0

    # ─── Trade Lifecycle ──────────────────────────────────────────────

    def record_trade_opened(self, symbol: str):
        """Record a trade was opened."""
        now = time.time()
        self.state.total_trades += 1
        self.state.daily_trades += 1

        # Hourly timestamp
        self.state.hourly_trade_timestamps.append(now)

        # Per-coin timestamp
        if symbol not in self.state.coin_trade_times:
            self.state.coin_trade_times[symbol] = []
        self.state.coin_trade_times[symbol].append(now)

        # Prune old coin entries (older than 24h)
        cutoff = now - 86400
        self.state.coin_trade_times = {
            s: [t for t in ts if t > cutoff]
            for s, ts in self.state.coin_trade_times.items()
            if any(t > cutoff for t in ts)
        }

    def record_trade_closed(self, pnl: float):
        """Record a trade was closed with P&L."""
        self.state.total_pnl += pnl
        self.state.daily_pnl += pnl

        if pnl > 0:
            self.state.winning_trades += 1
            self.state.consecutive_losses = 0
        else:
            self.state.losing_trades += 1
            self.state.consecutive_losses += 1

            # Activate loss cooldown if threshold reached
            if self.state.consecutive_losses >= settings.LOSS_COOLDOWN_COUNT:
                cooldown_secs = settings.LOSS_COOLDOWN_MINUTES * 60
                self.state.loss_cooldown_until = time.time() + cooldown_secs
                logger.warning(
                    f"🔴 Loss cooldown activated: {self.state.consecutive_losses} consecutive losses. "
                    f"Pausing for {settings.LOSS_COOLDOWN_MINUTES}m"
                )

    def record_skip(self):
        """Record that a trade was skipped."""
        self.state.skipped_trades_today += 1

    # ── V11: Global Daily P&L Entry Gate ─────────────────────────────

    def record_pnl(self, pnl_usdt: float, equity: float = 0.0):
        """
        V11: Record realised P&L (called after a position closes).
        Updates global daily P&L used by check_v11_entry_gate().
        Does NOT replace per-account daily_guard — this is a global safety net.
        """
        self._check_daily_reset(equity)
        self.state.daily_pnl_usdt += pnl_usdt
        if equity > 0 and self.state.daily_starting_equity <= 0:
            self.state.daily_starting_equity = equity

    def check_v11_entry_gate(self, current_equity: float = 0.0) -> dict:
        """
        V11: Global entry gate — blocks NEW trade entries when daily P&L
        limits are breached. Position manager still runs (closes active trades).

        Returns:
            {"allowed": bool, "reason": str}
        """
        self._check_daily_reset(current_equity)

        # If already blocked this session
        if self.state.v11_entry_blocked:
            return {"allowed": False, "reason": self.state.v11_block_reason}

        # Calculate daily P&L %
        equity = self.state.daily_starting_equity or current_equity
        if equity <= 0:
            return {"allowed": True, "reason": ""}

        pnl_pct = (self.state.daily_pnl_usdt / equity) * 100

        # Check daily loss gate
        if pnl_pct <= settings.V11_DAILY_LOSS_GATE_PCT:
            reason = (
                f"V11 Daily loss gate: {pnl_pct:+.2f}% "
                f"<= {settings.V11_DAILY_LOSS_GATE_PCT}% limit — new entries paused"
            )
            self.state.v11_entry_blocked = True
            self.state.v11_block_reason = reason
            logger.warning(f"🛑 {reason}")
            return {"allowed": False, "reason": reason}

        # Check daily profit lock
        if pnl_pct >= settings.V11_DAILY_PROFIT_LOCK_PCT:
            reason = (
                f"V11 Daily profit lock: {pnl_pct:+.2f}% "
                f">= {settings.V11_DAILY_PROFIT_LOCK_PCT}% — locking in gains, no new entries"
            )
            self.state.v11_entry_blocked = True
            self.state.v11_block_reason = reason
            logger.info(f"🔒 {reason}")
            return {"allowed": False, "reason": reason}

        return {"allowed": True, "reason": ""}

    def record_ai_call(self):
        """Record an AI API call."""
        self.state.ai_usage_count += 1

    # V10: Post-close per-coin cooldown ──────────────────────────────

    def record_post_close_cooldown(self, symbol: str, cooldown_minutes: int):
        """
        V10: Apply a post-CLOSE cooldown to a coin.
        Prevents re-entering the same coin too quickly after a trade exits.
        Called by /positions/close endpoint after successful close.
        Separate from open-trade cooldown (which uses COIN_COOLDOWN_MINUTES).
        """
        until = time.time() + cooldown_minutes * 60
        self.state.post_close_cooldown_until[symbol] = until
        logger.info(
            f"[Cooldown] Post-close cooldown set for {symbol}: "
            f"{cooldown_minutes}m (until {until:.0f})"
        )

    def is_post_close_cooldown_active(self, symbol: str) -> tuple[bool, int]:
        """
        V10: Check if a coin is in post-close cooldown.
        Returns (is_active, remaining_seconds).
        """
        now = time.time()
        until = self.state.post_close_cooldown_until.get(symbol, 0.0)
        if until > now:
            return True, int(until - now)
        return False, 0

    # ── V14: Signal Dedup Memory ──────────────────────────────────────

    # Cooldown durations per strategy mode (seconds)
    _SIGNAL_COOLDOWNS = {
        "scalp":  45 * 60,    # 45 minutes for scalp
        "sniper": 45 * 60,    # 45 minutes for sniper
        "swing":  6 * 3600,   # 6 hours for swing
    }
    # Minimum confidence improvement to override cooldown
    _CONFIDENCE_IMPROVEMENT_THRESHOLD = 10

    @staticmethod
    def _signal_fingerprint(symbol: str, side: str, strategy_type: str) -> str:
        """Build dedup key: BTCUSDT:BUY:scalp"""
        prefix = "scalp"
        st = (strategy_type or "").lower()
        if st.startswith("swing"):
            prefix = "swing"
        elif st.startswith("sniper"):
            prefix = "sniper"
        return f"{symbol}:{side}:{prefix}"

    def is_signal_duplicate(
        self, symbol: str, side: str, strategy_type: str, confidence: int
    ) -> tuple[bool, str]:
        """
        V14: Check if this signal was already sent recently.

        Returns (is_duplicate, reason).
        A signal is NOT duplicate if:
          - Never sent before
          - Cooldown expired (scalp=45m, swing=6h)
          - Confidence improved by 10+ points since last send
        """
        key = self._signal_fingerprint(symbol, side, strategy_type)
        prev = self.state.signal_fingerprints.get(key)

        if not prev:
            return False, ""

        now = time.time()
        sent_at = prev.get("sent_at", 0)
        prev_conf = prev.get("confidence", 0)

        # Determine cooldown for this strategy mode
        prefix = key.split(":")[-1]  # scalp / swing / sniper
        cooldown = self._SIGNAL_COOLDOWNS.get(prefix, 45 * 60)

        elapsed = now - sent_at
        if elapsed >= cooldown:
            return False, ""  # Cooldown expired — allow

        # Allow if confidence materially improved
        conf_improvement = confidence - prev_conf
        if conf_improvement >= self._CONFIDENCE_IMPROVEMENT_THRESHOLD:
            return False, ""  # Significant improvement — allow

        remaining_min = int((cooldown - elapsed) / 60)
        return True, (
            f"Duplicate signal blocked: {symbol} {side} ({prefix}) "
            f"already sent {int(elapsed/60)}m ago (conf {prev_conf}→{confidence}, "
            f"need +{self._CONFIDENCE_IMPROVEMENT_THRESHOLD} or wait {remaining_min}m)"
        )

    def record_signal_sent(
        self, symbol: str, side: str, strategy_type: str, confidence: int
    ):
        """V14: Record that a signal was sent to Telegram."""
        key = self._signal_fingerprint(symbol, side, strategy_type)
        self.state.signal_fingerprints[key] = {
            "sent_at": time.time(),
            "confidence": confidence,
        }

    def _prune_stale_fingerprints(self):
        """V14: Remove signal fingerprints older than 24h."""
        now = time.time()
        cutoff = now - 86400
        self.state.signal_fingerprints = {
            k: v for k, v in self.state.signal_fingerprints.items()
            if v.get("sent_at", 0) > cutoff
        }

    # ─── Stats ────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        total = self.state.total_trades
        win_rate = self.state.winning_trades / total * 100 if total > 0 else 0
        starting = self.state.daily_starting_balance
        daily_pnl_pct = (self.state.daily_pnl / starting * 100) if starting > 0 else 0.0

        now = time.time()
        one_hour_ago = now - 3600
        hourly_count = len([
            ts for ts in self.state.hourly_trade_timestamps if ts > one_hour_ago
        ])

        loss_cooldown_active, loss_cooldown_remaining = self.is_loss_cooldown_active()

        return {
            "total_trades": total,
            "winning_trades": self.state.winning_trades,
            "losing_trades": self.state.losing_trades,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(self.state.total_pnl, 4),
            "daily_date": self.state.daily_date,
            "daily_trades": self.state.daily_trades,
            "daily_pnl": round(self.state.daily_pnl, 4),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "trading_paused": self.state.trading_paused,
            "pause_reason": self.state.pause_reason,
            "hourly_trades": hourly_count,
            "hourly_max": settings.HOURLY_MAX_TRADES,
            "daily_max": settings.DAILY_MAX_TRADES,
            "consecutive_losses": self.state.consecutive_losses,
            "loss_cooldown_active": loss_cooldown_active,
            "loss_cooldown_remaining_secs": loss_cooldown_remaining,
            "ai_usage_count": self.state.ai_usage_count,
            "skipped_trades_today": self.state.skipped_trades_today,
        }


# Singleton
state_manager = StateManager()
