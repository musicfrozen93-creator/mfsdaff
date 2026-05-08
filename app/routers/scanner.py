"""V2 Scanner API endpoint — returns up to 100 coins with retry logic"""

import logging
from fastapi import APIRouter, HTTPException
from app.modules.scanner import MarketScanner

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/scan")
async def scan_market(top_n: int = 100):
    """
    Scan Binance Futures and return up to N coins
    sorted by 24h volume that pass quality filters.
    """
    try:
        scanner = MarketScanner()
        results = await scanner.scan(top_n=top_n)

        return {
            "status": "ok",
            "count": len(results),
            "coins": results,
        }
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
