"""
V3 Binance Futures Execution Module
Handles order placement, precision handling, leverage setting,
multi-account execution, exchange filter caching, TP/SL with retry,
and pre-entry quality checks.

V3 Changes:
  - TP/SL use closePosition=true for reliable closing
  - 3-retry loop for SL and TP placement
  - Pre-entry spread/chase/fee checks
  - Get actual fill price from order response
  - Limit order support for better fills
"""

import asyncio
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

# ── TP/SL retry config ───────────────────────────────────────────────
TP_SL_MAX_RETRIES = 3
TP_SL_RETRY_DELAY = 1.0  # seconds


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
    fill_price: float = 0.0           # V3: actual fill price
    stop_loss_order_id: Optional[int] = None
    take_profit_order_id: Optional[int] = None
    sl_attached: bool = False          # V3: tracking
    tp_attached: bool = False          # V3: tracking
    error: Optional[str] = None


@dataclass
class PreEntryCheck:
    """V3: Pre-entry quality check result."""
    passed: bool
    reason: str = ""
    spread_pct: float = 0.0
    slippage_estimate: float = 0.0
    fee_impact_pct: float = 0.0


class BinanceExecutor:
    """
    V3 Executor — supports per-account API credentials.
    Caches exchange info, retries TP/SL placement, pre-entry checks.
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

    async def get_book_ticker(self, symbol: str) -> dict:
        """Get best bid/ask for a symbol."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/ticker/bookTicker",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "bid": float(data.get("bidPrice", 0)),
                "ask": float(data.get("askPrice", 0)),
                "bid_qty": float(data.get("bidQty", 0)),
                "ask_qty": float(data.get("askQty", 0)),
            }

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

    # ─── V3: Pre-Entry Quality Checks ─────────────────────────────────

    async def pre_entry_check(
        self,
        symbol: str,
        side: str,
        tp_pct: float,
        atr: float = 0.0,
        entry_price: float = 0.0,
    ) -> PreEntryCheck:
        """
        V3: Pre-entry quality checks to prevent instant negative trades.
        1. Spread check (skip if > 0.10%)
        2. Candle chase detection (skip if last candle > 2x ATR)
        3. Fee impact check (skip if fees > 30% of TP reward)
        """
        try:
            book = await self.get_book_ticker(symbol)
            bid, ask = book["bid"], book["ask"]

            if bid <= 0 or ask <= 0:
                return PreEntryCheck(passed=False, reason="Invalid bid/ask prices")

            # 1. Spread check
            spread_pct = ((ask - bid) / bid) * 100
            max_spread = settings.MAX_SPREAD_ENTRY_PCT
            if spread_pct > max_spread:
                return PreEntryCheck(
                    passed=False,
                    reason=f"Spread too wide: {spread_pct:.4f}% > {max_spread}%",
                    spread_pct=spread_pct,
                )

            # 2. Slippage estimate (based on spread)
            slippage_estimate = spread_pct / 2  # Half-spread as slippage estimate

            # 3. Fee impact check
            # Binance taker fee = 0.04% (VIP0), maker = 0.02%
            # Round trip = 0.08% minimum
            fee_pct = 0.08  # Round-trip taker fees
            total_cost_pct = fee_pct + spread_pct  # Fees + spread
            if tp_pct > 0:
                fee_impact = (total_cost_pct / tp_pct) * 100
                if fee_impact > 30:
                    return PreEntryCheck(
                        passed=False,
                        reason=f"Fee impact too high: {fee_impact:.1f}% of TP reward consumed by fees+spread",
                        spread_pct=spread_pct,
                        fee_impact_pct=fee_impact,
                    )

            # 4. Candle chase filter (if ATR available)
            if atr > 0 and entry_price > 0:
                price = await self.get_market_price(symbol)
                price_diff = abs(price - entry_price)
                if price_diff > atr * 2:
                    return PreEntryCheck(
                        passed=False,
                        reason=f"Candle chase detected: price moved {price_diff:.6f} > 2×ATR ({atr * 2:.6f})",
                        spread_pct=spread_pct,
                    )

            return PreEntryCheck(
                passed=True,
                spread_pct=round(spread_pct, 4),
                slippage_estimate=round(slippage_estimate, 4),
                fee_impact_pct=round((fee_pct + spread_pct) / tp_pct * 100, 1) if tp_pct > 0 else 0,
            )

        except Exception as e:
            logger.warning(f"Pre-entry check failed for {symbol}: {e}")
            # Don't block trade on check failure — just warn
            return PreEntryCheck(passed=True, reason=f"Check failed: {e}")

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

    async def place_limit_order(self, symbol: str, side: str, quantity: float, price: float, precision: PrecisionInfo) -> dict:
        """V3: Limit order for better fills on liquid pairs."""
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        price_str = self.format_price(price, precision.price_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": qty_str,
            "price": price_str,
            "timeInForce": "GTC",
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    # ─── V3: TP/SL with closePosition + Retry ────────────────────────

    async def place_stop_loss(self, symbol: str, side: str, stop_price: float, precision: PrecisionInfo) -> dict:
        """V3: Uses closePosition=true for reliable SL. No quantity needed."""
        price_str = self.format_price(stop_price, precision.price_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": price_str,
            "closePosition": "true",
            "timeInForce": "GTE_GTC",
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_take_profit(self, symbol: str, side: str, stop_price: float, precision: PrecisionInfo) -> dict:
        """V3: Uses closePosition=true for reliable TP. No quantity needed."""
        price_str = self.format_price(stop_price, precision.price_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": price_str,
            "closePosition": "true",
            "timeInForce": "GTE_GTC",
        }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def _place_with_retry(
        self,
        place_func,
        order_type: str,
        symbol: str,
        side: str,
        stop_price: float,
        precision: PrecisionInfo,
    ) -> tuple[Optional[dict], str]:
        """
        V3: Retry TP/SL placement up to 3 times.
        Returns (order_result, error_message).
        """
        last_error = ""
        for attempt in range(1, TP_SL_MAX_RETRIES + 1):
            try:
                result = await place_func(symbol, side, stop_price, precision)
                logger.info(f"  ✅ {order_type} placed on attempt {attempt}: #{result.get('orderId')}")
                return result, ""
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"  ⚠️ {order_type} attempt {attempt}/{TP_SL_MAX_RETRIES} failed: {e}"
                )
                if attempt < TP_SL_MAX_RETRIES:
                    await asyncio.sleep(TP_SL_RETRY_DELAY)

        logger.error(f"  ❌ {order_type} FAILED after {TP_SL_MAX_RETRIES} retries: {last_error}")
        return None, last_error

    def _get_fill_price(self, order_response: dict) -> float:
        """V3: Extract actual fill price from market order response."""
        # Try avgPrice first (most accurate)
        avg_price = float(order_response.get("avgPrice", 0))
        if avg_price > 0:
            return avg_price
        # Fallback to price field
        price = float(order_response.get("price", 0))
        if price > 0:
            return price
        return 0.0

    # ─── V3: Full Trade Flow (with SL/TP Retry) ──────────────────────

    async def execute_trade(self, params, telegram_notifier=None) -> OrderResult:
        """
        V3 Full trade execution flow:
        1. Get precision
        2. Validate notional + min qty
        3. Set leverage + margin type
        4. Place market order
        5. Get actual fill price
        6. Recalculate TP/SL from fill price
        7. Place SL + TP with retry (closePosition=true)
        8. Alert on failure
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

            # V3: Get actual fill price
            fill_price = self._get_fill_price(order)
            if fill_price <= 0:
                fill_price = params.entry_price  # Fallback to estimated price

            # V3: Recalculate TP/SL from actual fill price
            if hasattr(params, 'tp_pct') and params.tp_pct > 0:
                tp_pct_decimal = params.tp_pct / 100.0
                sl_pct_decimal = params.sl_pct / 100.0
                if params.side == "BUY":
                    actual_tp = round(fill_price * (1 + tp_pct_decimal), precision.price_precision)
                    actual_sl = round(fill_price * (1 - sl_pct_decimal), precision.price_precision)
                else:
                    actual_tp = round(fill_price * (1 - tp_pct_decimal), precision.price_precision)
                    actual_sl = round(fill_price * (1 + sl_pct_decimal), precision.price_precision)
            else:
                actual_tp = params.take_profit
                actual_sl = params.stop_loss

            close_side = "SELL" if params.side == "BUY" else "BUY"

            # V3: SL with retry
            sl_order, sl_error = await self._place_with_retry(
                self.place_stop_loss, "STOP_LOSS",
                symbol, close_side, actual_sl, precision,
            )

            # V3: TP with retry
            tp_order, tp_error = await self._place_with_retry(
                self.place_take_profit, "TAKE_PROFIT",
                symbol, close_side, actual_tp, precision,
            )

            # V3: Alert on TP/SL failure via Telegram
            sl_attached = sl_order is not None
            tp_attached = tp_order is not None

            if not sl_attached or not tp_attached:
                failure_msg = []
                if not sl_attached:
                    failure_msg.append(f"SL: {sl_error}")
                if not tp_attached:
                    failure_msg.append(f"TP: {tp_error}")
                error_detail = " | ".join(failure_msg)
                logger.error(f"  🚨 TP/SL INCOMPLETE for {symbol}: {error_detail}")

                if telegram_notifier:
                    await telegram_notifier.tp_sl_failed(
                        symbol=symbol, side=params.side,
                        sl_attached=sl_attached, tp_attached=tp_attached,
                        error=error_detail,
                    )

            logger.info(
                f"  📊 Trade summary: fill={fill_price} TP={actual_tp} SL={actual_sl} "
                f"SL_ok={sl_attached} TP_ok={tp_attached}"
            )

            return OrderResult(
                success=True,
                order_id=order_id,
                symbol=symbol,
                side=params.side,
                quantity=params.quantity,
                entry_price=params.entry_price,
                fill_price=fill_price,
                stop_loss_order_id=sl_order.get("orderId") if sl_order else None,
                take_profit_order_id=tp_order.get("orderId") if tp_order else None,
                sl_attached=sl_attached,
                tp_attached=tp_attached,
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
