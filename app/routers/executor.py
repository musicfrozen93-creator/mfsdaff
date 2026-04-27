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

from sqlalchemy import select, true


from app.modules.risk_engine import RiskEngine
from app.modules.executor import BinanceExecutor
from app.modules.telegram import TelegramNotifier
from app.modules.crypto_utils import decrypt_api_key
from app.modules.daily_guard import daily_guard
from app.modules.strategy_tracker import strategy_tracker  # V7: per-coin cooldown
from app.modules.learning_engine import learning_engine      # V7: adaptive learning
from app.utils.state import state_manager
from app.config import settings
from app.database import async_session
from app.models.user import Account, ApiConnection
from app.models.trading import Signal, Trade, TradeSkip, OpenPosition
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

    # V10: Post-close cooldown check (fires after a trade on this coin was closed)
    post_cd_active, post_cd_remaining = state_manager.is_post_close_cooldown_active(symbol)
    if post_cd_active:
        return {
            "status": "skipped",
            "reason": f"Post-close cooldown active for {symbol} — {post_cd_remaining // 60}m {post_cd_remaining % 60}s remaining",
        }

    # V10: Concurrent trade limit check (scalp and swing caps)
    try:
        async with async_session() as session:
            result = await session.execute(
                select(OpenPosition).where(OpenPosition.status.in_(["open", "closing"]))
            )
            open_positions = result.scalars().all()

        is_scalp = req.strategy_type.startswith(("scalp", "sniper"))
        scalp_open = sum(1 for p in open_positions if (p.strategy_type or "").startswith(("scalp", "sniper")))
        swing_open = len(open_positions) - scalp_open

        if is_scalp and scalp_open >= settings.MAX_CONCURRENT_SCALP_TRADES:
            logger.info(
                f"[V11 GATE] {symbol} BLOCKED — scalp concurrent limit "
                f"{scalp_open}/{settings.MAX_CONCURRENT_SCALP_TRADES}"
            )
            return {
                "status": "skipped",
                "reason": f"Scalp concurrent limit reached: {scalp_open}/{settings.MAX_CONCURRENT_SCALP_TRADES} open scalp trades",
            }
        if not is_scalp and swing_open >= settings.MAX_CONCURRENT_SWING_TRADES:
            logger.info(
                f"[V11 GATE] {symbol} BLOCKED — swing concurrent limit "
                f"{swing_open}/{settings.MAX_CONCURRENT_SWING_TRADES}"
            )
            return {
                "status": "skipped",
                "reason": f"Swing concurrent limit reached: {swing_open}/{settings.MAX_CONCURRENT_SWING_TRADES} open swing trades",
            }

        # V11: Global MAX_OPEN_POSITIONS cap (scalp + swing combined)
        total_open = len(open_positions)
        if total_open >= settings.MAX_OPEN_POSITIONS:
            logger.info(
                f"[V11 GATE] {symbol} BLOCKED — global position cap "
                f"{total_open}/{settings.MAX_OPEN_POSITIONS}"
            )
            return {
                "status": "skipped",
                "reason": f"V11: Max open positions reached ({total_open}/{settings.MAX_OPEN_POSITIONS})",
            }

        # V11: Same-symbol block (prevent duplicate open positions on the same coin)
        same_symbol_open = [p for p in open_positions if p.symbol == symbol]
        if len(same_symbol_open) >= settings.MAX_SAME_SYMBOL_OPEN:
            logger.info(
                f"[V11 GATE] {symbol} BLOCKED — same-symbol already open "
                f"({len(same_symbol_open)} positions)"
            )
            return {
                "status": "skipped",
                "reason": f"V11: {symbol} already has {len(same_symbol_open)} open position(s)",
            }

    except Exception as cl_err:
        logger.warning(f"Concurrent limit check failed (non-critical): {cl_err}")

    # V11: Global daily P&L entry gate
    v11_gate = state_manager.check_v11_entry_gate()
    if not v11_gate["allowed"]:
        logger.info(f"[V11 GATE] {symbol} BLOCKED — daily P&L gate: {v11_gate['reason']}")
        return {"status": "skipped", "reason": v11_gate["reason"]}

    # V11: Verbose gate log (always shown — helps debug scalp execution issues)
    logger.info(
        f"[V11 GATE PASS] {symbol} {side} conf={req.confidence} strategy={req.strategy_type}\n"
        f"  HOURLY_LIMIT: OK | DAILY_LIMIT: OK | COIN_CD: OK | POST_CD: OK\n"
        f"  CONCURRENT: scalp={scalp_open if 'scalp_open' in dir() else '?'}/ "
        f"swing={swing_open if 'swing_open' in dir() else '?'} | "
        f"GLOBAL: {total_open if 'total_open' in dir() else '?'}/{settings.MAX_OPEN_POSITIONS} | "
        f"V11_GATE: PASS"
    )

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
        # Log at ERROR (not WARNING) so schema errors like
        # "column does not exist" are immediately visible in logs.
        logger.error(
            f"❌ Failed to save signal to DB: {e} — "
            f"check schema migration if error mentions missing columns",
            exc_info=True,
        )
        # Do NOT abort — signal save is non-critical; continue to account loading

    # ── Load active accounts (direct JOIN — bypass ORM relationship) ────
    # Uses sqlalchemy.true() for DB-safe boolean comparisons (avoids Python True
    # comparison mismatches with asyncpg on some PostgreSQL column types).
    accounts_data = []
    _load_error: str | None = None
    try:
        async with async_session() as session:
            stmt = (
                select(Account, ApiConnection)
                .join(ApiConnection, ApiConnection.account_id == Account.id)
                .where(
                    Account.is_active == true(),
                    Account.bot_enabled == true(),
                    ApiConnection.is_active == true(),
                )
            )
            result = await session.execute(stmt)
            raw_rows = result.all()  # consume cursor ONCE into a list

            logger.info(f"🔍 Account JOIN query returned {len(raw_rows)} raw row(s)")

            for row in raw_rows:
                acc: Account = row[0]
                conn: ApiConnection = row[1]

                logger.info(
                    f"  → Account found: id={acc.id} label='{acc.label}' "
                    f"is_active={acc.is_active} bot_enabled={acc.bot_enabled} "
                    f"conn.is_active={conn.is_active} "
                    f"has_key={bool(conn.api_key_encrypted)} "
                    f"has_secret={bool(conn.api_secret_encrypted)}"
                )

                # Guard: encrypted keys must not be empty
                if not conn.api_key_encrypted:
                    logger.warning(
                        f"  ⚠️ Skipped account id={acc.id} ('{acc.label}'): "
                        f"api_key_encrypted is NULL/empty in DB"
                    )
                    continue
                if not conn.api_secret_encrypted:
                    logger.warning(
                        f"  ⚠️ Skipped account id={acc.id} ('{acc.label}'): "
                        f"api_secret_encrypted is NULL/empty in DB"
                    )
                    continue

                # Verify decrypt works NOW — fail loudly, not silently later
                try:
                    _test_key = decrypt_api_key(conn.api_key_encrypted)
                    _test_secret = decrypt_api_key(conn.api_secret_encrypted)
                    if not _test_key or not _test_secret:
                        raise ValueError("Decrypted value is empty string")
                except Exception as dec_err:
                    logger.error(
                        f"  ❌ DECRYPT FAILED for account id={acc.id} ('{acc.label}'): "
                        f"{dec_err} — check ENCRYPTION_KEY env var matches what was used "
                        f"when the key was originally stored"
                    )
                    continue

                accounts_data.append({
                    "id": acc.id,
                    "label": acc.label,
                    "api_key_enc": conn.api_key_encrypted,
                    "api_secret_enc": conn.api_secret_encrypted,
                })
                logger.info(f"  ✅ Account id={acc.id} ('{acc.label}') added to execution list")

        if raw_rows and not accounts_data:
            logger.error(
                f"❌ {len(raw_rows)} DB row(s) found but ALL were filtered out "
                f"(likely decrypt failure or empty encrypted keys). "
                f"Check ENCRYPTION_KEY and api_connections table data."
            )
        elif not raw_rows:
            logger.error(
                "❌ Account JOIN returned 0 rows. "
                "Verify: accounts.is_active=true, accounts.bot_enabled=true, "
                "api_connections.is_active=true, and account_id FK is correct."
            )
        else:
            logger.info(f"🔑 {len(accounts_data)} account(s) ready for execution")

    except Exception as e:
        _load_error = str(e)
        logger.error(
            f"❌ CRITICAL: Account loading query failed with exception: {e}",
            exc_info=True,
        )

    # Fallback to master account only if env key is explicitly set
    if not accounts_data:
        master_key = (settings.BINANCE_API_KEY or "").strip()
        if master_key:
            logger.warning(
                "⚠️ No DB accounts loaded — falling back to master BINANCE_API_KEY env var"
            )
            accounts_data = [{
                "id": 0,
                "label": "Master",
                "api_key_enc": None,
                "api_secret_enc": None,
            }]
        else:
            err_detail = f" — cause: {_load_error}" if _load_error else ""
            logger.error(
                f"❌ No active accounts found and no master API key configured{err_detail}"
            )
            return {
                "status": "error",
                "message": f"No active accounts found{err_detail}",
            }

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

    # V10: Track best trade result for the single Telegram message
    # Note: sl_attached / tp_attached removed — Protection Engine owns that
    best_fill_price = 0.0
    best_leverage = 0
    best_tp = 0.0
    best_sl = 0.0
    best_tp_pct = 0.0
    best_sl_pct = 0.0
    best_grade = "C"
    best_order_method = "MARKET"
    best_rr = 0.0

    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent account executions

    async def execute_for_account(acc_data: dict):
        nonlocal best_fill_price, best_leverage, best_tp, best_sl
        nonlocal best_tp_pct, best_sl_pct, best_grade, best_order_method, best_rr

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
                    trade_db_id = None
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
                                sl_order_id=None,   # V10: No native SL placed -- Protection Engine manages
                                tp_order_id=None,   # V10: No native TP placed -- Protection Engine manages
                                status="open",
                                strategy_type=req.strategy_type,
                                regime=req.regime,
                                # V10: Protection Engine lifecycle
                                protection_status="PENDING",
                                virtual_sl=trade_params.stop_loss,
                                virtual_tp=trade_params.take_profit,
                                managed_by="external_engine",
                            )
                            session.add(trade)
                            await session.commit()
                            await session.refresh(trade)
                            trade_db_id = trade.id

                            # ── V9: Write OpenPosition for Position Manager ──
                            # Read hedge mode + position_side from executor result
                            _is_hedge = result.is_hedge_mode
                            _pos_side = result.position_side
                            # Determine timeframe from strategy type
                            _timeframe = (
                                "4h" if req.strategy_type.startswith("swing")
                                else ("15m" if req.strategy_type.startswith("sniper") else "1m")
                            )
                            # Trailing trigger = BREAK_EVEN_TRIGGER_PCT from settings
                            from app.config import settings as _s
                            open_pos = OpenPosition(
                                account_id=acc_id,
                                trade_id=trade_db_id,
                                symbol=symbol,
                                side=side,
                                entry_price=result.fill_price or entry_price,
                                quantity=trade_params.quantity,
                                leverage=trade_params.leverage,
                                position_size_usdt=trade_params.position_size_usdt,
                                strategy_type=req.strategy_type,
                                timeframe=_timeframe,
                                confidence=req.confidence,
                                regime=req.regime,
                                tp_price=trade_params.take_profit,
                                sl_price=trade_params.stop_loss,
                                tp_pct=trade_params.tp_pct,
                                sl_pct=trade_params.sl_pct,
                                trailing_trigger_pct=_s.BREAK_EVEN_TRIGGER_PCT,
                                entry_order_id=str(result.order_id) if result.order_id else None,
                                is_hedge_mode=_is_hedge,
                                position_side=_pos_side,
                                status="open",
                                last_price=result.fill_price or entry_price,
                                highest_price=result.fill_price or entry_price,
                                lowest_price=result.fill_price or entry_price,
                                # V11: entry_reason for traceability
                                entry_reason=req.reason[:500] if req.reason else None,
                            )
                            session.add(open_pos)
                            await session.commit()
                            logger.info(
                                f"  📌 [{internal_label}] OpenPosition saved: "
                                f"{symbol} {side} TP={trade_params.take_profit} "
                                f"SL={trade_params.stop_loss} strategy={req.strategy_type}"
                            )
                    except Exception as dbe:
                        logger.warning(f"  [{internal_label}] Failed to save trade to DB: {dbe}")

                    # V7: Notify learning engine — trade opened
                    try:
                        await learning_engine.record_trade(
                            strategy_id=req.strategy_type,
                            method="scalp" if "scalp" in req.strategy_type else (
                                "swing" if "swing" in req.strategy_type else "snipe"
                            ),
                            symbol=symbol,
                            side=side,
                            market_regime=req.regime,
                            entry_price=result.fill_price or entry_price,
                            exit_price=None,           # Not closed yet
                            pnl_pct=None,              # Not closed yet
                            won=None,                  # Not closed yet
                            confidence=req.confidence,
                            btc_trend=req.regime,
                            setup_grade=trade_params.setup_grade,
                        )
                    except Exception as le:
                        logger.debug(f"  [{internal_label}] Learning engine record failed: {le}")

                    # V10: Track best result for Telegram message
                    # sl_attached/tp_attached removed — Protection Engine owns TP/SL
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

                    logger.info(
                        f"  [V10] [{internal_label}] EXECUTED: "
                        f"order=#{result.order_id} fill={result.fill_price} "
                        f"method={best_order_method} "
                        f"-- Protection Engine will manage TP/SL"
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
                        # V10: No sl_attached/tp_attached -- Protection Engine manages
                        "protection_mode": "external_engine",
                        "tp_attached": result.tp_attached,
                        "order_method": best_order_method,
                    }
                else:
                    # V8: Differentiate pre-trade skip from exchange errors
                    error_msg = result.error or ""
                    if "PRE_TRADE_SKIP" in error_msg:
                        logger.warning(
                            f"  🛡️ [{internal_label}] V8 PRE-TRADE PROTECTION: "
                            f"TP/SL validation failed — trade skipped safely"
                        )
                        return _skip(
                            acc_id,
                            "TP/SL Protection",
                            f"Skipped before entry: {error_msg.replace('PRE_TRADE_SKIP: ', '')}",
                        )
                    elif getattr(result, 'tp_sl_protection_failed', False):
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
        # At least one account traded -- send execution result
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
            order_method=best_order_method,
            strategy_type=req.strategy_type,
            regime=req.regime,
            risk_reward=best_rr,
            # V10: Protection Engine manages TP/SL -- no attachment flags
            protection_mode="external_engine",
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
        # V10: entry engine only, protection is external
        "protection_mode": "external_engine",
        "entry_opened": executed_count > 0,
    }


# ═══════════════════════════════════════════════════════════════════════
# V8: PRE-TRADE VALIDATION ENDPOINT — called by n8n BEFORE execute-multi
# ═══════════════════════════════════════════════════════════════════════

class ValidateTpSlRequest(BaseModel):
    symbol: str
    action: str           # BUY | SELL
    current_price: float = 0.0
    tp_pct: float = 2.0   # Take profit %
    sl_pct: float = 1.0   # Stop loss %
    quantity: float = 0.0


@router.post("/validate-tpsl")
async def validate_tpsl(req: ValidateTpSlRequest):
    """
    V8: Dry-run TP/SL validation endpoint.

    Called by n8n BEFORE execute-multi to check if TP/SL can be placed.
    No positions opened. No exchange orders. Pure validation only.

    Returns:
        { valid: true/false, reason: "...", hedge_mode: bool,
          estimated_tp: float, estimated_sl: float }
    """
    symbol = req.symbol.upper().strip()
    side = req.action.upper().strip()

    if side not in ("BUY", "SELL"):
        return {"valid": False, "reason": f"Invalid action: {req.action}"}

    executor = BinanceExecutor()

    try:
        # Get precision
        precision = await executor.get_precision(symbol)

        # Get live price
        entry_price = req.current_price
        if entry_price <= 0:
            entry_price = await executor.get_market_price(symbol)

        # Detect account mode
        is_hedge_mode = await executor.detect_position_mode()

        # Compute estimated TP/SL
        tp_pct_dec = req.tp_pct / 100.0
        sl_pct_dec = req.sl_pct / 100.0
        if side == "BUY":
            est_tp = round(entry_price * (1 + tp_pct_dec), precision.price_precision)
            est_sl = round(entry_price * (1 - sl_pct_dec), precision.price_precision)
        else:
            est_tp = round(entry_price * (1 - tp_pct_dec), precision.price_precision)
            est_sl = round(entry_price * (1 + sl_pct_dec), precision.price_precision)

        # Run dry-run validation
        can_proceed, reason = await executor.can_place_tp_sl(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            tp_price=est_tp,
            sl_price=est_sl,
            precision=precision,
            is_hedge_mode=is_hedge_mode,
            quantity=req.quantity if is_hedge_mode else 0.0,
        )

        return {
            "valid": can_proceed,
            "reason": reason,
            "symbol": symbol,
            "side": side,
            "hedge_mode": is_hedge_mode,
            "estimated_tp": est_tp,
            "estimated_sl": est_sl,
            "entry_price": entry_price,
            "tick_size": precision.tick_size,
            "price_precision": precision.price_precision,
        }

    except Exception as e:
        logger.error(f"validate-tpsl error for {symbol}: {e}")
        return {"valid": False, "reason": f"Validation error: {str(e)[:200]}", "symbol": symbol}


# ═══════════════════════════════════════════════════════════════════════
# V10 POSITION MANAGER ENDPOINTS — called by n8n Position Manager workflow
# ═══════════════════════════════════════════════════════════════════════

class LockPositionRequest(BaseModel):
    position_id: int


class ClosePositionRequest(BaseModel):
    position_id: int
    close_reason: str  # tp_hit | sl_hit | trailing_exit


class HeartbeatRequest(BaseModel):
    position_id: int
    current_price: float


# ── GET /positions/open ──────────────────────────────────────────────

@router.get("/positions/open")
async def get_open_positions(type: Optional[str] = None):
    """
    V10: Return all open positions for n8n PM to monitor.

    Query param:
      type=scalp  → only positions whose strategy_type starts with 'scalp' or 'sniper'
      type=swing  → only positions whose strategy_type starts with 'swing'
      (omit)      → all open positions

    Also enforces concurrent trade limits — returns count metadata
    so n8n can skip execution when limits are hit.
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(OpenPosition).where(OpenPosition.status == "open")
            )
            positions = result.scalars().all()

        def _trade_mode(p: OpenPosition) -> str:
            st = (p.strategy_type or "").lower()
            if st.startswith("scalp") or st.startswith("sniper"):
                return "scalp"
            return "swing"

        all_open = [
            {
                "id": p.id,
                "trade_id": p.trade_id,
                "account_id": p.account_id,
                "symbol": p.symbol,
                "side": p.side,
                "entry_price": p.entry_price,
                "quantity": p.quantity,
                "leverage": p.leverage,
                "strategy_type": p.strategy_type or "",
                "trade_mode": _trade_mode(p),
                "timeframe": p.timeframe or "",
                "confidence": p.confidence or 0,
                "tp_price": p.tp_price,
                "sl_price": p.sl_price,
                "tp_pct": p.tp_pct or 0,
                "sl_pct": p.sl_pct or 0,
                "trailing_active": p.trailing_active,
                "trailing_sl_price": p.trailing_sl_price,
                "trailing_trigger_pct": p.trailing_trigger_pct or 0,
                "highest_price": p.highest_price or p.entry_price,
                "lowest_price": p.lowest_price or p.entry_price,
                "is_hedge_mode": p.is_hedge_mode,
                "position_side": p.position_side or "BOTH",
                "status": p.status,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "last_price": p.last_price,
                "check_count": p.check_count or 0,
            }
            for p in positions
        ]

        # Filter by type if requested
        if type == "scalp":
            filtered = [p for p in all_open if p["trade_mode"] == "scalp"]
        elif type == "swing":
            filtered = [p for p in all_open if p["trade_mode"] == "swing"]
        else:
            filtered = all_open

        scalp_count = sum(1 for p in all_open if p["trade_mode"] == "scalp")
        swing_count = sum(1 for p in all_open if p["trade_mode"] == "swing")

        return {
            "status": "ok",
            "count": len(filtered),
            "scalp_open": scalp_count,
            "swing_open": swing_count,
            "scalp_limit": settings.MAX_CONCURRENT_SCALP_TRADES,
            "swing_limit": settings.MAX_CONCURRENT_SWING_TRADES,
            "positions": filtered,
        }
    except Exception as e:
        logger.error(f"[positions/open] Error: {e}")
        return {"status": "error", "message": str(e), "count": 0, "positions": []}


# ── POST /positions/lock ─────────────────────────────────────────────

@router.post("/positions/lock")
async def lock_position(req: LockPositionRequest):
    """
    V10: Atomically set position status='closing' to prevent duplicate closes.

    Returns already_locked=true if position is already being closed by
    another process (Python PM or a concurrent n8n execution).
    """
    try:
        async with async_session() as session:
            pos = await session.get(OpenPosition, req.position_id)
            if not pos:
                return {"locked": False, "already_locked": False, "reason": "Position not found"}
            if pos.status != "open":
                return {
                    "locked": False,
                    "already_locked": pos.status in ("closing", "closed"),
                    "reason": f"Position status is already '{pos.status}'",
                    "current_status": pos.status,
                }
            pos.status = "closing"
            await session.commit()
            logger.info(f"[PM Lock] Position {req.position_id} ({pos.symbol}) locked → 'closing'")
            return {"locked": True, "already_locked": False, "position_id": req.position_id, "symbol": pos.symbol}
    except Exception as e:
        logger.error(f"[positions/lock] Error: {e}")
        return {"locked": False, "already_locked": False, "reason": str(e)}


# ── POST /positions/close ────────────────────────────────────────────

@router.post("/positions/close")
async def close_position_api(req: ClosePositionRequest):
    """
    V10: Execute market close for a position (called by n8n PM after lock).

    Steps:
      1. Load position + account credentials
      2. Call CloseEngine.market_close()
      3. Update open_positions + trades tables
      4. Apply post-close per-coin cooldown
      5. Return PnL result for Telegram message building

    Duplicate-safe: expects status='closing' (set by /positions/lock).
    If status is not 'closing', returns error to prevent accidental double-close.
    """
    from datetime import datetime, timezone
    from app.modules.close_engine import CloseEngine
    from app.modules.crypto_utils import decrypt_api_key
    from app.models.user import ApiConnection
    from sqlalchemy import text

    try:
        async with async_session() as session:
            pos = await session.get(OpenPosition, req.position_id)
            if not pos:
                return {"success": False, "error": "Position not found"}
            if pos.status != "closing":
                return {
                    "success": False,
                    "error": f"Position not in 'closing' state (current: {pos.status}). Lock first.",
                }

            # Load account credentials
            stmt = (
                select(ApiConnection)
                .where(ApiConnection.account_id == pos.account_id)
                .where(ApiConnection.is_active == True)  # noqa: E712
            )
            result = await session.execute(stmt)
            conn = result.scalars().first()

        # Build CloseEngine
        if conn and conn.api_key_encrypted and conn.api_secret_encrypted:
            try:
                api_key = decrypt_api_key(conn.api_key_encrypted)
                api_secret = decrypt_api_key(conn.api_secret_encrypted)
            except Exception:
                api_key = settings.BINANCE_API_KEY
                api_secret = settings.BINANCE_SECRET_KEY
        else:
            api_key = settings.BINANCE_API_KEY
            api_secret = settings.BINANCE_SECRET_KEY

        close_engine = CloseEngine(
            api_key=api_key,
            secret_key=api_secret,
            testnet=settings.BINANCE_TESTNET,
        )

        close_result = await close_engine.market_close(
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            close_reason=req.close_reason,
            is_hedge_mode=pos.is_hedge_mode,
            position_side=pos.position_side or "BOTH",
        )

        now = datetime.now(timezone.utc)

        if close_result.success:
            # Update DB
            async with async_session() as session:
                db_pos = await session.get(OpenPosition, req.position_id)
                if db_pos:
                    db_pos.status = "closed"
                    db_pos.close_price = close_result.close_price
                    db_pos.close_reason = req.close_reason
                    db_pos.pnl_usdt = close_result.pnl_usdt
                    db_pos.pnl_pct = close_result.pnl_pct
                    db_pos.closed_at = now
                    db_pos.last_checked_at = now

                if pos.trade_id:
                    db_trade = await session.get(Trade, pos.trade_id)
                    if db_trade:
                        db_trade.status = "closed"
                        db_trade.close_price = close_result.close_price
                        db_trade.pnl = close_result.pnl_usdt
                        db_trade.pnl_pct = close_result.pnl_pct
                        db_trade.close_reason = req.close_reason
                        db_trade.closed_at = now
                        db_trade.protection_status = "CLOSED"
                        db_trade.managed_by = "n8n_pm"

                await session.commit()

            # V10: Apply post-close per-coin cooldown
            trade_mode = "scalp" if (pos.strategy_type or "").startswith(("scalp", "sniper")) else "swing"
            cooldown_mins = (
                settings.SCALP_CLOSE_COOLDOWN_MINUTES if trade_mode == "scalp"
                else settings.SWING_CLOSE_COOLDOWN_MINUTES
            )
            state_manager.record_post_close_cooldown(pos.symbol, cooldown_mins)

            logger.info(
                f"[PM Close] {pos.symbol} {pos.side} closed. "
                f"reason={req.close_reason} pnl={close_result.pnl_usdt:+.4f} "
                f"cooldown={cooldown_mins}m"
            )

            # Duration
            opened_at = pos.opened_at
            if opened_at and opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            duration_mins = int((now - opened_at).total_seconds() / 60) if opened_at else 0

            return {
                "success": True,
                "symbol": pos.symbol,
                "side": pos.side,
                "strategy_type": pos.strategy_type or "",
                "trade_mode": trade_mode,
                "close_reason": req.close_reason,
                "entry_price": pos.entry_price,
                "close_price": close_result.close_price,
                "pnl_usdt": round(close_result.pnl_usdt, 4),
                "pnl_pct": round(close_result.pnl_pct, 2),
                "tp_price": pos.tp_price,
                "sl_price": pos.sl_price,
                "confidence": pos.confidence or 0,
                "duration_minutes": duration_mins,
                "peak_price": pos.highest_price or 0,
            }
        else:
            # Close failed — revert status back to 'open' so next cycle retries
            async with async_session() as session:
                db_pos = await session.get(OpenPosition, req.position_id)
                if db_pos and db_pos.status == "closing":
                    db_pos.status = "open"
                    await session.commit()
            logger.error(f"[PM Close] Close FAILED for {pos.symbol}: {close_result.error}")
            return {"success": False, "error": close_result.error or "Unknown close error", "symbol": pos.symbol}

    except Exception as e:
        logger.error(f"[positions/close] Error: {e}", exc_info=True)
        # Best-effort revert
        try:
            async with async_session() as session:
                db_pos = await session.get(OpenPosition, req.position_id)
                if db_pos and db_pos.status == "closing":
                    db_pos.status = "open"
                    await session.commit()
        except Exception:
            pass
        return {"success": False, "error": str(e)}


# ── POST /positions/heartbeat ────────────────────────────────────────

@router.post("/positions/heartbeat")
async def heartbeat_position(req: HeartbeatRequest):
    """
    V10: Update last_price and check_count for a position (no close logic).
    Called by n8n PM on every cycle for positions that did NOT trigger close.
    """
    from datetime import datetime, timezone
    try:
        async with async_session() as session:
            pos = await session.get(OpenPosition, req.position_id)
            if pos and pos.status == "open":
                pos.last_price = req.current_price
                pos.last_checked_at = datetime.now(timezone.utc)
                pos.check_count = (pos.check_count or 0) + 1
                await session.commit()
        return {"ok": True, "position_id": req.position_id, "last_price": req.current_price}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── GET /positions/count ─────────────────────────────────────────────

@router.get("/positions/count")
async def count_open_positions():
    """
    V10: Return counts of open scalp and swing positions for concurrent limit checks.
    Called by execute-multi before opening a new trade.
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(OpenPosition).where(OpenPosition.status.in_(["open", "closing"]))
            )
            positions = result.scalars().all()

        scalp = sum(
            1 for p in positions
            if (p.strategy_type or "").startswith(("scalp", "sniper"))
        )
        swing = len(positions) - scalp

        return {
            "scalp_open": scalp,
            "swing_open": swing,
            "total_open": len(positions),
            "scalp_limit": settings.MAX_CONCURRENT_SCALP_TRADES,
            "swing_limit": settings.MAX_CONCURRENT_SWING_TRADES,
            "scalp_slots_available": max(0, settings.MAX_CONCURRENT_SCALP_TRADES - scalp),
            "swing_slots_available": max(0, settings.MAX_CONCURRENT_SWING_TRADES - swing),
        }
    except Exception as e:
        logger.error(f"[positions/count] Error: {e}")
        return {"scalp_open": 0, "swing_open": 0, "total_open": 0, "error": str(e)}
