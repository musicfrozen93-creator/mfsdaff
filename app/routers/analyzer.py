"""
V2 Analyzer API — Confluence-based scalping signals
Supports single coin and batch analysis with layered scoring.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from app.modules.ai_engine import ScalpingEngine
from app.modules.orderbook import OrderBookAnalyzer
from app.config import settings
from app.utils.serialization import clean_json_types

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
    V2 single coin analysis with layered confluence + optional AI.
    """
    try:
        engine = ScalpingEngine()

        # Optional: orderbook analysis for AI prompt enrichment
        ob_data = None
        try:
            ob_analyzer = OrderBookAnalyzer()
            price = req.bid if req.bid > 0 else 0
            if price > 0:
                ob_result = await ob_analyzer.analyze(req.symbol, price)
                ob_data = ob_analyzer.to_dict(ob_result)
        except Exception as e:
            logger.warning(f"Orderbook analysis skipped for {req.symbol}: {e}")

        decision = await engine.analyze(
            req.symbol,
            spread_pct=req.spread_pct,
            orderbook_data=ob_data,
        )
        result = engine.to_dict(decision)

        return clean_json_types({
            "status": "ok",
            "symbol": req.symbol,
            "ai_decision": result,
        })

    except Exception as e:
        logger.error(f"Analysis failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-batch")
async def analyze_batch(req: BatchAnalyzeRequest):
    """
    Batch analysis for multiple coins with V2 confluence engine.
    Returns top N highest-confidence actionable signals.
    """
    if not req.coins:
        return {"status": "ok", "count": 0, "analyzed": 0, "has_signals": False, "coins": []}

    logger.info(f"📊 Batch analyzing {len(req.coins)} coins (top {req.top_n})...")

    engine = ScalpingEngine()
    scanner_data = {c.symbol: c for c in req.coins}

    semaphore = asyncio.Semaphore(10)

    async def analyze_with_limit(symbol: str, spread: float):
        async with semaphore:
            try:
                decision = await engine.analyze(symbol, spread_pct=spread)
                return symbol, engine.to_dict(decision)
            except Exception as e:
                logger.warning(f"Analysis failed for {symbol}: {e}")
                return symbol, None

    tasks = [
        analyze_with_limit(coin.symbol, coin.spread_pct)
        for coin in req.coins
    ]
    results = await asyncio.gather(*tasks)

    analyzed = []
    for symbol, ai_result in results:
        if ai_result is None:
            continue

        coin_info = scanner_data.get(symbol)
        analyzed.append({
            "symbol": symbol,
            "action": ai_result.get("action", "HOLD"),
            "confidence": ai_result.get("confidence", 0),
            "reason": ai_result.get("reason", ""),
            "current_price": ai_result.get("current_price", 0.0),
            "rsi": ai_result.get("rsi", 50.0),
            "trend": ai_result.get("trend", "NEUTRAL"),
            "htf_trend": ai_result.get("htf_trend", "NEUTRAL"),
            "atr": ai_result.get("atr", 0.0),
            "atr_pct": ai_result.get("atr_pct", 0.0),
            "vwap": ai_result.get("vwap", 0.0),
            "volume_spike": ai_result.get("volume_spike", False),
            "candle_type": ai_result.get("candle_type", "DOJI"),
            "is_choppy": ai_result.get("is_choppy", False),
            "ai_called": ai_result.get("ai_called", False),
            "ai_fallback": ai_result.get("ai_fallback", False),
            "spread_pct": coin_info.spread_pct if coin_info else 0.0,
            "volume_24h": coin_info.volume_24h if coin_info else 0.0,
        })

    # Sort by confidence descending
    analyzed.sort(key=lambda x: x["confidence"], reverse=True)
    top_coins = analyzed[:req.top_n]

    tradeable = [
        c for c in top_coins
        if c["action"] != "HOLD" and c["confidence"] >= settings.MIN_CONFIDENCE
    ]

    logger.info(
        f"  Analyzed: {len(analyzed)} | Top {len(top_coins)} | "
        f"Tradeable (conf>={settings.MIN_CONFIDENCE}): {len(tradeable)}"
    )

    has_signals = len(tradeable) > 0

    # Build summary for downstream
    summary = "No signals met trade criteria" if not tradeable else (
        f"{len(tradeable)} tradeable: " +
        ", ".join(f"{c['symbol']}({c['action']}/{c['confidence']})" for c in tradeable[:5])
    )

    return clean_json_types({
        "status": "ok",
        "analyzed": len(analyzed),
        "count": len(top_coins),
        "tradeable_count": len(tradeable),
        "has_signals": has_signals,
        "summary": summary,
        "coins": top_coins,
    })
