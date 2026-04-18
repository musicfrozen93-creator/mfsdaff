"""
V4 Telegram Notification Module — Privacy-Safe Group Channel Output

V4 Design Rules:
  - NEVER expose account names, labels, emails, IDs, or balances
  - ONE message per signal (not per account)
  - Aggregated skip reasons only (counts, not names)
  - No scan spam, no per-account alerts
  - Only actionable output goes to Telegram

Kept from V3:
  - TP/SL failure alerts (critical safety)
  - Error alerts (critical)
  - Daily report (aggregated)

Removed from public channel:
  - Per-account trade_opened
  - Per-account trade_skipped
  - Per-account daily_target_hit / daily_loss_hit
  - scan_complete / no_signals (spam)
  - signal_summary (replaced by unified messages)
"""

import logging
from typing import Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)


class TelegramNotifier:

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — skipping notification")
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════
    # V4: ONE CLEAN FINAL MESSAGE — Trade Executed
    # ═══════════════════════════════════════════════════════════════════

    async def send_execution_result(
        self,
        symbol: str,
        side: str,
        confidence: int,
        executed_count: int,
        skipped_count: int,
        skip_reasons: dict,        # {"daily target": 2, "low balance": 1}
        entry_price: float,
        fill_price: float,
        leverage: int,
        take_profit: float,
        stop_loss: float,
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        reason: str = "",
        setup_grade: str = "",
        sl_attached: bool = True,
        tp_attached: bool = True,
        order_method: str = "MARKET",
    ):
        """
        V4: Single clean Telegram message for executed trades.
        NO account names/labels/IDs/balances.
        Shows totals only.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        # Grade emoji
        grade_emoji = {"A": "🅰️", "B": "🅱️", "C": "©️"}.get(setup_grade, "")
        grade_line = f"\nGrade: <b>{grade_emoji} {setup_grade}</b>" if setup_grade else ""

        # TP/SL status
        if sl_attached and tp_attached:
            tp_sl_status = "✅ TP/SL attached successfully"
        elif sl_attached and not tp_attached:
            tp_sl_status = "⚠️ SL attached, TP FAILED — check manually"
        elif not sl_attached and tp_attached:
            tp_sl_status = "⚠️ TP attached, SL FAILED — check manually"
        else:
            tp_sl_status = "🚨 BOTH TP/SL FAILED — manual action required!"

        # Skip reasons (aggregated, no names)
        skip_block = ""
        if skip_reasons:
            skip_lines = []
            for reason_cat, count in skip_reasons.items():
                skip_lines.append(f"  • {count} {reason_cat.lower()}")
            skip_block = "\n<b>Skipped Reasons:</b>\n" + "\n".join(skip_lines)

        # Reason (truncated)
        reason_block = ""
        if reason:
            truncated = reason[:200]
            reason_block = f"\n\n<b>Reason:</b>\n<i>{truncated}</i>"

        # Use fill_price if available, otherwise entry_price
        display_price = fill_price if fill_price > 0 else entry_price

        # TP/SL percentage display
        tp_pct_str = f" (+{tp_pct:.1f}%)" if tp_pct > 0 else ""
        sl_pct_str = f" (-{sl_pct:.1f}%)" if sl_pct > 0 else ""

        msg = (
            f"🚀 <b>TRADE EXECUTED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{grade_line}\n\n"
            f"Executed Accounts: <b>{executed_count}</b>\n"
            f"Skipped Accounts: <b>{skipped_count}</b>\n\n"
            f"Entry: <b>${display_price:,.6f}</b>\n"
            f"Leverage: <b>{leverage}x</b>\n"
            f"Size: <b>Dynamic</b>\n"
            f"Method: <b>{order_method}</b>\n\n"
            f"TP: <b>${take_profit:,.6f}</b>{tp_pct_str}\n"
            f"SL: <b>${stop_loss:,.6f}</b>{sl_pct_str}"
            f"{reason_block}\n\n"
            f"<b>Status:</b>\n{tp_sl_status}"
            f"{skip_block}"
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # V4: ONE CLEAN FINAL MESSAGE — No Execution
    # ═══════════════════════════════════════════════════════════════════

    async def send_no_execution(
        self,
        symbol: str,
        side: str,
        confidence: int,
        skipped_count: int,
        skip_reasons: dict,
    ):
        """
        V4: Single message when all accounts were skipped.
        Only sent if there were accounts to try (not on empty account list).
        NO account names/labels/IDs.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        skip_lines = []
        if skip_reasons:
            for reason_cat, count in skip_reasons.items():
                skip_lines.append(f"  • {count} {reason_cat.lower()}")

        skip_block = "\n".join(skip_lines) if skip_lines else "  • Unknown"

        msg = (
            f"⚠️ <b>NO EXECUTION</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Signal: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>\n\n"
            f"Executed Accounts: <b>0</b>\n"
            f"Skipped Accounts: <b>{skipped_count}</b>\n\n"
            f"<b>Reasons:</b>\n{skip_block}"
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # CRITICAL ALERTS ONLY (kept from V3)
    # ═══════════════════════════════════════════════════════════════════

    async def tp_sl_failed(
        self,
        symbol: str,
        side: str,
        sl_attached: bool,
        tp_attached: bool,
        error: str,
    ):
        """V3: Alert when TP/SL placement fails after 3 retries."""
        failed_items = []
        if not sl_attached:
            failed_items.append("❌ STOP LOSS")
        if not tp_attached:
            failed_items.append("❌ TAKE PROFIT")

        msg = (
            f"🚨 <b>TP/SL ATTACHMENT FAILED</b>\n\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Side: <b>{side}</b>\n"
            f"Failed: {', '.join(failed_items)}\n"
            f"Error: <code>{error[:200]}</code>\n\n"
            f"⚠️ <b>UNPROTECTED POSITION — Manual action required!</b>"
        )
        await self.send(msg)

    # ─── Error Alert ──────────────────────────────────────────────────

    async def error_alert(self, context: str, error: str):
        msg = (
            f"⚠️ <b>ERROR</b>\n"
            f"Context: {context}\n"
            f"Error: <code>{error[:300]}</code>"
        )
        await self.send(msg)

    # ─── Trading Paused ──────────────────────────────────────────────

    async def trading_paused(self, reason: str):
        msg = (
            f"⛔ <b>TRADING PAUSED</b>\n\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    # ─── Daily Report (Aggregated — no account names) ─────────────────

    async def daily_report(
        self,
        total_trades: int,
        win_rate: float,
        pnl: float,
        active_accounts: int,
        skipped_trades: int,
        ai_calls: int,
    ):
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"📊 <b>DAILY REPORT</b>\n\n"
            f"Total Trades: <b>{total_trades}</b>\n"
            f"Win Rate: <b>{win_rate:.1f}%</b>\n"
            f"P&L: <b>{pnl_emoji} ${pnl:,.2f}</b>\n"
            f"Active Accounts: <b>{active_accounts}</b>\n"
            f"Skipped Trades: <b>{skipped_trades}</b>\n"
            f"AI Calls: <b>{ai_calls}</b>"
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # BACKWARD COMPAT — Single-account endpoints (no account label)
    # ═══════════════════════════════════════════════════════════════════

    async def trade_opened(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        leverage: int,
        position_size: float,
        take_profit: float,
        stop_loss: float,
        confidence: int,
        # V3 additions
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        setup_grade: str = "",
        daily_pnl_pct: float = 0.0,
        # V4: account_label accepted but NEVER displayed
        account_label: str = "",
    ):
        """
        Single-account trade notification.
        Used by /execute-full endpoint only.
        V4: account_label parameter kept for API compat but NEVER shown.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        # V3: Grade emoji
        grade_emoji = {"A": "🅰️", "B": "🅱️", "C": "©️"}.get(setup_grade, "")
        grade_line = f"\nGrade: <b>{grade_emoji} {setup_grade}</b>" if setup_grade else ""

        # V3: TP/SL percentage display
        tp_sl_pct_line = ""
        if tp_pct > 0 or sl_pct > 0:
            tp_sl_pct_line = f"\nTP: <b>{tp_pct:.1f}%</b> | SL: <b>{sl_pct:.1f}%</b>"

        # V3: Daily progress
        daily_line = ""
        if daily_pnl_pct != 0:
            daily_emoji = "📈" if daily_pnl_pct >= 0 else "📉"
            daily_line = f"\nDaily: <b>{daily_emoji} {daily_pnl_pct:+.1f}%</b>"

        msg = (
            f"✅ <b>TRADE OPENED</b>\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"Leverage: <b>{leverage}x</b>\n"
            f"Size: <b>${position_size:,.2f}</b>\n"
            f"TP: <b>${take_profit:,.6f}</b>\n"
            f"SL: <b>${stop_loss:,.6f}</b>"
            f"{tp_sl_pct_line}\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{grade_line}"
            f"{daily_line}"
        )
        await self.send(msg)

    async def trade_skipped(self, symbol: str, reason: str, account_label: str = ""):
        """Single-account skip notification. V4: No account label shown."""
        msg = (
            f"⏭️ <b>TRADE SKIPPED</b>\n"
            f"Symbol: {symbol}\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    async def loss_cooldown(self, consecutive_losses: int, cooldown_minutes: int):
        msg = (
            f"🔴 <b>LOSS COOLDOWN ACTIVATED</b>\n\n"
            f"Consecutive losses: <b>{consecutive_losses}</b>\n"
            f"Pausing for: <b>{cooldown_minutes} minutes</b>"
        )
        await self.send(msg)
