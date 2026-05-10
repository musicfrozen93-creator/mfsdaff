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
    # SIGNAL-ONLY ENGINE — AI Signal Delivery (no execution)
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _build_signal_message(
        symbol: str,
        side: str,
        confidence: int,
        entry_price: float,
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
        # V15: Entry zone fields
        entry_zone_low: float = 0.0,
        entry_zone_high: float = 0.0,
        ideal_entry: float = 0.0,
        invalidation: float = 0.0,
        quality_tier: str = "",
        entry_quality_score: int = -1,
        zone_sources: list = None,
        # V16: Multi-TP fields
        multi_tp: dict = None,
    ) -> str:
        """
        V16 Professional signal message builder.
        Multi-TP format with entry zones, ROI/RR/leverage/quality tiers.
        """
        from datetime import datetime, timezone

        direction = "\U0001f7e2 LONG" if side == "BUY" else "\U0001f534 SHORT"

        # Quality tier with emoji
        tier = quality_tier or ""
        tier_emoji_map = {
            "ELITE": "\U0001f451",     # 👑
            "STRONG": "\U0001f537",    # 🔷
            "NORMAL": "\U0001f538",    # 🔸
            "WEAK": "\u26aa",          # ⚪
        }
        tier_emoji = tier_emoji_map.get(tier, "")
        tier_display = f" [{tier_emoji} {tier}]" if tier else ""

        # Mode-specific title
        if strategy_type.startswith("swing"):
            title = f"\U0001f30a <b>SWING SIGNAL \u2014 AI Engine V16</b>{tier_display}"
        elif strategy_type.startswith("sniper"):
            title = f"\U0001f3af <b>SNIPER SIGNAL \u2014 AI Engine V16</b>{tier_display}"
        else:
            title = f"\U0001f680 <b>SCALP SIGNAL \u2014 AI Engine V16</b>{tier_display}"

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

        # ── V16: ENTRY ZONE BLOCK with distance from market ──────────
        if entry_zone_low > 0 and entry_zone_high > 0:
            dist_str = ""
            if ideal_entry > 0 and entry_price > 0:
                dist_pct = abs(entry_price - ideal_entry) / entry_price * 100
                direction_word = "below" if side == "BUY" else "above"
                dist_str = f"  <i>({dist_pct:.1f}% {direction_word} market)</i>"

            ideal_str = f"\n\U0001f3af Ideal Entry: <b>${ideal_entry:,.6f}</b>" if ideal_entry > 0 else ""

            inv_direction = "below" if side == "BUY" else "above"
            invalidation_str = f"\n\u274c Invalidation: <b>{inv_direction} ${invalidation:,.6f}</b>" if invalidation > 0 else ""

            entry_block = (
                f"\U0001f4cd <b>Entry Zone:</b>\n"
                f"<b>${entry_zone_low:,.6f} \u2013 ${entry_zone_high:,.6f}</b>{dist_str}"
                f"{ideal_str}"
                f"{invalidation_str}"
            )
        elif entry_price > 0:
            entry_block = f"Entry: <b>${entry_price:,.6f}</b>"
        else:
            entry_block = "Entry: <b>market</b>"

        leverage_str = f"{leverage}x" if leverage > 0 else "auto"

        # ── V16: MULTI-TP BLOCK ──────────────────────────────────────
        if multi_tp and multi_tp.get("enabled"):
            tp1_p = multi_tp.get("tp1_price", 0)
            tp2_p = multi_tp.get("tp2_price", 0)
            tp3_p = multi_tp.get("tp3_price", 0)
            tp1_r = multi_tp.get("tp1_roi_pct", 0)
            tp2_r = multi_tp.get("tp2_roi_pct", 0)
            tp3_r = multi_tp.get("tp3_roi_pct", 0)
            sl_r = multi_tp.get("sl_roi_pct", 0)
            sl_p = multi_tp.get("stop_loss", stop_loss)
            tp1_c = multi_tp.get("tp1_close_pct", 40)
            tp2_c = multi_tp.get("tp2_close_pct", 30)
            tp3_c = multi_tp.get("tp3_close_pct", 30)

            tp_block = (
                f"\U0001f3af TP1: <b>${tp1_p:,.6f}</b> (+{tp1_r:.1f}%) \u2014 close {tp1_c}%\n"
                f"\U0001f3af TP2: <b>${tp2_p:,.6f}</b> (+{tp2_r:.1f}%) \u2014 close {tp2_c}%\n"
                f"\U0001f3af TP3: <b>${tp3_p:,.6f}</b> (+{tp3_r:.1f}%) \u2014 trail {tp3_c}%\n\n"
                f"\U0001f6d1 SL: <b>${sl_p:,.6f}</b> ({sl_r:.1f}%)"
            )
        elif take_profit > 0 and stop_loss > 0:
            tp_pct_display = f" (+{tp_pct:.2f}%)" if tp_pct > 0 else ""
            sl_pct_display = f" (-{sl_pct:.2f}%)" if sl_pct > 0 else ""
            tp_block = (
                f"TP Price: <b>${take_profit:,.6f}</b>{tp_pct_display}\n"
                f"SL Price: <b>${stop_loss:,.6f}</b>{sl_pct_display}"
            )
        else:
            tp_block = "TP/SL: <i>calculate based on your risk</i>"

        # TP/SL ROI line (only if no multi-TP)
        roi_line = ""
        if not (multi_tp and multi_tp.get("enabled")):
            if tp_roi_pct > 0 and sl_roi_pct > 0:
                roi_line = f"\nTP ROI: <b>+{tp_roi_pct:.0f}%</b> | SL ROI: <b>-{sl_roi_pct:.0f}%</b>"

        # R:R line
        rr_line = f"\n\nR:R = <b>1:{risk_reward:.1f}</b>" if risk_reward > 0 else ""

        # V15: Entry quality score line
        eq_line = ""
        if entry_quality_score >= 0:
            eq_line = f"\nEntry Quality: <b>{entry_quality_score}/100</b>"

        # Zone sources
        sources_line = ""
        if zone_sources:
            sources_line = f"\n\U0001f4ca <i>Zone: {', '.join(zone_sources[:3])}</i>"

        # Protection line
        protection_line = "\U0001f6e1\ufe0f <b>External Engine Active</b>"

        # Reason block
        reason_block = ""
        if reason:
            reason_block = f"\n\n<b>Analysis:</b>\n<i>{reason[:300]}</i>"

        # Timestamp
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

        msg = (
            f"{title}\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{grade_line}"
            f"{strategy_line}"
            f"{regime_line}\n\n"
            f"{entry_block}\n"
            f"Leverage: <b>{leverage_str}</b>"
            f"{eq_line}"
            f"{roi_line}\n\n"
            f"{tp_block}"
            f"{rr_line}"
            f"{sources_line}\n\n"
            f"<b>Protection:</b>\n{protection_line}"
            f"{reason_block}\n\n"
            f"<i>Signal from AI analysis \u2014 no execution dependency</i>\n"
            f"<i>\U0001f552 {ts}</i>"
        )
        return msg

    async def send_signal(
        self,
        symbol: str,
        side: str,
        confidence: int,
        entry_price: float,
        leverage: int,
        take_profit: float,
        stop_loss: float,
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        tp_roi_pct: float = 0.0,
        sl_roi_pct: float = 0.0,
        reason: str = "",
        setup_grade: str = "",
        strategy_type: str = "",
        regime: str = "",
        risk_reward: float = 0.0,
        # V15: Entry zone fields
        entry_zone_low: float = 0.0,
        entry_zone_high: float = 0.0,
        ideal_entry: float = 0.0,
        invalidation: float = 0.0,
        quality_tier: str = "",
        entry_quality_score: int = -1,
        zone_sources: list = None,
        # V16: Multi-TP
        multi_tp: dict = None,
    ):
        """V16: Signal-only delivery — send AI signal with entry zones + multi-TP to Telegram."""
        msg = self._build_signal_message(
            symbol=symbol, side=side, confidence=confidence,
            entry_price=entry_price, leverage=leverage,
            take_profit=take_profit, stop_loss=stop_loss,
            tp_roi_pct=tp_roi_pct, sl_roi_pct=sl_roi_pct,
            tp_pct=tp_pct, sl_pct=sl_pct, risk_reward=risk_reward,
            setup_grade=setup_grade, strategy_type=strategy_type,
            regime=regime, reason=reason,
            entry_zone_low=entry_zone_low, entry_zone_high=entry_zone_high,
            ideal_entry=ideal_entry, invalidation=invalidation,
            quality_tier=quality_tier, entry_quality_score=entry_quality_score,
            zone_sources=zone_sources,
            multi_tp=multi_tp,
        )
        await self.send(msg)

    # ═══════════════════════════════════════════════════════════════════
    # LEGACY: Execution-based messages (kept for backward compat)
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

        msg = (
            f"{title}\n\n"
            f"Coin: <b>{symbol}</b>\n"
            f"Side: <b>{direction}</b>\n"
            f"Confidence: <b>{confidence}%</b>"
            f"{grade_line}"
            f"{strategy_line}"
            f"{regime_line}\n\n"
            f"{accounts_block}"
            f"Entry: <b>{entry_str}</b>\n"
            f"Leverage: <b>{leverage_str}</b>\n"
            f"Method: <b>{order_method}</b>"
            f"{roi_line}\n\n"
            f"{tp_block}"
            f"{rr_line}\n\n"
            f"<b>Protection:</b>\n{protection_line}"
            f"{proof_line}"
            f"{reason_block}"
            f"{skip_block}"
        )
        return msg

    async def send_execution_result(
        self,
        symbol: str,
        side: str,
        confidence: int,
        executed_count: int,
        skipped_count: int,
        skip_reasons: dict,
        entry_price: float,
        fill_price: float,
        leverage: int,
        take_profit: float,
        stop_loss: float,
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        tp_roi_pct: float = 0.0,
        sl_roi_pct: float = 0.0,
        margin_pct: float = 0.0,        # kept for API compat, NOT shown
        margin_usdt: float = 0.0,       # kept for API compat, NOT shown
        account_balance: float = 0.0,   # kept for API compat, NOT shown
        reason: str = "",
        setup_grade: str = "",
        order_method: str = "MARKET",
        strategy_type: str = "",
        regime: str = "",
        sl_attached: bool = True,
        tp_attached: bool = True,
        sl_order_id: str = "",
        tp_order_id: str = "",
        risk_reward: float = 0.0,
        partial_tp_enabled: bool = False,
        tp1_price: float = 0.0,
        tp2_price: float = 0.0,
        protection_mode: str = "",
    ):
        """V13: Multi-account execution message. Uses unified builder."""
        msg = self._build_trade_opened_message(
            symbol=symbol, side=side, confidence=confidence,
            entry_price=entry_price, fill_price=fill_price,
            leverage=leverage, take_profit=take_profit, stop_loss=stop_loss,
            tp_roi_pct=tp_roi_pct, sl_roi_pct=sl_roi_pct,
            tp_pct=tp_pct, sl_pct=sl_pct, risk_reward=risk_reward,
            setup_grade=setup_grade, strategy_type=strategy_type,
            regime=regime, reason=reason, order_method=order_method,
            executed_count=executed_count, skipped_count=skipped_count,
            skip_reasons=skip_reasons or {},
            protection_mode=protection_mode,
            sl_attached=sl_attached, tp_attached=tp_attached,
            sl_order_id=sl_order_id, tp_order_id=tp_order_id,
            partial_tp_enabled=partial_tp_enabled,
            tp1_price=tp1_price, tp2_price=tp2_price,
        )
        await self.send(msg)

    async def trade_opened(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        leverage: int,
        position_size: float = 0.0,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        confidence: int = 0,
        tp_pct: float = 0.0,
        sl_pct: float = 0.0,
        setup_grade: str = "",
        daily_pnl_pct: float = 0.0,
        account_label: str = "",       # API compat, never shown
        tp_roi_pct: float = 0.0,
        sl_roi_pct: float = 0.0,
        risk_reward: float = 0.0,
        strategy_type: str = "",
        regime: str = "",
        reason: str = "",
        order_method: str = "MARKET",
        fill_price: float = 0.0,
    ):
        """
        V13: Single-account trade notification.
        Old simple template REMOVED — always uses unified full-detail builder.
        Called by /execute and /execute-full routes.
        """
        msg = self._build_trade_opened_message(
            symbol=symbol, side=side, confidence=confidence,
            entry_price=entry_price, fill_price=fill_price,
            leverage=leverage, take_profit=take_profit, stop_loss=stop_loss,
            tp_roi_pct=tp_roi_pct, sl_roi_pct=sl_roi_pct,
            tp_pct=tp_pct, sl_pct=sl_pct, risk_reward=risk_reward,
            setup_grade=setup_grade, strategy_type=strategy_type,
            regime=regime, reason=reason, order_method=order_method,
            executed_count=1, skipped_count=0, skip_reasons={},
            protection_mode="external_engine",
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
