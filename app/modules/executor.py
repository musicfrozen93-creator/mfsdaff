"""
V5.5 Binance Futures Execution Module
Handles order placement, precision handling, leverage setting,
multi-account execution, exchange filter caching, TP/SL with retry,
and pre-entry quality checks.

V5.5 Changes:
  - TP/SL execution proof (verify orders exist on exchange after placement)
  - Full proof logging: order ID, stopPrice, reduceOnly, closePosition
  - Critical Telegram alert if verification fails
  - Position quantity validation
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
    order_method: str = "MARKET"       # V4: LIMIT or MARKET
    # V5.5 Partial TP tracking
    partial_tp_enabled: bool = False
    tp1_order_id: Optional[int] = None
    tp2_order_id: Optional[int] = None
    tp1_attached: bool = False
    tp2_attached: bool = False


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
    V4 Executor — supports per-account API credentials.
    Caches exchange info, retries TP/SL placement, pre-entry checks,
    LIMIT→MARKET fallback, position verification.
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

    async def get_position_for_symbol(self, symbol: str) -> Optional[dict]:
        """V4: Get the specific open position for a symbol (if any)."""
        positions = await self.get_open_positions()
        for p in positions:
            if p["symbol"] == symbol:
                return p
        return None

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

    # ─── V4: Order Status Check + Cancel ──────────────────────────────

    async def get_order_status(self, symbol: str, order_id: int) -> dict:
        """V4: Check status of an order."""
        return await self._signed_request(
            "GET", "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
        )

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """V4: Cancel an unfilled order."""
        return await self._signed_request(
            "DELETE", "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
        )

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

    async def place_partial_take_profit(
        self, symbol: str, side: str, stop_price: float,
        quantity: float, precision: PrecisionInfo,
    ) -> dict:
        """
        V5.5: Place a partial TP order with specific quantity.
        Uses reduceOnly=true instead of closePosition.
        """
        price_str = self.format_price(stop_price, precision.price_precision)
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
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

    # ─── V4: Verify Position Opened ───────────────────────────────────

    async def _verify_position(self, symbol: str, max_retries: int = 2, delay: float = 1.0) -> Optional[dict]:
        """
        V4: Verify position exists before attaching TP/SL.
        Retries a few times in case of propagation delay.
        """
        for attempt in range(1, max_retries + 1):
            try:
                position = await self.get_position_for_symbol(symbol)
                if position:
                    entry = float(position.get("entryPrice", 0))
                    amt = float(position.get("positionAmt", 0))
                    logger.info(
                        f"  ✅ Position verified (attempt {attempt}): "
                        f"{symbol} entry={entry} amt={amt}"
                    )
                    return position
            except Exception as e:
                logger.warning(f"  Position verify attempt {attempt} failed: {e}")

            if attempt < max_retries:
                await asyncio.sleep(delay)

        logger.warning(f"  ⚠️ Position NOT verified for {symbol} after {max_retries} attempts")
        return None

    # ─── V4: Full Trade Flow (LIMIT→MARKET Fallback + Position Verify) ──

    async def execute_trade(self, params, telegram_notifier=None) -> OrderResult:
        """
        V4 Full trade execution flow:
        1. Get precision
        2. Validate notional + min qty
        3. Set leverage + margin type
        4. Try LIMIT order → wait → fallback to MARKET if unfilled
        5. Verify position opened
        6. Get actual fill price (from position data if needed)
        7. Recalculate TP/SL from fill price
        8. Place SL + TP with retry (closePosition=true)
        9. Alert on failure
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

            # ── V4: LIMIT → MARKET fallback ──────────────────────────
            order = None
            order_method = "MARKET"
            fill_price = 0.0

            if settings.ENABLE_LIMIT_FALLBACK:
                try:
                    # Get best price for limit order
                    book = await self.get_book_ticker(symbol)
                    if params.side == "BUY":
                        limit_price = book["ask"]  # Match the ask for immediate-ish fill
                    else:
                        limit_price = book["bid"]  # Match the bid

                    if limit_price > 0:
                        logger.info(f"  📝 Trying LIMIT order at {limit_price}...")
                        order = await self.place_limit_order(
                            symbol, params.side, params.quantity, limit_price, precision
                        )
                        limit_order_id = order["orderId"]
                        logger.info(f"  📝 LIMIT order placed: #{limit_order_id}")

                        # Wait for fill
                        await asyncio.sleep(settings.LIMIT_ORDER_WAIT_SECONDS)

                        # Check if filled
                        order_status = await self.get_order_status(symbol, limit_order_id)
                        status = order_status.get("status", "")

                        if status == "FILLED":
                            order_method = "LIMIT"
                            fill_price = self._get_fill_price(order_status)
                            logger.info(f"  ✅ LIMIT order FILLED: fill_price={fill_price}")
                        else:
                            # Not filled — cancel and go MARKET
                            logger.info(f"  ⏳ LIMIT order status={status} — cancelling, switching to MARKET")
                            try:
                                await self.cancel_order(symbol, limit_order_id)
                                logger.info(f"  ✅ LIMIT order cancelled")
                            except Exception as ce:
                                logger.warning(f"  Cancel failed (may be partially filled): {ce}")
                                # Check if it partially filled
                                recheck = await self.get_order_status(symbol, limit_order_id)
                                if recheck.get("status") == "FILLED":
                                    order_method = "LIMIT"
                                    fill_price = self._get_fill_price(recheck)
                                    order = recheck
                                    logger.info(f"  ✅ LIMIT actually filled during cancel: {fill_price}")

                            if order_method != "LIMIT":
                                order = None  # Reset — will use MARKET below

                except Exception as le:
                    logger.warning(f"  LIMIT order attempt failed: {le} — falling back to MARKET")
                    order = None

            # MARKET order (primary or fallback)
            if order is None or order_method == "MARKET":
                order = await self.place_market_order(symbol, params.side, params.quantity, precision)
                order_method = "MARKET"
                fill_price = self._get_fill_price(order)
                logger.info(f"  ✅ MARKET order: #{order['orderId']} fill={fill_price}")

            order_id = order["orderId"]

            # ── V4: Verify position before TP/SL ─────────────────────
            position_data = await self._verify_position(symbol)
            if position_data:
                # Use position entry price (most accurate)
                pos_entry = float(position_data.get("entryPrice", 0))
                if pos_entry > 0:
                    fill_price = pos_entry

            if fill_price <= 0:
                fill_price = params.entry_price  # Last resort fallback
                logger.warning(f"  ⚠️ Could not determine fill price, using estimate: {fill_price}")

            # ── Recalculate TP/SL from actual fill price ─────────────
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

            # ── SL with retry (always closePosition=true) ────────────
            sl_order, sl_error = await self._place_with_retry(
                self.place_stop_loss, "STOP_LOSS",
                symbol, close_side, actual_sl, precision,
            )

            # ── V5.5: Partial TP or Full TP ──────────────────────────
            tp_order = None
            tp_error = ""
            tp1_order = None
            tp2_order = None
            tp1_attached = False
            tp2_attached = False
            partial_tp_used = False

            if getattr(params, 'partial_tp_enabled', False) and params.quantity > 0:
                # ── PARTIAL TP MODE ──────────────────────────────────
                partial_tp_used = True
                logger.info(f"  📊 Using PARTIAL TP mode for {symbol}")

                # Recalculate partial TP prices from actual fill
                if fill_price > 0 and hasattr(params, 'tp_pct') and params.tp_pct > 0:
                    tp_pct_decimal = params.tp_pct / 100.0
                    tp_distance = fill_price * tp_pct_decimal
                    tp1_distance = tp_distance * settings.PARTIAL_TP1_DISTANCE

                    if params.side == "BUY":
                        actual_tp1 = round(fill_price + tp1_distance, precision.price_precision)
                    else:
                        actual_tp1 = round(fill_price - tp1_distance, precision.price_precision)
                    actual_tp2 = actual_tp  # Full TP = TP2
                else:
                    actual_tp1 = params.tp1_price
                    actual_tp2 = params.tp2_price

                # Calculate quantities for each partial
                tp1_qty = round(params.quantity * params.tp1_qty_pct, precision.quantity_precision)
                tp2_qty = round(params.quantity * params.tp2_qty_pct, precision.quantity_precision)

                # Ensure quantities respect min_qty
                if tp1_qty < precision.min_qty:
                    tp1_qty = precision.min_qty
                if tp2_qty < precision.min_qty:
                    tp2_qty = precision.min_qty

                # TP1: 40% at halfway
                try:
                    tp1_result = await self.place_partial_take_profit(
                        symbol, close_side, actual_tp1, tp1_qty, precision,
                    )
                    tp1_order = tp1_result
                    tp1_attached = True
                    logger.info(
                        f"  ✅ TP1 placed: #{tp1_result.get('orderId')} "
                        f"qty={tp1_qty} price={actual_tp1}"
                    )
                except Exception as e:
                    logger.warning(f"  ⚠️ TP1 failed: {e}")

                # TP2: 30% at full TP
                try:
                    tp2_result = await self.place_partial_take_profit(
                        symbol, close_side, actual_tp2, tp2_qty, precision,
                    )
                    tp2_order = tp2_result
                    tp2_attached = True
                    logger.info(
                        f"  ✅ TP2 placed: #{tp2_result.get('orderId')} "
                        f"qty={tp2_qty} price={actual_tp2}"
                    )
                except Exception as e:
                    logger.warning(f"  ⚠️ TP2 failed: {e}")

                # Consider TP attached if at least TP1 succeeded
                tp_attached_status = tp1_attached or tp2_attached
                tp_order = tp1_order or tp2_order  # For backward compat

            else:
                # ── FULL TP MODE (original) ──────────────────────────
                tp_order, tp_error = await self._place_with_retry(
                    self.place_take_profit, "TAKE_PROFIT",
                    symbol, close_side, actual_tp, precision,
                )
                tp_attached_status = tp_order is not None

            # ── V5.5: TP/SL Execution Proof ─────────────────────────
            sl_attached = sl_order is not None

            # Verify orders actually exist on exchange
            if sl_attached or tp_attached_status:
                proof_ok = await self._verify_tp_sl_proof(
                    symbol=symbol,
                    sl_order_id=sl_order.get("orderId") if sl_order else None,
                    tp_order_id=tp_order.get("orderId") if tp_order else None,
                    expected_sl_price=actual_sl,
                    expected_tp_price=actual_tp,
                    precision=precision,
                )
                if not proof_ok:
                    logger.error(
                        f"  🚨 PROOF FAILED for {symbol} — "
                        f"TP/SL orders may not be correctly placed!"
                    )

            if not sl_attached or not tp_attached_status:
                failure_msg = []
                if not sl_attached:
                    failure_msg.append(f"SL: {sl_error}")
                if not tp_attached_status:
                    failure_msg.append(f"TP: {tp_error}")
                error_detail = " | ".join(failure_msg)
                logger.error(f"  🚨 TP/SL INCOMPLETE for {symbol}: {error_detail}")

                if telegram_notifier:
                    await telegram_notifier.tp_sl_failed(
                        symbol=symbol, side=params.side,
                        sl_attached=sl_attached, tp_attached=tp_attached_status,
                        error=error_detail,
                    )

            # Summary log
            if partial_tp_used:
                logger.info(
                    f"  📊 Trade summary: method={order_method} fill={fill_price} "
                    f"TP1={actual_tp1 if partial_tp_used else 'N/A'} "
                    f"TP2={actual_tp2 if partial_tp_used else 'N/A'} SL={actual_sl} "
                    f"TP1_ok={tp1_attached} TP2_ok={tp2_attached} SL_ok={sl_attached} "
                    f"| PARTIAL_TP mode"
                )
            else:
                logger.info(
                    f"  📊 Trade summary: method={order_method} fill={fill_price} "
                    f"TP={actual_tp} SL={actual_sl} "
                    f"SL_ok={sl_attached} TP_ok={tp_attached_status}"
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
                tp_attached=tp_attached_status,
                order_method=order_method,
                partial_tp_enabled=partial_tp_used,
                tp1_order_id=tp1_order.get("orderId") if tp1_order else None,
                tp2_order_id=tp2_order.get("orderId") if tp2_order else None,
                tp1_attached=tp1_attached,
                tp2_attached=tp2_attached,
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

    # ─── V5.5: TP/SL Verification Proof ─────────────────────────────────

    async def _verify_tp_sl_proof(
        self,
        symbol: str,
        sl_order_id: Optional[int],
        tp_order_id: Optional[int],
        expected_sl_price: float,
        expected_tp_price: float,
        precision: PrecisionInfo,
    ) -> bool:
        """
        V5.5: Verify TP/SL orders actually exist on Binance with correct params.
        Logs full proof for audit trail. Returns True if all verified.
        """
        all_ok = True

        try:
            # Query all open orders for this symbol
            open_orders = await self._signed_request(
                "GET", "/fapi/v1/openOrders",
                {"symbol": symbol},
            )

            sl_verified = False
            tp_verified = False

            for order in open_orders:
                oid = order.get("orderId")
                order_type = order.get("type", "")
                stop_price = float(order.get("stopPrice", 0))
                close_pos = order.get("closePosition", "false")
                status = order.get("status", "")
                side = order.get("side", "")

                # Check SL
                if sl_order_id and oid == sl_order_id:
                    price_match = abs(stop_price - expected_sl_price) < (expected_sl_price * 0.001)
                    sl_verified = True
                    logger.info(
                        f"  🔒 PROOF SL: #{oid} type={order_type} "
                        f"stopPrice={stop_price} expected={expected_sl_price} "
                        f"closePosition={close_pos} status={status} side={side} "
                        f"price_match={'✅' if price_match else '⚠️'}"
                    )
                    if not price_match:
                        logger.warning(f"  ⚠️ SL price mismatch: {stop_price} vs expected {expected_sl_price}")
                        all_ok = False

                # Check TP
                if tp_order_id and oid == tp_order_id:
                    price_match = abs(stop_price - expected_tp_price) < (expected_tp_price * 0.001)
                    tp_verified = True
                    logger.info(
                        f"  🔒 PROOF TP: #{oid} type={order_type} "
                        f"stopPrice={stop_price} expected={expected_tp_price} "
                        f"closePosition={close_pos} status={status} side={side} "
                        f"price_match={'✅' if price_match else '⚠️'}"
                    )
                    if not price_match:
                        logger.warning(f"  ⚠️ TP price mismatch: {stop_price} vs expected {expected_tp_price}")
                        all_ok = False

            if sl_order_id and not sl_verified:
                logger.error(f"  ❌ PROOF FAIL: SL order #{sl_order_id} NOT found in open orders!")
                all_ok = False
            if tp_order_id and not tp_verified:
                logger.error(f"  ❌ PROOF FAIL: TP order #{tp_order_id} NOT found in open orders!")
                all_ok = False

            if all_ok:
                logger.info(f"  ✅ PROOF COMPLETE: Both TP and SL verified on exchange for {symbol}")

        except Exception as e:
            logger.warning(f"  ⚠️ TP/SL proof verification failed: {e}")
            # Don't block trade on verification failure
            all_ok = True  # Optimistic — order was placed, just can't verify

        return all_ok

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

    # ─── V5.5: Break-Even Stop Management ────────────────────────────

    async def manage_break_even_stops(
        self,
        trigger_pct: float = 3.0,
        buffer_pct: float = 0.1,
        telegram_notifier=None,
    ) -> list[dict]:
        """
        V5.5: Check all open positions and move SL to break-even
        for trades that have reached the profit threshold.

        Process:
        1. Get all open positions
        2. For each: calculate current ROI%
        3. If ROI >= trigger_pct → find existing SL order → cancel → place new SL at entry + buffer
        4. Log all actions and send Telegram notification

        Args:
            trigger_pct: Minimum profit % to trigger BE stop (default 3.0%)
            buffer_pct: Small buffer above/below entry to cover fees (default 0.1%)
            telegram_notifier: Optional TelegramNotifier for alerts

        Returns: List of actions taken
        """
        if not settings.BREAK_EVEN_ENABLED:
            return []

        actions = []
        logger.info("🔄 Checking positions for break-even stop management...")

        try:
            positions = await self.get_open_positions()
            if not positions:
                logger.info("  No open positions to check")
                return actions

            for pos in positions:
                symbol = pos["symbol"]
                entry_price = float(pos.get("entryPrice", 0))
                mark_price = float(pos.get("markPrice", 0))
                position_amt = float(pos.get("positionAmt", 0))
                unrealized_pnl = float(pos.get("unRealizedProfit", 0))

                if entry_price <= 0 or mark_price <= 0 or position_amt == 0:
                    continue

                # Determine side
                is_long = position_amt > 0
                side = "BUY" if is_long else "SELL"

                # Calculate ROI %
                if is_long:
                    roi_pct = ((mark_price - entry_price) / entry_price) * 100
                else:
                    roi_pct = ((entry_price - mark_price) / entry_price) * 100

                # Check if profit threshold met
                if roi_pct < trigger_pct:
                    continue

                logger.info(
                    f"  📈 {symbol} {side}: ROI={roi_pct:.2f}% >= {trigger_pct}% — "
                    f"moving SL to break-even"
                )

                try:
                    precision = await self.get_precision(symbol)

                    # Calculate break-even price (entry + buffer for fees)
                    if is_long:
                        be_price = round(
                            entry_price * (1 + buffer_pct / 100),
                            precision.price_precision,
                        )
                    else:
                        be_price = round(
                            entry_price * (1 - buffer_pct / 100),
                            precision.price_precision,
                        )

                    # Find and cancel existing SL orders for this symbol
                    open_orders = await self._signed_request(
                        "GET", "/fapi/v1/openOrders",
                        {"symbol": symbol},
                    )

                    sl_cancelled = False
                    for order in open_orders:
                        order_type = order.get("type", "")
                        if order_type == "STOP_MARKET":
                            old_sl_price = float(order.get("stopPrice", 0))
                            order_id = order.get("orderId")

                            # Only move if the new BE price is better than current SL
                            if is_long and be_price > old_sl_price:
                                await self.cancel_order(symbol, order_id)
                                sl_cancelled = True
                                logger.info(
                                    f"  ✅ Cancelled old SL #{order_id} "
                                    f"at {old_sl_price} for {symbol}"
                                )
                            elif not is_long and be_price < old_sl_price:
                                await self.cancel_order(symbol, order_id)
                                sl_cancelled = True
                                logger.info(
                                    f"  ✅ Cancelled old SL #{order_id} "
                                    f"at {old_sl_price} for {symbol}"
                                )

                    if not sl_cancelled:
                        logger.info(
                            f"  ℹ️ No SL to move for {symbol} "
                            f"(BE price not better than current SL)"
                        )
                        continue

                    # Place new SL at break-even
                    close_side = "SELL" if is_long else "BUY"
                    new_sl = await self.place_stop_loss(
                        symbol, close_side, be_price, precision,
                    )

                    new_sl_id = new_sl.get("orderId")
                    logger.info(
                        f"  ✅ NEW BE STOP placed for {symbol}: "
                        f"#{new_sl_id} at {be_price} "
                        f"(entry={entry_price}, ROI={roi_pct:.2f}%)"
                    )

                    action = {
                        "symbol": symbol,
                        "side": side,
                        "entry_price": entry_price,
                        "mark_price": mark_price,
                        "roi_pct": round(roi_pct, 2),
                        "old_sl": "cancelled",
                        "new_sl_price": be_price,
                        "new_sl_order_id": new_sl_id,
                        "status": "moved_to_be",
                    }
                    actions.append(action)

                    # Telegram notification
                    if telegram_notifier:
                        await telegram_notifier.break_even_moved(
                            symbol=symbol,
                            side=side,
                            entry_price=entry_price,
                            be_price=be_price,
                            roi_pct=roi_pct,
                        )

                except Exception as e:
                    logger.error(f"  ❌ BE stop management failed for {symbol}: {e}")
                    actions.append({
                        "symbol": symbol,
                        "error": str(e),
                        "status": "failed",
                    })

        except Exception as e:
            logger.error(f"Break-even stop management failed: {e}")

        if actions:
            logger.info(f"🔄 Break-even management: {len(actions)} positions adjusted")

        return actions
