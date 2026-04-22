"""
V5 Executor API — Multi-Account Trade Execution with Strategy Awareness

Endpoints:
  POST /execute       — Single-account backward-compatible execution
  POST /execute-full  — Single-account with risk engine + SL/TP
  POST /execute-multi — Multi-account execution for all connected accounts

V5 Changes:
  - strategy_type + regime passed through entire pipeline
  - Signal and Trade DB records tagged with strategy + regime
  - Telegram notification shows strategy type
  - Risk engine uses strategy-based TP/SL
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from sqlalchemy import select


from app.modules.risk_engine import RiskEngine
from app.modules.executor import BinanceExecutor
from app.modules.telegram import TelegramNotifier
from app.modules.crypto_utils import decrypt_api_key
from app.modules.daily_guard import daily_guard
from app.modules.strategy_tracker import strategy_tracker  # V7: per-coin cooldown
from app.utils.state import state_manager
from app.config import settings
from app.database import async_session
from app.models.user import Account
from app.models.trading import Signal, Trade, TradeSkip
from app.utils.subscription_guard import check_account_eligible

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
    # V5 additions
    strategy_type: str = "trend_pullback"
    regime: str = ""


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

    # Daily trade count limit
    daily_check = state_manager.check_daily_limits(0)
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
    """Full single-account execution with V4 risk engine + SL/TP."""
    telegram = TelegramNotifier()
    binance = BinanceExecutor()

    # Daily trade count limit
    daily_check = state_manager.check_daily_limits(0)
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
        balance = await binance.get_account_balance()
    except Exception:
        balance = 20.0

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
    result = await binance.execute_trade(trade_params, telegram_notifier=telegram)

    if result.success:
        state_manager.record_trade_opened(req.symbol)

        await telegram.trade_opened(
            symbol=req.symbol, side=side, entry_price=entry_price,
            leverage=trade_params.leverage, position_size=trade_params.position_size_usdt,
            take_profit=trade_params.take_profit, stop_loss=trade_params.stop_loss,
            confidence=req.confidence,
            tp_pct=trade_params.tp_pct, sl_pct=trade_params.sl_pct,
            setup_grade=trade_params.setup_grade,
        )

        return {
            "status": "executed", "order_id": result.order_id,
            "symbol": req.symbol, "side": side,
            "quantity": trade_params.quantity, "entry_price": entry_price,
            "fill_price": result.fill_price,
            "stop_loss": trade_params.stop_loss, "take_profit": trade_params.take_profit,
            "leverage": trade_params.leverage, "risk_reward": trade_params.risk_reward,
            "confidence": req.confidence, "position_size": trade_params.position_size_usdt,
            "setup_grade": trade_params.setup_grade,
            "tp_pct": trade_params.tp_pct, "sl_pct": trade_params.sl_pct,
            "sl_attached": result.sl_attached, "tp_attached": result.tp_attached,
        }
    else:
        # V7: Differentiate TP/SL protection failures
        if getattr(result, 'tp_sl_protection_failed', False):
            emergency_status = "closed" if getattr(result, 'emergency_closed', False) else "FAILED"
            # Telegram alert already sent by executor
            return {
                "status": "skipped",
                "reason": f"V7 Atomic Protection: TP/SL failed → position {emergency_status}",
                "tp_sl_protection_failed": True,
                "emergency_closed": getattr(result, 'emergency_closed', False),
            }
        else:
            await telegram.error_alert("Trade Execution", result.error or "Unknown error")
            raise HTTPException(status_code=500, detail=result.error)


# ═══════════════════════════════════════════════════════════════════════
# V4 MULTI-ACCOUNT EXECUTE — Hardened account loop, single Telegram msg
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-multi")
async def execute_multi_account(req: MultiExecuteRequest):
    """
    V4 Multi-account execution flow:
    1. Validate signal + save to DB
    2. Load all active accounts
    3. Run pre-entry check ONCE (symbol-level, not per-account)
    4. For each account: daily guard → validate → calculate risk → execute → log
    5. Collect ALL results first
    6. Send ONE Telegram message at the end
    7. If nobody traded → send nothing (or compact no-execution message)
    """
    telegram = TelegramNotifier()

    symbol = req.symbol.upper().strip()
    side = req.action.upper().strip()

    if side not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action: {req.action}"}
    if req.confidence < settings.MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Confidence {req.confidence} < {settings.MIN_CONFIDENCE}"}

    # ── Global rate limits (trade count + cooldowns only) ────────────
    hourly_limited, _ = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        return {"status": "skipped", "reason": "Hourly trade limit reached"}

    # V4: check_daily_limits only checks trade COUNT now (not P&L — that's per-account)
    daily_check = state_manager.check_daily_limits(0)
    if not daily_check["allowed"]:
        return {"status": "trading_paused", "reason": daily_check["reason"]}

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
                strategy_type=req.strategy_type,
                regime=req.regime,
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

    # ── V4: Pre-entry check ONCE (symbol-level, not per account) ─────
    risk_engine = RiskEngine()
    setup_grade = req.indicators.get("setup_grade", "C")
    volume_spike = req.indicators.get("volume_spike", False)

    # Create a scanner executor for pre-entry (uses master/first account)
    if accounts_data[0]["api_key_enc"]:
        try:
            scanner_api_key = decrypt_api_key(accounts_data[0]["api_key_enc"])
            scanner_api_secret = decrypt_api_key(accounts_data[0]["api_secret_enc"])
            scanner_executor = BinanceExecutor(api_key=scanner_api_key, secret_key=scanner_api_secret)
        except Exception:
            scanner_executor = BinanceExecutor()
    else:
        scanner_executor = BinanceExecutor()

    # Get entry price ONCE
    entry_price = req.current_price
    if entry_price <= 0:
        try:
            entry_price = await scanner_executor.get_market_price(symbol)
        except Exception as e:
            logger.error(f"Failed to get market price for {symbol}: {e}")
            return {"status": "error", "message": f"Cannot get price for {symbol}"}

    # Pre-entry quality check (ONCE for the signal)
    tp_pct_decimal, _ = risk_engine.get_tp_sl_pct(
        req.confidence, req.atr_pct, strategy_type=req.strategy_type,
    )
    tp_pct_for_check = tp_pct_decimal * 100

    pre_check = await scanner_executor.pre_entry_check(
        symbol=symbol, side=side,
        tp_pct=tp_pct_for_check,
        atr=req.indicators.get("atr", 0),
        entry_price=entry_price,
    )
    if not pre_check.passed:
        logger.info(f"  Pre-entry check FAILED (signal-level): {pre_check.reason}")
        # V4: Don't send Telegram for pre-entry failures — not actionable
        return {
            "status": "skipped",
            "reason": f"Pre-entry check failed: {pre_check.reason}",
        }

    logger.info(
        f"  Pre-entry PASSED: spread={pre_check.spread_pct:.4f}% "
        f"slippage_est={pre_check.slippage_estimate:.4f}% "
        f"fee_impact={pre_check.fee_impact_pct:.1f}%"
    )

    # ── Execute for each account (hardened loop) ─────────────────────
    executed = []
    skipped = []
    skip_reasons_map = {}

    # V4: Track best trade result for the single Telegram message
    best_fill_price = 0.0
    best_leverage = 0
    best_tp = 0.0
    best_sl = 0.0
    best_tp_pct = 0.0
    best_sl_pct = 0.0
    best_grade = "C"
    all_sl_attached = True
    all_tp_attached = True
    best_order_method = "MARKET"
    # V5.5: Track order IDs for proof + R:R
    best_sl_order_id = ""
    best_tp_order_id = ""
    best_rr = 0.0

    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent account executions

    async def execute_for_account(acc_data: dict):
        nonlocal best_fill_price, best_leverage, best_tp, best_sl
        nonlocal best_tp_pct, best_sl_pct, best_grade
        nonlocal all_sl_attached, all_tp_attached, best_order_method
        nonlocal best_sl_order_id, best_tp_order_id, best_rr

        async with semaphore:
            acc_id = acc_data["id"]
            # V4: Use generic label for internal logging (never sent to Telegram)
            internal_label = f"account_{acc_id}"

            try:
                # ── 0. V6: Subscription + Ban pre-check ─────────────
                if acc_id > 0:  # Skip for master/fallback account (id=0)
                    try:
                        async with async_session() as sub_session:
                            eligibility = await check_account_eligible(sub_session, acc_id)
                            if not eligibility["eligible"]:
                                reason = eligibility["reason"]
                                logger.info(f"  [{internal_label}] SKIPPED: {reason}")
                                return _skip(acc_id, reason, reason)
                    except Exception as sub_err:
                        logger.warning(f"  [{internal_label}] Subscription check error: {sub_err}")
                        # On check error, allow trade to proceed

                # ── 1. Create executor with account's credentials ─────
                if acc_data["api_key_enc"]:
                    api_key = decrypt_api_key(acc_data["api_key_enc"])
                    api_secret = decrypt_api_key(acc_data["api_secret_enc"])
                    executor = BinanceExecutor(api_key=api_key, secret_key=api_secret)
                else:
                    executor = BinanceExecutor()

                # ── 2. Get balance ───────────────────────────────────
                try:
                    balance = await executor.get_account_balance()
                except Exception as be:
                    logger.error(f"  [{internal_label}] Balance fetch failed: {be}")
                    return _skip(acc_id, "API Error", f"Balance fetch failed: {str(be)[:80]}")

                if balance < 5:
                    logger.info(f"  [{internal_label}] Balance ${balance:.2f} too low → SKIP")
                    return _skip(acc_id, "Insufficient Balance", f"Balance ${balance:.2f} below $5 minimum")

                # ── 3. Per-account daily guard check ─────────────────
                guard_result = daily_guard.check_allowed(acc_id, balance, req.confidence)

                # V4: Log full guard state (private, never Telegram)
                guard_log = daily_guard.get_guard_log(acc_id, balance)
                logger.info(
                    f"  [{internal_label}] GUARD: "
                    f"pnl=${guard_log['pnl_today']} ({guard_log['pnl_pct']:+.1f}%) | "
                    f"trades={guard_log['trades_today']} | "
                    f"stopped={guard_log['is_stopped']} | "
                    f"decision={'PASS' if guard_result['allowed'] else 'BLOCKED'}"
                )

                if not guard_result["allowed"]:
                    guard_reason = guard_result["reason"]
                    # V4: NO Telegram per-account daily guard messages
                    # Categorize for aggregated skip reasons
                    if "profit" in guard_reason.lower() or "safe mode" in guard_reason.lower():
                        return _skip(acc_id, "Daily Target Reached", guard_reason)
                    elif "loss" in guard_reason.lower():
                        return _skip(acc_id, "Daily Loss Limit", guard_reason)
                    elif "pause" in guard_reason.lower():
                        return _skip(acc_id, "Loss Cooldown", guard_reason)
                    else:
                        return _skip(acc_id, "Daily Guard", guard_reason)

                size_multiplier = guard_result["size_multiplier"]

                # ── 4. Check open position ───────────────────────────
                try:
                    if await executor.has_open_position(symbol):
                        logger.info(f"  [{internal_label}] Already has {symbol} position → SKIP")
                        return _skip(acc_id, "Existing Position", f"Already has {symbol} position")
                except Exception as pe:
                    logger.warning(f"  [{internal_label}] Position check failed: {pe}")
                    # Don't skip on position check failure — try to trade

                # ── 4.5 V7: Per-coin cooldown check ──────────────────
                try:
                    is_cooled, cooldown_reason = await strategy_tracker.check_per_coin_cooldown(
                        symbol=symbol,
                        strategy_type=req.strategy_type,
                        max_losses=settings.V7_PER_COIN_COOLDOWN_LOSSES,
                        lookback_days=settings.V7_PER_COIN_COOLDOWN_DAYS,
                        cooldown_hours=settings.V7_PER_COIN_COOLDOWN_HOURS,
                    )
                    if is_cooled:
                        logger.info(f"  [{internal_label}] {cooldown_reason}")
                        return _skip(acc_id, "Coin Cooldown", cooldown_reason)
                except Exception as cd_err:
                    logger.warning(f"  [{internal_label}] Cooldown check failed: {cd_err}")

                # ── 5. Calculate risk parameters ─────────────────────
                precision = await executor.get_precision(symbol)

                trade_params = risk_engine.calculate(
                    symbol=symbol, side=side, confidence=req.confidence,
                    entry_price=entry_price, atr_pct=req.atr_pct,
                    account_balance=balance,
                    min_notional=precision.min_notional, min_qty=precision.min_qty,
                    step_size=precision.step_size,
                    quantity_precision=precision.quantity_precision,
                    price_precision=precision.price_precision,
                    volume_spike=volume_spike,
                    size_multiplier=size_multiplier,
                    strategy_type=req.strategy_type,
                )

                # ── 5.5 V7: Enforce max leverage cap ─────────────────
                if trade_params.leverage > settings.V7_MAX_LEVERAGE:
                    logger.info(
                        f"  [{internal_label}] V7: Capping leverage "
                        f"{trade_params.leverage}x → {settings.V7_MAX_LEVERAGE}x"
                    )
                    trade_params.leverage = settings.V7_MAX_LEVERAGE

                if not trade_params.approved:
                    # Categorize reject reason
                    reason_lower = trade_params.reject_reason.lower()
                    if "notional" in reason_lower or "margin" in reason_lower:
                        category = "Insufficient Margin"
                    elif "quantity" in reason_lower or "min" in reason_lower:
                        category = "Min Quantity Invalid"
                    elif "confidence" in reason_lower:
                        category = "Low Confidence"
                    else:
                        category = "Risk Limit"
                    logger.info(f"  [{internal_label}] Risk rejected: {trade_params.reject_reason}")
                    return _skip(acc_id, category, trade_params.reject_reason)

                # ── 6. Comprehensive trade log (private) ─────────────
                logger.info(
                    f"  📋 TRADE LOG [{internal_label}]: "
                    f"symbol={symbol} side={side} | "
                    f"balance=${balance:.2f} pos_size=${trade_params.position_size_usdt:.2f} | "
                    f"leverage={trade_params.leverage}x qty={trade_params.quantity} | "
                    f"spread={pre_check.spread_pct:.4f}% | "
                    f"confidence={req.confidence} grade={trade_params.setup_grade} | "
                    f"entry={entry_price} TP={trade_params.take_profit} SL={trade_params.stop_loss} | "
                    f"TP%={trade_params.tp_pct} SL%={trade_params.sl_pct} RR={trade_params.risk_reward} | "
                    f"size_mult={size_multiplier}"
                )

                # ── 7. Execute trade (LIMIT→MARKET fallback in executor) ──
                # V7: Pass telegram_notifier so atomic protection can send
                # emergency close alerts directly from the executor
                result = await executor.execute_trade(trade_params, telegram_notifier=telegram)

                if result.success:
                    # Save trade to DB
                    try:
                        async with async_session() as session:
                            trade = Trade(
                                signal_id=signal_id, account_id=acc_id,
                                symbol=symbol, side=side,
                                entry_price=result.fill_price or entry_price,
                                quantity=trade_params.quantity,
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
                                strategy_type=req.strategy_type,
                                regime=req.regime,
                            )
                            session.add(trade)
                            await session.commit()
                    except Exception as dbe:
                        logger.warning(f"  [{internal_label}] Failed to save trade to DB: {dbe}")

                    # Track best result for Telegram message
                    if result.fill_price > 0:
                        best_fill_price = result.fill_price
                    best_leverage = trade_params.leverage
                    best_tp = trade_params.take_profit
                    best_sl = trade_params.stop_loss
                    best_tp_pct = trade_params.tp_pct
                    best_sl_pct = trade_params.sl_pct
                    best_grade = trade_params.setup_grade
                    best_order_method = getattr(result, 'order_method', 'MARKET')
                    best_rr = trade_params.risk_reward
                    if result.stop_loss_order_id:
                        best_sl_order_id = str(result.stop_loss_order_id)
                    if result.take_profit_order_id:
                        best_tp_order_id = str(result.take_profit_order_id)
                    if not result.sl_attached:
                        all_sl_attached = False
                    if not result.tp_attached:
                        all_tp_attached = False

                    logger.info(
                        f"  ✅ [{internal_label}] EXECUTED: "
                        f"order=#{result.order_id} fill={result.fill_price} "
                        f"method={best_order_method} "
                        f"SL_ok={result.sl_attached} TP_ok={result.tp_attached}"
                    )

                    return {
                        "status": "executed",
                        "account_id": acc_id,
                        "leverage": trade_params.leverage,
                        "position_size": trade_params.position_size_usdt,
                        "quantity": trade_params.quantity,
                        "tp": trade_params.take_profit,
                        "sl": trade_params.stop_loss,
                        "tp_pct": trade_params.tp_pct,
                        "sl_pct": trade_params.sl_pct,
                        "setup_grade": trade_params.setup_grade,
                        "fill_price": result.fill_price,
                        "sl_attached": result.sl_attached,
                        "tp_attached": result.tp_attached,
                        "order_method": best_order_method,
                    }
                else:
                    # V7: Differentiate TP/SL protection failures from exchange rejections
                    if getattr(result, 'tp_sl_protection_failed', False):
                        emergency_status = "closed" if getattr(result, 'emergency_closed', False) else "FAILED"
                        logger.error(
                            f"  🚨 [{internal_label}] V7 ATOMIC PROTECTION: "
                            f"TP/SL failed → position {emergency_status}"
                        )
                        return _skip(
                            acc_id,
                            "TP/SL Protection Failed",
                            f"Position emergency {emergency_status}: {result.error or 'TP/SL attach failed'}",
                        )
                    else:
                        logger.error(f"  ❌ [{internal_label}] Execution error: {result.error}")
                        return _skip(acc_id, "Exchange Rejected", result.error or "Unknown execution error")

            except Exception as e:
                logger.error(f"  ❌ [{internal_label}] Account failed: {e}", exc_info=True)
                return _skip(acc_id, "System Error", str(e)[:100])

    def _skip(acc_id, category, reason):
        """Helper to build a skip result. V4: No label exposed."""
        return {
            "status": "skipped",
            "account_id": acc_id,
            "category": category,
            "reason": reason,
        }

    # ── Execute all accounts ─────────────────────────────────────────
    tasks = [execute_for_account(acc) for acc in accounts_data]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            logger.error(f"  Account task exception: {r}")
            skipped.append({"status": "skipped", "category": "System Error", "reason": str(r)[:100]})
            continue
        if r is None:
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

    # ═════════════════════════════════════════════════════════════════
    # V4: ONE TELEGRAM MESSAGE (or nothing)
    # ═════════════════════════════════════════════════════════════════

    executed_count = len(executed)
    skipped_count = len(skipped)

    if executed_count > 0:
        # At least one account traded → send execution result
        display_fill = best_fill_price if best_fill_price > 0 else entry_price

        await telegram.send_execution_result(
            symbol=symbol,
            side=side,
            confidence=req.confidence,
            executed_count=executed_count,
            skipped_count=skipped_count,
            skip_reasons=skip_reasons_map,
            entry_price=entry_price,
            fill_price=display_fill,
            leverage=best_leverage,
            take_profit=best_tp,
            stop_loss=best_sl,
            tp_pct=best_tp_pct,
            sl_pct=best_sl_pct,
            reason=req.reason,
            setup_grade=best_grade,
            sl_attached=all_sl_attached,
            tp_attached=all_tp_attached,
            order_method=best_order_method,
            strategy_type=req.strategy_type,
            regime=req.regime,
            sl_order_id=best_sl_order_id,
            tp_order_id=best_tp_order_id,
            risk_reward=best_rr,
        )
    elif skipped_count > 0:
        # V4: All accounts skipped — send compact no-execution message
        # Only if there were real accounts to try
        await telegram.send_no_execution(
            symbol=symbol,
            side=side,
            confidence=req.confidence,
            skipped_count=skipped_count,
            skip_reasons=skip_reasons_map,
        )
    # else: No accounts at all — don't send anything

    logger.info(
        f"📊 Multi-account result: {executed_count} executed, {skipped_count} skipped "
        f"out of {len(accounts_data)} accounts | Skip reasons: {skip_reasons_map}"
    )

    return {
        "status": "ok",
        "symbol": symbol,
        "side": side,
        "confidence": req.confidence,
        "total_accounts": len(accounts_data),
        "executed_count": executed_count,
        "skipped_count": skipped_count,
        "executed": executed,
        "skipped": skipped,
        "skip_reasons": skip_reasons_map,
    }
