"""
Signal-Only AI Trading Engine
AI signal generation → Telegram delivery (no Binance execution)
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
from app.modules.learning_engine import learning_engine  # V7: Adaptive strategy system

# Setup logging
setup_logger()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Signal-Only AI Engine starting up...")
    await init_db()
    await seed_admin()
    await run_subscription_expiry_check()
    await learning_engine.seed_strategy_registry()
    logger.info("✅ Signal-Only Engine initialized (AI → Telegram)")
    yield
    await close_db()
    logger.info("🛑 Signal-Only AI Engine shutting down...")


app = FastAPI(
    title="Signal-Only AI Trading Engine",
    description=(
        "AI-powered signal generation engine. Analyzes markets, generates "
        "BUY/SELL signals with TP/SL targets, and delivers them via Telegram. "
        "No Binance execution — pure signal intelligence."
    ),
    version="9.0.0-signal",
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
    return {
        "status": "ok",
        "service": "signal-only-ai-engine",
        "version": "9.0.0-signal",
        "mode": "signal_only",
        "features": [
            "ai_signal_generation",
            "telegram_delivery",
            "confidence_scoring",
            "tp_sl_calculation",
            "regime_detection",
            "multi_strategy_analysis",
        ],
    }
