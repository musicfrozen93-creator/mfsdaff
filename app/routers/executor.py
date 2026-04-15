"""
Executor API endpoint
Supports two modes:
  1. SIMPLE MODE: { symbol, action, usdt_amount }
     → Backend fetches price, calculates quantity, places order
  2. AI PIPELINE MODE: { symbol, action, confidence, reason, current_price, ... }
     → Full safety checks + risk engine + SL/TP
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


# ─── Simple Execute Request ──────────────────────────────────────────
# n8n sends ONLY this: { symbol, action, usdt_amount }
# Backend handles EVERYTHING else.

class SimpleExecuteRequest(BaseModel):
    symbol: str
    action: str                     # BUY | SELL
    usdt_amount: float = 5.0        # Amount in USDT to trade


# ─── Full AI Pipeline Request (kept for backward compat) ────────────

class FullExecuteRequest(BaseModel):
    symbol: str
    action: str                     # BUY | SELL | HOLD
    confidence: int
    reason: str
    current_price: float
    spread_pct: float = 0.0
    volume_24h: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
# SIMPLE EXECUTE — n8n sends { symbol, action, usdt_amount }
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute")
async def execute_trade(req: SimpleExecuteRequest):
    """
    Simple trade execution.
    n8n sends: { "symbol": "SOLUSDT", "action": "BUY", "usdt_amount": 5 }
    Backend handles:
      1. Validate input
      2. Check daily limits + cooldowns
      3. Fetch market price
      4. Convert USDT → quantity with correct precision
      5. Validate notional & min qty
      6. Place MARKET order
      7. Return clean result
    """
    telegram = TelegramNotifier()
    binance = BinanceExecutor()

    # ── Input validation ─────────────────────────────────────────────
    symbol = req.symbol.upper().strip()
    action = req.action.upper().strip()

    if action not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action '{req.action}'. Must be BUY or SELL."}

    if req.usdt_amount <= 0:
        return {"status": "error", "message": f"usdt_amount must be > 0, got {req.usdt_amount}"}

    if not symbol.endswith("USDT"):
        return {"status": "error", "message": f"Invalid symbol '{symbol}'. Must end with USDT."}

    # ── Daily risk control ───────────────────────────────────────────
    try:
        balance = await binance.get_account_balance()
    except Exception:
        balance = settings.ACCOUNT_BALANCE

    daily_check = state_manager.check_daily_limits(balance)
    if not daily_check["allowed"]:
        await telegram.trading_paused(daily_check["reason"])
        return {"status": "error", "message": f"Trading paused: {daily_check['reason']}"}

    # ── Hourly rate limit ────────────────────────────────────────────
    hourly_limited, hourly_count = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        msg = f"Hourly limit reached: {hourly_count}/{settings.HOURLY_MAX_TRADES} trades this hour"
        return {"status": "error", "message": msg}

    # ── Per-coin cooldown ────────────────────────────────────────────
    on_cooldown, remaining_secs = state_manager.is_coin_on_cooldown(symbol)
    if on_cooldown:
        remaining_min = remaining_secs // 60
        msg = f"Cooldown: {symbol} was traded recently — {remaining_min}m remaining"
        return {"status": "error", "message": msg}

    # ── Open position check ──────────────────────────────────────────
    try:
        has_position = await binance.has_open_position(symbol)
        if has_position:
            msg = f"Already have open position on {symbol}"
            return {"status": "error", "message": msg}
    except Exception as e:
        logger.warning(f"Position check failed: {e}")

    # ── Execute the trade ────────────────────────────────────────────
    try:
        result = await binance.execute_simple(
            symbol=symbol,
            side=action,
            usdt_amount=req.usdt_amount,
        )

        # Record in state
        state_manager.open_trade(symbol)

        # Send Telegram notification
        await telegram.send(
            f"✅ <b>TRADE EXECUTED</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Side: <b>{action}</b>\n"
            f"Amount: <b>${req.usdt_amount}</b>\n"
            f"Price: <b>${result['price']:,.6f}</b>\n"
            f"Quantity: <b>{result['quantity']}</b>\n"
            f"Order ID: <b>{result['order_id']}</b>"
        )

        return {
            "status": "success",
            "symbol": symbol,
            "side": action,
            "quantity": result["quantity"],
            "price": result["price"],
            "notional": result["notional"],
            "order_id": result["order_id"],
        }

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Trade execution failed: {error_msg}")
        await telegram.error_alert("Trade Execution", error_msg)
        return {"status": "error", "message": error_msg}


# ═══════════════════════════════════════════════════════════════════════
# FULL AI PIPELINE EXECUTE — with risk engine, SL/TP, confidence checks
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-full")
async def execute_trade_full(req: FullExecuteRequest):
    """
    Full AI pipeline execution with risk engine, SL/TP.
    Kept for the automated AI analysis pipeline.
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

    # ── Hourly rate limit (max 3 trades/hour) ────────────────────────
    hourly_limited, hourly_count = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        msg = f"Hourly limit reached: {hourly_count}/{settings.HOURLY_MAX_TRADES} trades this hour"
        logger.info(f"  ⏰ {msg}")
        return {"status": "skipped", "reason": msg}

    # ── Per-coin cooldown (1 hour) ───────────────────────────────────
    on_cooldown, remaining_secs = state_manager.is_coin_on_cooldown(req.symbol)
    if on_cooldown:
        remaining_min = remaining_secs // 60
        msg = f"Cooldown: {req.symbol} was traded recently — {remaining_min}m remaining"
        logger.info(f"  🕐 {msg}")
        return {"status": "skipped", "reason": msg}

    # ── Volatility skip (ATR% too high) ──────────────────────────────
    if req.atr_pct > settings.MAX_VOLATILITY_PCT:
        msg = f"Volatility too high: ATR%={req.atr_pct:.2f}% > max {settings.MAX_VOLATILITY_PCT}%"
        logger.info(f"  🌊 {msg}")
        await telegram.trade_skipped(req.symbol, msg)
        return {"status": "skipped", "reason": msg}

    # ── Duplicate trade protection ───────────────────────────────────
    if state_manager.is_duplicate_trade(req.symbol):
        msg = f"Duplicate: {req.symbol} was in last 2 trades — skipping"
        await telegram.trade_skipped(req.symbol, msg)
        return {"status": "skipped", "reason": msg}

    # ── Open position check ──────────────────────────────────────────
    try:
        binance = BinanceExecutor()
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
