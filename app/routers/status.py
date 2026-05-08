"""
V5 Status Dashboard API — comprehensive trading + strategy statistics
"""

import logging
from datetime import datetime, date, timedelta
from fastapi import APIRouter
from sqlalchemy import select, func, and_

from app.utils.state import state_manager
from app.database import async_session
from app.models.trading import Trade, Signal, TradeSkip
from app.models.user import Account
from app.modules.executor import BinanceExecutor
from app.modules.strategy_tracker import strategy_tracker
from app.modules.market_regime import MarketRegimeRouter

router = APIRouter()
logger = logging.getLogger(__name__)

# Track bot start time
_bot_start_time = datetime.utcnow()


@router.get("/status")
async def get_status():
    """
    Full dashboard status:
    - Trading stats (today, week)
    - Win rate, P&L
    - Active accounts
    - AI usage
    - Open positions
    - System uptime
    """
    stats = state_manager.get_stats()

    # ── DB stats ─────────────────────────────────────────────────────
    db_stats = {}
    try:
        async with async_session() as session:
            today = date.today()
            week_ago = today - timedelta(days=7)

            # Trades today
            today_result = await session.execute(
                select(func.count(Trade.id)).where(
                    func.date(Trade.created_at) == today
                )
            )
            db_stats["total_trades_today"] = today_result.scalar() or 0

            # Trades this week
            week_result = await session.execute(
                select(func.count(Trade.id)).where(
                    Trade.created_at >= datetime.combine(week_ago, datetime.min.time())
                )
            )
            db_stats["total_trades_week"] = week_result.scalar() or 0

            # P&L today
            pnl_today_result = await session.execute(
                select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                    and_(func.date(Trade.created_at) == today, Trade.pnl.isnot(None))
                )
            )
            db_stats["pnl_today"] = round(float(pnl_today_result.scalar() or 0), 2)

            # P&L this week
            pnl_week_result = await session.execute(
                select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                    and_(
                        Trade.created_at >= datetime.combine(week_ago, datetime.min.time()),
                        Trade.pnl.isnot(None),
                    )
                )
            )
            db_stats["pnl_week"] = round(float(pnl_week_result.scalar() or 0), 2)

            # Win rate today
            wins_today = await session.execute(
                select(func.count(Trade.id)).where(
                    and_(func.date(Trade.created_at) == today, Trade.pnl > 0)
                )
            )
            wins = wins_today.scalar() or 0
            total_today = db_stats["total_trades_today"]
            db_stats["win_rate_today"] = round(wins / total_today * 100, 1) if total_today > 0 else 0

            # Active accounts
            active_accounts = await session.execute(
                select(func.count(Account.id)).where(Account.is_active == True)
            )
            db_stats["active_accounts"] = active_accounts.scalar() or 0

            # Skipped trades today
            skips_today = await session.execute(
                select(func.count(TradeSkip.id)).where(
                    func.date(TradeSkip.created_at) == today
                )
            )
            db_stats["skipped_trades_today"] = skips_today.scalar() or 0

            # AI usage today
            ai_today = await session.execute(
                select(func.count(Signal.id)).where(
                    and_(func.date(Signal.created_at) == today, Signal.ai_called == True)
                )
            )
            db_stats["ai_usage_today"] = ai_today.scalar() or 0

            # Open positions count
            open_trades = await session.execute(
                select(func.count(Trade.id)).where(Trade.status == "open")
            )
            db_stats["open_positions_count"] = open_trades.scalar() or 0

    except Exception as e:
        logger.warning(f"DB stats query failed: {e}")

    # ── Open positions from Binance ──────────────────────────────────
    open_positions = []
    try:
        executor = BinanceExecutor()
        positions = await executor.get_open_positions()
        for p in positions:
            open_positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if float(p["positionAmt"]) > 0 else "SHORT",
                "quantity": abs(float(p["positionAmt"])),
                "entry_price": float(p["entryPrice"]),
                "unrealized_pnl": round(float(p["unRealizedProfit"]), 4),
                "leverage": int(p["leverage"]),
            })
    except Exception as e:
        logger.warning(f"Position fetch failed: {e}")

    # ── System uptime ────────────────────────────────────────────────
    uptime = datetime.utcnow() - _bot_start_time
    days = uptime.days
    hours, rem = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    # ── V5: Strategy performance stats ───────────────────────────────
    strategy_stats = {}
    try:
        strategy_stats = await strategy_tracker.get_strategy_stats(days=30)
    except Exception as e:
        logger.warning(f"Strategy stats failed: {e}")

    # ── V5: Current regime ────────────────────────────────────────
    current_regime = {}
    try:
        regime_router = MarketRegimeRouter()
        regime = await regime_router.detect_regime()
        current_regime = regime_router.to_dict(regime)
    except Exception as e:
        logger.warning(f"Regime fetch failed: {e}")

    return {
        "status": "ok",
        "version": "5.5.0",
        # In-memory stats
        **stats,
        # DB stats
        **db_stats,
        # V5: Strategy performance
        "strategy_performance": strategy_stats,
        "current_regime": current_regime,
        # Live data
        "open_positions": open_positions,
        "system_uptime": uptime_str,
    }


@router.get("/strategy-report")
async def get_strategy_report():
    """
    V5.5: Comprehensive per-engine strategy report.
    Shows win rate, profit factor, R multiple, drawdown by strategy and regime.
    """
    try:
        report = await strategy_tracker.get_strategy_report(days=30)

        # Get current regime
        current_regime = {}
        try:
            regime_router = MarketRegimeRouter()
            regime = await regime_router.detect_regime()
            current_regime = regime_router.to_dict(regime)
        except Exception:
            pass

        # Get weight adjustments
        weights = {}
        try:
            weights = await strategy_tracker.get_strategy_weight_adjustments(days=14)
        except Exception:
            pass

        return {
            "status": "ok",
            "report": report,
            "current_regime": current_regime,
            "weight_adjustments": weights,
            "period_days": 30,
        }
    except Exception as e:
        logger.error(f"Strategy report failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/manage-breakeven")
async def manage_break_even():
    """
    V5.5: Check all open positions and move SL to break-even
    for those that have reached the profit threshold.

    Call this endpoint periodically (e.g., every 5 minutes via n8n)
    to protect winning positions automatically.
    """
    from app.modules.telegram import TelegramNotifier
    from app.config import settings

    telegram = TelegramNotifier()
    executor = BinanceExecutor()

    try:
        actions = await executor.manage_break_even_stops(
            trigger_pct=settings.BREAK_EVEN_TRIGGER_PCT,
            buffer_pct=settings.BREAK_EVEN_BUFFER_PCT,
            telegram_notifier=telegram,
        )

        return {
            "status": "ok",
            "positions_adjusted": len(actions),
            "actions": actions,
            "trigger_pct": settings.BREAK_EVEN_TRIGGER_PCT,
            "buffer_pct": settings.BREAK_EVEN_BUFFER_PCT,
        }
    except Exception as e:
        logger.error(f"Break-even management failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/strategy-report-full")
async def get_strategy_report_full():
    """
    V5.5: Enhanced strategy report with TP/SL success rates,
    partial TP stats, and break-even stop effectiveness.
    """
    try:
        report = await strategy_tracker.get_strategy_report(days=30)

        # Get current regime
        current_regime = {}
        try:
            regime_router = MarketRegimeRouter()
            regime = await regime_router.detect_regime()
            current_regime = regime_router.to_dict(regime)
        except Exception:
            pass

        # Get weight adjustments
        weights = {}
        try:
            weights = await strategy_tracker.get_strategy_weight_adjustments(days=14)
        except Exception:
            pass

        # TP/SL success metrics from trades
        tp_sl_stats = {}
        try:
            async with async_session() as session:
                today = date.today()
                month_ago = today - timedelta(days=30)

                # Total trades with TP/SL data
                all_trades = await session.execute(
                    select(Trade).where(
                        Trade.created_at >= datetime.combine(month_ago, datetime.min.time())
                    )
                )
                trades = all_trades.scalars().all()

                total = len(trades)
                tp_hit = sum(1 for t in trades if t.close_reason == "tp_hit")
                sl_hit = sum(1 for t in trades if t.close_reason == "sl_hit")
                manual = sum(1 for t in trades if t.close_reason == "manual")
                still_open = sum(1 for t in trades if t.status == "open")

                tp_sl_stats = {
                    "total_trades_30d": total,
                    "tp_hit_count": tp_hit,
                    "sl_hit_count": sl_hit,
                    "manual_close_count": manual,
                    "still_open": still_open,
                    "tp_hit_rate": round(tp_hit / total * 100, 1) if total > 0 else 0,
                    "sl_hit_rate": round(sl_hit / total * 100, 1) if total > 0 else 0,
                }
        except Exception as e:
            logger.warning(f"TP/SL stats query failed: {e}")

        return {
            "status": "ok",
            "report": report,
            "current_regime": current_regime,
            "weight_adjustments": weights,
            "tp_sl_success": tp_sl_stats,
            "period_days": 30,
            "version": "5.5.0",
        }
    except Exception as e:
        logger.error(f"Full strategy report failed: {e}")
        return {"status": "error", "message": str(e)}
