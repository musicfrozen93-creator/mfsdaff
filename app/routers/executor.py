"""
Executor API endpoint — Scalping mode
Integrates dynamic risk management, daily limits, duplicate protection.
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.modules.risk_engine import RiskEngine, TradeParameters
from app.modules.executor import BinanceExecutor
from app.modules.telegram import TelegramNotifier
from app.utils.state import state_manager
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class ExecuteRequest(BaseModel):
    # From scanner
    symbol: str
    spread_pct: float = 0.0
    volume_24h: float = 0.0
    # From scalping analyzer
    action: str          # BUY | SELL | HOLD
    confidence: int
    reason: str
    current_price: float
    atr: float = 0.0
    atr_pct: float = 0.0


@router.post("/execute")
async def execute_trade(req: ExecuteRequest):
    """
    Scalping execution:
    1. Check daily risk limits
    2. Check duplicate trades
    3. Apply dynamic risk engine
    4. Execute trade
    5. Send Telegram notification
    """
    telegram = TelegramNotifier()

    # ── Daily risk control ───────────────────────────────────────────
    try:
        binance = BinanceExecutor()
        balance = await binance.get_account_balance()
    except Exception:
        balance = settings.ACCOUNT_BALANCE

    daily_check = state_manager.check_daily_limits(balance)
    if not daily_check["allowed"]:
        await telegram.trading_paused(daily_check["reason"])
        return {"status": "trading_paused", "reason": daily_check["reason"]}

    # ── HOLD check ───────────────────────────────────────────────────
    if req.action == "HOLD":
        return {"status": "skipped", "reason": f"HOLD — {req.reason}"}

    # ── Confidence check ─────────────────────────────────────────────
    if req.confidence < settings.MIN_CONFIDENCE:
        msg = f"Confidence {req.confidence} < minimum {settings.MIN_CONFIDENCE}"
        return {"status": "skipped", "reason": msg}

    # ── Duplicate trade protection ───────────────────────────────────
    if state_manager.is_duplicate_trade(req.symbol):
        msg = f"Duplicate: {req.symbol} was in last 2 trades — skipping"
        await telegram.trade_skipped(req.symbol, msg)
        return {"status": "skipped", "reason": msg}

    # ── Open position check ──────────────────────────────────────────
    try:
        has_position = await binance.has_open_position(req.symbol)
        if has_position:
            msg = f"Already have open position on {req.symbol}"
            await telegram.trade_skipped(req.symbol, msg)
            return {"status": "skipped", "reason": msg}
    except Exception as e:
        logger.warning(f"Position check failed: {e}")

    # ── Dynamic risk engine ──────────────────────────────────────────
    side = "BUY" if req.action == "BUY" else "SELL"

    risk_engine = RiskEngine()
    trade_params = risk_engine.calculate(
        symbol=req.symbol,
        side=side,
        confidence=req.confidence,
        entry_price=req.current_price,
        atr_pct=req.atr_pct,
        account_balance=balance,
    )

    if not trade_params.approved:
        await telegram.trade_skipped(req.symbol, trade_params.reject_reason)
        return {"status": "skipped", "reason": trade_params.reject_reason}

    # ── Execute ──────────────────────────────────────────────────────
    result = await binance.execute_trade(trade_params)

    if result.success:
        state_manager.open_trade(req.symbol)

        await telegram.scalp_trade(
            symbol=req.symbol,
            action=side,
            confidence=req.confidence,
            entry_price=req.current_price,
            take_profit=trade_params.take_profit,
            stop_loss=trade_params.stop_loss,
            leverage=trade_params.leverage,
            reason=req.reason,
        )

        return {
            "status": "executed",
            "order_id": result.order_id,
            "symbol": req.symbol,
            "side": side,
            "quantity": trade_params.quantity,
            "entry_price": req.current_price,
            "stop_loss": trade_params.stop_loss,
            "take_profit": trade_params.take_profit,
            "leverage": trade_params.leverage,
            "risk_reward": trade_params.risk_reward,
            "confidence": req.confidence,
        }
    else:
        await telegram.error_alert("Trade Execution", result.error or "Unknown error")
        raise HTTPException(status_code=500, detail=result.error)
