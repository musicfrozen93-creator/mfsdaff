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
        order_method: str = "MARKET",
        # V5 additions
        strategy_type: str = "",
        regime: str = "",
        # V5.5 additions (kept for API compat with /execute-full, but not used by /execute-multi)
        sl_attached: bool = True,
        tp_attached: bool = True,
        sl_order_id: str = "",
        tp_order_id: str = "",
        risk_reward: float = 0.0,
        partial_tp_enabled: bool = False,
        tp1_price: float = 0.0,
        tp2_price: float = 0.0,
        # V10: Two-Engine Architecture
        protection_mode: str = "",  # "external_engine" = Protection Engine manages TP/SL
    ):
        """
        V10: Single clean Telegram message for executed trades.
        NO account names/labels/IDs/balances.
        Shows totals only.
        In 2-Engine mode (protection_mode='external_engine'): shows
        Protection Engine banner instead of TP/SL order status.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        # Grade emoji
        grade_emoji = {"A": "🅰️", "B": "🅱️", "C": "©️"}.get(setup_grade, "")
        grade_line = f"\nGrade: <b>{grade_emoji} {setup_grade}</b>" if setup_grade else ""

        # V10: Protection status line
        if protection_mode == "external_engine":
            tp_sl_status = "🛡️ <b>Protection: External Engine Active</b>"
        elif sl_attached and tp_attached:
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
        # V10: In external_engine mode, no order IDs (no native orders placed)
        proof_line = ""
        if protection_mode != "external_engine" and (sl_order_id or tp_order_id):
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
            f"🚀 <b>TRADE OPENED</b>\n\n"
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
            f"{rr_line}\n\n"
            f"<b>Protection:</b>\n{tp_sl_status}"
            f"{proof_line}"
            f"{reason_block}"
            f"{skip_block}"
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # V10: Protection Engine — Trade Closed notification
    # ═══════════════════════════════════════════════════════════════════

    async def send_position_closed_by_engine(
        self,
        symbol: str,
        side: str,
        close_reason: str,           # "tp_hit" | "sl_hit" | "trailing_exit" | "manual"
        pnl_pct: float,
        accounts_closed: int = 1,
        entry_price: float = 0.0,
        close_price: float = 0.0,
        duration_minutes: int = 0,
        strategy_type: str = "",
    ):
        """
        V10: Protection Engine bulk close notification.

        Sent when position_manager.py closes a trade on behalf of all accounts.
        Format: Shield POSITION CLOSED / Coin / Side / Accounts / Reason / PnL
        """
        direction = "\U0001f7e2 LONG" if side == "BUY" else "\U0001f534 SHORT"
        pnl_sign = "+" if pnl_pct >= 0 else ""
        pnl_emoji = "\U0001f4c8" if pnl_pct >= 0 else "\U0001f4c9"

        reason_map = {
            "tp_hit": "TAKE PROFIT",
            "sl_hit": "STOP LOSS",
            "trailing_exit": "TRAILING EXIT",
            "manual": "MANUAL CLOSE",
        }
        reason_display = reason_map.get(close_reason, close_reason.upper())

        entry_line = f"\nEntry: <b>${entry_price:,.6f}</b>" if entry_price > 0 else ""
        close_line = f"\nClose: <b>${close_price:,.6f}</b>" if close_price > 0 else ""
        dur_line = f"\nDuration: <b>{duration_minutes}m</b>" if duration_minutes > 0 else ""

        strategy_display = ""
        if strategy_type:
            if strategy_type.startswith("swing"):
                strategy_display = "\U0001f30a Swing"
            elif strategy_type.startswith("sniper"):
                strategy_display = "\U0001f3af Sniper"
            else:
                strategy_display = "\u26a1 Scalp"
        type_line = f"\nType: <b>{strategy_display}</b>" if strategy_display else ""

        msg = (
            f"\U0001f6e1\ufe0f <b>POSITION CLOSED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>"
            f"{type_line}\n"
            f"Accounts Closed: <b>{accounts_closed}</b>\n\n"
            f"Reason: <b>{reason_display}</b>"
            f"{entry_line}"
            f"{close_line}"
            f"{dur_line}\n\n"
            f"PnL: <b>{pnl_emoji} {pnl_sign}{pnl_pct:.2f}%</b>\n\n"
            f"<i>Managed by Protection Engine</i>"
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
        """V9: Alert when position was emergency-closed due to TP/SL failure."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        failed_items = []
        if not sl_attached:
            failed_items.append("❌ STOP LOSS")
        if not tp_attached:
            failed_items.append("❌ TAKE PROFIT")

        msg = (
            f"🚨 <b>PROTECTION FAILED — Position closed safely</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Entry: <b>${fill_price:,.6f}</b>\n\n"
            f"<b>Reason:</b> TP/SL attachment failed after all retries\n"
            f"Failed: {', '.join(failed_items)}\n"
            f"Error: <code>{error[:150]}</code>\n\n"
            f"✅ <b>Position was closed at market to prevent unprotected exposure.</b>\n"
            f"<i>V9 Bracket Protection: No naked positions allowed.</i>"
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # V7: Categorized Skip & Watchlist Notifications
    # ═══════════════════════════════════════════════════════════════════

    async def send_skipped(
        self,
        symbol: str,
        side: str,
        confidence: int,
        category: str,
        reason: str,
        strategy_type: str = "",
    ):
        """
        V7: Send a categorized skip notification.
        Categories: Low Confidence, Cooldown, Daily Guard, TP/SL Failed,
                    Subscription, Insufficient Balance, etc.
        """
        category_emojis = {
            "Low Confidence": "📉",
            "Cooldown": "🧊",
            "Coin Cooldown": "🧊",
            "Daily Guard": "🛡️",
            "Daily Target Reached": "🎯",
            "Daily Loss Limit": "🔴",
            "Loss Cooldown": "⏸️",
            "TP/SL Protection Failed": "🔥",
            "Subscription": "🔒",
            "Insufficient Balance": "💰",
            "Existing Position": "📌",
            "Risk Limit": "⚖️",
            "Exchange Rejected": "❌",
        }
        emoji = category_emojis.get(category, "⏭️")
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        strategy_line = f"\nStrategy: <b>{strategy_type}</b>" if strategy_type else ""

        msg = (
            f"{emoji} <b>SKIPPED — {category}</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{strategy_line}\n\n"
            f"Reason: <i>{reason[:200]}</i>"
        )
        await self.send(msg)

    async def send_watchlisted(
        self,
        symbol: str,
        side: str,
        setup_type: str,
        confidence: int,
        trigger_price: float,
        current_price: float,
        reason: str = "",
    ):
        """V7: Notify when a new swing setup is added to the watchlist."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        msg = (
            f"🔭 <b>WATCHLISTED — Swing Setup</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Type: <b>{setup_type}</b>\n"
            f"Confidence: <b>{confidence}%</b>\n"
            f"Current: <b>${current_price:,.6f}</b>\n"
            f"Trigger: <b>${trigger_price:,.6f}</b>\n\n"
            f"<i>{reason[:150]}</i>\n"
            f"<i>Will execute when trigger price is hit + confidence ≥ "
            f"{confidence}%</i>"
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

    # ═══════════════════════════════════════════════════════════════════
    # V9 Position Manager — Trade Close Notifications
    # ═══════════════════════════════════════════════════════════════════

    async def trade_closed_tp(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        close_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        strategy_type: str = "",
        confidence: int = 0,
        tp_price: float = 0.0,
        duration_minutes: int = 0,
    ):
        """
        V9: Position Manager — Take Profit hit notification.
        Sent when position_manager.py closes a trade at TP.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        pnl_emoji = "📈" if pnl_usdt >= 0 else "📉"

        strategy_display = ""
        if strategy_type:
            if strategy_type.startswith("swing"):
                strategy_display = "🌊 Swing"
            elif strategy_type.startswith("sniper"):
                strategy_display = "🎯 Sniper"
            else:
                strategy_display = "⚡ Scalp"

        type_line = f"\nType: <b>{strategy_display}</b>" if strategy_display else ""
        conf_line = f"\nConfidence: <b>{confidence}%</b>" if confidence > 0 else ""
        tp_line = f"\nTP Level: <b>${tp_price:,.6f}</b>" if tp_price > 0 else ""
        dur_line = f"\nDuration: <b>{duration_minutes}m</b>" if duration_minutes > 0 else ""
        pnl_sign = "+" if pnl_usdt >= 0 else ""

        msg = (
            f"✅ <b>TP HIT</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>"
            f"{type_line}"
            f"{conf_line}\n\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"Close: <b>${close_price:,.6f}</b>"
            f"{tp_line}"
            f"{dur_line}\n\n"
            f"P&L: <b>{pnl_emoji} {pnl_sign}${pnl_usdt:,.4f} ({pnl_sign}{pnl_pct:.2f}%)</b>"
        )
        await self.send(msg)

    async def trade_closed_sl(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        close_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        strategy_type: str = "",
        confidence: int = 0,
        sl_price: float = 0.0,
        duration_minutes: int = 0,
    ):
        """
        V9: Position Manager — Stop Loss hit notification.
        Sent when position_manager.py closes a trade at SL.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        strategy_display = ""
        if strategy_type:
            if strategy_type.startswith("swing"):
                strategy_display = "🌊 Swing"
            elif strategy_type.startswith("sniper"):
                strategy_display = "🎯 Sniper"
            else:
                strategy_display = "⚡ Scalp"

        type_line = f"\nType: <b>{strategy_display}</b>" if strategy_display else ""
        conf_line = f"\nConfidence: <b>{confidence}%</b>" if confidence > 0 else ""
        sl_line = f"\nSL Level: <b>${sl_price:,.6f}</b>" if sl_price > 0 else ""
        dur_line = f"\nDuration: <b>{duration_minutes}m</b>" if duration_minutes > 0 else ""
        pnl_sign = "+" if pnl_usdt >= 0 else ""

        msg = (
            f"🛑 <b>SL HIT</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>"
            f"{type_line}"
            f"{conf_line}\n\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"Close: <b>${close_price:,.6f}</b>"
            f"{sl_line}"
            f"{dur_line}\n\n"
            f"P&L: <b>📉 {pnl_sign}${pnl_usdt:,.4f} ({pnl_sign}{pnl_pct:.2f}%)</b>"
        )
        await self.send(msg)

    async def trade_closed_trailing(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        close_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        peak_price: float = 0.0,
        strategy_type: str = "",
        duration_minutes: int = 0,
    ):
        """
        V9: Position Manager — Trailing stop exit notification.
        """
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        pnl_emoji = "📈" if pnl_usdt >= 0 else "📉"
        pnl_sign = "+" if pnl_usdt >= 0 else ""

        strategy_display = ""
        if strategy_type:
            if strategy_type.startswith("swing"):
                strategy_display = "🌊 Swing"
            elif strategy_type.startswith("sniper"):
                strategy_display = "🎯 Sniper"
            else:
                strategy_display = "⚡ Scalp"

        type_line = f"\nType: <b>{strategy_display}</b>" if strategy_display else ""
        peak_line = f"\nPeak: <b>${peak_price:,.6f}</b>" if peak_price > 0 else ""
        dur_line = f"\nDuration: <b>{duration_minutes}m</b>" if duration_minutes > 0 else ""

        msg = (
            f"📈 <b>TRAILING EXIT</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>"
            f"{type_line}\n\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"Close: <b>${close_price:,.6f}</b>"
            f"{peak_line}"
            f"{dur_line}\n\n"
            f"P&L: <b>{pnl_emoji} {pnl_sign}${pnl_usdt:,.4f} ({pnl_sign}{pnl_pct:.2f}%)</b>\n\n"
            f"<i>Position trailed to profit — locked in gains</i>"
        )
        await self.send(msg)

    async def position_manager_started(self, version: str = "V9"):
        """V9: Notify that Position Manager has started/restarted."""
        msg = (
            f"🤖 <b>POSITION MANAGER STARTED</b>\n\n"
            f"Version: <b>{version}</b>\n"
            f"Status: <b>✅ Online — monitoring all open positions</b>\n\n"
            f"<i>Will auto-close trades on TP/SL/trailing trigger.</i>"
        )
        await self.send(msg)

    async def position_manager_error(self, error: str, context: str = ""):
        """V9: Alert when Position Manager encounters a critical error."""
        ctx_line = f"\nContext: <code>{context[:150]}</code>" if context else ""
        msg = (
            f"🔥 <b>POSITION MANAGER ERROR</b>\n\n"
            f"Error: <code>{error[:300]}</code>"
            f"{ctx_line}\n\n"
            f"⚠️ <b>Positions may not be monitored — check VPS!</b>"
        )
        await self.send(msg)

    async def close_failed_manual(self, symbol: str, side: str, reason: str, error: str):
        """V9: Alert when Position Manager cannot close a position — needs manual action."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        msg = (
            f"🔥 <b>CLOSE FAILED — MANUAL ACTION REQUIRED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Trigger: <b>{reason}</b>\n\n"
            f"Error: <code>{error[:200]}</code>\n\n"
            f"⚠️ <b>Close this position manually on Binance immediately!</b>"
        )
        await self.send(msg)


    # =================================================================
    # V11: Grouped Watchlist Messages (replaces per-coin spam)
    # =================================================================

    async def send_scalp_watchlist(self, setups: list) -> bool:
        """
        V11: Send ONE grouped SCALP near-miss message instead of per-coin spam.
        Format:
            🔥 SCALP WATCHLIST
            1. BTCUSDT 🟢 LONG 63%
            2. SOLUSDT 🔴 SHORT 60%
            Total: 2 | Execute at 65%+
        """
        if not setups:
            return False
        lines = ["🔥 <b>SCALP WATCHLIST</b>", ""]
        for i, s in enumerate(setups[:10], 1):
            sym = s.get("symbol", "?")
            side_icon = "🟢 LONG" if s.get("action", "") == "BUY" else "🔴 SHORT"
            conf = s.get("confidence", 0)
            strat = (s.get("strategy_type", "") or "").replace("scalp_", "").replace("_", " ")
            lines.append(
                f"{i}. <b>{sym}</b> {side_icon} <b>{conf}%</b>"
                + (f" — {strat}" if strat else "")
            )
        lines += ["", f"Total: <b>{len(setups)}</b> near-miss setup(s)"]
        lines += ["<i>Will execute at 65%+ confidence on next scan.</i>"]
        return await self.send("\n".join(lines))

    async def send_swing_watchlist(self, setups: list) -> bool:
        """
        V11: Send ONE grouped SWING watchlist message instead of per-coin spam.
        Format:
            🌊 SWING WATCHLIST
            1. ETHUSDT 🟢 LONG 78% — ema20 pullback
            2. LINKUSDT 🔴 SHORT 75% — breakout retest
            Total: 2 | Execute at 80%+
        """
        if not setups:
            return False
        lines = ["🌊 <b>SWING WATCHLIST</b>", ""]
        for i, s in enumerate(setups[:15], 1):
            sym = s.get("symbol", "?")
            raw_side = s.get("action") or s.get("side", "BUY")
            side_icon = "🟢 LONG" if raw_side == "BUY" else "🔴 SHORT"
            conf = s.get("confidence", 0)
            setup = (s.get("setup_type") or s.get("strategy_type") or "").replace("swing_", "").replace("_", " ")
            trigger = s.get("trigger_price", 0)
            t_str = f" | trigger ${trigger:,.4f}" if trigger > 0 else ""
            lines.append(
                f"{i}. <b>{sym}</b> {side_icon} <b>{conf}%</b>"
                + (f" — {setup}" if setup else "")
                + t_str
            )
        lines += ["", f"Total: <b>{len(setups)}</b> swing setup(s)"]
        lines += ["<i>Will execute when confidence >= 80% and trigger is hit.</i>"]
        return await self.send("\n".join(lines))

    async def send_stale_trade_alert(
        self,
        symbol: str,
        side: str,
        strategy_type: str,
        entry_price: float,
        current_price: float,
        open_hours: float,
        stale_threshold_hours: int,
        will_force_close: bool = False,
    ) -> bool:
        """V11: Alert when a position has been open too long without hitting TP/SL."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        action_line = (
            "⚠️ <b>Force-closing stale position now.</b>"
            if will_force_close
            else "⚠️ <b>Review manually — position has not moved to TP/SL.</b>"
        )
        msg = (
            f"⏰ <b>STALE TRADE DETECTED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Type: <b>{strategy_type}</b>\n\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"Current: <b>${current_price:,.6f}</b>\n"
            f"Open: <b>{open_hours:.1f}h</b> (limit: {stale_threshold_hours}h)\n\n"
            f"{action_line}"
        )
        return await self.send(msg)

    async def send_orphan_position_alert(
        self,
        symbol: str,
        side: str,
        db_status: str,
        binance_status: str,
        position_id: int,
    ) -> bool:
        """V11: Alert when DB open_positions row does not match Binance live state."""
        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        msg = (
            f"🔍 <b>ORPHAN POSITION DETECTED</b>\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Position ID: <code>{position_id}</code>\n\n"
            f"DB Status: <b>{db_status}</b>\n"
            f"Binance: <b>{binance_status}</b>\n\n"
            f"⚠️ <b>DB and Binance are out of sync.</b>\n"
            f"<i>Position Manager will attempt to reconcile automatically.</i>"
        )
        return await self.send(msg)
