"""
V4 State Manager — DB-backed with in-memory cache.
Tracks trade limits, cooldowns, and rate limiting.
Designed for multi-account operation.

V4 Changes:
  - UTC timezone for daily reset (was local time — caused mismatch with daily_guard)
  - Removed duplicate daily P&L tracking (per-account daily_guard handles it)
  - Kept: hourly rate limits, coin cooldowns, trade counts (global, valid)
  - Simplified check_daily_limits to only check trade count limits
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

    # Hourly rate limit: list of unix timestamps of recent trades
    hourly_trade_timestamps: List[float] = field(default_factory=list)

    # Consecutive loss tracking (global — per-account is in daily_guard)
    consecutive_losses: int = 0
    loss_cooldown_until: float = 0.0  # Unix timestamp

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

    def record_ai_call(self):
        """Record an AI API call."""
        self.state.ai_usage_count += 1

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
