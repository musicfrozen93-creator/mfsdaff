"""
Signal-Only Engine — AI Signal Generation → Telegram Delivery

Endpoints:
  POST /execute-multi — AI signal generation + Telegram delivery (SIGNAL ONLY, no execution)
  POST /execute       — Legacy single-account (kept for backward compat)
  POST /execute-full  — Legacy single-account with risk engine (kept for backward compat)

Signal-Only Changes:
  - execute-multi NO LONGER executes trades on Binance
  - Telegram signals are sent IMMEDIATELY after AI validation
  - No Binance account loading, no multi-account execution
  - No execution dependency — signals delivered even if Binance is down
  - TP/SL calculated via RiskEngine (no exchange API needed)
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional, Any

from sqlalchemy import select, true


from app.modules.risk_engine import RiskEngine
from app.modules.executor import BinanceExecutor
from app.modules.telegram import TelegramNotifier
from app.modules.crypto_utils import decrypt_api_key
from app.modules.daily_guard import daily_guard
from app.modules.strategy_tracker import strategy_tracker  # V7: per-coin cooldown
from app.modules.learning_engine import learning_engine      # V7: adaptive learning
from app.modules.binance_sync import get_binance_live_positions, count_all_live_positions  # V12
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


# V13 valid strategy_type prefixes — anything starting with these passes
_VALID_STRATEGY_PREFIXES = (
    "scalp", "swing", "sniper", "trend", "breakout", "reversal",
    "range", "vwap", "ema", "liquidation", "funding", "volume", "news",
)


class MultiExecuteRequest(BaseModel):
    symbol: str
    action: str  # BUY | SELL
    confidence: Any = 70          # V13: Any so we can coerce str→int safely
    reason: str = ""
    current_price: Any = 0.0      # V13: Any so we can coerce str→float
    atr_pct: Any = 0.0
    spread_pct: Any = 0.0
    indicators: dict = {}
    strategy_type: str = "scalp_trend_pullback"
    regime: str = ""

    # V13: Coerce numeric fields sent as strings from n8n
    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v):
        try:
            return int(float(str(v))) if v is not None else 70
        except (ValueError, TypeError):
            return 70

    @field_validator("current_price", mode="before")
    @classmethod
    def coerce_current_price(cls, v):
        try:
            return float(str(v)) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @field_validator("atr_pct", mode="before")
    @classmethod
    def coerce_atr_pct(cls, v):
        try:
            return float(str(v)) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @field_validator("spread_pct", mode="before")
    @classmethod
    def coerce_spread_pct(cls, v):
        try:
            return float(str(v)) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @field_validator("strategy_type", mode="before")
    @classmethod
    def normalize_strategy_type(cls, v):
        """V13: Accept missing/None/empty strategy_type safely."""
        if not v or not isinstance(v, str):
            return "scalp_trend_pullback"
        v = v.strip().lower().replace(" ", "_")
        # Accept if starts with any known prefix
        if any(v.startswith(p) for p in _VALID_STRATEGY_PREFIXES):
            return v
        # Unknown prefix — treat as generic scalp (don't crash)
        logger.warning(f"[V13] Unknown strategy_type='{v}' — defaulting to scalp_trend_pullback")
        return "scalp_trend_pullback"

    @field_validator("indicators", mode="before")
    @classmethod
    def coerce_indicators(cls, v):
        """Accept null/missing indicators as empty dict."""
        if v is None or v == "{}" or v == "":
            return {}
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v if isinstance(v, dict) else {}

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v):
        if not v:
            return "BUY"
        return str(v).upper().strip()

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, v):
        if not v:
            raise ValueError("symbol is required")
        return str(v).upper().strip()


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
            symbol=symbol, side=action,
            entry_price=result.get("price", 0),
            fill_price=result.get("price", 0),
            leverage=1, position_size=req.usdt_amount,
            take_profit=0, stop_loss=0, confidence=0,
            strategy_type="scalp_trend_pullback",
            order_method="MARKET",
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
            symbol=req.symbol, side=side,
            entry_price=entry_price,
            fill_price=result.fill_price or entry_price,
            leverage=trade_params.leverage,
            position_size=trade_params.position_size_usdt,
            take_profit=trade_params.take_profit,
            stop_loss=trade_params.stop_loss,
            confidence=req.confidence,
            tp_pct=trade_params.tp_pct,
            sl_pct=trade_params.sl_pct,
            tp_roi_pct=getattr(trade_params, 'tp_roi_pct', 0.0),
            sl_roi_pct=getattr(trade_params, 'sl_roi_pct', 0.0),
            risk_reward=trade_params.risk_reward,
            setup_grade=trade_params.setup_grade,
            strategy_type=getattr(req, 'strategy_type', ''),
            regime=getattr(req, 'regime', ''),
            reason=getattr(req, 'reason', ''),
            order_method=getattr(result, 'order_method', 'MARKET'),
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
# SIGNAL-ONLY ENGINE — AI Signal → Telegram (no Binance execution)
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-multi")
async def execute_multi_account(req: MultiExecuteRequest):
    """
    SIGNAL-ONLY engine (formerly multi-account execution).
    Flow: validate → risk calc → Telegram signal → return.
    No Binance API calls. No account loading. No trade execution.
    Telegram delivery is INDEPENDENT of any execution logic.
    """
    try:
        return await _signal_only_inner(req)
    except Exception as exc:
        logger.error(
            f"[SIGNAL ENGINE] Unhandled exception for {getattr(req, 'symbol', '?')} "
            f"{getattr(req, 'action', '?')} conf={getattr(req, 'confidence', '?')}: {exc}",
            exc_info=True,
        )
        return {
            "status": "error",
            "message": f"Internal error: {type(exc).__name__}: {str(exc)[:200]}",
            "symbol": getattr(req, "symbol", "?"),
        }


async def _signal_only_inner(req: MultiExecuteRequest):
    """
    SIGNAL-ONLY engine — replaces old multi-account execution.
    Flow:
      1. Validate signal (confidence, cooldowns)
      2. Save signal to DB
      3. Calculate TP/SL via RiskEngine (no Binance API needed)
      4. Send Telegram signal IMMEDIATELY
      5. Return structured result

    CRITICAL: Telegram delivery does NOT depend on:
      - Binance execution success
      - Account availability
      - Position placement
      - Balance checks
      - Any external API
    """
    telegram = TelegramNotifier()

    symbol = req.symbol.upper().strip()
    side = req.action.upper().strip()

    if side not in ("BUY", "SELL"):
        return {"status": "error", "message": f"Invalid action: {req.action}"}

    # ── Confidence gates (signal-level, not account-level) ───────────
    _is_swing_mode   = req.strategy_type.startswith("swing")
    _is_sniper_mode  = req.strategy_type.startswith("sniper")
    _is_scalp_mode   = not _is_swing_mode and not _is_sniper_mode

    if _is_scalp_mode and req.confidence < settings.V13_SCALP_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Scalp conf {req.confidence} < V13 min {settings.V13_SCALP_MIN_CONFIDENCE}"}
    elif _is_swing_mode and req.confidence < settings.V13_SWING_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Swing conf {req.confidence} < V13 min {settings.V13_SWING_MIN_CONFIDENCE}"}
    elif _is_sniper_mode and req.confidence < settings.V13_SNIPER_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Sniper conf {req.confidence} < V13 min {settings.V13_SNIPER_MIN_CONFIDENCE}"}

    # ── Signal-level rate limits (no Binance dependency) ─────────────
    hourly_limited, _ = state_manager.is_hourly_limit_reached()
    if hourly_limited:
        return {"status": "skipped", "reason": "Hourly signal limit reached"}

    daily_check = state_manager.check_daily_limits(0)
    if not daily_check["allowed"]:
        return {"status": "skipped", "reason": daily_check["reason"]}

    on_cooldown, _ = state_manager.is_coin_on_cooldown(symbol)
    if on_cooldown:
        return {"status": "skipped", "reason": f"Cooldown active for {symbol}"}

    post_cd_active, post_cd_remaining = state_manager.is_post_close_cooldown_active(symbol)
    if post_cd_active:
        return {"status": "skipped", "reason": f"Post-close cooldown for {symbol} — {post_cd_remaining // 60}m remaining"}

    # ── Save signal to DB (non-blocking — failure doesn't stop signal) ─
    signal_id = None
    try:
        async with async_session() as session:
            signal = Signal(
                symbol=symbol, side=side, confidence=req.confidence,
                reason=req.reason, indicators_json=req.indicators,
                strategy_type=req.strategy_type, regime=req.regime,
            )
            session.add(signal)
            await session.commit()
            await session.refresh(signal)
            signal_id = signal.id
            logger.info(f"📝 Signal #{signal_id} saved: {symbol} {side} conf={req.confidence}")
    except Exception as e:
        logger.warning(f"Signal DB save failed (non-critical): {e}")

    # ── Calculate TP/SL via RiskEngine (NO Binance API needed) ───────
    risk_engine = RiskEngine()
    entry_price = req.current_price if req.current_price > 0 else 0.0

    # Use a reference balance for TP/SL calculation (signal display only)
    signal_ref_balance = getattr(settings, 'SIGNAL_REF_BALANCE', 100.0)

    leverage = risk_engine.get_leverage(req.confidence, req.strategy_type, req.atr_pct)
    tp_roi_pct, sl_roi_pct = risk_engine.get_tp_sl_roi(req.confidence, strategy_type=req.strategy_type)
    tp_price_pct = risk_engine.roi_to_price_pct(tp_roi_pct, leverage)
    sl_price_pct = risk_engine.roi_to_price_pct(sl_roi_pct, leverage)
    setup_grade = risk_engine.determine_setup_grade(req.confidence, req.indicators.get("volume_spike", False))

    # Calculate TP/SL price levels
    if entry_price > 0:
        if side == "BUY":
            take_profit = round(entry_price * (1 + tp_price_pct), 6)
            stop_loss   = round(entry_price * (1 - sl_price_pct), 6)
        else:
            take_profit = round(entry_price * (1 - tp_price_pct), 6)
            stop_loss   = round(entry_price * (1 + sl_price_pct), 6)

        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        risk_reward = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0
    else:
        take_profit = 0.0
        stop_loss = 0.0
        risk_reward = 0.0

    tp_pct_display = round(tp_price_pct * 100, 2)
    sl_pct_display = round(sl_price_pct * 100, 2)

    logger.info(
        f"📡 SIGNAL GENERATED: {symbol} {side} conf={req.confidence} "
        f"strategy={req.strategy_type} grade={setup_grade} | "
        f"entry={entry_price} TP={take_profit} SL={stop_loss} "
        f"lev={leverage}x RR={risk_reward} regime={req.regime}"
    )

    # ── SEND TELEGRAM SIGNAL IMMEDIATELY — NO EXECUTION DEPENDENCY ───
    try:
        await telegram.send_signal(
            symbol=symbol,
            side=side,
            confidence=req.confidence,
            entry_price=entry_price,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss,
            tp_pct=tp_pct_display,
            sl_pct=sl_pct_display,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            reason=req.reason,
            setup_grade=setup_grade,
            strategy_type=req.strategy_type,
            regime=req.regime,
            risk_reward=risk_reward,
        )
        logger.info(f"✅ Telegram signal sent for {symbol} {side}")
    except Exception as tg_err:
        logger.error(f"❌ Telegram send failed (signal still valid): {tg_err}")

    # Record in state manager
    state_manager.record_trade_opened(symbol)

    return {
        "status": "signal_sent",
        "symbol": symbol,
        "side": side,
        "confidence": req.confidence,
        "strategy_type": req.strategy_type,
        "regime": req.regime,
        "entry_price": entry_price,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "tp_pct": tp_pct_display,
        "sl_pct": sl_pct_display,
        "tp_roi_pct": tp_roi_pct,
        "sl_roi_pct": sl_roi_pct,
        "leverage": leverage,
        "risk_reward": risk_reward,
        "setup_grade": setup_grade,
        "signal_id": signal_id,
        "mode": "signal_only",
    }


# ═══════════════════════════════════════════════════════════════════════
# V14: REGISTER SIGNAL — computes TP/SL, checks dedup, sends Telegram.
#      Called by n8n after signal validation.  Telegram is sent HERE
#      (with full professional formatting) so the n8n message-build node
#      is no longer needed.  Duplicate signals are blocked in-memory.
# ═══════════════════════════════════════════════════════════════════════

class RegisterSignalRequest(BaseModel):
    symbol: str
    action: str  # BUY | SELL
    confidence: Any = 70
    reason: str = ""
    current_price: Any = 0.0
    atr_pct: Any = 0.0
    spread_pct: Any = 0.0
    strategy_type: str = "scalp_trend_pullback"
    regime: str = ""
    indicators: dict = {}

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v):
        try:
            return int(float(str(v))) if v is not None else 70
        except (ValueError, TypeError):
            return 70

    @field_validator("current_price", "atr_pct", "spread_pct", mode="before")
    @classmethod
    def coerce_float(cls, v):
        try:
            return float(str(v)) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    @field_validator("indicators", mode="before")
    @classmethod
    def coerce_indicators(cls, v):
        if v is None or v == "{}" or v == "":
            return {}
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v if isinstance(v, dict) else {}


@router.post("/register-signal")
async def register_signal(req: RegisterSignalRequest):
    """
    V14: Full signal registration + Telegram delivery endpoint.
    Called by n8n workflow AFTER signal validation passes.

    This endpoint:
      1. Checks signal dedup (blocks duplicate broadcasts)
      2. Computes full TP/SL/ROI/leverage via RiskEngine
      3. Sends PROFESSIONAL Telegram signal with all trade data
      4. Saves signal to DB
      5. Records state (cooldowns, daily counts)
      6. Returns full computed data to n8n

    CRITICAL: Signal dedup prevents the same symbol+side+strategy
    from being broadcast multiple times within the cooldown window
    (scalp=45min, swing=6hr) unless confidence improved by 10+ points.
    """
    try:
        symbol = req.symbol.upper().strip()
        side = req.action.upper().strip()

        if side not in ("BUY", "SELL"):
            return {"status": "error", "message": f"Invalid action: {req.action}"}

        logger.info(
            f"📝 [V14 REGISTER] Signal received: {symbol} {side} "
            f"conf={req.confidence} strategy={req.strategy_type} "
            f"regime={req.regime}"
        )

        # ── DEDUP CHECK — block duplicate signal broadcasts ──────────
        is_dup, dup_reason = state_manager.is_signal_duplicate(
            symbol, side, req.strategy_type, req.confidence
        )
        if is_dup:
            logger.info(f"🔇 [V14 DEDUP] {dup_reason}")
            return {
                "status": "duplicate_blocked",
                "message": dup_reason,
                "symbol": symbol,
                "side": side,
            }

        # ── Calculate TP/SL via RiskEngine (NO Binance API needed) ───
        risk_engine = RiskEngine()
        entry_price = req.current_price if req.current_price > 0 else 0.0

        leverage = risk_engine.get_leverage(req.confidence, req.strategy_type, req.atr_pct)
        tp_roi_pct, sl_roi_pct = risk_engine.get_tp_sl_roi(
            req.confidence, strategy_type=req.strategy_type
        )
        tp_price_pct = risk_engine.roi_to_price_pct(tp_roi_pct, leverage)
        sl_price_pct = risk_engine.roi_to_price_pct(sl_roi_pct, leverage)
        setup_grade = risk_engine.determine_setup_grade(
            req.confidence, req.indicators.get("volume_spike", False)
        )

        # Calculate TP/SL price levels
        if entry_price > 0:
            if side == "BUY":
                take_profit = round(entry_price * (1 + tp_price_pct), 6)
                stop_loss   = round(entry_price * (1 - sl_price_pct), 6)
            else:
                take_profit = round(entry_price * (1 - tp_price_pct), 6)
                stop_loss   = round(entry_price * (1 + sl_price_pct), 6)

            sl_distance = abs(entry_price - stop_loss)
            tp_distance = abs(take_profit - entry_price)
            risk_reward = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0
        else:
            take_profit = 0.0
            stop_loss = 0.0
            risk_reward = 0.0

        tp_pct_display = round(tp_price_pct * 100, 2)
        sl_pct_display = round(sl_price_pct * 100, 2)

        logger.info(
            f"📡 [V14] SIGNAL COMPUTED: {symbol} {side} conf={req.confidence} "
            f"strategy={req.strategy_type} grade={setup_grade} | "
            f"entry={entry_price} TP={take_profit} SL={stop_loss} "
            f"lev={leverage}x RR={risk_reward} regime={req.regime}"
        )

        # ── SEND PROFESSIONAL TELEGRAM SIGNAL ────────────────────────
        telegram = TelegramNotifier()
        try:
            await telegram.send_signal(
                symbol=symbol,
                side=side,
                confidence=req.confidence,
                entry_price=entry_price,
                leverage=leverage,
                take_profit=take_profit,
                stop_loss=stop_loss,
                tp_pct=tp_pct_display,
                sl_pct=sl_pct_display,
                tp_roi_pct=tp_roi_pct,
                sl_roi_pct=sl_roi_pct,
                reason=req.reason,
                setup_grade=setup_grade,
                strategy_type=req.strategy_type,
                regime=req.regime,
                risk_reward=risk_reward,
            )
            logger.info(f"✅ [V14] Professional Telegram signal sent for {symbol} {side}")
        except Exception as tg_err:
            logger.error(f"❌ [V14] Telegram send failed: {tg_err}")

        # ── RECORD DEDUP FINGERPRINT — only after successful processing ─
        state_manager.record_signal_sent(symbol, side, req.strategy_type, req.confidence)

        # ── Save signal to DB (non-blocking) ─────────────────────────
        signal_id = None
        try:
            async with async_session() as session:
                signal = Signal(
                    symbol=symbol, side=side, confidence=req.confidence,
                    reason=req.reason, indicators_json=req.indicators,
                    strategy_type=req.strategy_type, regime=req.regime,
                )
                session.add(signal)
                await session.commit()
                await session.refresh(signal)
                signal_id = signal.id
                logger.info(f"📝 Signal #{signal_id} saved to DB")
        except Exception as db_err:
            logger.warning(f"Signal DB save failed (non-critical): {db_err}")

        # ── Record in state manager (cooldowns, daily counts) ────────
        try:
            state_manager.record_trade_opened(symbol)
        except Exception as state_err:
            logger.warning(f"State update failed (non-critical): {state_err}")

        return {
            "status": "signal_sent",
            "symbol": symbol,
            "side": side,
            "confidence": req.confidence,
            "signal_id": signal_id,
            "strategy_type": req.strategy_type,
            "regime": req.regime,
            "entry_price": entry_price,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "tp_pct": tp_pct_display,
            "sl_pct": sl_pct_display,
            "tp_roi_pct": tp_roi_pct,
            "sl_roi_pct": sl_roi_pct,
            "leverage": leverage,
            "risk_reward": risk_reward,
            "setup_grade": setup_grade,
            "mode": "signal_only",
        }

    except Exception as exc:
        logger.error(f"[REGISTER SIGNAL] Error: {exc}", exc_info=True)
        return {
            "status": "error",
            "message": f"Registration error: {str(exc)[:200]}",
            "symbol": getattr(req, "symbol", "?"),
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
    position_id: int = 0          # 0 = null/unknown — will search by symbol+side+account
    symbol: Optional[str] = None  # V12: fallback search fields
    side: Optional[str] = None
    account_id: Optional[int] = None


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
    V12: Binance-first open positions endpoint.

    Truth source = Binance live futures positions (positionAmt != 0).
    DB is treated as a mirror only — auto-synced every call:
      - Binance open  + DB missing  → create DB row  (orphan recovery)
      - Binance closed + DB open    → mark DB closed  (ghost cleanup)
      - Both match                  → update last_price in DB

    Query param:
      type=scalp  → only scalp/sniper positions
      type=swing  → only swing positions
      (omit)      → all positions

    Always returns count=len(positions) based on LIVE Binance data.
    """
    import time as _time
    from datetime import datetime, timezone
    from app.modules.binance_sync import get_binance_live_positions, sync_all_accounts
    from app.modules.crypto_utils import decrypt_api_key

    now = datetime.now(timezone.utc)
    cycle_start = _time.monotonic()

    # ── Helper: normalise strategy_type to trade_mode ─────────────────
    def _trade_mode(strategy: str) -> str:
        """V12: normalise all scalp/sniper variants to 'scalp'."""
        st = (strategy or "").strip().lower()
        if st.startswith(("scalp", "sniper", "breakout", "range_reversal", "trend_pullback", "binance_sync")):
            return "scalp"
        if st.startswith("swing"):
            return "swing"
        # Fallback: default to scalp so nothing is accidentally hidden
        return "scalp"

    try:
        # ── STEP 1: Fetch Binance live positions for ALL accounts ─────
        binance_live: list[dict] = []
        try:
            async with async_session() as session:
                from sqlalchemy import select as _select
                from app.models.user import Account, ApiConnection
                stmt = (
                    _select(Account, ApiConnection)
                    .join(ApiConnection, ApiConnection.account_id == Account.id)
                    .where(Account.is_active == True)
                    .where(Account.bot_enabled == True)
                    .where(ApiConnection.is_active == True)
                )
                acc_result = await session.execute(stmt)
                acc_rows = acc_result.all()

            tasks = []
            acc_ids = []
            for acc, conn in acc_rows:
                if not conn.api_key_encrypted or not conn.api_secret_encrypted:
                    continue
                try:
                    ak = decrypt_api_key(conn.api_key_encrypted)
                    ask = decrypt_api_key(conn.api_secret_encrypted)
                    tasks.append(get_binance_live_positions(ak, ask))
                    acc_ids.append(acc.id)
                except Exception:
                    pass

            # Fallback: master key if no accounts configured
            if not tasks and settings.BINANCE_API_KEY:
                tasks.append(get_binance_live_positions(
                    settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY
                ))
                acc_ids.append(0)

            if tasks:
                raw_results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in raw_results:
                    if isinstance(res, list):
                        binance_live.extend(res)

        except Exception as bnc_err:
            logger.error(f"[positions/open] Binance fetch failed: {bnc_err}")

        binance_symbols = {p.get("symbol"): p for p in binance_live}
        logger.info(
            f"[positions/open] Binance live count={len(binance_live)} | "
            f"symbols={list(binance_symbols.keys())[:10]}"
        )

        # ── STEP 2: Trigger async DB sync (non-blocking background) ─────
        try:
            await sync_all_accounts()
        except Exception as sync_err:
            logger.debug(f"[positions/open] DB sync error (non-critical): {sync_err}")

        # ── STEP 3: Fetch DB open positions ──────────────────────────────
        async with async_session() as session:
            db_result = await session.execute(
                select(OpenPosition).where(OpenPosition.status == "open")
            )
            db_positions = db_result.scalars().all()

        db_symbols = {p.symbol: p for p in db_positions}
        logger.info(
            f"[positions/open] DB open count={len(db_positions)} | "
            f"symbols={list(db_symbols.keys())[:10]}"
        )

        # ── STEP 4: Build merged position list ───────────────────────────
        # Primary: DB rows (now synced). Fill in live price from Binance.
        all_open = []

        for p in db_positions:
            live = binance_symbols.get(p.symbol)
            mode = _trade_mode(p.strategy_type)

            # Use Binance mark price if available
            live_price = float(live.get("markPrice", 0)) if live else 0
            last_price = live_price if live_price > 0 else (p.last_price or p.entry_price or 0)

            logger.debug(
                f"  [TYPE MAP] {p.symbol}: strategy_type={p.strategy_type!r} → trade_mode={mode} "
                f"| binance_live={live is not None}"
            )

            all_open.append({
                "id": p.id,
                "trade_id": p.trade_id,
                "account_id": p.account_id,
                "symbol": p.symbol,
                "side": p.side,
                "entry_price": p.entry_price,
                "quantity": p.quantity,
                "leverage": p.leverage,
                "strategy_type": p.strategy_type or "",
                "trade_mode": mode,
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
                "last_price": last_price,
                "check_count": p.check_count or 0,
                # V12: indicate live Binance confirmation
                "binance_confirmed": live is not None,
                "binance_unrealized_pnl": float(live.get("unrealizedProfit", 0)) if live else None,
                "binance_source": True,
            })

        # ── STEP 5: Add Binance positions missing from DB ─────────────────
        # These survived sync (new positions opened since last sync tick)
        for sym, live in binance_symbols.items():
            if sym not in db_symbols:
                amt = float(live.get("positionAmt", 0))
                side = "BUY" if amt > 0 else "SELL"
                entry = float(live.get("entryPrice", 0))
                mark = float(live.get("markPrice", entry))
                mode = "scalp"  # Default unknown Binance positions to scalp

                logger.warning(
                    f"[positions/open] LIVE-ONLY position {sym} (not in DB yet) "
                    f"side={side} entry={entry} amt={amt} — adding to response"
                )

                all_open.append({
                    "id": None,
                    "trade_id": None,
                    "account_id": None,
                    "symbol": sym,
                    "side": side,
                    "entry_price": entry,
                    "quantity": abs(amt),
                    "leverage": int(live.get("leverage", 1)),
                    "strategy_type": "binance_sync",
                    "trade_mode": mode,
                    "timeframe": "unknown",
                    "confidence": 0,
                    "tp_price": 0,
                    "sl_price": 0,
                    "tp_pct": 0,
                    "sl_pct": 0,
                    "trailing_active": False,
                    "trailing_sl_price": None,
                    "trailing_trigger_pct": 0,
                    "highest_price": mark,
                    "lowest_price": mark,
                    "is_hedge_mode": live.get("positionSide", "BOTH") in ("LONG", "SHORT"),
                    "position_side": live.get("positionSide", "BOTH"),
                    "status": "open",
                    "opened_at": now.isoformat(),
                    "last_price": mark,
                    "check_count": 0,
                    "binance_confirmed": True,
                    "binance_unrealized_pnl": float(live.get("unrealizedProfit", 0)),
                    "binance_source": True,
                })

        # ── STEP 6: Filter by type ────────────────────────────────────────
        if type == "scalp":
            filtered = [p for p in all_open if p["trade_mode"] == "scalp"]
        elif type == "swing":
            filtered = [p for p in all_open if p["trade_mode"] == "swing"]
        else:
            filtered = all_open

        scalp_count = sum(1 for p in all_open if p["trade_mode"] == "scalp")
        swing_count = sum(1 for p in all_open if p["trade_mode"] == "swing")

        elapsed_ms = int((_time.monotonic() - cycle_start) * 1000)

        logger.info(
            f"[positions/open] FINAL: total={len(all_open)} scalp={scalp_count} swing={swing_count} "
            f"returned={len(filtered)} type_filter={type!r} | "
            f"binance_live={len(binance_live)} db_open={len(db_positions)} | {elapsed_ms}ms"
        )

        return {
            "status": "ok",
            "count": len(filtered),
            "total_open": len(all_open),
            "scalp_open": scalp_count,
            "swing_open": swing_count,
            "binance_live_count": len(binance_live),
            "db_open_count": len(db_positions),
            "scalp_limit": settings.MAX_CONCURRENT_SCALP_TRADES,
            "swing_limit": settings.MAX_CONCURRENT_SWING_TRADES,
            "positions": filtered,
            # V12 debug fields
            "type_filter": type,
            "elapsed_ms": elapsed_ms,
            "truth_source": "binance_live",
        }

    except Exception as e:
        logger.error(f"[positions/open] Error: {e}", exc_info=True)
        return {"status": "error", "message": str(e), "count": 0, "positions": []}


# ── POST /positions/lock ─────────────────────────────────────────────

@router.post("/positions/lock")
async def lock_position(req: LockPositionRequest):
    """
    V12: Atomically set position status='closing' to prevent duplicate closes.

    Null-id resilience: if position_id is 0/null, searches DB by symbol+side+account_id.
    If still not found (Binance-only position), creates a minimal recovery row before locking.
    Returns already_locked=true if position is already being closed by
    another process (Python PM or a concurrent n8n execution).
    """
    from datetime import datetime, timezone

    try:
        pos = None

        # ── Path A: direct lookup by position_id ────────────────────────
        if req.position_id and req.position_id > 0:
            async with async_session() as session:
                pos = await session.get(OpenPosition, req.position_id)

        # ── Path B: fallback — search by symbol + side + account_id ─────
        if pos is None and req.symbol and req.side:
            logger.info(
                f"[PM Lock] position_id={req.position_id} missing/null — "
                f"searching by symbol={req.symbol} side={req.side} account={req.account_id}"
            )
            async with async_session() as session:
                stmt = select(OpenPosition).where(
                    OpenPosition.symbol == req.symbol.upper(),
                    OpenPosition.side == req.side.upper(),
                    OpenPosition.status == "open",
                )
                if req.account_id:
                    stmt = stmt.where(OpenPosition.account_id == req.account_id)
                result = await session.execute(stmt)
                pos = result.scalars().first()

        # ── Path C: create minimal recovery row (Binance-only position) ──
        if pos is None and req.symbol and req.side:
            logger.warning(
                f"[PM Lock] No DB row found for {req.symbol}/{req.side} — "
                f"creating minimal recovery row so lock can proceed"
            )
            now = datetime.now(timezone.utc)
            try:
                async with async_session() as session:
                    recovery = OpenPosition(
                        account_id=req.account_id or 0,
                        symbol=req.symbol.upper(),
                        side=req.side.upper(),
                        entry_price=0.0,
                        quantity=0.0,
                        tp_price=0.0,
                        sl_price=0.0,
                        strategy_type="lock_recovery",
                        trade_mode="scalp",
                        status="open",
                        entry_reason="created_by_lock_fallback",
                        opened_at=now,
                    )
                    session.add(recovery)
                    await session.commit()
                    await session.refresh(recovery)
                    pos = recovery
                    logger.info(
                        f"[PM Lock] Recovery row created: id={pos.id} {pos.symbol} {pos.side}"
                    )
            except Exception as create_err:
                logger.error(f"[PM Lock] Recovery row creation failed: {create_err}")
                return {"locked": False, "already_locked": False, "reason": f"Recovery creation failed: {create_err}"}

        if pos is None:
            return {"locked": False, "already_locked": False, "reason": "Position not found (no symbol+side fallback provided)"}

        # ── Lock the found/created row ───────────────────────────────────
        if pos.status != "open":
            return {
                "locked": False,
                "already_locked": pos.status in ("closing", "closed"),
                "reason": f"Position status is already '{pos.status}'",
                "current_status": pos.status,
            }

        async with async_session() as session:
            db_pos = await session.get(OpenPosition, pos.id)
            if db_pos and db_pos.status == "open":
                db_pos.status = "closing"
                await session.commit()
                logger.info(f"[PM Lock] Position {pos.id} ({pos.symbol}) locked → 'closing'")
                return {"locked": True, "already_locked": False, "position_id": pos.id, "symbol": pos.symbol}
            elif db_pos:
                return {
                    "locked": False,
                    "already_locked": db_pos.status in ("closing", "closed"),
                    "reason": f"Position status is already '{db_pos.status}'",
                    "current_status": db_pos.status,
                }
            else:
                return {"locked": False, "already_locked": False, "reason": "Position disappeared during lock"}

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
    V12: Return counts of open scalp and swing positions for concurrent limit checks.
    Truth source = Binance live positions (via count_all_live_positions).
    Falls back to DB if Binance fetch fails.
    """
    binance_live_total = None
    truth_source = "db"

    # ── Primary: Binance live count ───────────────────────────────────
    if settings.BINANCE_TRUTH_SOURCE:
        try:
            binance_live_total = await count_all_live_positions()
            truth_source = "binance_live"
            logger.info(f"[positions/count] Binance live total={binance_live_total}")
        except Exception as bnc_err:
            logger.warning(f"[positions/count] Binance fetch failed — DB fallback: {bnc_err}")

    # ── Fallback / detailed breakdown: DB ────────────────────────────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(OpenPosition).where(OpenPosition.status.in_(["open", "closing"]))
            )
            positions = result.scalars().all()

        scalp = sum(
            1 for p in positions
            if (p.trade_mode or (p.strategy_type or "")).startswith(("scalp", "sniper", "breakout", "range", "trend"))
        )
        swing = sum(
            1 for p in positions
            if (p.trade_mode == "swing") or (p.trade_mode is None and (p.strategy_type or "").startswith("swing"))
        )
        db_total = len(positions)

        # If Binance live total available, use it as the authoritative total
        total = binance_live_total if binance_live_total is not None else db_total

        return {
            "scalp_open": scalp,
            "swing_open": swing,
            "total_open": total,
            "db_open_count": db_total,
            "binance_live_count": binance_live_total,
            "truth_source": truth_source,
            "scalp_limit": settings.MAX_CONCURRENT_SCALP_TRADES,
            "swing_limit": settings.MAX_CONCURRENT_SWING_TRADES,
            "scalp_slots_available": max(0, settings.MAX_CONCURRENT_SCALP_TRADES - scalp),
            "swing_slots_available": max(0, settings.MAX_CONCURRENT_SWING_TRADES - swing),
        }
    except Exception as e:
        logger.error(f"[positions/count] Error: {e}")
        return {
            "scalp_open": 0, "swing_open": 0, "total_open": binance_live_total or 0,
            "binance_live_count": binance_live_total, "truth_source": truth_source,
            "error": str(e),
        }
