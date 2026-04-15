"""
Analyzer API endpoint — Scalping mode
Supports single coin analysis and batch analysis with ranking.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

from app.modules.ai_engine import ScalpingEngine
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    symbol: str
    price_change_pct: float = 0.0
    volume_24h: float = 0.0
    score: float = 0.0
    spread_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0


class CoinInput(BaseModel):
    symbol: str
    price: float = 0.0
    volume_24h: float = 0.0
    price_change_pct: float = 0.0
    spread_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    score: float = 0.0


class BatchAnalyzeRequest(BaseModel):
    coins: List[CoinInput]
    top_n: int = 10


@router.post("/analyze")
async def analyze_coin(req: AnalyzeRequest):
    """
    Scalping analysis for a single coin:
    RSI-based decision with trend confirmation.
    Returns consistent, flat data structure.
    """
    try:
        engine = ScalpingEngine()
        decision = await engine.analyze(req.symbol)
        result = engine.to_dict(decision)

        return {
            "status": "ok",
            "symbol": req.symbol,
            "ai_decision": result,
        }

    except Exception as e:
        logger.error(f"Analysis failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-batch")
async def analyze_batch(req: BatchAnalyzeRequest):
    """
    Batch analysis for multiple coins:
    1. Analyze all coins concurrently (with concurrency limit)
    2. Sort by confidence descending
    3. Return top N (default 10) highest-confidence coins

    Returns a flat, consistent data structure per coin
    that maps directly to the /execute endpoint input.
    """
    if not req.coins:
        return {"status": "ok", "count": 0, "analyzed": 0, "coins": []}

    logger.info(f"📊 Batch analyzing {len(req.coins)} coins (top {req.top_n})...")

    engine = ScalpingEngine()

    # Build a lookup of scanner data keyed by symbol
    scanner_data = {c.symbol: c for c in req.coins}

    # Analyze coins concurrently with a semaphore to limit concurrent API calls
    semaphore = asyncio.Semaphore(10)  # Max 10 concurrent analyses

    async def analyze_with_limit(symbol: str):
        async with semaphore:
            try:
                decision = await engine.analyze(symbol)
                return symbol, engine.to_dict(decision)
            except Exception as e:
                logger.warning(f"Analysis failed for {symbol}: {e}")
                return symbol, None

    tasks = [analyze_with_limit(coin.symbol) for coin in req.coins]
    results = await asyncio.gather(*tasks)

    # Build flat result objects with consistent structure
    analyzed = []
    for symbol, ai_result in results:
        if ai_result is None:
            continue

        coin_info = scanner_data.get(symbol)

        analyzed.append({
            # Identification
            "symbol": symbol,
            # AI decision fields (flat — matches /execute input)
            "action": ai_result.get("action", "HOLD"),
            "confidence": ai_result.get("confidence", 0),
            "reason": ai_result.get("reason", ""),
            "current_price": ai_result.get("current_price", 0.0),
            "rsi": ai_result.get("rsi", 50.0),
            "trend": ai_result.get("trend", "NEUTRAL"),
            "atr": ai_result.get("atr", 0.0),
            "atr_pct": ai_result.get("atr_pct", 0.0),
            # Scanner data (passed through for context)
            "spread_pct": coin_info.spread_pct if coin_info else 0.0,
            "volume_24h": coin_info.volume_24h if coin_info else 0.0,
        })

    # Sort by confidence descending
    analyzed.sort(key=lambda x: x["confidence"], reverse=True)

    # Take top N
    top_coins = analyzed[:req.top_n]

    # Log summary
    tradeable = [c for c in top_coins if c["action"] != "HOLD" and c["confidence"] >= settings.MIN_CONFIDENCE]
    logger.info(
        f"  Analyzed: {len(analyzed)} | Top {len(top_coins)} | "
        f"Tradeable (action!=HOLD & conf>={settings.MIN_CONFIDENCE}): {len(tradeable)}"
    )

    if tradeable:
        logger.info(
            f"  Best signals: {[(c['symbol'], c['action'], c['confidence']) for c in tradeable[:5]]}"
        )

    return {
        "status": "ok",
        "analyzed": len(analyzed),
        "count": len(top_coins),
        "tradeable_count": len(tradeable),
        "coins": top_coins,
    }
