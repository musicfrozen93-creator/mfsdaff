"""Scanner API endpoint — returns single coin"""

import logging
from fastapi import APIRouter, HTTPException
from app.modules.scanner import MarketScanner

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/scan")
async def scan_market():
    """
    Scan Binance Futures and return 1 randomly selected coin.
    Top volume → top 10 → top 5 → random 1.
    """
    try:
        scanner = MarketScanner()
        results = await scanner.scan(top_n=1)

        if not results:
            return {"status": "ok", "count": 0, "coins": [], "selected": None}

        return {
            "status": "ok",
            "count": len(results),
            "coins": results,
            "selected": results[0],
        }
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
