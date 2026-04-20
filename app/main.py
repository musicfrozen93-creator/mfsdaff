"""
V5 AI-Powered Multi-Strategy Crypto Futures Trading System
FastAPI Backend — Main Entry Point
"""

import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import scanner, analyzer, executor, status, accounts
from app.utils.logger import setup_logger
from app.database import init_db, close_db

# Setup logging
setup_logger()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 V5 Multi-Strategy Crypto Trading Bot starting up...")
    await init_db()
    logger.info("✅ All systems initialized")
    yield
    await close_db()
    logger.info("🛑 Crypto Trading Bot shutting down...")


app = FastAPI(
    title="V5 Multi-Strategy Crypto Futures Trading Bot",
    description=(
        "Professional automated crypto futures trading system with "
        "multi-strategy engines (Scalp/Swing/Sniper), market regime routing, "
        "multi-account support, AI verification, and dynamic risk management."
    ),
    version="5.5.0",
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


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "crypto-trading-bot-v5",
        "version": "5.5.0",
    }
