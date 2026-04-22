"""
V5.5 Telegram Notification Module — Privacy-Safe Group Channel Output

V5.5 Design Rules:
  - NEVER expose account names, labels, emails, IDs, or balances
  - ONE message per signal (not per account)
  - Aggregated skip reasons only (counts, not names)
  - No scan spam, no per-account alerts
  - Only actionable output goes to Telegram
  - Shows strategy type (Scalping / Swing / Sniper)
  - Shows market regime when relevant
  - Shows TP/SL verification status with order IDs
  - Shows R:R ratio
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
        # V5 additions
        strategy_type: str = "",
        regime: str = "",
        # V5.5 additions
        sl_order_id: str = "",
        tp_order_id: str = "",
        risk_reward: float = 0.0,
        # V5.5 Partial TP
        partial_tp_enabled: bool = False,
        tp1_price: float = 0.0,
        tp2_price: float = 0.0,
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

        # V5: Strategy type display
        strategy_display = ""
        if strategy_type:
            if strategy_type.startswith("swing"):
                strategy_display = "🌊 Swing"
            elif strategy_type.startswith("sniper"):
                strategy_display = "🎯 Sniper"
            else:
                strategy_display = "⚡ Scalping"

        # V5: Regime display
        regime_display = ""
        if regime:
            regime_map = {
                "TRENDING_BULL": "🟢 Trending Bull",
                "TRENDING_BEAR": "🔴 Trending Bear",
                "SIDEWAYS_RANGE": "↔️ Sideways",
                "BREAKOUT_EXPANSION": "💥 Breakout",
                "HIGH_VOLATILITY": "⚠️ High Volatility",
                "DEAD_MARKET": "💤 Dead Market",
            }
            regime_display = regime_map.get(regime, regime)

        strategy_line = f"\nType: <b>{strategy_display}</b>" if strategy_display else ""
        regime_line = f"\nRegime: <b>{regime_display}</b>" if regime_display else ""

        # V5.5: TP/SL proof line with order IDs
        proof_line = ""
        if sl_order_id or tp_order_id:
            proof_parts = []
            if sl_order_id:
                proof_parts.append(f"SL=#{sl_order_id}")
            if tp_order_id:
                proof_parts.append(f"TP=#{tp_order_id}")
            proof_line = f"\nOrders: <code>{' | '.join(proof_parts)}</code>"

        # V5.5: Risk/Reward display
        rr_line = ""
        if risk_reward > 0:
            rr_line = f"\nR:R = <b>1:{risk_reward:.1f}</b>"

        # V5.5: Partial TP display
        if partial_tp_enabled and tp1_price > 0:
            tp_block = (
                f"TP Mode: <b>📊 Partial (40/30/30)</b>\n"
                f"TP1: <b>${tp1_price:,.6f}</b> (close 40%)\n"
                f"TP2: <b>${tp2_price:,.6f}</b> (close 30%)\n"
                f"Trail: <b>30%</b> with BE stop\n"
                f"SL: <b>${stop_loss:,.6f}</b>{sl_pct_str}"
            )
        else:
            tp_block = (
                f"TP: <b>${take_profit:,.6f}</b>{tp_pct_str}\n"
                f"SL: <b>${stop_loss:,.6f}</b>{sl_pct_str}"
            )

        msg = (
            f"🚀 <b>TRADE EXECUTED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{grade_line}"
            f"{strategy_line}"
            f"{regime_line}\n\n"
            f"Executed Accounts: <b>{executed_count}</b>\n"
            f"Skipped Accounts: <b>{skipped_count}</b>\n\n"
            f"Entry: <b>${display_price:,.6f}</b>\n"
            f"Leverage: <b>{leverage}x</b>\n"
            f"Method: <b>{order_method}</b>\n\n"
            f"{tp_block}"
            f"{rr_line}"
            f"{reason_block}\n\n"
            f"<b>Status:</b>\n{tp_sl_status}"
            f"{proof_line}"
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
    # CRITICAL ALERTS (V7: Atomic TP/SL Protection)
    # ═══════════════════════════════════════════════════════════════════

    async def tp_sl_failed(
        self,
        symbol: str,
        side: str,
        sl_attached: bool,
        tp_attached: bool,
        error: str,
    ):
        """V7: Alert when TP/SL fails AND emergency close also failed — CRITICAL."""
        failed_items = []
        if not sl_attached:
            failed_items.append("❌ STOP LOSS")
        if not tp_attached:
            failed_items.append("❌ TAKE PROFIT")

        msg = (
            f"🔥 <b>CRITICAL: UNPROTECTED POSITION</b>\n\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Side: <b>{side}</b>\n"
            f"Failed: {', '.join(failed_items)}\n"
            f"Error: <code>{error[:200]}</code>\n\n"
            f"🚨 <b>EMERGENCY CLOSE ALSO FAILED!</b>\n"
            f"⚠️ <b>MANUAL ACTION REQUIRED IMMEDIATELY!</b>"
        )
        await self.send(msg)

    async def send_emergency_close(
        self,
        symbol: str,
        side: str,
        fill_price: float,
        sl_attached: bool,
        tp_attached: bool,
        error: str,
    ):
        """V7: Alert when position was emergency-closed due to TP/SL failure."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        failed_items = []
        if not sl_attached:
            failed_items.append("❌ STOP LOSS")
        if not tp_attached:
            failed_items.append("❌ TAKE PROFIT")

        msg = (
            f"🚨 <b>POSITION EMERGENCY CLOSED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Entry: <b>${fill_price:,.6f}</b>\n\n"
            f"<b>Reason:</b> TP/SL attachment failed after all retries\n"
            f"Failed: {', '.join(failed_items)}\n"
            f"Error: <code>{error[:150]}</code>\n\n"
            f"✅ <b>Position was closed to prevent unprotected exposure.</b>\n"
            f"<i>V7 Atomic Protection: No naked positions allowed.</i>"
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

    # ═══════════════════════════════════════════════════════════════════
    # V5.5: Break-Even & Partial TP Notifications
    # ═══════════════════════════════════════════════════════════════════

    async def break_even_moved(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        be_price: float,
        roi_pct: float,
    ):
        """V5.5: Notify when SL is moved to break-even to protect profits."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        msg = (
            f"🛡️ <b>BREAK-EVEN STOP ACTIVATED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"New SL: <b>${be_price:,.6f}</b>\n"
            f"Current ROI: <b>+{roi_pct:.1f}%</b>\n\n"
            f"✅ <i>Position now risk-free — profits protected</i>"
        )
        await self.send(msg)

    async def partial_tp_hit(
        self,
        symbol: str,
        side: str,
        tp_level: str,
        close_pct: int,
        price: float,
        remaining_pct: int,
    ):
        """V5.5: Notify when a partial TP level is hit."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        msg = (
            f"💰 <b>PARTIAL TP HIT — {tp_level}</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Closed: <b>{close_pct}%</b> of position\n"
            f"At Price: <b>${price:,.6f}</b>\n"
            f"Remaining: <b>{remaining_pct}%</b> trailing\n\n"
            f"<i>Position scaling out — locking profits</i>"
        )
        await self.send(msg)
