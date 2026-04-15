"""
Telegram Notification Module — Scalping Format
"""

import logging
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
                    },
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")
            return False

    async def scalp_trade(
        self,
        symbol: str,
        action: str,
        confidence: int,
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        leverage: int,
        reason: str,
    ):
        msg = (
            f"🚀 <b>SCALP TRADE</b>\n\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Action: <b>{action}</b>\n"
            f"Confidence: <b>{confidence}%</b>\n\n"
            f"Entry: <b>${entry_price:,.6f}</b>\n"
            f"TP: <b>${take_profit:,.6f}</b>\n"
            f"SL: <b>${stop_loss:,.6f}</b>\n"
            f"Leverage: <b>{leverage}x</b>\n\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    async def trade_skipped(self, symbol: str, reason: str):
        msg = (
            f"⏭️ <b>TRADE SKIPPED</b>\n"
            f"Symbol: {symbol}\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    async def trading_paused(self, reason: str):
        msg = (
            f"⛔ <b>TRADING PAUSED</b>\n\n"
            f"Reason: <i>{reason}</i>"
        )
        await self.send(msg)

    async def error_alert(self, context: str, error: str):
        msg = (
            f"⚠️ <b>ERROR</b>\n"
            f"Context: {context}\n"
            f"Error: <code>{error[:300]}</code>"
        )
        await self.send(msg)
