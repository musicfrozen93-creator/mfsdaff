"""
V2 Status Dashboard API — comprehensive trading statistics
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

    return {
        "status": "ok",
        "version": "2.0.0",
        # In-memory stats
        **stats,
        # DB stats
        **db_stats,
        # Live data
        "open_positions": open_positions,
        "system_uptime": uptime_str,
    }
