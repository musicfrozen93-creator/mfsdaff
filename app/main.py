"""
V8 Protected Execution Crypto Futures Trading System
V8: + Hedge Mode detection, pre-trade TP/SL dry-run validation, positionSide-aware orders
FastAPI Backend — Main Entry Point
"""

import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import scanner, analyzer, executor, status, accounts, admin
from app.utils.logger import setup_logger
from app.database import init_db, close_db
from app.utils.seed_admin import seed_admin
from app.utils.subscription_guard import run_subscription_expiry_check
from app.modules.learning_engine import learning_engine
from app.modules.signal_tracker_v16 import signal_tracker_v16
from app.modules.telegram import TelegramNotifier
from app.modules.signal_activation_monitor import router as monitor_router  # V17
from app.modules.daily_report import router as daily_report_router          # V17

# Setup logging
setup_logger()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 V17 Signal Engine starting up...")
    await init_db()
    await seed_admin()
    await run_subscription_expiry_check()
    await learning_engine.seed_strategy_registry()
    logger.info("✅ All V17 systems initialized (Signal Engine active — no Binance execution)")

    # V17: Start signal monitor background task (30s cycle)
    import asyncio as _asyncio
    monitor_task = _asyncio.create_task(_signal_monitor_loop())

    yield

    monitor_task.cancel()
    try:
        await monitor_task
    except _asyncio.CancelledError:
        pass
    await close_db()
    logger.info("🛑 V16 Signal Engine shutting down...")


async def _signal_monitor_loop():
    """
    V16: Background loop — runs every 60 seconds.
    Fetches current prices for all active signals and updates their state.
    Triggers Telegram notifications on TP/SL/Entry/Drawdown events.
    """
    import asyncio as _asyncio
    import httpx
    from app.config import settings as _settings

    telegram = TelegramNotifier()
    logger.info("  🔁 V16 Signal monitor loop started")

    while True:
        try:
            await _asyncio.sleep(30)   # V17: check every 30 seconds (was 60s)

            active = signal_tracker_v16.get_active_signals()
            if not active:
                continue

            symbols = list({s["symbol"] for s in active})
            prices: dict[str, float] = {}

            # Batch fetch mark prices from Binance
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{_settings.binance_base_url}/fapi/v1/ticker/price"
                    )
                    resp.raise_for_status()
                    ticker_data = resp.json()
                    for t in ticker_data:
                        if t["symbol"] in symbols:
                            prices[t["symbol"]] = float(t["price"])
            except Exception as fetch_err:
                logger.warning(f"  [SignalMonitor] Price fetch failed: {fetch_err}")
                continue

            await signal_tracker_v16.update_all(prices, telegram)
            logger.debug(
                f"  [SignalMonitor] Updated {len(active)} signals | "
                f"prices fetched: {len(prices)}"
            )

        except _asyncio.CancelledError:
            logger.info("  [SignalMonitor] Background loop cancelled — shutting down")
            break
        except Exception as e:
            logger.error(f"  [SignalMonitor] Unexpected error: {e}")
            await _asyncio.sleep(15)   # back-off on error


app = FastAPI(
    title="V17 AI Signal Engine — Crypto Futures",
    description=(
        "V17 High-Quality Signal Engine: AI signal generation, BTC directional filter, "
        "ATR-based TP/SL, virtual signal tracking, daily performance reports, "
        "and Telegram alerts. No auto-trading. V17: recalibrated confidence engine, "
        "adaptive volume spike, improved SHORT detection, 30s monitor loop."
    ),
    version="17.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(scanner.router,       prefix="/api/v1", tags=["Scanner"])
app.include_router(analyzer.router,      prefix="/api/v1", tags=["Analyzer"])
app.include_router(executor.router,      prefix="/api/v1", tags=["Executor"])
app.include_router(status.router,        prefix="/api/v1", tags=["Status"])
app.include_router(accounts.router,      prefix="/api/v1", tags=["Accounts"])
app.include_router(admin.router,         tags=["Admin"])
app.include_router(monitor_router,       prefix="/api/v1", tags=["Monitor"])       # V17
app.include_router(daily_report_router,  prefix="/api/v1", tags=["DailyReport"])   # V17


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "crypto-signal-engine-v17",
        "version": "17.0.0",
        "mode": "SIGNAL_ONLY — no Binance execution",
        "features": [
            "btc_directional_bias_filter",
            "smart_entry_logic",
            "atr_adjusted_tp_sl",
            "daily_signal_numbering",
            "virtual_signal_tracker",
            "drawdown_monitoring",
            "reversal_detection",
            "15min_quiet_timer",
            "daily_performance_report",
            "ai_pre_filter",
            "5min_analysis_cache",
            # V17 new features
            "adaptive_volume_spike",
            "improved_short_detection",
            "momentum_bypass_trigger",
            "30s_monitor_loop",
            "watchlist_spam_suppression",
            "symbol_signal_cooldown",
            "stale_signal_detection",
            "daily_report_endpoint",
        ],
        "active_signals": signal_tracker_v16.active_count(),
    }
