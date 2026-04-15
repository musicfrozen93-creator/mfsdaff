"""
Analyzer API endpoint — Scalping mode
Uses fast RSI-based analysis instead of OpenAI
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.modules.ai_engine import ScalpingEngine

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


@router.post("/analyze")
async def analyze_coin(req: AnalyzeRequest):
    """
    Scalping analysis for a single coin:
    RSI-based decision with trend confirmation.
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
