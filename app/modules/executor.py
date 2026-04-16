"""
V2 Binance Futures Execution Module
Handles order placement, precision handling, leverage setting,
multi-account execution, and exchange filter caching.
"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Exchange info cache (shared across accounts) ──────────────────────
_exchange_info_cache: dict = {}
_exchange_info_ts: float = 0.0
CACHE_TTL = 300  # 5 minutes


@dataclass
class PrecisionInfo:
    symbol: str
    quantity_precision: int
    price_precision: int
    min_qty: float
    step_size: float
    tick_size: float
    min_notional: float


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[int]
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss_order_id: Optional[int] = None
    take_profit_order_id: Optional[int] = None
    error: Optional[str] = None


class BinanceExecutor:
    """
    V2 Executor — supports per-account API credentials.
    Caches exchange info to reduce API calls.
    """

    def __init__(self, api_key: str = None, secret_key: str = None, testnet: bool = None):
        self.api_key = api_key or settings.BINANCE_API_KEY
        self.secret_key = secret_key or settings.BINANCE_SECRET_KEY
        self.is_testnet = testnet if testnet is not None else settings.BINANCE_TESTNET
        self.base_url = (
            "https://testnet.binancefuture.com"
            if self.is_testnet
            else "https://fapi.binance.com"
        )

    # ─── Signing ──────────────────────────────────────────────────────

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

            resp.raise_for_status()
            return resp.json()

    # ─── Price Fetching ───────────────────────────────────────────────

    async def get_market_price(self, symbol: str) -> float:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/ticker/price",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data["price"])
            if price <= 0:
                raise ValueError(f"Invalid price {price} for {symbol}")
            return price

    # ─── Precision (with caching) ─────────────────────────────────────

    async def get_precision(self, symbol: str) -> PrecisionInfo:
        """
        Fetch exchange info with 5-minute cache.
        Extracts LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL filters.
        """
        global _exchange_info_cache, _exchange_info_ts

        now = time.time()
        if not _exchange_info_cache or (now - _exchange_info_ts) > CACHE_TTL:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
                resp.raise_for_status()
                data = resp.json()

            _exchange_info_cache = {s["symbol"]: s for s in data["symbols"]}
            _exchange_info_ts = now
            logger.info(f"  Refreshed exchange info cache: {len(_exchange_info_cache)} symbols")

        sym_info = _exchange_info_cache.get(symbol)
        if not sym_info:
            raise ValueError(f"Symbol {symbol} not found in exchange info")

        qty_precision = sym_info["quantityPrecision"]
        price_precision = sym_info["pricePrecision"]
        min_qty = 0.0
        step_size = 0.0
        tick_size = 0.0
        min_notional = 5.0

        for f in sym_info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                min_qty = float(f["minQty"])
                step_size = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                min_notional = float(f.get("notional", 5.0))

        return PrecisionInfo(
            symbol=symbol,
            quantity_precision=qty_precision,
            price_precision=price_precision,
            min_qty=min_qty,
            step_size=step_size,
            tick_size=tick_size,
            min_notional=min_notional,
        )

    def format_quantity(self, qty: float, precision: int) -> str:
        return f"{qty:.{precision}f}"

    def format_price(self, price: float, precision: int) -> str:
        return f"{price:.{precision}f}"

    # ─── Account / Position Checks ────────────────────────────────────

    async def get_open_positions(self) -> list[dict]:
        data = await self._signed_request("GET", "/fapi/v2/positionRisk")
        return [p for p in data if float(p["positionAmt"]) != 0]

    async def has_open_position(self, symbol: str) -> bool:
        positions = await self.get_open_positions()
        return any(p["symbol"] == symbol for p in positions)

    async def get_account_balance(self) -> float:
        """Returns available USDT balance."""
        data = await self._signed_request("GET", "/fapi/v2/balance")
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    async def get_full_balance_info(self) -> dict:
        """Returns balance + margin info for USDT."""
        data = await self._signed_request("GET", "/fapi/v2/balance")
        for asset in data:
            if asset["asset"] == "USDT":
                return {
                    "balance": float(asset.get("balance", 0)),
                    "available": float(asset.get("availableBalance", 0)),
                    "cross_unrealized_pnl": float(asset.get("crossUnPnl", 0)),
                }
        return {"balance": 0, "available": 0, "cross_unrealized_pnl": 0}

    # ─── Trade Execution ──────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._signed_request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        try:
            await self._signed_request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
            )
        except Exception:
            pass  # -4046 = already set

    async def place_market_order(self, symbol: str, side: str, quantity: float, precision: PrecisionInfo) -> dict:
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty_str,
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float, precision: PrecisionInfo) -> dict:
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        price_str = self.format_price(stop_price, precision.price_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": price_str,
            "quantity": qty_str,
            "reduceOnly": "true",
            "timeInForce": "GTE_GTC",
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_take_profit(self, symbol: str, side: str, quantity: float, stop_price: float, precision: PrecisionInfo) -> dict:
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        price_str = self.format_price(stop_price, precision.price_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": price_str,
            "quantity": qty_str,
            "reduceOnly": "true",
            "timeInForce": "GTE_GTC",
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    # ─── Full Trade Flow (with SL/TP) ────────────────────────────────

    async def execute_trade(self, params) -> OrderResult:
        """
        Full trade execution flow:
        1. Get precision
        2. Validate notional + min qty
        3. Set leverage + margin type
        4. Place market order
        5. Place SL + TP bracket orders
        """
        symbol = params.symbol
        logger.info(f"⚡ Executing {params.side} on {symbol} (lev={params.leverage}x qty={params.quantity})...")

        try:
            precision = await self.get_precision(symbol)

            # Validate notional
            notional = params.quantity * params.entry_price
            if notional < precision.min_notional:
                raise ValueError(f"Notional {notional:.2f} below min {precision.min_notional}")
            if params.quantity < precision.min_qty:
                raise ValueError(f"Qty {params.quantity} below min {precision.min_qty}")

            # Configure
            await self.set_margin_type(symbol, "ISOLATED")
            await self.set_leverage(symbol, params.leverage)

            # Market order
            order = await self.place_market_order(symbol, params.side, params.quantity, precision)
            order_id = order["orderId"]
            logger.info(f"  ✅ Market order: #{order_id}")

            close_side = "SELL" if params.side == "BUY" else "BUY"

            # SL
            sl_order = await self.place_stop_loss(
                symbol, close_side, params.quantity, params.stop_loss, precision
            )
            logger.info(f"  🛡️ SL at {params.stop_loss}")

            # TP
            tp_order = await self.place_take_profit(
                symbol, close_side, params.quantity, params.take_profit, precision
            )
            logger.info(f"  🎯 TP at {params.take_profit}")

            return OrderResult(
                success=True,
                order_id=order_id,
                symbol=symbol,
                side=params.side,
                quantity=params.quantity,
                entry_price=params.entry_price,
                stop_loss_order_id=sl_order.get("orderId"),
                take_profit_order_id=tp_order.get("orderId"),
            )

        except Exception as e:
            logger.error(f"  ❌ Execution failed: {e}")
            return OrderResult(
                success=False,
                order_id=None,
                symbol=symbol,
                side=params.side,
                quantity=params.quantity,
                entry_price=params.entry_price,
                error=str(e),
            )

    # ─── Simple USDT-Based Execution (backward compat) ────────────────

    async def execute_simple(self, symbol: str, side: str, usdt_amount: float) -> dict:
        """Simple trade from USDT amount — no SL/TP."""
        logger.info(f"⚡ Simple execute: {side} ${usdt_amount} of {symbol}...")

        price = await self.get_market_price(symbol)
        precision = await self.get_precision(symbol)
        raw_quantity = usdt_amount / price
        quantity = round(raw_quantity, precision.quantity_precision)

        if quantity <= 0:
            raise ValueError(f"Quantity is 0 after rounding")

        notional = quantity * price
        if notional < precision.min_notional:
            raise ValueError(f"Notional {notional:.2f} below min {precision.min_notional}")
        if quantity < precision.min_qty:
            raise ValueError(f"Quantity {quantity} below min {precision.min_qty}")

        await self.set_margin_type(symbol, "ISOLATED")
        order = await self.place_market_order(symbol, side, quantity, precision)
        order_id = order.get("orderId")

        return {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "notional": round(notional, 2),
        }
