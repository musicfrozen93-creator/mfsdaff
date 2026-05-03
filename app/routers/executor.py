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
from datetime import datetime, timezone

# ── V15: Pending Signal Store (in-memory) ───────────────────────────────
# Keyed by symbol. Signals expire after SCALP_TIMEOUT_MINUTES or SWING_TIMEOUT_MINUTES.
# Each entry: { symbol, side, signal_price, strategy_type, confidence,
#               tp_pct, sl_pct, stored_at, indicators, regime, reason, atr_pct }
_pending_signals: dict[str, dict] = {}

# V15: Fixed SL/TP percentages (price-based, not ROI-based)
_SCALP_TP_PCT  = 15.0   # +15% from entry
_SCALP_SL_PCT  =  5.0   # -5% from entry
_SWING_TP_PCT  = 40.0   # +40% from entry
_SWING_SL_PCT  = 30.0   # -30% from entry

# V15: Pullback ranges for entry zone display
_SCALP_PULLBACK_MIN = 2.0   # 2% min pullback for scalp
_SCALP_PULLBACK_MAX = 5.0   # 5% max pullback for scalp
_SWING_PULLBACK_MIN = 5.0   # 5% min pullback for swing
_SWING_PULLBACK_MAX = 12.0  # 12% max pullback for swing

# V15: Timeout windows
_SCALP_TIMEOUT_MINUTES = 15
_SWING_TIMEOUT_MINUTES = 240  # 4 hours
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
    # V15: when True, skip Phase-1 signal Telegram (n8n already sent it)
    skip_signal_telegram: bool = False

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
# V4 MULTI-ACCOUNT EXECUTE — Hardened account loop, single Telegram msg
# ═══════════════════════════════════════════════════════════════════════

@router.post("/execute-multi")
async def execute_multi_account(req: MultiExecuteRequest):
    """
    V13 Multi-account execution flow.
    Fully guarded: all uncaught exceptions return structured JSON, never 500.
    """
    try:
        return await _execute_multi_inner(req)
    except Exception as exc:
        logger.error(
            f"[V13 EXECUTE-MULTI] Unhandled exception for {getattr(req, 'symbol', '?')} "
            f"{getattr(req, 'action', '?')} conf={getattr(req, 'confidence', '?')}: {exc}",
            exc_info=True,
        )
        return {
            "status": "error",
            "message": f"Internal error: {type(exc).__name__}: {str(exc)[:200]}",
            "symbol": getattr(req, "symbol", "?"),
        }


async def _execute_multi_inner(req: MultiExecuteRequest):
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

    # V13: Per-mode confidence gates (replaces single MIN_CONFIDENCE)
    _is_swing_mode   = req.strategy_type.startswith("swing")
    _is_sniper_mode  = req.strategy_type.startswith("sniper")
    _is_scalp_mode   = not _is_swing_mode and not _is_sniper_mode

    if _is_scalp_mode and req.confidence < settings.V13_SCALP_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Scalp conf {req.confidence} < V13 min {settings.V13_SCALP_MIN_CONFIDENCE}"}
    elif _is_swing_mode and req.confidence < settings.V13_SWING_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Swing conf {req.confidence} < V13 min {settings.V13_SWING_MIN_CONFIDENCE}"}
    elif _is_sniper_mode and req.confidence < settings.V13_SNIPER_MIN_CONFIDENCE:
        return {"status": "skipped", "reason": f"Sniper conf {req.confidence} < V13 min {settings.V13_SNIPER_MIN_CONFIDENCE}"}

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

    # V12: Concurrent position check — uses BINANCE LIVE POSITIONS as truth source.
    # DB open_positions is NOT used to gate entries (stale counts cause freezes).
    # MAX_CONCURRENT_SCALP_TRADES and MAX_CONCURRENT_SWING_TRADES are set to 99999
    # (effectively unlimited). This block only runs same-symbol check.
    is_scalp = req.strategy_type.startswith(("scalp", "sniper"))
    live_count = 0
    live_symbol_count = 0

    if settings.BINANCE_TRUTH_SOURCE:
        try:
            # Fetch live count from Binance (all accounts combined)
            live_count = await count_all_live_positions()
            logger.info(
                f"[V12 LIVE COUNT] {symbol}: Binance live positions = {live_count} | "
                f"limits: scalp={settings.MAX_CONCURRENT_SCALP_TRADES} "
                f"swing={settings.MAX_CONCURRENT_SWING_TRADES} "
                f"global={settings.MAX_OPEN_POSITIONS} (all 99999=unlimited)"
            )

            # Same-symbol live check (still enforce — prevent stacking same coin)
            # Use first available account credentials for symbol check
            async with async_session() as session:
                stmt = (
                    select(Account, ApiConnection)
                    .join(ApiConnection, ApiConnection.account_id == Account.id)
                    .where(Account.is_active == True)
                    .where(Account.bot_enabled == True)
                    .where(ApiConnection.is_active == True)
                )
                result = await session.execute(stmt)
                rows = result.all()

            for acc_row, conn_row in rows[:1]:  # Check first account only
                if conn_row.api_key_encrypted and conn_row.api_secret_encrypted:
                    try:
                        _ak = decrypt_api_key(conn_row.api_key_encrypted)
                        _ask = decrypt_api_key(conn_row.api_secret_encrypted)
                        live_positions = await get_binance_live_positions(_ak, _ask)
                        live_symbol_count = sum(1 for p in live_positions if p.get("symbol") == symbol)
                    except Exception:
                        live_symbol_count = 0
                    break

            if live_symbol_count >= settings.MAX_SAME_SYMBOL_OPEN:
                logger.info(
                    f"[V12 GATE] {symbol} BLOCKED — live same-symbol positions: "
                    f"{live_symbol_count}/{settings.MAX_SAME_SYMBOL_OPEN}"
                )
                return {
                    "status": "skipped",
                    "reason": f"V12: {symbol} already has {live_symbol_count} live Binance position(s)",
                }

        except Exception as live_err:
            logger.warning(
                f"[V12] Binance live count failed — falling back to DB count: {live_err}"
            )
            # Fallback to DB count if Binance fetch fails
            try:
                async with async_session() as session:
                    result = await session.execute(
                        select(OpenPosition).where(OpenPosition.status.in_(["open", "closing"]))
                    )
                    open_positions = result.scalars().all()
                same_symbol_open = [p for p in open_positions if p.symbol == symbol]
                if len(same_symbol_open) >= settings.MAX_SAME_SYMBOL_OPEN:
                    return {
                        "status": "skipped",
                        "reason": f"[DB fallback] {symbol} already has {len(same_symbol_open)} open position(s)",
                    }
            except Exception:
                pass  # Non-critical — don't block on fallback failure
    else:
        # BINANCE_TRUTH_SOURCE=false: use DB count (legacy behavior)
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.status.in_(["open", "closing"]))
                )
                open_positions = result.scalars().all()
            same_symbol_open = [p for p in open_positions if p.symbol == symbol]
            if len(same_symbol_open) >= settings.MAX_SAME_SYMBOL_OPEN:
                return {
                    "status": "skipped",
                    "reason": f"[DB] {symbol} already has {len(same_symbol_open)} open position(s)",
                }
        except Exception as db_err:
            logger.warning(f"DB concurrent check failed: {db_err}")

    # V11: Global daily P&L entry gate
    v11_gate = state_manager.check_v11_entry_gate()
    if not v11_gate["allowed"]:
        logger.info(f"[V11 GATE] {symbol} BLOCKED — daily P&L gate: {v11_gate['reason']}")
        return {"status": "skipped", "reason": v11_gate["reason"]}

    # V12: Verbose gate pass log
    logger.info(
        f"[V12 GATE PASS] {symbol} {side} conf={req.confidence} strategy={req.strategy_type} | "
        f"Binance live count={live_count} | same-symbol live={live_symbol_count} | "
        f"daily P&L gate: OK | truth_source={'BINANCE' if settings.BINANCE_TRUTH_SOURCE else 'DB'}"
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
    # V13: ROI-based TP calc via get_tp_sl_roi() + roi_to_price_pct()
    try:
        tp_roi_pct, _sl_roi_pct = risk_engine.get_tp_sl_roi(
            req.confidence, strategy_type=req.strategy_type
        )
        leverage_for_check = risk_engine.get_leverage(
            req.confidence, req.strategy_type, req.atr_pct
        )
        # Convert ROI% → price% for the pre_entry_check spread/slippage calculation
        tp_pct_for_check = risk_engine.roi_to_price_pct(tp_roi_pct, leverage_for_check) * 100
    except Exception as re_err:
        logger.warning(f"  [V13] Risk engine pre-calc failed: {re_err} — using 1.0% TP default")
        tp_pct_for_check = 1.0

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

    # ── V15 Phase 1: Send signal alert NOW — before any account execution ──
    # Fires for every valid setup that passes all gates above.
    # V15: skip if n8n already sent signal (skip_signal_telegram=True) to avoid duplicates.
    # Failsafe: wrapped in try/except so Telegram failure never blocks execution.
    _signal_lev_suggestion = ""
    _signal_grade = req.indicators.get("setup_grade", "C")
    _is_swing_signal = req.strategy_type.lower().startswith("swing")

    # V15: Fixed SL/TP for signal display
    _sig_tp_pct = _SWING_TP_PCT if _is_swing_signal else _SCALP_TP_PCT
    _sig_sl_pct = _SWING_SL_PCT if _is_swing_signal else _SCALP_SL_PCT
    _sig_pullback_max = _SWING_PULLBACK_MAX if _is_swing_signal else _SCALP_PULLBACK_MAX

    # V15: Calculate entry zone (signal_price ± pullback range)
    _zone_low  = entry_price * (1 - _sig_pullback_max / 100) if side == "BUY" else entry_price
    _zone_high = entry_price if side == "BUY" else entry_price * (1 + _sig_pullback_max / 100)

    # V15: Calculate TP/SL absolute prices for signal display
    if side == "BUY":
        _signal_tp = entry_price * (1 + _sig_tp_pct / 100)
        _signal_sl = entry_price * (1 - _sig_sl_pct / 100)
    else:
        _signal_tp = entry_price * (1 - _sig_tp_pct / 100)
        _signal_sl = entry_price * (1 + _sig_sl_pct / 100)

    # Leverage suggestion via risk engine (non-critical)
    try:
        _sig_lev = risk_engine.get_leverage(req.confidence, req.strategy_type, req.atr_pct)
        _signal_lev_suggestion = f"{max(1, _sig_lev - 2)}x–{_sig_lev}x"
        _signal_grade = req.indicators.get("setup_grade", "C")
    except Exception as _sig_err:
        logger.warning(f"[V15] Signal leverage estimation failed (non-critical): {_sig_err}")

    if not req.skip_signal_telegram:
        try:
            await telegram.send_signal_detected(
                symbol=symbol,
                side=side,
                confidence=req.confidence,
                strategy_type=req.strategy_type,
                regime=req.regime,
                setup_grade=_signal_grade,
                entry_price=entry_price,
                take_profit=_signal_tp,
                stop_loss=_signal_sl,
                tp_pct=_sig_tp_pct,
                sl_pct=_sig_sl_pct,
                tp_roi_pct=0.0,
                sl_roi_pct=0.0,
                leverage_suggestion=_signal_lev_suggestion,
                reason=req.reason,
                entry_zone_low=_zone_low,
                entry_zone_high=_zone_high,
                pullback_monitoring=True,
            )
            logger.info(f"[V15 SIGNAL] 📡 Signal alert sent for {symbol} {side} conf={req.confidence}")
        except Exception as _tg_err:
            logger.error(f"[V15 SIGNAL] Phase 1 Telegram failed (non-critical, execution continues): {_tg_err}")
    else:
        logger.info(f"[V15 SIGNAL] skip_signal_telegram=True — signal already sent by n8n for {symbol} {side}")

    # ── Execute for each account (hardened loop) ─────────────────────
    executed = []
    skipped = []
    skip_reasons_map = {}

    # V13: add margin_pct + ROI targets to Telegram
    best_fill_price = 0.0
    best_leverage = 0
    best_tp = 0.0
    best_sl = 0.0
    best_tp_pct = 0.0
    best_sl_pct = 0.0
    best_tp_roi_pct = 0.0
    best_sl_roi_pct = 0.0
    best_margin_pct = 0.0
    best_margin_usdt = 0.0   # V13 Part H
    best_balance = 0.0       # V13 Part H
    best_grade = "C"
    best_order_method = "MARKET"
    best_rr = 0.0

    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent account executions

    async def execute_for_account(acc_data: dict):
        nonlocal best_fill_price, best_leverage, best_tp, best_sl
        nonlocal best_tp_pct, best_sl_pct, best_grade, best_order_method, best_rr
        nonlocal best_tp_roi_pct, best_sl_roi_pct, best_margin_pct, best_margin_usdt, best_balance

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

                if balance < settings.V13_MIN_TRADE_BALANCE:
                    logger.info(f"  [{internal_label}] Balance ${balance:.2f} below V13 min ${settings.V13_MIN_TRADE_BALANCE} → SKIP")
                    return _skip(acc_id, "Insufficient Balance", f"Balance ${balance:.2f} below V13 minimum ${settings.V13_MIN_TRADE_BALANCE}")


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

                # ── 5.5 V15: Override SL/TP with fixed percentages ─────
                # Preserves risk engine sizing/leverage; only replaces TP/SL prices.
                _is_swing_trade = req.strategy_type.lower().startswith("swing")
                _tp_pct_fixed = _SWING_TP_PCT if _is_swing_trade else _SCALP_TP_PCT
                _sl_pct_fixed = _SWING_SL_PCT if _is_swing_trade else _SCALP_SL_PCT

                if trade_params.approved:
                    try:
                        _ep = result.fill_price if hasattr(result, 'fill_price') and result.fill_price else entry_price
                        if side == "BUY":
                            trade_params.take_profit = round(
                                entry_price * (1 + _tp_pct_fixed / 100), precision.price_precision
                            )
                            trade_params.stop_loss = round(
                                entry_price * (1 - _sl_pct_fixed / 100), precision.price_precision
                            )
                        else:  # SELL
                            trade_params.take_profit = round(
                                entry_price * (1 - _tp_pct_fixed / 100), precision.price_precision
                            )
                            trade_params.stop_loss = round(
                                entry_price * (1 + _sl_pct_fixed / 100), precision.price_precision
                            )
                        trade_params.tp_pct = _tp_pct_fixed
                        trade_params.sl_pct = _sl_pct_fixed
                        logger.info(
                            f"  [V15 SL/TP] {internal_label}: {'swing' if _is_swing_trade else 'scalp'} "
                            f"TP={_tp_pct_fixed}% → ${trade_params.take_profit} | "
                            f"SL={_sl_pct_fixed}% → ${trade_params.stop_loss}"
                        )
                    except Exception as _tpsl_err:
                        logger.warning(f"  [V15 SL/TP] Override failed (non-critical): {_tpsl_err}")

                # ── 5.5 V13: V7_MAX_LEVERAGE cap removed — V13 tiers handle leverage ──
                # Previously capped at V7_MAX_LEVERAGE=7, now up to 15x by confidence tier
                # (ATR volatility dampener in risk_engine handles any excess)

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
                    # V12: DB save with 3-attempt retry + urgent Telegram on permanent failure
                    trade_db_id = None
                    _save_success = False
                    for _attempt in range(1, 4):
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
                                    sl_order_id=None,
                                    tp_order_id=None,
                                    status="open",
                                    strategy_type=req.strategy_type,
                                    regime=req.regime,
                                    protection_status="PENDING",
                                    virtual_sl=trade_params.stop_loss,
                                    virtual_tp=trade_params.take_profit,
                                    managed_by="external_engine",
                                )
                                session.add(trade)
                                await session.commit()
                                await session.refresh(trade)
                                trade_db_id = trade.id

                                # V12: Explicit trade_mode tag at creation time
                                _is_scalp = req.strategy_type.lower().startswith(("scalp", "sniper", "breakout", "range", "trend"))
                                _trade_mode = "scalp" if _is_scalp else "swing"

                                # V9: Write OpenPosition for Position Manager
                                _is_hedge = result.is_hedge_mode
                                _pos_side = result.position_side
                                _timeframe = (
                                    "4h" if req.strategy_type.startswith("swing")
                                    else ("15m" if req.strategy_type.startswith("sniper") else "1m")
                                )
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
                                    trade_mode=_trade_mode,   # V12: explicit tag
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
                                    entry_reason=req.reason[:500] if req.reason else None,
                                )
                                session.add(open_pos)
                                await session.commit()
                                _save_success = True
                                logger.info(
                                    f"  📌 [{internal_label}] OpenPosition saved (attempt {_attempt}): "
                                    f"{symbol} {side} mode={_trade_mode} TP={trade_params.take_profit} "
                                    f"SL={trade_params.stop_loss} strategy={req.strategy_type}"
                                )
                                break  # Success — exit retry loop

                        except Exception as dbe:
                            logger.warning(
                                f"  [{internal_label}] DB save attempt {_attempt}/3 failed: {dbe}"
                            )
                            if _attempt < 3:
                                await asyncio.sleep(1.0)

                    if not _save_success:
                        logger.error(
                            f"  [{internal_label}] PERMANENT DB SAVE FAILURE after 3 attempts "
                            f"for {symbol} — trade IS open on Binance but not recorded in DB!"
                        )
                        # Send urgent Telegram alert (non-blocking)
                        try:
                            await telegram.send_message(
                                f"🚨 CRITICAL: DB SAVE FAILED for {symbol} {side}\n"
                                f"Trade IS OPEN on Binance (order #{result.order_id}) "
                                f"but NOT recorded in database after 3 attempts.\n"
                                f"Manual sync required — run Binance sync immediately!",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

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
                    # V13: ROI % and margin %
                    best_tp_roi_pct  = getattr(trade_params, 'tp_roi_pct', 0.0)
                    best_sl_roi_pct  = getattr(trade_params, 'sl_roi_pct', 0.0)
                    best_margin_pct  = getattr(trade_params, 'margin_pct', 0.0)
                    best_margin_usdt = getattr(trade_params, 'safe_margin', 0.0)  # V13 Part H
                    best_balance     = balance  # V13 Part H


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
    # V14: PHASE 2 — Post-execution Telegram (signal already sent above)
    # ═════════════════════════════════════════════════════════════════

    executed_count = len(executed)
    skipped_count = len(skipped)

    if executed_count > 0:
        # Phase 2a: At least one account filled — send short follow-up.
        # Signal message was already sent in Phase 1 above.
        display_fill = best_fill_price if best_fill_price > 0 else entry_price
        try:
            await telegram.send_execution_followup(
                symbol=symbol,
                side=side,
                executed_count=executed_count,
                skipped_count=skipped_count,
                fill_price=display_fill,
                entry_price=entry_price,
                leverage=best_leverage,
                take_profit=best_tp,
                stop_loss=best_sl,
                strategy_type=req.strategy_type,
            )
            logger.info(
                f"[V14 EXEC] ✅ Execution follow-up sent: {symbol} {side} "
                f"filled={executed_count} skipped={skipped_count}"
            )
        except Exception as _tg_err2:
            logger.error(f"[V14 EXEC] Phase 2 follow-up Telegram failed: {_tg_err2}")

    elif skipped_count > 0:
        # Phase 2b: All accounts skipped — update signal with no-execution note.
        # Signal message was already sent in Phase 1; this clarifies the outcome.
        try:
            await telegram.send_no_execution_signal(
                symbol=symbol,
                side=side,
                confidence=req.confidence,
                skip_reasons=skip_reasons_map,
                strategy_type=req.strategy_type,
            )
            logger.info(
                f"[V14 SKIP] 📡 Signal no-execution update sent: {symbol} {side} "
                f"skipped={skipped_count} reasons={skip_reasons_map}"
            )
        except Exception as _tg_err3:
            logger.error(f"[V14 SKIP] Phase 2 no-execution Telegram failed: {_tg_err3}")
    # else: No accounts found at all — signal already sent in Phase 1, no follow-up needed

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


# ═══════════════════════════════════════════════════════════════════════
# V15: PENDING SIGNAL STORE — Pullback Entry System
# ═══════════════════════════════════════════════════════════════════════

class SignalStoreRequest(BaseModel):
    symbol: str
    side: str              # BUY | SELL
    signal_price: float
    strategy_type: str = "scalp_trend_pullback"
    confidence: int = 70
    reason: str = ""
    regime: str = ""
    atr_pct: float = 0.0
    indicators: dict = {}
    send_telegram: bool = True   # if False, just store (no Telegram)

    @field_validator("symbol", mode="before")
    @classmethod
    def _norm_sym(cls, v): return str(v).upper().strip() if v else "UNKNOWN"

    @field_validator("side", mode="before")
    @classmethod
    def _norm_side(cls, v): return str(v).upper().strip() if v else "BUY"

    @field_validator("strategy_type", mode="before")
    @classmethod
    def _norm_strat(cls, v):
        if not v or not isinstance(v, str):
            return "scalp_trend_pullback"
        v = v.strip().lower().replace(" ", "_")
        # Map bare words to canonical form
        if v in ("scalp", "scalping"):
            return "scalp_trend_pullback"
        if v in ("swing", "swinging"):
            return "swing_trend_continuation"
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_conf(cls, v):
        try: return int(float(str(v)))
        except: return 70

    @field_validator("signal_price", "atr_pct", mode="before")
    @classmethod
    def _norm_float(cls, v):
        try: return float(str(v))
        except: return 0.0

    @field_validator("indicators", mode="before")
    @classmethod
    def _norm_ind(cls, v):
        if not v: return {}
        if isinstance(v, str):
            import json
            try: return json.loads(v)
            except: return {}
        return v if isinstance(v, dict) else {}


@router.post("/signals/store")
async def store_pending_signal(req: SignalStoreRequest):
    """
    V15: Store a pending signal in the in-memory pullback queue.
    Optionally sends the SIGNAL DETECTED Telegram alert immediately.
    Called by n8n BEFORE the pullback monitoring loop.

    Timeout: scalp = 15 min, swing = 4 hours.
    Only one pending signal per symbol is kept (newer overwrites older).
    """
    telegram = TelegramNotifier()
    _is_swing = req.strategy_type.lower().startswith("swing")

    tp_pct  = _SWING_TP_PCT  if _is_swing else _SCALP_TP_PCT
    sl_pct  = _SWING_SL_PCT  if _is_swing else _SCALP_SL_PCT
    pb_max  = _SWING_PULLBACK_MAX if _is_swing else _SCALP_PULLBACK_MAX
    timeout = _SWING_TIMEOUT_MINUTES if _is_swing else _SCALP_TIMEOUT_MINUTES

    now = datetime.now(timezone.utc)

    # Compute entry zone
    if req.side == "BUY":
        zone_low  = req.signal_price * (1 - pb_max / 100)
        zone_high = req.signal_price
        tp_price  = req.signal_price * (1 + tp_pct / 100)
        sl_price  = req.signal_price * (1 - sl_pct / 100)
    else:
        zone_low  = req.signal_price
        zone_high = req.signal_price * (1 + pb_max / 100)
        tp_price  = req.signal_price * (1 - tp_pct / 100)
        sl_price  = req.signal_price * (1 + sl_pct / 100)

    signal_data = {
        "symbol":        req.symbol,
        "side":          req.side,
        "signal_price":  req.signal_price,
        "strategy_type": req.strategy_type,
        "confidence":    req.confidence,
        "reason":        req.reason,
        "regime":        req.regime,
        "atr_pct":       req.atr_pct,
        "indicators":    req.indicators,
        "tp_pct":        tp_pct,
        "sl_pct":        sl_pct,
        "tp_price":      tp_price,
        "sl_price":      sl_price,
        "zone_low":      zone_low,
        "zone_high":     zone_high,
        "timeout_minutes": timeout,
        "stored_at":     now.isoformat(),
        "is_swing":      _is_swing,
    }

    _pending_signals[req.symbol] = signal_data
    logger.info(
        f"[V15 STORE] Signal stored: {req.symbol} {req.side} "
        f"{'swing' if _is_swing else 'scalp'} conf={req.confidence} "
        f"price={req.signal_price} zone={zone_low:.6f}–{zone_high:.6f} "
        f"timeout={timeout}m"
    )

    # Send Telegram signal alert
    if req.send_telegram:
        try:
            await telegram.send_signal_detected(
                symbol=req.symbol,
                side=req.side,
                confidence=req.confidence,
                strategy_type=req.strategy_type,
                regime=req.regime,
                setup_grade=req.indicators.get("setup_grade", "C"),
                entry_price=req.signal_price,
                take_profit=tp_price,
                stop_loss=sl_price,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                tp_roi_pct=0.0,
                sl_roi_pct=0.0,
                leverage_suggestion="",
                reason=req.reason,
                entry_zone_low=zone_low,
                entry_zone_high=zone_high,
                pullback_monitoring=True,
            )
            logger.info(f"[V15 STORE] 📡 Signal Telegram sent for {req.symbol}")
        except Exception as tg_err:
            logger.error(f"[V15 STORE] Telegram failed (non-critical): {tg_err}")

    return {
        "status": "stored",
        "symbol": req.symbol,
        "side": req.side,
        "signal_price": req.signal_price,
        "strategy_type": req.strategy_type,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "timeout_minutes": timeout,
        "expires_at": (now.replace(microsecond=0).isoformat()),
    }


@router.get("/signals/pending")
async def get_pending_signals(type: Optional[str] = None):
    """
    V15: Return all non-expired pending signals.
    Called by n8n each scan cycle to check for pullback entry opportunities.

    Query param:
      type=scalp  → only scalp/sniper pending signals
      type=swing  → only swing pending signals
      (omit)      → all

    Also prunes expired signals from the store.
    """
    now = datetime.now(timezone.utc)
    active = []
    expired_keys = []

    for sym, sig in list(_pending_signals.items()):
        try:
            stored_at = datetime.fromisoformat(sig["stored_at"])
            if stored_at.tzinfo is None:
                stored_at = stored_at.replace(tzinfo=timezone.utc)
            elapsed_minutes = (now - stored_at).total_seconds() / 60
            timeout = sig.get("timeout_minutes", _SCALP_TIMEOUT_MINUTES)

            if elapsed_minutes > timeout:
                expired_keys.append(sym)
                logger.info(
                    f"[V15 PENDING] Signal {sym} expired ({elapsed_minutes:.1f}m > {timeout}m)"
                )
                continue

            sig_out = {**sig, "elapsed_minutes": round(elapsed_minutes, 1)}

            if type == "scalp" and sig.get("is_swing"):
                continue
            elif type == "swing" and not sig.get("is_swing"):
                continue

            active.append(sig_out)
        except Exception as parse_err:
            logger.warning(f"[V15 PENDING] Error reading signal {sym}: {parse_err}")
            expired_keys.append(sym)

    # Auto-prune expired
    for k in expired_keys:
        _pending_signals.pop(k, None)

    return {
        "status": "ok",
        "count": len(active),
        "signals": active,
        "type_filter": type,
    }


@router.delete("/signals/cancel/{symbol}")
async def cancel_pending_signal(symbol: str, reason: str = "manual"):
    """
    V15: Remove a pending signal (anti-chase, timeout, or manual).
    Called by n8n when anti-chase rule triggers or timeout window closes.
    """
    sym = symbol.upper().strip()
    sig = _pending_signals.pop(sym, None)
    if sig:
        logger.info(f"[V15 CANCEL] Signal {sym} removed. reason={reason}")
        return {"status": "cancelled", "symbol": sym, "reason": reason}
    return {"status": "not_found", "symbol": sym}


# ─── GET /price/{symbol} ─────────────────────────────────────────────

@router.get("/price/{symbol}")
async def get_symbol_price(symbol: str):
    """
    V15: Return current Binance mark price for a symbol.
    Used by n8n pullback monitor to check entry conditions each loop iteration.
    """
    sym = symbol.upper().strip()
    try:
        executor_inst = BinanceExecutor()
        price = await executor_inst.get_market_price(sym)
        return {"status": "ok", "symbol": sym, "price": price}
    except Exception as e:
        logger.error(f"[V15 PRICE] Failed to get price for {sym}: {e}")
        return {"status": "error", "symbol": sym, "price": 0.0, "error": str(e)}
