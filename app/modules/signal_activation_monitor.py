"""
V17 Signal Activation Monitor — 30-second monitoring loop

Responsibilities:
- Monitor all pending signals against live prices
- Detect trigger activation with momentum bypass
- Detect missed entries, invalidated setups, momentum loss
- Detect opposite-direction conflicts
- Auto-cancel stale setups
- Monitor TP/SL hit
- Track partial progress after activation
- Send Telegram notifications for all state changes

Usage: GET /monitor/signals — called by n8n every 30 seconds
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter

from app.config import settings
from app.modules.signal_tracker_v16 import signal_tracker_v16
from app.modules.telegram import TelegramNotifier
from app.utils.serialization import clean_json_types

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Price fetch cache (30-sec TTL) ────────────────────────────────────────────
_price_cache: dict[str, float] = {}
_price_cache_ts: float = 0.0
_PRICE_CACHE_TTL = 25.0  # seconds — refresh every 25s (< 30s cycle)


async def _fetch_live_prices(symbols: list[str]) -> dict[str, float]:
    """Batch fetch live prices from Binance for all active signal symbols."""
    global _price_cache, _price_cache_ts
    import time as _time

    now = _time.time()
    if _price_cache and (now - _price_cache_ts) < _PRICE_CACHE_TTL:
        # Return cached prices for symbols we have
        result = {s: _price_cache[s] for s in symbols if s in _price_cache}
        if len(result) == len(symbols):
            return result

    prices = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Use bookTicker for all symbols efficiently
            tasks = []
            for symbol in symbols:
                tasks.append(
                    client.get(
                        f"{settings.binance_base_url}/fapi/v1/ticker/price",
                        params={"symbol": symbol},
                    )
                )
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, resp in zip(symbols, responses):
                if isinstance(resp, Exception):
                    continue
                try:
                    resp.raise_for_status()
                    prices[symbol] = float(resp.json()["price"])
                except Exception:
                    pass

        _price_cache = prices
        _price_cache_ts = now
    except Exception as e:
        logger.warning(f"[Monitor] Price fetch failed: {e}")

    return prices


# ── Diagnostics tracker ───────────────────────────────────────────────────────
_monitor_stats = {
    "cycles": 0,
    "activations": 0,
    "tp_hits": 0,
    "sl_hits": 0,
    "invalidations": 0,
    "missed_entries": 0,
    "stale_cancels": 0,
    "last_run": None,
}


@router.get("/monitor/signals")
async def monitor_signals():
    """
    V17 Signal Activation Monitor — called every 30 seconds by n8n.

    Checks all active signals against live prices and fires Telegram alerts
    for: trigger activation, TP hit, SL hit, invalidation, missed entry,
    stale cancel, momentum loss, opposite conflict.
    """
    global _monitor_stats

    _monitor_stats["cycles"] += 1
    _monitor_stats["last_run"] = datetime.now(timezone.utc).isoformat()

    active = signal_tracker_v16.get_active_signals()
    if not active:
        logger.debug("[Monitor] No active signals to monitor")
        return clean_json_types({
            "status": "ok",
            "active_signals": 0,
            "message": "No active signals",
            "stats": _monitor_stats,
        })

    # Fetch live prices for all active symbols
    symbols = list({s["symbol"] for s in active})
    prices = await _fetch_live_prices(symbols)

    if not prices:
        logger.warning("[Monitor] Could not fetch prices — skipping cycle")
        return clean_json_types({
            "status": "error",
            "message": "Price fetch failed",
            "active_signals": len(active),
        })

    # Run the tracker update (handles all state transitions internally)
    telegram = TelegramNotifier()
    try:
        await signal_tracker_v16.update_all(prices, telegram)
    except Exception as e:
        logger.error(f"[Monitor] update_all failed: {e}")

    # Collect post-update state for response
    remaining = signal_tracker_v16.get_active_signals()

    logger.info(
        f"[Monitor] Cycle {_monitor_stats['cycles']} | "
        f"Active: {len(active)} -> {len(remaining)} | "
        f"Prices fetched: {len(prices)}"
    )

    return clean_json_types({
        "status": "ok",
        "cycle": _monitor_stats["cycles"],
        "active_signals_before": len(active),
        "active_signals_after": len(remaining),
        "prices_fetched": len(prices),
        "symbols_monitored": symbols,
        "signals": remaining,
        "stats": _monitor_stats,
    })


@router.get("/monitor/status")
async def monitor_status():
    """Return current signal tracker state without running update."""
    active = signal_tracker_v16.get_active_signals()
    return clean_json_types({
        "status": "ok",
        "active_count": len(active),
        "signals": active,
        "stats": _monitor_stats,
    })


@router.post("/monitor/cancel/{signal_number}")
async def cancel_signal(signal_number: int):
    """Manually cancel a signal by number."""
    telegram = TelegramNotifier()
    cancelled = False
    for sig_id, state in list(signal_tracker_v16._signals.items()):
        if state.signal_number == signal_number and state.is_active():
            await signal_tracker_v16._close_signal(
                state, "CANCELLED", "manual_cancel", 0.0, telegram
            )
            signal_tracker_v16._signals.pop(sig_id, None)
            cancelled = True
            logger.info(f"[Monitor] Manually cancelled signal #{signal_number:03d}")
            break

    return clean_json_types({
        "status": "ok" if cancelled else "not_found",
        "signal_number": signal_number,
        "cancelled": cancelled,
    })
