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
        _tok = bool(self.token)
        _cid = bool(self.chat_id)
        if not _tok or not _cid:
            logger.warning(
                "[TELEGRAM CONFIG] MISSING credentials: token_set=%s chat_id_set=%s"
                " -- set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars",
                _tok, _cid
            )
        else:
            logger.debug("[TELEGRAM CONFIG] OK token=%s... chat_id=%s", self.token[:8], self.chat_id)

    async def send(self, message: str) -> bool:

        if not self.token or not self.chat_id:
            logger.error(
                "[TELEGRAM SEND FAILED] token_set=%s chat_id_set=%s"
                " -- TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars not set",
                bool(self.token), bool(self.chat_id)
            )
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
            logger.error("[TELEGRAM SEND FAILED] HTTP error: %s", e)
            return False

    async def send_simple(self, message: str) -> bool:

        """

        V17: Send a plain message with Markdown parse mode.

        Used by daily_report, stale signal detection, and simple alerts.

        Falls back to HTML send() if Markdown fails.

        """

        if not self.token or not self.chat_id:

            logger.warning("Telegram not configured — skipping send_simple")

            return False

        try:

            async with httpx.AsyncClient(timeout=10) as client:

                resp = await client.post(

                    f"{self.base_url}/sendMessage",

                    json={

                        "chat_id": self.chat_id,

                        "text": message,

                        "parse_mode": "Markdown",

                        "disable_web_page_preview": True,

                    },

                )

                resp.raise_for_status()

                return True

        except Exception:

            # Fallback: try without parse mode

            try:

                async with httpx.AsyncClient(timeout=10) as client:

                    resp = await client.post(

                        f"{self.base_url}/sendMessage",

                        json={

                            "chat_id": self.chat_id,

                            "text": message,

                            "disable_web_page_preview": True,

                        },

                    )

                    resp.raise_for_status()

                    return True

            except Exception as e2:

                logger.error(f"Telegram send_simple failed: {e2}")

                return False

    async def send_message(self, message: str) -> bool:

        """Alias for send_simple — for backward compatibility."""

        return await self.send_simple(message)

    # ═══════════════════════════════════════════════════════════════════

    # V4: ONE CLEAN FINAL MESSAGE — Trade Executed

    # ═══════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════

    # V13 UNIFIED TRADE OPENED BUILDER — used by ALL execution paths

    # ═══════════════════════════════════════════════════════════════════

    @staticmethod

    def _build_trade_opened_message(

        symbol: str,

        side: str,

        confidence: int,

        entry_price: float,

        fill_price: float,

        leverage: int,

        take_profit: float,

        stop_loss: float,

        tp_roi_pct: float = 0.0,

        sl_roi_pct: float = 0.0,

        tp_pct: float = 0.0,

        sl_pct: float = 0.0,

        risk_reward: float = 0.0,

        setup_grade: str = "",

        strategy_type: str = "",

        regime: str = "",

        reason: str = "",

        order_method: str = "MARKET",

        executed_count: int = 1,

        skipped_count: int = 0,

        skip_reasons: dict = None,

        protection_mode: str = "external_engine",

        sl_attached: bool = True,

        tp_attached: bool = True,

        sl_order_id: str = "",

        tp_order_id: str = "",

        partial_tp_enabled: bool = False,

        tp1_price: float = 0.0,

        tp2_price: float = 0.0,

    ) -> str:

        """

        V13 Unified trade opened message builder.

        ALL execution paths (execute, execute-full, execute-multi) use this.

        No margin / balance tier fields shown.

        """

        direction = "\U0001f7e2 LONG" if side == "BUY" else "\U0001f534 SHORT"

        # Mode-specific title

        if strategy_type.startswith("swing"):

            title = "\U0001f30a <b>SWING TRADE OPENED \u2014 V13</b>"

        elif strategy_type.startswith("sniper"):

            title = "\U0001f3af <b>SNIPER TRADE OPENED \u2014 V13</b>"

        else:

            title = "\U0001f680 <b>SCALP TRADE OPENED \u2014 V13</b>"

        # Grade line

        grade_emoji = {"A": "\u2b50", "B": "\U0001f537", "C": "\U0001f538"}.get(setup_grade, "")

        grade_line = f"\nGrade: <b>{grade_emoji} {setup_grade}</b>" if setup_grade else ""

        # Strategy type line

        if strategy_type.startswith("swing"):

            strat_display = "\U0001f30a Swing"

        elif strategy_type.startswith("sniper"):

            strat_display = "\U0001f3af Sniper"

        else:

            strat_display = "\u26a1 Scalping"

        strategy_line = f"\nType: <b>{strat_display}</b>"

        # Regime line

        regime_display = ""

        if regime:

            regime_map = {

                "TRENDING_BULL": "\U0001f7e2 Trending Bull",

                "TRENDING_BEAR": "\U0001f534 Trending Bear",

                "SIDEWAYS_RANGE": "\u2194\ufe0f Sideways",

                "BREAKOUT_EXPANSION": "\U0001f4a5 Breakout",

                "HIGH_VOLATILITY": "\u26a0\ufe0f High Volatility",

                "DEAD_MARKET": "\U0001f4a4 Dead Market",

            }

            regime_display = regime_map.get(regime, regime)

        regime_line = f"\nRegime: <b>{regime_display}</b>" if regime_display else ""

        # Accounts block (only for multi-account)

        if executed_count > 1 or skipped_count > 0:

            accounts_block = (

                f"Executed Accounts: <b>{executed_count}</b>\n"

                f"Skipped Accounts: <b>{skipped_count}</b>\n\n"

            )

        else:

            accounts_block = ""

        # Entry price — prefer fill price, never show zero

        display_price = fill_price if fill_price > 0 else entry_price

        entry_str = f"${display_price:,.6f}" if display_price > 0 else "pending..."

        leverage_str = f"{leverage}x" if leverage > 0 else "auto"

        # TP/SL ROI line

        if tp_roi_pct > 0 and sl_roi_pct > 0:

            roi_line = f"\nTP ROI: <b>+{tp_roi_pct:.0f}%</b> | SL ROI: <b>-{sl_roi_pct:.0f}%</b>"

        else:

            roi_line = ""

        # TP/SL price block

        if partial_tp_enabled and tp1_price > 0:

            tp_block = (

                f"TP Mode: <b>\U0001f4ca Partial (40/30/30)</b>\n"

                f"TP1: <b>${tp1_price:,.6f}</b> (close 40%)\n"

                f"TP2: <b>${tp2_price:,.6f}</b> (close 30%)\n"

                f"Trail: <b>30%</b> with BE stop\n"

                f"SL Price: <b>${stop_loss:,.6f}</b>"

            )

        elif take_profit > 0 and stop_loss > 0:

            tp_pct_display = f" (+{tp_pct:.2f}%)" if tp_pct > 0 else ""

            sl_pct_display = f" (-{sl_pct:.2f}%)" if sl_pct > 0 else ""

            tp_block = (

                f"TP Price: <b>${take_profit:,.6f}</b>{tp_pct_display}\n"

                f"SL Price: <b>${stop_loss:,.6f}</b>{sl_pct_display}"

            )

        else:

            tp_block = "TP/SL: <i>set by Protection Engine</i>"

        # R:R line

        rr_line = f"\n\nR:R = <b>1:{risk_reward:.1f}</b>" if risk_reward > 0 else ""

        # Protection line

        if protection_mode == "external_engine":

            protection_line = "\U0001f6e1\ufe0f <b>External Engine Active</b>"

        elif sl_attached and tp_attached:

            protection_line = "\u2705 TP/SL attached successfully"

        elif sl_attached:

            protection_line = "\u26a0\ufe0f SL attached, TP FAILED \u2014 check manually"

        elif tp_attached:

            protection_line = "\u26a0\ufe0f TP attached, SL FAILED \u2014 check manually"

        else:

            protection_line = "\U0001f6a8 BOTH TP/SL FAILED \u2014 manual action required!"

        # Order ID proof

        proof_line = ""

        if protection_mode != "external_engine" and (sl_order_id or tp_order_id):

            parts = []

            if sl_order_id:

                parts.append(f"SL=#{sl_order_id}")

            if tp_order_id:

                parts.append(f"TP=#{tp_order_id}")

            proof_line = f"\nOrders: <code>{' | '.join(parts)}</code>"

        # Reason block

        reason_block = ""

        if reason:

            reason_block = f"\n\n<b>Reason:</b>\n<i>{reason[:300]}</i>"

        # Skip reasons block (multi-account only)

        skip_block = ""

        if skip_reasons:

            skip_lines_list = [f"  \u2022 {cnt} {cat.lower()}" for cat, cnt in skip_reasons.items()]

            skip_block = "\n<b>Skipped:</b>\n" + "\n".join(skip_lines_list)

        # V18: TP1 = midpoint, TP2 = full target
        if side == "BUY":
            tp1_price = entry_price * (1 + (tp_pct * 0.5) / 100)
            tp2_price = tp_price
        else:
            tp1_price = entry_price * (1 - (tp_pct * 0.5) / 100)
            tp2_price = tp_price

        tp1_line = f"🎯 TP1: <b>${tp1_price:,.6f}</b>"
        tp2_line = f"🎯 TP2: <b>${tp2_price:,.6f}</b> ({tp_sign}{tp_pct:.1f}%)"
        status_line = "🟡 <b>WAITING FOR ENTRY</b>"
        strat_display = (strategy_type or "momentum_continuation").replace("_", " ").title()

        msg = (
            f"{title_line}\n\n"
            f"💱 Pair: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"\n"
            f"📍 Entry Zone:\n"
            f"  <b>${min(entry_zone_low, entry_zone_high):,.6f}</b>"
            f" – <b>${max(entry_zone_low, entry_zone_high):,.6f}</b>\n"
            f"\n"
            f"🛡 SL: <b>${sl_price:,.6f}</b> ({sl_sign}{sl_pct:.1f}%)\n"
            f"{tp1_line}\n"
            f"{tp2_line}\n"
            f"\n"
            f"Confidence: <b>{confidence}%</b>\n"
            f"Strategy: <b>{strat_display}</b>"
            f"{regime_line}"
            f"{btc_line}"
            f"{rr_line}"
            f"\n\n"
            f"Status: {status_line}"
            f"{reason_block}"
        )
        try:
            return await self.send(msg)
        except Exception as e:
            logger.error("[TELEGRAM SEND FAILED] send_signal_alert: %s", e)
            return False

    async def send_execution_followup(

        self,

        symbol: str,

        side: str,

        executed_count: int,

        skipped_count: int = 0,

        fill_price: float = 0.0,

        entry_price: float = 0.0,

        leverage: int = 0,

        take_profit: float = 0.0,

        stop_loss: float = 0.0,

        strategy_type: str = "",

    ):

        """

        V14 Phase 2: Short follow-up sent ONLY when at least one account fills.

        Keeps signal and execution as two separate messages.

        """

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        display_price = fill_price if fill_price > 0 else entry_price

        price_str = f"${display_price:,.6f}" if display_price > 0 else "—"

        lev_str = f"{leverage}x" if leverage > 0 else "—"

        tp_line = f"\nTP: <b>${take_profit:,.6f}</b>" if take_profit > 0 else ""

        sl_line = f"\nSL: <b>${stop_loss:,.6f}</b>" if stop_loss > 0 else ""

        if strategy_type.startswith("swing"):

            note = "<i>🌊 Swing PM is now monitoring this position.</i>"

        elif strategy_type.startswith("sniper"):

            note = "<i>🎯 Sniper PM is monitoring this position.</i>"

        else:

            note = "<i>⚡ Scalp PM is monitoring this position.</i>"

        skip_note = f"\nSkipped Accounts: <b>{skipped_count}</b>" if skipped_count > 0 else ""

        msg = (

            f"✅ <b>ACCOUNT ENTRY EXECUTED</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Side: <b>{direction}</b>\n"

            f"Accounts Filled: <b>{executed_count}</b>"

            f"{skip_note}\n"

            f"Entry: <b>{price_str}</b>\n"

            f"Leverage: <b>{lev_str}</b>"

            f"{tp_line}"

            f"{sl_line}\n\n"

            f"{note}"

        )

        try:

            await self.send(msg)

        except Exception as e:

            logger.error(f"[V14] send_execution_followup failed: {e}")

    async def send_no_execution_signal(

        self,

        symbol: str,

        side: str,

        confidence: int,

        skip_reasons: dict,

        strategy_type: str = "",

    ):

        """

        V14: Update the original signal message to show that no account filled.

        Called after the account loop finishes with zero executions, so signal

        subscribers know the setup was valid but accounts could not trade.

        """

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        skip_summary = ", ".join(

            f"{cnt} {cat.lower()}" for cat, cnt in skip_reasons.items()

        ) if skip_reasons else "account-side conditions"

        if strategy_type.startswith("swing"):

            type_str = "Swing"

        elif strategy_type.startswith("sniper"):

            type_str = "Sniper"

        else:

            type_str = "Scalp"

        msg = (

            f"📡 <b>SIGNAL UPDATE — No Account Execution</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Side: <b>{direction}</b>\n"

            f"Confidence: <b>{confidence}%</b>\n"

            f"Type: <b>{type_str}</b>\n\n"

            f"Execution Status: <b>No account trade opened</b>\n"

            f"Reason: <i>{skip_summary}</i>\n\n"

            f"<i>Signal was valid. Account-side conditions prevented entry.\n"

            f"Signal followers may enter manually.</i>"

        )

        try:

            await self.send(msg)

        except Exception as e:

            logger.error(f"[V14] send_no_execution_signal failed: {e}")

    # ═══════════════════════════════════════════════════════════════════

    # V15: Pullback Monitor Outcome Notifications

    # ═══════════════════════════════════════════════════════════════════

    async def send_signal_timeout(

        self,

        symbol: str,

        side: str,

        confidence: int,

        strategy_type: str = "",

        signal_price: float = 0.0,

        timeout_minutes: int = 15,

    ):

        """

        V15: Fired when a pending signal expires without a pullback entry.

        Informs followers that the setup was valid but entry window closed.

        """

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        if strategy_type.startswith("swing"):

            type_str = "🌊 Swing"

        elif strategy_type.startswith("sniper"):

            type_str = "🎯 Sniper"

        else:

            type_str = "⚡ Scalp"

        timeout_str = f"{timeout_minutes}m" if timeout_minutes < 60 else f"{timeout_minutes // 60}h"

        price_line = f"\nSignal Price: <b>${signal_price:,.6f}</b>" if signal_price > 0 else ""

        msg = (

            f"⏱️ <b>SIGNAL EXPIRED — No Entry</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Side: <b>{direction}</b>\n"

            f"Type: <b>{type_str}</b>\n"

            f"Confidence: <b>{confidence}%</b>"

            f"{price_line}\n\n"

            f"Timeout: <b>{timeout_str}</b> elapsed without pullback\n"

            f"Execution Status: <b>Cancelled — entry window closed</b>\n\n"

            f"<i>No trade was opened. The setup may re-appear on next scan.</i>"

        )

        try:

            await self.send(msg)

        except Exception as e:

            logger.error(f"[V15] send_signal_timeout failed: {e}")

    async def send_signal_cancelled(

        self,

        symbol: str,

        side: str,

        confidence: int,

        strategy_type: str = "",

        signal_price: float = 0.0,

        current_price: float = 0.0,

        cancel_reason: str = "anti_chase",

    ):

        """

        V15: Fired when anti-chase rule triggers — price ran away from signal zone.

        LONG: price rose >2% above signal → skip to avoid chasing.

        SHORT: price fell >2% below signal → skip.

        """

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        if strategy_type.startswith("swing"):

            type_str = "🌊 Swing"

        else:

            type_str = "⚡ Scalp"

        price_line = ""

        if signal_price > 0 and current_price > 0:

            move_pct = abs(current_price - signal_price) / signal_price * 100

            price_line = (

                f"\nSignal Price: <b>${signal_price:,.6f}</b>\n"

                f"Current Price: <b>${current_price:,.6f}</b>\n"

                f"Price Move: <b>{move_pct:.2f}%</b>"

            )

        reason_display = {

            "anti_chase": "Price ran away from entry zone (+2% anti-chase rule)",

            "price_too_high": "LONG: price rose above signal zone — not pulling back",

            "price_too_low": "SHORT: price fell below signal zone — not bouncing",

        }.get(cancel_reason, cancel_reason)

        msg = (

            f"🚫 <b>SIGNAL CANCELLED — Anti-Chase</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Side: <b>{direction}</b>\n"

            f"Type: <b>{type_str}</b>\n"

            f"Confidence: <b>{confidence}%</b>"

            f"{price_line}\n\n"

            f"Reason: <i>{reason_display}</i>\n"

            f"Execution Status: <b>Cancelled — will not chase</b>\n\n"

            f"<i>Protecting against bad entry. Waiting for next valid setup.</i>"

        )

        try:

            await self.send(msg)

        except Exception as e:

            logger.error(f"[V15] send_signal_cancelled failed: {e}")

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
        """V18: DEPRECATED — watchlist messages replaced by direct signals.
        This method is a no-op stub kept for backward compatibility."""
        logger.info(
            "[V18 DEPRECATED] send_swing_watchlist called with %d setups"
            " — SUPPRESSED (V18 direct-signal mode active)",
            len(setups)
        )
        return False

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

    # ═══════════════════════════════════════════════════════════════════

    # V16: Signal Engine — Pure Signal Messages (no execution)

    # ═══════════════════════════════════════════════════════════════════

    async def send_signal_alert(

        self,

        signal_number: int,

        symbol: str,

        side: str,

        confidence: int,

        entry_price: float,

        entry_zone_low: float,

        entry_zone_high: float,

        tp_price: float,

        sl_price: float,

        tp_pct: float,

        sl_pct: float,

        strategy_type: str = "",

        setup_grade: str = "",

        regime: str = "",

        btc_bias: str = "NEUTRAL",

        reason: str = "",

        atr_pct: float = 0.0,

        risk_reward: float = 0.0,

    ) -> bool:

        """

        V18: Primary signal alert.

        Sends SCALP (SCP-NNN) or SWING (SWG-NNN) labelled signal to Telegram.

        Differentiates title: "SCALP SIGNAL" vs "SWING SIGNAL".

        """

        is_swing  = strategy_type.startswith("swing")

        is_sniper = strategy_type.startswith("sniper")

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        # V18: Human-readable signal ID

        prefix   = "SWG" if is_swing else "SCP"

        id_label = f"{prefix}-{signal_number:03d}"

        # V18: Title per signal type

        if is_swing:

            title_line = f"🌊 <b>SWING SIGNAL {id_label}</b>"

        elif is_sniper:

            title_line = f"🎯 <b>SNIPER SIGNAL {id_label}</b>"

        else:

            title_line = f"⚡ <b>SCALP SIGNAL {id_label}</b>"

        # Strategy label

        if is_swing:

            strat_emoji = "🌊 Swing"

        elif is_sniper:

            strat_emoji = "🎯 Sniper"

        else:

            strat_emoji = "⚡ Scalp"

        # Grade

        grade_emoji = {"A": "⭐", "B": "🔷", "C": "🔸"}.get(setup_grade, "")

        grade_line  = f"\nGrade: <b>{grade_emoji} {setup_grade}</b>" if setup_grade else ""

        # Regime

        regime_map = {

            "TRENDING_BULL":      "🟢 Trending Bull",

            "TRENDING_BEAR":      "🔴 Trending Bear",

            "SIDEWAYS_RANGE":     "↔️ Sideways",

            "BREAKOUT_EXPANSION": "💥 Breakout",

            "HIGH_VOLATILITY":    "⚠️ High Volatility",

            "DEAD_MARKET":        "💤 Dead Market",

        }

        regime_line = f"\nRegime: <b>{regime_map.get(regime, regime)}</b>" if regime else ""

        # Entry zone

        if entry_zone_low > 0 and entry_zone_high > 0:

            zone_str = f"<b>${min(entry_zone_low, entry_zone_high):,.6f} – ${max(entry_zone_low, entry_zone_high):,.6f}</b>"

        else:

            zone_str = f"<b>${entry_price:,.6f}</b>"

        # BTC bias

        bias_map = {

            "BULLISH": "🟢 Bullish ✓" if side == "BUY" else "🟢 Bullish ⚠️",

            "BEARISH": "🔴 Bearish ✓" if side == "SELL" else "🔴 Bearish ⚠️",

            "NEUTRAL": "⬜ Neutral",

        }

        btc_line = f"\nBTC Bias: <b>{bias_map.get(btc_bias, btc_bias)}</b>"

        # TP/SL

        tp_sign = "+" if side == "BUY" else "-"

        sl_sign = "-" if side == "BUY" else "+"

        tp_line = f"🎯 TP: <b>${tp_price:,.6f}</b> ({tp_sign}{tp_pct:.1f}%)"

        sl_line = f"🛡 SL: <b>${sl_price:,.6f}</b> ({sl_sign}{sl_pct:.1f}%)"

        # R:R

        rr_line = f"\nR:R = <b>1:{risk_reward:.1f}</b>" if risk_reward > 0 else ""

        # ATR

        atr_line = f"\nATR: <b>{atr_pct:.2f}%</b>" if atr_pct > 0 else ""

        # Reason

        reason_block = f"\n\n<b>AI Reason:</b>\n<i>{reason[:280]}</i>" if reason else ""

        msg = (

            f"{title_line}\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Direction: <b>{direction}</b>\n"

            f"Type: <b>{strat_emoji}</b>"

            f"{grade_line}"

            f"{regime_line}"

            f"{btc_line}"

            f"\nConfidence: <b>{confidence}%</b>\n\n"

            f"Entry Zone: {zone_str}\n"

            f"{tp_line}\n"

            f"{sl_line}"

            f"{rr_line}"

            f"{atr_line}"

            f"{reason_block}"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V18] send_signal_alert failed: {e}")

            return False

    async def send_signal_entry_triggered(

        self,

        signal_number: int,

        symbol: str,

        side: str,

        entry_price: float,

        tp_price: float,

        sl_price: float,

    ) -> bool:

        """V16: Fired when price enters the entry zone for a pending signal."""

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        num_str   = f"#{signal_number:03d}"

        msg = (

            f"✅ <b>ENTRY TRIGGERED — Signal {num_str}</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Direction: <b>{direction}</b>\n"

            f"Entry @ <b>${entry_price:,.6f}</b>\n\n"

            f"TP: <b>${tp_price:,.6f}</b>\n"

            f"SL: <b>${sl_price:,.6f}</b>\n\n"

            f"<i>Virtual trade opened — monitoring TP/SL…</i>"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_signal_entry_triggered failed: {e}")

            return False

    async def send_signal_status_update(

        self,

        signal_number: int,

        symbol: str,

        side: str,

        status: str,

        current_price: float,

        entry_price: float,

        drawdown_pct: float,

    ) -> bool:

        """V16: Drawdown warning update for an active signal."""

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        num_str   = f"#{signal_number:03d}"

        warn_level = "⚠️" if abs(drawdown_pct) < 15 else "🚨"

        dd_str = f"{drawdown_pct:.1f}%"

        msg = (

            f"{warn_level} <b>Signal {num_str} UPDATE</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Direction: <b>{direction}</b>\n"

            f"Status: <b>In Drawdown ({dd_str})</b>\n\n"

            f"Current: <b>${current_price:,.6f}</b>\n"

            f"Entry:   <b>${entry_price:,.6f}</b>\n\n"

            f"<i>Signal still active — SL not hit yet.</i>"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_signal_status_update failed: {e}")

            return False

    async def send_signal_result(

        self,

        signal_number: int,

        symbol: str,

        side: str,

        result: str,

        entry_price: float,

        close_price: float,

        tp_pct: float,

        sl_pct: float,

        duration_minutes: int = 0,

    ) -> bool:

        """V16: Final outcome message for a closed signal (TP or SL)."""

        direction = "🟢 LONG" if side == "BUY" else "🔴 SHORT"

        num_str   = f"#{signal_number:03d}"

        if result == "TP":

            emoji   = "✅"

            verdict = "TP HIT"

            move_pct = tp_pct

        else:

            emoji   = "❌"

            verdict = "SL HIT"

            move_pct = -sl_pct

        pct_str = f"{move_pct:+.1f}%"

        dur_str = ""

        if duration_minutes > 0:

            h, m = divmod(duration_minutes, 60)

            dur_str = f"\nDuration: <b>{h}h {m}m</b>" if h > 0 else f"\nDuration: <b>{m}m</b>"

        msg = (

            f"{emoji} <b>{verdict} — Signal {num_str}</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Direction: <b>{direction}</b>\n\n"

            f"Entry: <b>${entry_price:,.6f}</b>\n"

            f"Close: <b>${close_price:,.6f}</b>"

            f"{dur_str}\n\n"

            f"Result: <b>{pct_str}</b>"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_signal_result failed: {e}")

            return False

    async def send_reversal_warning(

        self,

        invalidated_number: int,

        symbol: str,

        old_side: str,

    ) -> bool:

        """V16: Notify when an existing signal is invalidated by an opposite signal."""

        old_dir = "🟢 LONG" if old_side == "BUY" else "🔴 SHORT"

        new_dir = "🔴 SHORT" if old_side == "BUY" else "🟢 LONG"

        num_str = f"#{invalidated_number:03d}"

        msg = (

            f"⚠️ <b>REVERSAL — Signal {num_str} INVALIDATED</b>\n\n"

            f"Coin: <b>{symbol}</b>\n"

            f"Was: <b>{old_dir}</b>\n"

            f"New Direction: <b>{new_dir}</b>\n\n"

            f"<i>Opposite signal detected — previous setup cancelled.</i>"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_reversal_warning failed: {e}")

            return False

    async def send_quiet_market(self, active_signals: int = 0) -> bool:

        """

        V16: Sent at most once every 15 minutes when no high-quality signals found.

        Suppresses per-cycle spam. Shows count of still-active signals.

        """

        active_line = (

            f"\n<i>⏳ {active_signals} signal(s) still being monitored.</i>"

            if active_signals > 0 else ""

        )

        msg = (

            f"💤 <b>Market Quiet</b>\n\n"

            f"No high-quality signals detected this cycle.\n"

            f"Scanning continues…{active_line}"

        )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_quiet_market failed: {e}")

            return False

    async def send_daily_report(

        self,

        date_str: str,

        total_signals: int,

        tp_count: int,

        sl_count: int,

        invalid_count: int,

        pending_count: int,

        win_rate: float,

        signal_log: list[dict],

    ) -> bool:

        """

        V16: Daily performance report — sent at midnight or on demand.

        signal_log: [{"number": 1, "symbol": "ETHUSDT", "side": "BUY", "result": "TP"}, ...]

        """

        if total_signals == 0:

            msg = (

                f"📊 <b>DAILY REPORT — {date_str}</b>\n\n"

                f"No signals generated today."

            )

        else:

            # Result lines (max 20 to avoid message overflow)

            result_emoji = {"TP": "✅", "SL": "❌", "INVALID": "⚠️", "CANCELLED": "⬜", "PENDING": "⏳"}

            log_lines = []

            for s in signal_log[:20]:

                side_ch = "L" if s.get("side") == "BUY" else "S"

                emoji   = result_emoji.get(s.get("result", "PENDING"), "⬜")

                log_lines.append(

                    f"  {emoji} #{s['number']:03d} {s['symbol']} {side_ch} → {s.get('result','?')}"

                )

            if len(signal_log) > 20:

                log_lines.append(f"  … and {len(signal_log) - 20} more")

            wr_str = f"{win_rate:.1f}%" if tp_count + sl_count > 0 else "—"

            msg = (

                f"📊 <b>DAILY REPORT — {date_str}</b>\n\n"

                f"Total Signals: <b>{total_signals}</b>\n"

                f"✅ TP Hit:       <b>{tp_count}</b>\n"

                f"❌ SL Hit:       <b>{sl_count}</b>\n"

                f"⚠️ Invalidated: <b>{invalid_count}</b>\n"

                f"⏳ Still Open:  <b>{pending_count}</b>\n"

                f"Win Rate: <b>{wr_str}</b>\n\n"

                f"<b>Signal Log:</b>\n" +

                "\n".join(log_lines)

            )

        try:

            return await self.send(msg)

        except Exception as e:

            logger.error(f"[V16] send_daily_report failed: {e}")

            return False
