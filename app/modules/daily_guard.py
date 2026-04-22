"""
V7 Per-Account Daily Guard — Daily Profit/Loss Limits + Consecutive Loss Control

Per-account tracking (NOT global):
  +4% daily profit → SAFE MODE (91%+ only, 50% size, max 1 more trade)
  +6% daily profit → STOP trading that account (gains locked)
  -2% daily loss   → 50% size reduction, elite only
  -3% daily loss   → STOP trading that account
  2 consecutive losses → 30% size reduction
  3 consecutive losses → 1 hour pause

V7 Changes (from V4):
  - Tighter profit lock: +4% safe mode (was +5%), +6% hard stop (was +7%)
  - Tighter loss limit: -3% max loss (was -8%)
  - 3 consecutive losses to pause (was 4)
  - -2% triggers size reduction (was -5%)

V4 Fixes (kept):
  - PnL=0 + no trades today → ALWAYS allow (never false block)
  - Null / zero starting_balance → auto-fix from current balance
  - Detailed decision logging at every check
  - Timezone-safe UTC reset
  - force_reset() for manual recovery

Resets at UTC midnight.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AccountDayState:
    """Tracks one account's daily trading state."""
    account_id: int = 0
    date_str: str = ""
    starting_balance: float = 0.0
    current_pnl: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    consecutive_losses: int = 0
    is_stopped: bool = False
    stop_reason: str = ""
    is_safe_mode: bool = False
    safe_mode_trades_remaining: int = 0
    pause_until: float = 0.0   # Unix timestamp


class DailyGuard:
    """
    V4 Per-account daily limit manager.
    In-memory tracking, resets at UTC midnight.
    """

    def __init__(self):
        self._accounts: Dict[int, AccountDayState] = {}

    def _get_today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_state(self, account_id: int, balance: float = 0.0) -> AccountDayState:
        """Get or create today's state for an account. Auto-resets on new day."""
        today = self._get_today()
        state = self._accounts.get(account_id)

        if state is None or state.date_str != today:
            # New day or new account — reset
            if state and state.date_str != today:
                logger.info(
                    f"📅 Daily reset for account #{account_id}: "
                    f"prev day P&L: ${state.current_pnl:.2f} | "
                    f"trades: {state.trades_today} W/L: {state.wins_today}/{state.losses_today}"
                )
            state = AccountDayState(
                account_id=account_id,
                date_str=today,
                starting_balance=balance if balance > 0 else 0.0,
            )
            self._accounts[account_id] = state
            logger.info(
                f"  GUARD RESET: account #{account_id} | new day={today} | "
                f"starting_balance=${balance:.2f}"
            )

        # V4: Fix missing starting_balance — update from current balance
        if state.starting_balance <= 0 and balance > 0:
            state.starting_balance = balance
            logger.info(
                f"  GUARD: Fixed missing starting_balance for account #{account_id} → ${balance:.2f}"
            )

        return state

    # ─── Pre-Trade Check ─────────────────────────────────────────────

    def check_allowed(
        self,
        account_id: int,
        balance: float,
        confidence: int,
    ) -> dict:
        """
        Check if trading is allowed for this account.
        Returns: {
            "allowed": bool,
            "reason": str,
            "size_multiplier": float,  # 1.0 = full, 0.5 = half, etc.
            "safe_mode": bool,
        }
        """
        state = self._get_state(account_id, balance)
        now = time.time()

        # ── V4: If no trades today and PnL is 0, ALWAYS allow ────────
        if state.trades_today == 0 and state.current_pnl == 0:
            logger.info(
                f"  GUARD PASS: account #{account_id} | no trades today, PnL=$0 → allowed"
            )
            return {
                "allowed": True,
                "reason": "",
                "size_multiplier": 1.0,
                "safe_mode": False,
            }

        # ── Check if stopped ─────────────────────────────────────────
        if state.is_stopped:
            logger.info(
                f"  GUARD BLOCKED: account #{account_id} | stopped → {state.stop_reason}"
            )
            return {
                "allowed": False,
                "reason": state.stop_reason,
                "size_multiplier": 0.0,
                "safe_mode": False,
            }

        # ── Check consecutive loss pause ─────────────────────────────
        if state.pause_until > now:
            remaining_min = int((state.pause_until - now) / 60)
            reason = f"Consecutive loss pause — {remaining_min}m remaining"
            logger.info(
                f"  GUARD BLOCKED: account #{account_id} | {reason}"
            )
            return {
                "allowed": False,
                "reason": reason,
                "size_multiplier": 0.0,
                "safe_mode": False,
            }

        # ── Calculate daily P&L percentage ───────────────────────────
        daily_pnl_pct = 0.0
        if state.starting_balance > 0:
            daily_pnl_pct = (state.current_pnl / state.starting_balance) * 100
        else:
            # V4: Cannot calculate PnL% without starting balance — allow trading
            logger.warning(
                f"  GUARD: account #{account_id} starting_balance=0, cannot calculate PnL% → allowing"
            )
            return {
                "allowed": True,
                "reason": "",
                "size_multiplier": 1.0,
                "safe_mode": False,
            }

        size_multiplier = 1.0

        # Log current state for debugging
        logger.info(
            f"  GUARD CHECK: account #{account_id} | "
            f"pnl=${state.current_pnl:.2f} ({daily_pnl_pct:+.1f}%) | "
            f"trades={state.trades_today} W/L={state.wins_today}/{state.losses_today} | "
            f"target=+{settings.DAILY_PROFIT_LIMIT_PCT}% / {settings.DAILY_LOSS_LIMIT_PCT}% | "
            f"consec_losses={state.consecutive_losses} | balance=${balance:.2f}"
        )

        # ── +7% → FULL STOP ──────────────────────────────────────────
        if daily_pnl_pct >= settings.DAILY_PROFIT_LIMIT_PCT:
            state.is_stopped = True
            state.stop_reason = (
                f"Daily profit target hit: +{daily_pnl_pct:.1f}% "
                f"(limit: +{settings.DAILY_PROFIT_LIMIT_PCT}%). Gains locked."
            )
            logger.info(f"  GUARD STOP: account #{account_id} | {state.stop_reason}")
            return {
                "allowed": False,
                "reason": state.stop_reason,
                "size_multiplier": 0.0,
                "safe_mode": False,
            }

        # ── -8% → FULL STOP ──────────────────────────────────────────
        if daily_pnl_pct <= settings.DAILY_LOSS_LIMIT_PCT:
            state.is_stopped = True
            state.stop_reason = (
                f"Daily loss limit hit: {daily_pnl_pct:.1f}% "
                f"(limit: {settings.DAILY_LOSS_LIMIT_PCT}%). Account protected."
            )
            logger.info(f"  GUARD STOP: account #{account_id} | {state.stop_reason}")
            return {
                "allowed": False,
                "reason": state.stop_reason,
                "size_multiplier": 0.0,
                "safe_mode": False,
            }

        # ── +5% → SAFE MODE ──────────────────────────────────────────
        if daily_pnl_pct >= settings.DAILY_SAFE_MODE_PCT:
            if not state.is_safe_mode:
                state.is_safe_mode = True
                state.safe_mode_trades_remaining = 1
                logger.info(
                    f"🛡️ Account #{account_id} entering SAFE MODE: "
                    f"+{daily_pnl_pct:.1f}% daily profit"
                )

            if state.safe_mode_trades_remaining <= 0:
                state.is_stopped = True
                state.stop_reason = "Safe mode: max trades reached after +5% daily"
                logger.info(f"  GUARD STOP: account #{account_id} | {state.stop_reason}")
                return {
                    "allowed": False,
                    "reason": state.stop_reason,
                    "size_multiplier": 0.0,
                    "safe_mode": True,
                }

            # Safe mode: only 91%+ confidence
            if confidence < 91:
                reason = f"Safe mode active (+{daily_pnl_pct:.1f}%): only 91%+ confidence allowed"
                logger.info(f"  GUARD BLOCKED: account #{account_id} | {reason}")
                return {
                    "allowed": False,
                    "reason": reason,
                    "size_multiplier": 0.5,
                    "safe_mode": True,
                }

            size_multiplier = 0.5  # 50% size in safe mode

        # ── -5% → REDUCED SIZE ───────────────────────────────────────
        if daily_pnl_pct <= -settings.DAILY_LOSS_REDUCE_PCT:
            # Only elite setups allowed
            if confidence < 91:
                reason = f"Loss reduction active ({daily_pnl_pct:.1f}%): only 91%+ confidence allowed"
                logger.info(f"  GUARD BLOCKED: account #{account_id} | {reason}")
                return {
                    "allowed": False,
                    "reason": reason,
                    "size_multiplier": 0.5,
                    "safe_mode": False,
                }
            size_multiplier = min(size_multiplier, 0.5)  # 50% size

        # ── Consecutive loss size reduction ──────────────────────────
        if state.consecutive_losses >= settings.CONSECUTIVE_LOSS_REDUCE_THRESHOLD:
            loss_reduction = 0.7  # 30% reduction
            size_multiplier = min(size_multiplier, loss_reduction)
            logger.info(
                f"  Consecutive loss reduction: {state.consecutive_losses} losses → "
                f"size multiplier={size_multiplier:.2f}"
            )

        logger.info(
            f"  GUARD PASS: account #{account_id} | "
            f"pnl={daily_pnl_pct:+.1f}% | multiplier={size_multiplier:.2f} | "
            f"safe_mode={state.is_safe_mode}"
        )

        return {
            "allowed": True,
            "reason": "",
            "size_multiplier": size_multiplier,
            "safe_mode": state.is_safe_mode,
        }

    # ─── Record Trade Result ─────────────────────────────────────────

    def record_trade(self, account_id: int, pnl: float, balance: float = 0.0):
        """Record a trade result for daily tracking."""
        state = self._get_state(account_id, balance)
        state.current_pnl += pnl
        state.trades_today += 1

        if pnl > 0:
            state.wins_today += 1
            state.consecutive_losses = 0
        else:
            state.losses_today += 1
            state.consecutive_losses += 1

            # Check consecutive loss pause
            if state.consecutive_losses >= settings.CONSECUTIVE_LOSS_PAUSE_THRESHOLD:
                pause_secs = settings.CONSECUTIVE_LOSS_PAUSE_MINUTES * 60
                state.pause_until = time.time() + pause_secs
                logger.warning(
                    f"⏸️ Account #{account_id} paused: "
                    f"{state.consecutive_losses} consecutive losses. "
                    f"Pausing for {settings.CONSECUTIVE_LOSS_PAUSE_MINUTES}m"
                )

        # Decrement safe mode trades if active
        if state.is_safe_mode and state.safe_mode_trades_remaining > 0:
            state.safe_mode_trades_remaining -= 1

        daily_pnl_pct = 0.0
        if state.starting_balance > 0:
            daily_pnl_pct = (state.current_pnl / state.starting_balance) * 100

        logger.info(
            f"  Daily guard update: account #{account_id} | "
            f"P&L: ${state.current_pnl:.2f} ({daily_pnl_pct:+.1f}%) | "
            f"W/L: {state.wins_today}/{state.losses_today} | "
            f"Consec losses: {state.consecutive_losses}"
        )

    # ─── V4: Force Reset (manual or automatic recovery) ──────────────

    def force_reset(self, account_id: int, balance: float = 0.0):
        """
        Force-reset an account's daily state.
        Use for recovery from corrupted state or manual override.
        """
        today = self._get_today()
        old_state = self._accounts.get(account_id)
        if old_state:
            logger.warning(
                f"🔄 FORCE RESET: account #{account_id} | "
                f"was: stopped={old_state.is_stopped} pnl=${old_state.current_pnl:.2f} "
                f"trades={old_state.trades_today}"
            )

        self._accounts[account_id] = AccountDayState(
            account_id=account_id,
            date_str=today,
            starting_balance=balance if balance > 0 else 0.0,
        )
        logger.info(
            f"  FORCE RESET complete: account #{account_id} → clean state, balance=${balance:.2f}"
        )

    # ─── Get Daily Stats ─────────────────────────────────────────────

    def get_daily_pnl_pct(self, account_id: int, balance: float = 0.0) -> float:
        """Get current day's P&L percentage for an account."""
        state = self._get_state(account_id, balance)
        if state.starting_balance > 0:
            return round((state.current_pnl / state.starting_balance) * 100, 2)
        return 0.0

    def get_account_stats(self, account_id: int) -> dict:
        """Get full daily stats for an account."""
        state = self._accounts.get(account_id)
        if not state:
            return {
                "trades_today": 0, "wins_today": 0, "losses_today": 0,
                "daily_pnl": 0.0, "daily_pnl_pct": 0.0,
                "consecutive_losses": 0, "is_stopped": False, "is_safe_mode": False,
            }

        daily_pnl_pct = 0.0
        if state.starting_balance > 0:
            daily_pnl_pct = (state.current_pnl / state.starting_balance) * 100

        return {
            "trades_today": state.trades_today,
            "wins_today": state.wins_today,
            "losses_today": state.losses_today,
            "daily_pnl": round(state.current_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "consecutive_losses": state.consecutive_losses,
            "is_stopped": state.is_stopped,
            "stop_reason": state.stop_reason,
            "is_safe_mode": state.is_safe_mode,
        }

    # ─── V4: Structured Guard Log (for private internal logging) ─────

    def get_guard_log(self, account_id: int, balance: float = 0.0) -> dict:
        """
        Returns structured log dict for internal debugging.
        Never sent to Telegram.
        """
        state = self._get_state(account_id, balance)
        daily_pnl_pct = 0.0
        if state.starting_balance > 0:
            daily_pnl_pct = (state.current_pnl / state.starting_balance) * 100

        return {
            "account_id": account_id,
            "date": state.date_str,
            "starting_balance": state.starting_balance,
            "current_balance": balance,
            "pnl_today": round(state.current_pnl, 2),
            "pnl_pct": round(daily_pnl_pct, 2),
            "trades_today": state.trades_today,
            "wins": state.wins_today,
            "losses": state.losses_today,
            "consecutive_losses": state.consecutive_losses,
            "is_stopped": state.is_stopped,
            "stop_reason": state.stop_reason,
            "is_safe_mode": state.is_safe_mode,
            "profit_target": settings.DAILY_PROFIT_LIMIT_PCT,
            "loss_limit": settings.DAILY_LOSS_LIMIT_PCT,
        }


# Singleton
daily_guard = DailyGuard()
