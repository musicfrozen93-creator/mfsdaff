"""
Signal-Only AI Trading Engine
AI signal generation → Telegram delivery (no Binance execution)
FastAPI Backend — Main Entry Point

V18: Signal Lifecycle Engine — local rule-based monitoring
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
from app.modules.learning_engine import learning_engine  # V7: Adaptive strategy system
from app.config import settings

# Setup logging
setup_logger()
logger = logging.getLogger(__name__)

# V18: Lifecycle monitor reference (started in lifespan)
_lifecycle_monitor = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _lifecycle_monitor

    logger.info("🚀 Signal-Only AI Engine starting up...")
    await init_db()
    await seed_admin()
    await run_subscription_expiry_check()
    await learning_engine.seed_strategy_registry()

    # V18: Start Signal Lifecycle Monitor
    if settings.V18_LIFECYCLE_ENABLED:
        try:
            from app.modules.signal_lifecycle import signal_monitor
            from app.modules.signal_store import signal_store

            # Load persisted signals
            active_count = signal_store.count_active()
            logger.info(
                f"📦 [V18] Signal store loaded: {active_count} active signals"
            )

            # Start background monitor
            await signal_monitor.start()
            _lifecycle_monitor = signal_monitor
            logger.info(
                f"🔄 [V18] Lifecycle Monitor started "
                f"(interval={settings.V18_MONITOR_INTERVAL_SEC}s)"
            )
        except Exception as e:
            logger.error(f"[V18] Failed to start lifecycle monitor: {e}", exc_info=True)
    else:
        logger.info("⏸️ [V18] Lifecycle Monitor disabled (V18_LIFECYCLE_ENABLED=false)")

    logger.info("✅ Signal-Only Engine initialized (AI → Telegram)")
    yield

    # V18: Stop lifecycle monitor
    if _lifecycle_monitor:
        try:
            await _lifecycle_monitor.stop()
            logger.info("🔄 [V18] Lifecycle Monitor stopped")
        except Exception as e:
            logger.warning(f"[V18] Error stopping lifecycle monitor: {e}")

    await close_db()
    logger.info("🛑 Signal-Only AI Engine shutting down...")


app = FastAPI(
    title="Signal-Only AI Trading Engine",
    description=(
        "AI-powered signal generation engine. Analyzes markets, generates "
        "BUY/SELL signals with TP/SL targets, and delivers them via Telegram. "
        "V18: Signal Lifecycle Engine with local rule-based monitoring."
    ),
    version="18.0.0-lifecycle",
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
app.include_router(scanner.router,  prefix="/api/v1", tags=["Scanner"])
app.include_router(analyzer.router, prefix="/api/v1", tags=["Analyzer"])
app.include_router(executor.router, prefix="/api/v1", tags=["Executor"])
app.include_router(status.router,   prefix="/api/v1", tags=["Status"])
app.include_router(accounts.router, prefix="/api/v1", tags=["Accounts"])
app.include_router(admin.router,    tags=["Admin"])


@app.get("/health")
async def health_check():
    # V18: Include lifecycle status
    lifecycle_status = "disabled"
    active_signals = 0
    if settings.V18_LIFECYCLE_ENABLED:
        try:
            from app.modules.signal_store import signal_store
            active_signals = signal_store.count_active()
            lifecycle_status = "running" if _lifecycle_monitor and _lifecycle_monitor.is_running else "stopped"
        except Exception:
            lifecycle_status = "error"

    return {
        "status": "ok",
        "service": "signal-lifecycle-engine",
        "version": "18.0.0-lifecycle",
        "mode": "signal_lifecycle",
        "lifecycle": {
            "enabled": settings.V18_LIFECYCLE_ENABLED,
            "status": lifecycle_status,
            "active_signals": active_signals,
            "monitor_interval_sec": settings.V18_MONITOR_INTERVAL_SEC,
            "watch_first_mode": settings.V18_WATCH_FIRST_MODE,
        },
        "features": [
            "ai_signal_generation",
            "telegram_delivery",
            "confidence_scoring",
            "tp_sl_calculation",
            "regime_detection",
            "multi_strategy_analysis",
            "signal_lifecycle_tracking",
            "local_price_monitoring",
            "entry_hit_detection",
            "tp_sl_auto_tracking",
        ],
    }

