"""
Binance Futures Execution Module
Handles order placement, precision handling, leverage setting,
and position monitoring.
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


@dataclass
class PrecisionInfo:
    symbol: str
    quantity_precision: int
    price_precision: int
    min_qty: float
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
    Executes trades on Binance Futures with proper precision,
    leverage management, and SL/TP placement.
    """

    def __init__(self):
        self.api_key    = settings.BINANCE_API_KEY
        self.secret_key = settings.BINANCE_SECRET_KEY
        self.base_url   = settings.binance_base_url

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
        """Make an authenticated signed request to Binance Futures API"""
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
        """Fetch the current market price for a symbol"""
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

    # ─── Precision ────────────────────────────────────────────────────

    async def get_precision(self, symbol: str) -> PrecisionInfo:
        """
        Fetch exchange info and extract quantity/price precision for a symbol.
        Critical for avoiding LOT_SIZE and PRICE_FILTER errors.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
            resp.raise_for_status()
            data = resp.json()

        for s in data["symbols"]:
            if s["symbol"] == symbol:
                qty_precision   = s["quantityPrecision"]
                price_precision = s["pricePrecision"]
                min_qty         = 0.0
                min_notional    = 5.0  # Binance Futures minimum ~5 USDT

                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        min_qty = float(f["minQty"])
                    if f["filterType"] == "MIN_NOTIONAL":
                        min_notional = float(f.get("notional", 5.0))

                return PrecisionInfo(
                    symbol=symbol,
                    quantity_precision=qty_precision,
                    price_precision=price_precision,
                    min_qty=min_qty,
                    min_notional=min_notional,
                )

        raise ValueError(f"Symbol {symbol} not found in exchange info")

    def format_quantity(self, qty: float, precision: int) -> str:
        return f"{qty:.{precision}f}"

    def format_price(self, price: float, precision: int) -> str:
        return f"{price:.{precision}f}"

    # ─── Account / Position Checks ────────────────────────────────────

    async def get_open_positions(self) -> list[dict]:
        """Return list of symbols with non-zero position size"""
        data = await self._signed_request("GET", "/fapi/v2/positionRisk")
        return [p for p in data if float(p["positionAmt"]) != 0]

    async def has_open_position(self, symbol: str) -> bool:
        positions = await self.get_open_positions()
        return any(p["symbol"] == symbol for p in positions)

    async def get_account_balance(self) -> float:
        """Returns available USDT balance"""
        data = await self._signed_request("GET", "/fapi/v2/balance")
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    # ─── Trade Execution ──────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._signed_request(
            "POST", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        """Set margin type; ignore error if already set"""
        try:
            await self._signed_request(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
            )
        except Exception:
            pass  # -4046 = already set

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        precision: PrecisionInfo,
    ) -> dict:
        """Place a market order"""
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        params = {
            "symbol": symbol,
            "side": side,          # BUY | SELL
            "type": "MARKET",
            "quantity": qty_str,
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_stop_loss(
        self,
        symbol: str,
        side: str,           # Closing side (opposite of position)
        quantity: float,
        stop_price: float,
        precision: PrecisionInfo,
    ) -> dict:
        """Place a STOP_MARKET order for stop-loss"""
        qty_str   = self.format_quantity(quantity, precision.quantity_precision)
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

    async def place_take_profit(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        precision: PrecisionInfo,
    ) -> dict:
        """Place a TAKE_PROFIT_MARKET order"""
        qty_str   = self.format_quantity(quantity, precision.quantity_precision)
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

    # ─── Simple USDT-Based Execution ─────────────────────────────────

    async def execute_simple(self, symbol: str, side: str, usdt_amount: float) -> dict:
        """
        Simple trade execution from USDT amount:
        1. Fetch current market price
        2. Get symbol precision
        3. Convert USDT → quantity (quantity = usdt_amount / price)
        4. Validate quantity and notional
        5. Place market order

        Returns a clean result dict.
        """
        logger.info(f"⚡ Simple execute: {side} ${usdt_amount} of {symbol}...")

        # 1. Fetch current market price
        price = await self.get_market_price(symbol)
        logger.info(f"  Market price: ${price}")

        # 2. Get symbol precision
        precision = await self.get_precision(symbol)

        # 3. Convert USDT → quantity
        raw_quantity = usdt_amount / price
        quantity = round(raw_quantity, precision.quantity_precision)
        logger.info(f"  Quantity: {raw_quantity} → rounded to {quantity} (precision={precision.quantity_precision})")

        # 4. Validate
        if quantity <= 0:
            raise ValueError(
                f"Quantity is 0 after rounding. "
                f"usdt_amount={usdt_amount}, price={price}, precision={precision.quantity_precision}"
            )

        notional = quantity * price
        if notional < precision.min_notional:
            raise ValueError(
                f"Notional {notional:.2f} USDT below minimum {precision.min_notional}. "
                f"Increase usdt_amount (currently {usdt_amount})"
            )

        if quantity < precision.min_qty:
            raise ValueError(
                f"Quantity {quantity} below minimum {precision.min_qty} for {symbol}"
            )

        # 5. Set margin type (ignore if already set)
        await self.set_margin_type(symbol, "ISOLATED")

        # 6. Place market order
        order = await self.place_market_order(symbol, side, quantity, precision)
        order_id = order.get("orderId")
        logger.info(f"  ✅ Market order placed: #{order_id}")

        return {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "notional": round(notional, 2),
        }

    # ─── Full Trade Flow (with SL/TP) ────────────────────────────────

    async def execute_trade(self, params) -> OrderResult:
        """
        Full trade execution flow with SL/TP:
        1. Validate precision
        2. Set leverage + margin type
        3. Place market order
        4. Place SL + TP bracket orders
        """
        symbol = params.symbol
        logger.info(f"⚡ Executing {params.side} trade on {symbol}...")

        try:
            # Get precision info
            precision = await self.get_precision(symbol)

            # Validate minimum notional
            notional = params.quantity * params.entry_price
            if notional < precision.min_notional:
                raise ValueError(
                    f"Notional {notional:.2f} USDT below minimum {precision.min_notional}"
                )
            if params.quantity < precision.min_qty:
                raise ValueError(
                    f"Quantity {params.quantity} below minimum {precision.min_qty}"
                )

            # Configure margin and leverage
            await self.set_margin_type(symbol, "ISOLATED")
            await self.set_leverage(symbol, params.leverage)
            logger.info(f"  Leverage set to {params.leverage}x (ISOLATED)")

            # Place market order
            order = await self.place_market_order(
                symbol, params.side, params.quantity, precision
            )
            order_id = order["orderId"]
            logger.info(f"  ✅ Market order placed: #{order_id}")

            # Determine closing side
            close_side = "SELL" if params.side == "BUY" else "BUY"

            # Place SL
            sl_order = await self.place_stop_loss(
                symbol, close_side, params.quantity, params.stop_loss, precision
            )
            logger.info(f"  🛡️  Stop-loss set at {params.stop_loss}")

            # Place TP
            tp_order = await self.place_take_profit(
                symbol, close_side, params.quantity, params.take_profit, precision
            )
            logger.info(f"  🎯 Take-profit set at {params.take_profit}")

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
            logger.error(f"  ❌ Trade execution failed: {e}")
            return OrderResult(
                success=False,
                order_id=None,
                symbol=symbol,
                side=params.side,
                quantity=params.quantity,
                entry_price=params.entry_price,
                error=str(e),
            )
