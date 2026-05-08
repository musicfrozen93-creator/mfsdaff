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
from app.modules.learning_engine import learning_engine  # V7: Adaptive strategy system

# Setup logging
setup_logger()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 V8 Protected Crypto Trading Bot starting up...")
    await init_db()
    await seed_admin()
    await run_subscription_expiry_check()
    await learning_engine.seed_strategy_registry()
    logger.info("✅ All V8 systems initialized (Hedge Mode detection active)")
    yield
    await close_db()
    logger.info("🛑 V7 Crypto Trading Bot shutting down...")


app = FastAPI(
    title="V8 Protected Crypto Futures Trading Bot",
    description=(
        "Production-grade automated crypto futures trading system with "
        "V8 Hedge Mode detection, pre-trade TP/SL validation, atomic protection, "
        "adaptive learning, multi-account support, and configurable risk guardrails."
    ),
    version="8.0.0",
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
        "service": "crypto-trading-bot-v8",
        "version": "8.0.0",
        "features": [
            "hedge_mode_detection",
            "pre_trade_tpsl_validation",
            "atomic_tpsl_protection",
            "database_account_loader",
            "subscription_guard",
            "adaptive_learning",
        ],
    }
