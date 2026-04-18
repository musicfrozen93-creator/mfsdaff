"""
V3 Telegram Notification Module — Premium Message Formats
Clean, professional trade alerts with V3 additions:
  - TP/SL percentages
  - Setup grade (A/B/C)
  - Daily progress %
  - TP/SL failure alerts
  - Daily target/loss limit alerts
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

    # ─── V3 Trade Opened (Enhanced) ───────────────────────────────────

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
        account_label: str = "",
        # V3 additions
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        setup_grade: str = "",
        daily_pnl_pct: float = 0.0,
    ):
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        account_line = f"\nAccount: <b>{account_label}</b>" if account_label else ""

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
            f"{account_line}"
        )
        await self.send(msg)

    # ─── V3: TP/SL Failed Alert ───────────────────────────────────────

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

    # ─── V3: Daily Target Hit ─────────────────────────────────────────

    async def daily_target_hit(
        self,
        account_label: str,
        daily_pnl_pct: float,
        mode: str = "safe",  # "safe" or "stop"
    ):
        """V3: Alert when account hits daily profit target."""
        if mode == "stop":
            msg = (
                f"🔒 <b>DAILY PROFIT TARGET — TRADING STOPPED</b>\n\n"
                f"Account: <b>{account_label}</b>\n"
                f"Daily P&L: <b>+{daily_pnl_pct:.1f}%</b>\n\n"
                f"✅ Gains locked. No more trades until tomorrow.\n"
                f"Great discipline! 💪"
            )
        else:
            msg = (
                f"🛡️ <b>SAFE MODE ACTIVATED</b>\n\n"
                f"Account: <b>{account_label}</b>\n"
                f"Daily P&L: <b>+{daily_pnl_pct:.1f}%</b>\n\n"
                f"Only elite setups (91%+ confidence) allowed.\n"
                f"Position size reduced by 50%.\n"
                f"Max 1 more trade."
            )
        await self.send(msg)

    # ─── V3: Daily Loss Limit Hit ─────────────────────────────────────

    async def daily_loss_hit(
        self,
        account_label: str,
        daily_pnl_pct: float,
        mode: str = "reduce",  # "reduce" or "stop"
    ):
        """V3: Alert when account hits daily loss limit."""
        if mode == "stop":
            msg = (
                f"🛑 <b>DAILY LOSS LIMIT — TRADING STOPPED</b>\n\n"
                f"Account: <b>{account_label}</b>\n"
                f"Daily P&L: <b>{daily_pnl_pct:.1f}%</b>\n\n"
                f"Account protected. No more trades until tomorrow.\n"
                f"Reviewing strategy is recommended."
            )
        else:
            msg = (
                f"⚠️ <b>DAILY LOSS REDUCTION ACTIVE</b>\n\n"
                f"Account: <b>{account_label}</b>\n"
                f"Daily P&L: <b>{daily_pnl_pct:.1f}%</b>\n\n"
                f"Position size reduced by 50%.\n"
                f"Only elite setups allowed."
            )
        await self.send(msg)

    # ─── Signal Summary (Multi-Account) ───────────────────────────────

    async def signal_summary(
        self,
        symbol: str,
        side: str,
        confidence: int,
        executed_count: int,
        skipped_count: int,
        skip_reasons: dict,  # {"Low Balance": 3, "Risk Limit": 1, ...}
        total_accounts: int,
    ):
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        lines = [
            f"📊 <b>SIGNAL SUMMARY</b>\n",
            f"Coin: <b>{symbol}</b>",
            f"Side: <b>{direction}</b>",
            f"Confidence: <b>{confidence}%</b>\n",
            f"✅ Executed: <b>{executed_count}</b> accounts",
            f"⏭️ Skipped: <b>{skipped_count}</b> accounts",
        ]

        if skip_reasons:
            lines.append("\n<b>Skip Reasons:</b>")
            for reason, count in skip_reasons.items():
                lines.append(f"  • {count} {reason}")

        msg = "\n".join(lines)
        await self.send(msg)

    # ─── Trade Skipped ────────────────────────────────────────────────

    async def trade_skipped(self, symbol: str, reason: str, account_label: str = ""):
        account_line = f"\nAccount: {account_label}" if account_label else ""
        msg = (
            f"⏭️ <b>TRADE SKIPPED</b>\n"
            f"Symbol: {symbol}\n"
            f"Reason: <i>{reason}</i>"
            f"{account_line}"
        )
        await self.send(msg)

    # ─── Scan Complete ────────────────────────────────────────────────

    async def scan_complete(self, count: int, top_coins: list, tradeable_count: int = 0):
        if count == 0:
            msg = (
                f"🔍 <b>SCAN COMPLETE — NO CANDIDATES</b>\n"
                f"No coins passed quality filters.\n"
                f"Next scan in 30 minutes."
            )
        else:
            coin_list = ", ".join(top_coins[:5])
            if len(top_coins) > 5:
                coin_list += f" +{len(top_coins) - 5} more"
            msg = (
                f"🔍 <b>SCAN COMPLETE</b>\n\n"
                f"Candidates: <b>{count}</b>\n"
                f"Tradeable signals: <b>{tradeable_count}</b>\n"
                f"Top: {coin_list}"
            )
        await self.send(msg)

    # ─── No Signals ───────────────────────────────────────────────────

    async def no_signals(self, analyzed_count: int = 0):
        msg = (
            f"🔍 <b>SCAN COMPLETE — NO SIGNALS</b>\n"
            f"Analyzed: {analyzed_count} coins\n"
            f"No trades met confluence criteria.\n"
            f"Next scan in 30 minutes."
        )
        await self.send(msg)

    # ─── Trading Paused ──────────────────────────────────────────────

    async def trading_paused(self, reason: str):
        msg = (
            f"⛔ <b>TRADING PAUSED</b>\n\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    # ─── Loss Cooldown ────────────────────────────────────────────────

    async def loss_cooldown(self, consecutive_losses: int, cooldown_minutes: int):
        msg = (
            f"🔴 <b>LOSS COOLDOWN ACTIVATED</b>\n\n"
            f"Consecutive losses: <b>{consecutive_losses}</b>\n"
            f"Pausing for: <b>{cooldown_minutes} minutes</b>"
        )
        await self.send(msg)

    # ─── Daily Report ─────────────────────────────────────────────────

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

    # ─── Error Alert ──────────────────────────────────────────────────

    async def error_alert(self, context: str, error: str):
        msg = (
            f"⚠️ <b>ERROR</b>\n"
            f"Context: {context}\n"
            f"Error: <code>{error[:300]}</code>"
        )
        await self.send(msg)
