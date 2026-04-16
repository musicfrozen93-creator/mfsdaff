"""
V2 Executor API — Multi-Account Trade Execution

Endpoints:
  POST /execute       — Single-account backward-compatible execution
  POST /execute-full  — Single-account with risk engine + SL/TP
  POST /execute-multi — Multi-account execution for all connected accounts
"""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.risk_engine import RiskEngine
from app.modules.executor import BinanceExecutor
from app.modules.telegram import TelegramNotifier
from app.modules.crypto_utils import decrypt_api_key
from app.utils.state import state_manager
from app.config import settings
from app.database import get_db, async_session
from app.models.user import Account, ApiConnection, Balance
from app.models.trading import Signal, Trade, TradeSkip

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── Request Models ──────────────────────────────────────────────────

class SimpleExecuteRequest(BaseModel):
    symbol: str
    action: str
    usdt_amount: float = 5.0


class FullExecuteRequest(BaseModel):
    symbol: str
    action: str
    confidence: int
    reason: str = ""
    current_price: float = 0.0
    spread_pct: float = 0.0
    volume_24h: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0


class MultiExecuteRequest(BaseModel):
    symbol: str
    action: str  # BUY | SELL
    confidence: int
    reason: str = ""
    current_price: float = 0.0
    atr_pct: float = 0.0
    spread_pct: float = 0.0
    indicators: dict = {}


# ═══════════════════════════════════════════════════════════════════════
# SIMPLE EXECUTE — backward compatible
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute")
async def execute_trade(req: SimpleExecuteRequest):
    """Simple trade execution — backward compatible with v1 n8n workflow."""
    telegram = TelegramNotifier()
    binance = BinanceExecutor()

    symbol = req.symbol.upper().strip()
    action = req.action.upper().strip()

    if action not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action '{req.action}'"}
    if req.usdt_amount <= 0:
        return {"status": "error", "message": f"usdt_amount must be > 0"}
    if not symbol.endswith("USDT"):
        return {"status": "error", "message": f"Invalid symbol '{symbol}'"}

    # Daily risk control
    try:
        balance = await binance.get_account_balance()
    except Exception:
        balance = 20.0

    daily_check = state_manager.check_daily_limits(balance)
    if not daily_check["allowed"]:
        await telegram.trading_paused(daily_check["reason"])
        return {"status": "error", "message": f"Trading paused: {daily_check['reason']}"}

    # Hourly rate limit
    hourly_limited, hourly_count = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        return {"status": "error", "message": f"Hourly limit: {hourly_count}/{settings.HOURLY_MAX_TRADES}"}

    # Cooldown
    on_cooldown, remaining_secs = state_manager.is_coin_on_cooldown(symbol)
    if on_cooldown:
        return {"status": "error", "message": f"Cooldown: {symbol} — {remaining_secs // 60}m remaining"}

    # Loss cooldown
    loss_cd, loss_remaining = state_manager.is_loss_cooldown_active()
    if loss_cd:
        return {"status": "error", "message": f"Loss cooldown active — {loss_remaining}s remaining"}

    # Position check
    try:
        if await binance.has_open_position(symbol):
            return {"status": "error", "message": f"Open position exists on {symbol}"}
    except Exception as e:
        logger.warning(f"Position check failed: {e}")

    # Execute
    try:
        result = await binance.execute_simple(symbol=symbol, side=action, usdt_amount=req.usdt_amount)
        state_manager.record_trade_opened(symbol)

        await telegram.trade_opened(
            symbol=symbol, side=action, entry_price=result["price"],
            leverage=1, position_size=req.usdt_amount,
            take_profit=0, stop_loss=0, confidence=0,
        )

        return {
            "status": "success", "symbol": symbol, "side": action,
            "quantity": result["quantity"], "price": result["price"],
            "notional": result["notional"], "order_id": result["order_id"],
        }
    except Exception as e:
        logger.error(f"❌ Trade failed: {e}")
        await telegram.error_alert("Trade Execution", str(e))
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# FULL AI PIPELINE EXECUTE — single account with risk engine
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-full")
async def execute_trade_full(req: FullExecuteRequest):
    """Full single-account execution with V2 risk engine + SL/TP."""
    telegram = TelegramNotifier()
    binance = BinanceExecutor()

    # Daily limits
    try:
        balance = await binance.get_account_balance()
    except Exception:
        balance = 20.0

    daily_check = state_manager.check_daily_limits(balance)
    if not daily_check["allowed"]:
        await telegram.trading_paused(daily_check["reason"])
        return {"status": "trading_paused", "reason": daily_check["reason"]}

    if req.action == "HOLD":
        return {"status": "skipped", "reason": f"HOLD — {req.reason}"}

    if req.confidence < settings.MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Confidence {req.confidence} < {settings.MIN_CONFIDENCE}"}

    # Rate limits
    hourly_limited, hourly_count = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        return {"status": "skipped", "reason": f"Hourly limit: {hourly_count}/{settings.HOURLY_MAX_TRADES}"}

    on_cooldown, remaining = state_manager.is_coin_on_cooldown(req.symbol)
    if on_cooldown:
        return {"status": "skipped", "reason": f"Cooldown: {req.symbol} — {remaining // 60}m"}

    loss_cd, loss_remaining = state_manager.is_loss_cooldown_active()
    if loss_cd:
        return {"status": "skipped", "reason": f"Loss cooldown — {loss_remaining}s remaining"}

    if req.atr_pct > settings.MAX_VOLATILITY_PCT:
        return {"status": "skipped", "reason": f"ATR%={req.atr_pct:.2f}% > max {settings.MAX_VOLATILITY_PCT}%"}

    try:
        if await binance.has_open_position(req.symbol):
            return {"status": "skipped", "reason": f"Open position on {req.symbol}"}
    except Exception:
        pass

    # Risk engine
    side = "BUY" if req.action == "BUY" else "SELL"
    precision = await binance.get_precision(req.symbol)

    risk_engine = RiskEngine()
    entry_price = req.current_price if req.current_price > 0 else await binance.get_market_price(req.symbol)

    trade_params = risk_engine.calculate(
        symbol=req.symbol, side=side, confidence=req.confidence,
        entry_price=entry_price, atr_pct=req.atr_pct,
        account_balance=balance,
        min_notional=precision.min_notional, min_qty=precision.min_qty,
        step_size=precision.step_size,
        quantity_precision=precision.quantity_precision,
        price_precision=precision.price_precision,
    )

    if not trade_params.approved:
        await telegram.trade_skipped(req.symbol, trade_params.reject_reason)
        return {"status": "skipped", "reason": trade_params.reject_reason}

    # Execute
    result = await binance.execute_trade(trade_params)

    if result.success:
        state_manager.record_trade_opened(req.symbol)

        await telegram.trade_opened(
            symbol=req.symbol, side=side, entry_price=entry_price,
            leverage=trade_params.leverage, position_size=trade_params.position_size_usdt,
            take_profit=trade_params.take_profit, stop_loss=trade_params.stop_loss,
            confidence=req.confidence,
        )

        return {
            "status": "executed", "order_id": result.order_id,
            "symbol": req.symbol, "side": side,
            "quantity": trade_params.quantity, "entry_price": entry_price,
            "stop_loss": trade_params.stop_loss, "take_profit": trade_params.take_profit,
            "leverage": trade_params.leverage, "risk_reward": trade_params.risk_reward,
            "confidence": req.confidence, "position_size": trade_params.position_size_usdt,
        }
    else:
        await telegram.error_alert("Trade Execution", result.error or "Unknown error")
        raise HTTPException(status_code=500, detail=result.error)


# ═══════════════════════════════════════════════════════════════════════
# MULTI-ACCOUNT EXECUTE — processes signal across all connected accounts
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-multi")
async def execute_multi_account(req: MultiExecuteRequest):
    """
    Multi-account execution flow:
    1. Save signal to DB
    2. Load all active accounts
    3. For each: validate → calculate risk → execute → log
    4. Send Telegram summary
    """
    telegram = TelegramNotifier()

    symbol = req.symbol.upper().strip()
    side = req.action.upper().strip()

    if side not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action: {req.action}"}
    if req.confidence < settings.MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Confidence {req.confidence} < {settings.MIN_CONFIDENCE}"}

    # Global rate limits
    hourly_limited, _ = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        return {"status": "skipped", "reason": "Hourly trade limit reached"}

    daily_check = state_manager.check_daily_limits(0)
    if not daily_check["allowed"]:
        return {"status": "trading_paused", "reason": daily_check["reason"]}

    loss_cd, _ = state_manager.is_loss_cooldown_active()
    if loss_cd:
        return {"status": "skipped", "reason": "Loss cooldown active"}

    on_cooldown, _ = state_manager.is_coin_on_cooldown(symbol)
    if on_cooldown:
        return {"status": "skipped", "reason": f"Cooldown active for {symbol}"}

    # ── Save signal to DB ────────────────────────────────────────────
    signal_id = None
    try:
        async with async_session() as session:
            signal = Signal(
                symbol=symbol, side=side, confidence=req.confidence,
                reason=req.reason, indicators_json=req.indicators,
            )
            session.add(signal)
            await session.commit()
            await session.refresh(signal)
            signal_id = signal.id
            logger.info(f"📝 Signal #{signal_id} saved: {symbol} {side} conf={req.confidence}")
    except Exception as e:
        logger.warning(f"Failed to save signal to DB: {e}")

    # ── Load active accounts ─────────────────────────────────────────
    accounts_data = []
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account)
                .where(Account.is_active == True)
            )
            accounts = result.scalars().all()

            for acc in accounts:
                if acc.api_connection and acc.api_connection.is_active:
                    accounts_data.append({
                        "id": acc.id,
                        "label": acc.label,
                        "api_key_enc": acc.api_connection.api_key_encrypted,
                        "api_secret_enc": acc.api_connection.api_secret_encrypted,
                    })
    except Exception as e:
        logger.warning(f"Failed to load accounts from DB: {e}")

    # Fallback to master account if no DB accounts
    if not accounts_data:
        if settings.BINANCE_API_KEY:
            accounts_data = [{
                "id": 0,
                "label": "Master",
                "api_key_enc": None,
                "api_secret_enc": None,
            }]
        else:
            return {"status": "error", "message": "No active accounts found"}

    # ── Execute for each account ─────────────────────────────────────
    risk_engine = RiskEngine()
    executed = []
    skipped = []
    skip_reasons_map = {}

    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent account executions

    async def execute_for_account(acc_data: dict):
        async with semaphore:
            acc_id = acc_data["id"]
            label = acc_data["label"]

            try:
                # Create executor with account's credentials
                if acc_data["api_key_enc"]:
                    api_key = decrypt_api_key(acc_data["api_key_enc"])
                    api_secret = decrypt_api_key(acc_data["api_secret_enc"])
                    executor = BinanceExecutor(api_key=api_key, secret_key=api_secret)
                else:
                    executor = BinanceExecutor()

                # Get balance
                balance = await executor.get_account_balance()

                if balance < 5:
                    return _skip(acc_id, label, "Low Balance", f"Balance ${balance:.2f} too low")

                # Check open position
                try:
                    if await executor.has_open_position(symbol):
                        return _skip(acc_id, label, "Existing Position", f"Already has {symbol} position")
                except Exception:
                    pass

                # Get entry price
                entry_price = req.current_price if req.current_price > 0 else await executor.get_market_price(symbol)

                # Get precision
                precision = await executor.get_precision(symbol)

                # Calculate risk
                trade_params = risk_engine.calculate(
                    symbol=symbol, side=side, confidence=req.confidence,
                    entry_price=entry_price, atr_pct=req.atr_pct,
                    account_balance=balance,
                    min_notional=precision.min_notional, min_qty=precision.min_qty,
                    step_size=precision.step_size,
                    quantity_precision=precision.quantity_precision,
                    price_precision=precision.price_precision,
                )

                if not trade_params.approved:
                    category = "Min Notional" if "notional" in trade_params.reject_reason.lower() else "Risk Limit"
                    return _skip(acc_id, label, category, trade_params.reject_reason)

                # Execute trade
                result = await executor.execute_trade(trade_params)

                if result.success:
                    # Save trade to DB
                    try:
                        async with async_session() as session:
                            trade = Trade(
                                signal_id=signal_id, account_id=acc_id,
                                symbol=symbol, side=side,
                                entry_price=entry_price, quantity=trade_params.quantity,
                                position_size_usdt=trade_params.position_size_usdt,
                                leverage=trade_params.leverage,
                                take_profit=trade_params.take_profit,
                                stop_loss=trade_params.stop_loss,
                                risk_pct=trade_params.risk_pct,
                                confidence=req.confidence,
                                order_id=str(result.order_id),
                                sl_order_id=str(result.stop_loss_order_id) if result.stop_loss_order_id else None,
                                tp_order_id=str(result.take_profit_order_id) if result.take_profit_order_id else None,
                                status="open",
                            )
                            session.add(trade)
                            await session.commit()
                    except Exception as e:
                        logger.warning(f"Failed to save trade to DB: {e}")

                    return {
                        "status": "executed",
                        "account_id": acc_id,
                        "label": label,
                        "balance": balance,
                        "leverage": trade_params.leverage,
                        "position_size": trade_params.position_size_usdt,
                        "quantity": trade_params.quantity,
                        "tp": trade_params.take_profit,
                        "sl": trade_params.stop_loss,
                    }
                else:
                    return _skip(acc_id, label, "Execution Error", result.error or "Unknown")

            except Exception as e:
                logger.error(f"Account {label} execution failed: {e}")
                return _skip(acc_id, label, "Error", str(e)[:100])

    def _skip(acc_id, label, category, reason):
        """Helper to build a skip result."""
        # Save skip to DB
        try:
            # We'll batch save skips after all accounts complete
            pass
        except Exception:
            pass
        return {
            "status": "skipped",
            "account_id": acc_id,
            "label": label,
            "category": category,
            "reason": reason,
        }

    # Execute all accounts
    tasks = [execute_for_account(acc) for acc in accounts_data]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            skipped.append({"status": "skipped", "category": "Error", "reason": str(r)})
            continue
        if r.get("status") == "executed":
            executed.append(r)
        else:
            skipped.append(r)
            category = r.get("category", "Unknown")
            skip_reasons_map[category] = skip_reasons_map.get(category, 0) + 1

    # Record in state manager
    if executed:
        state_manager.record_trade_opened(symbol)

    for s in skipped:
        state_manager.record_skip()

    # Save skips to DB
    try:
        async with async_session() as session:
            for s in skipped:
                skip_record = TradeSkip(
                    signal_id=signal_id,
                    account_id=s.get("account_id"),
                    symbol=symbol,
                    reason=s.get("reason", "Unknown"),
                    category=s.get("category", "Unknown"),
                )
                session.add(skip_record)
            await session.commit()
    except Exception as e:
        logger.warning(f"Failed to save skips to DB: {e}")

    # Send Telegram summary
    await telegram.signal_summary(
        symbol=symbol, side=side, confidence=req.confidence,
        executed_count=len(executed), skipped_count=len(skipped),
        skip_reasons=skip_reasons_map, total_accounts=len(accounts_data),
    )

    logger.info(
        f"📊 Multi-account result: {len(executed)} executed, {len(skipped)} skipped "
        f"out of {len(accounts_data)} accounts"
    )

    return {
        "status": "ok",
        "symbol": symbol,
        "side": side,
        "confidence": req.confidence,
        "total_accounts": len(accounts_data),
        "executed_count": len(executed),
        "skipped_count": len(skipped),
        "executed": executed,
        "skipped": skipped,
        "skip_reasons": skip_reasons_map,
    }
