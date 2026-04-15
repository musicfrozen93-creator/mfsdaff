"""Status and stats endpoint"""

from fastapi import APIRouter
from app.utils.state import state_manager

router = APIRouter()


@router.get("/status")
async def get_status():
    return {"status": "ok", **state_manager.get_stats()}
