"""
V9 Close Engine — Production-grade market close for Position Manager.

Handles:
  - Correct quantity precision (from exchange info cache)
  - Hedge mode / One-way mode awareness (reduceOnly vs positionSide)
  - Verifies position actually closed after market order
  - Returns structured CloseResult with PnL calculation

Strategy-agnostic: called by position_manager.py when TP or SL is hit.
Does NOT re-calculate TP/SL — those are stored in open_positions at trade open.
"""

import asyncio
import hashlib
import hmac
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ── Retry config ──────────────────────────────────────────────────────
CLOSE_MAX_RETRIES = 3
CLOSE_RETRY_DELAY = 1.5   # seconds

# ── Exchange info cache (shared with main executor, 5-min TTL) ────────
_exchange_info_cache: dict = {}
_exchange_info_ts: float = 0.0
CACHE_TTL = 300


@dataclass
class CloseResult:
    success: bool
    symbol: str
    close_price: float        # Actual fill price
    quantity: float
    pnl_usdt: float           # Estimated PnL in USDT
    pnl_pct: float            # Estimated PnL % on position
    close_reason: str         # tp_hit | sl_hit | trailing_exit | manual
    order_id: Optional[int] = None
    error: Optional[str] = None


class CloseEngine:
    """
    V9 Production Close Engine — executes market close orders for the
    Position Manager bot.

    Completely independent from the main bot's executor — no shared state.
    Reads its own exchange info cache, uses its own credentials.
    """

    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.is_testnet = testnet
        self.base_url = (
            "https://testnet.binancefuture.com"
            if testnet
            else "https://fapi.binance.com"
        )

    # ── Signing ──────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    async def _signed_request(self, method: str, path: str, params: dict = None) -> dict:
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = self._sign(params)

        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            if method == "GET":
                resp = await client.get(url, params=params, headers=self._headers())
            elif method == "POST":
                resp = await client.post(url, params=params, headers=self._headers())
            elif method == "DELETE":
                resp = await client.delete(url, params=params, headers=self._headers())
            else:
                raise ValueError(f"Unknown method: {method}")

            if not resp.is_success:
                try:
                    err = resp.json()
                    raise ValueError(f"Binance {err.get('code')}: {err.get('msg')}")
                except ValueError:
                    raise
                except Exception:
                    resp.raise_for_status()
            return resp.json()

    # ── Exchange Info + Precision ─────────────────────────────────────

    async def _get_precision(self, symbol: str) -> dict:
        """Return {qty_precision, price_precision, min_qty, step_size, tick_size}."""
        global _exchange_info_cache, _exchange_info_ts

        now = time.time()
        if not _exchange_info_cache or (now - _exchange_info_ts) > CACHE_TTL:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
                resp.raise_for_status()
                data = resp.json()
            _exchange_info_cache = {s["symbol"]: s for s in data["symbols"]}
            _exchange_info_ts = now

        sym = _exchange_info_cache.get(symbol)
        if not sym:
            raise ValueError(f"Symbol {symbol} not in exchange info")

        qty_p = sym["quantityPrecision"]
        price_p = sym["pricePrecision"]
        min_qty = 0.0
        step_size = 0.0
        tick_size = 0.0

        for f in sym.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                min_qty = float(f["minQty"])
                step_size = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])

        return {
            "qty_precision": qty_p,
            "price_precision": price_p,
            "min_qty": min_qty,
            "step_size": step_size,
            "tick_size": tick_size,
        }

    def _round_qty(self, qty: float, precision: int, step_size: float) -> float:
        """Round quantity to exchange step_size and precision."""
        if step_size > 0:
            qty = math.floor(qty / step_size) * step_size
        return round(qty, precision)

    def _format_qty(self, qty: float, precision: int) -> str:
        return f"{qty:.{precision}f}"

    # ── Live Position Lookup ──────────────────────────────────────────

    async def get_live_position(self, symbol: str) -> Optional[dict]:
        """
        Fetch live position for a symbol from Binance.
        Returns None if no open position.
        """
        try:
            data = await self._signed_request("GET", "/fapi/v2/positionRisk")
            for p in data:
                if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
                    return p
        except Exception as e:
            logger.warning(f"  [CloseEngine] get_live_position({symbol}) failed: {e}")
        return None

    async def get_market_price(self, symbol: str) -> float:
        """Fetch current mark price for a symbol."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/ticker/price",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            return float(resp.json()["price"])

    # ── Cancel All Open Orders ────────────────────────────────────────

    async def cancel_all_orders(self, symbol: str) -> None:
        """Cancel all open orders for a symbol (e.g. stale TP/SL from main bot)."""
        try:
            await self._signed_request(
                "DELETE", "/fapi/v1/allOpenOrders",
                {"symbol": symbol},
            )
            logger.info(f"  [CloseEngine] Cancelled all open orders for {symbol}")
        except Exception as e:
            logger.warning(f"  [CloseEngine] Cancel orders failed for {symbol}: {e}")

    # ── Core Close Function ───────────────────────────────────────────

    async def market_close(
        self,
        symbol: str,
        side: str,             # Original trade side: BUY (long) or SELL (short)
        quantity: float,       # Expected quantity from open_positions
        entry_price: float,    # For PnL calculation
        close_reason: str,     # tp_hit | sl_hit | trailing_exit | manual
        is_hedge_mode: bool = False,
        position_side: str = "BOTH",  # BOTH | LONG | SHORT
    ) -> CloseResult:
        """
        Execute a market close order with correct precision, reduceOnly,
        and hedge mode awareness.

        Strategy:
        1. Fetch live position to get actual qty (in case of partial fills)
        2. Calculate correct rounded qty
        3. Cancel any stale open orders (native TP/SL from main bot)
        4. Place MARKET close order
        5. Verify position is gone
        6. Calculate PnL
        """
        close_side = "SELL" if side == "BUY" else "BUY"

        for attempt in range(1, CLOSE_MAX_RETRIES + 1):
            try:
                logger.info(
                    f"  [CloseEngine] Closing {symbol} {close_reason} "
                    f"(attempt {attempt}/{CLOSE_MAX_RETRIES}) "
                    f"side={close_side} hedge={is_hedge_mode} pos_side={position_side}"
                )

                # ── 1. Get live position qty (authoritative source) ───
                live_pos = await self.get_live_position(symbol)
                if not live_pos:
                    logger.info(
                        f"  [CloseEngine] {symbol}: No live position found "
                        f"(already closed?)"
                    )
                    # Already closed — treat as success with estimated PnL
                    try:
                        close_price = await self.get_market_price(symbol)
                    except Exception:
                        close_price = entry_price
                    pnl_pct, pnl_usdt = self._calc_pnl(side, entry_price, close_price, quantity)
                    return CloseResult(
                        success=True,
                        symbol=symbol,
                        close_price=close_price,
                        quantity=quantity,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=pnl_pct,
                        close_reason=close_reason,
                    )

                live_qty = abs(float(live_pos.get("positionAmt", 0)))
                live_entry = float(live_pos.get("entryPrice", entry_price))
                pos_side_live = live_pos.get("positionSide", "BOTH")

                if live_qty <= 0:
                    logger.info(f"  [CloseEngine] {symbol}: Position qty=0 (closed)")
                    close_price = await self.get_market_price(symbol)
                    pnl_pct, pnl_usdt = self._calc_pnl(side, live_entry, close_price, quantity)
                    return CloseResult(
                        success=True,
                        symbol=symbol,
                        close_price=close_price,
                        quantity=quantity,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=pnl_pct,
                        close_reason=close_reason,
                    )

                # ── 2. Get precision ──────────────────────────────────
                prec = await self._get_precision(symbol)
                qty_rounded = self._round_qty(
                    live_qty,
                    prec["qty_precision"],
                    prec["step_size"],
                )
                if qty_rounded <= 0:
                    qty_rounded = prec["min_qty"] if prec["min_qty"] > 0 else live_qty
                qty_str = self._format_qty(qty_rounded, prec["qty_precision"])

                # ── 3. Cancel stale open orders (TP/SL from main bot) ─
                await self.cancel_all_orders(symbol)

                # ── 4. Place MARKET close order ───────────────────────
                params: dict = {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": qty_str,
                }

                if is_hedge_mode and pos_side_live in ("LONG", "SHORT"):
                    params["positionSide"] = pos_side_live
                else:
                    params["reduceOnly"] = "true"

                result = await self._signed_request("POST", "/fapi/v1/order", params)
                order_id = result.get("orderId")
                avg_price = float(result.get("avgPrice", 0)) or float(result.get("price", 0))

                logger.info(
                    f"  [CloseEngine] ✅ {symbol} closed: "
                    f"order=#{order_id} qty={qty_str} "
                    f"avg_price={avg_price} reason={close_reason}"
                )

                # ── 5. Verify position gone (short wait for propagation) ──
                await asyncio.sleep(1.0)
                remaining = await self.get_live_position(symbol)
                if remaining:
                    remaining_qty = abs(float(remaining.get("positionAmt", 0)))
                    if remaining_qty > 0:
                        logger.warning(
                            f"  [CloseEngine] ⚠️ {symbol} still has qty={remaining_qty} "
                            f"after close — will retry"
                        )
                        if attempt < CLOSE_MAX_RETRIES:
                            await asyncio.sleep(CLOSE_RETRY_DELAY)
                            continue

                # ── 6. Calculate PnL ──────────────────────────────────
                close_price = avg_price if avg_price > 0 else await self.get_market_price(symbol)
                pnl_pct, pnl_usdt = self._calc_pnl(side, live_entry, close_price, qty_rounded)

                return CloseResult(
                    success=True,
                    symbol=symbol,
                    close_price=close_price,
                    quantity=qty_rounded,
                    pnl_usdt=round(pnl_usdt, 4),
                    pnl_pct=round(pnl_pct, 4),
                    close_reason=close_reason,
                    order_id=order_id,
                )

            except Exception as e:
                logger.error(
                    f"  [CloseEngine] ❌ Close attempt {attempt}/{CLOSE_MAX_RETRIES} "
                    f"failed for {symbol}: {e}"
                )
                if attempt < CLOSE_MAX_RETRIES:
                    await asyncio.sleep(CLOSE_RETRY_DELAY)

        # All attempts failed
        logger.critical(
            f"  [CloseEngine] 🔥 FAILED to close {symbol} after "
            f"{CLOSE_MAX_RETRIES} attempts — MANUAL ACTION REQUIRED"
        )
        return CloseResult(
            success=False,
            symbol=symbol,
            close_price=0.0,
            quantity=quantity,
            pnl_usdt=0.0,
            pnl_pct=0.0,
            close_reason=close_reason,
            error=f"Close failed after {CLOSE_MAX_RETRIES} retries",
        )

    # ── PnL Calculation ───────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(
        side: str,
        entry_price: float,
        close_price: float,
        quantity: float,
    ) -> tuple[float, float]:
        """
        Returns (pnl_pct, pnl_usdt).
        pnl_pct is the percentage return on the position (not leveraged ROE).
        """
        if entry_price <= 0 or quantity <= 0:
            return 0.0, 0.0

        if side == "BUY":  # Long
            pnl_usdt = (close_price - entry_price) * quantity
            pnl_pct = (close_price - entry_price) / entry_price * 100
        else:  # Short
            pnl_usdt = (entry_price - close_price) * quantity
            pnl_pct = (entry_price - close_price) / entry_price * 100

        return round(pnl_pct, 4), round(pnl_usdt, 4)
