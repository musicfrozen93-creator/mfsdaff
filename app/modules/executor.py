"""
V8 Binance Futures Execution Module

Key V8 changes:
  - Auto-detect Hedge Mode vs One-way Mode per account
  - positionSide-aware TP/SL (fixes -4061 error on Hedge Mode accounts)
  - Pre-trade dry-run validation: skip BEFORE entry if TP/SL cannot be placed
  - Retry with fresh precision on -1111 errors
  - Emergency close only as last resort (position is NEVER left naked)
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

# ── V8: Hedge Mode cache (per api_key, 10-minute TTL) ────────────────
_hedge_mode_cache: dict = {}   # api_key -> (is_hedge: bool, ts: float)
HEDGE_MODE_CACHE_TTL = 600    # 10 minutes


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
    # V7: TP/SL atomic protection
    tp_sl_protection_failed: bool = False   # True if TP/SL couldn't be attached
    emergency_closed: bool = False          # True if position was emergency-closed
    # V9: Hedge mode info (needed by Position Manager)
    is_hedge_mode: bool = False
    position_side: str = "BOTH"             # BOTH | LONG | SHORT


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

            if not resp.is_success:
                # V9: Log FULL Binance error response for debugging
                try:
                    err_body = resp.json()
                    err_code = err_body.get('code', resp.status_code)
                    err_msg  = err_body.get('msg', resp.text)
                    logger.error(
                        f"  ❌ Binance API error {err_code}: {err_msg} "
                        f"| path={path} params={params} | full_body={err_body}"
                    )
                    raise ValueError(f"Binance error {err_code}: {err_msg}")
                except ValueError:
                    raise
                except Exception:
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

    def round_to_tick(self, price: float, tick_size: float, price_precision: int) -> str:
        """Snap price to nearest tick_size grid — prevents Binance -1111 errors."""
        if tick_size <= 0:
            return self.format_price(price, price_precision)
        import math
        snapped = math.floor(price / tick_size) * tick_size
        return f"{snapped:.{price_precision}f}"

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

    # ─── V8: Hedge Mode Detection ─────────────────────────────────────

    async def detect_position_mode(self) -> bool:
        """
        V8: Detect if this account uses Hedge Mode (dualSidePosition=true)
        or One-way Mode (dualSidePosition=false).

        Cached per api_key for 10 minutes to avoid hammering the API.
        Returns True if Hedge Mode, False if One-way Mode.
        """
        global _hedge_mode_cache
        cache_key = self.api_key[:16] if self.api_key else "default"
        now = time.time()

        if cache_key in _hedge_mode_cache:
            is_hedge, ts = _hedge_mode_cache[cache_key]
            if (now - ts) < HEDGE_MODE_CACHE_TTL:
                return is_hedge

        try:
            data = await self._signed_request("GET", "/fapi/v1/positionSide/dual", {})
            is_hedge = data.get("dualSidePosition", False)
            _hedge_mode_cache[cache_key] = (is_hedge, now)
            mode_str = "HEDGE" if is_hedge else "ONE-WAY"
            logger.info(f"  📋 V8 Position Mode: {mode_str} (account={cache_key[:8]}...)")
            return is_hedge
        except Exception as e:
            logger.warning(f"  ⚠️ V8: Could not detect position mode, assuming One-way: {e}")
            _hedge_mode_cache[cache_key] = (False, now)
            return False

    # ─── V8: TP/SL Placement (Hedge + One-way aware) ─────────────────

    async def place_stop_loss(
        self, symbol: str, side: str, stop_price: float,
        precision: PrecisionInfo,
        position_side: str = "BOTH",
        quantity: float = 0.0,
    ) -> dict:
        """
        V9: Place SL order — production-grade bracket protection.
        One-way Mode: closePosition=true (NO timeInForce — Binance rejects -1106)
        Hedge Mode:   positionSide + quantity + reduceOnly (NO closePosition)
        Price snapped to tickSize grid to prevent -1111 errors.
        """
        price_str = self.round_to_tick(stop_price, precision.tick_size, precision.price_precision)
        logger.info(f"  [SL] Placing STOP_MARKET: {symbol} {side} stopPrice={price_str} mode={position_side}")

        if position_side in ("LONG", "SHORT"):  # Hedge Mode
            if quantity <= 0:
                raise ValueError("Hedge Mode SL requires a quantity > 0")
            qty_str = self.format_quantity(quantity, precision.quantity_precision)
            params = {
                "symbol": symbol,
                "side": side,
                "type": "STOP_MARKET",
                "stopPrice": price_str,
                "quantity": qty_str,
                "positionSide": position_side,
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                # NOTE: timeInForce NOT sent for STOP_MARKET in hedge mode
            }
        else:  # One-way Mode (BOTH)
            # CRITICAL: closePosition=true is INCOMPATIBLE with timeInForce
            # Sending timeInForce causes Binance error -1106
            params = {
                "symbol": symbol,
                "side": side,
                "type": "STOP_MARKET",
                "stopPrice": price_str,
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                # NO timeInForce here — Binance rejects -1106 with closePosition=true
            }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_take_profit(
        self, symbol: str, side: str, stop_price: float,
        precision: PrecisionInfo,
        position_side: str = "BOTH",
        quantity: float = 0.0,
    ) -> dict:
        """
        V9: Place TP order — production-grade bracket protection.
        One-way Mode: closePosition=true (NO timeInForce — Binance rejects -1106)
        Hedge Mode:   positionSide + quantity + reduceOnly (NO closePosition)
        Price snapped to tickSize grid to prevent -1111 errors.
        """
        price_str = self.round_to_tick(stop_price, precision.tick_size, precision.price_precision)
        logger.info(f"  [TP] Placing TAKE_PROFIT_MARKET: {symbol} {side} stopPrice={price_str} mode={position_side}")

        if position_side in ("LONG", "SHORT"):  # Hedge Mode
            if quantity <= 0:
                raise ValueError("Hedge Mode TP requires a quantity > 0")
            qty_str = self.format_quantity(quantity, precision.quantity_precision)
            params = {
                "symbol": symbol,
                "side": side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": price_str,
                "quantity": qty_str,
                "positionSide": position_side,
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                # NOTE: timeInForce NOT sent for TAKE_PROFIT_MARKET in hedge mode
            }
        else:  # One-way Mode (BOTH)
            # CRITICAL: closePosition=true is INCOMPATIBLE with timeInForce
            # Sending timeInForce causes Binance error -1106
            params = {
                "symbol": symbol,
                "side": side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": price_str,
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                # NO timeInForce here — Binance rejects -1106 with closePosition=true
            }
        return await self._signed_request("POST", "/fapi/v1/order", params)

    async def place_partial_take_profit(
        self, symbol: str, side: str, stop_price: float,
        quantity: float, precision: PrecisionInfo,
        position_side: str = "BOTH",
    ) -> dict:
        """
        V9: Partial TP — tick-snapped price, no invalid timeInForce.
        Always uses explicit quantity + reduceOnly=true (or positionSide in hedge mode).
        """
        price_str = self.round_to_tick(stop_price, precision.tick_size, precision.price_precision)
        qty_str = self.format_quantity(quantity, precision.quantity_precision)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": price_str,
            "quantity": qty_str,
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            # No timeInForce — not required for TAKE_PROFIT_MARKET with quantity
        }
        if position_side in ("LONG", "SHORT"):
            params["positionSide"] = position_side
            del params["reduceOnly"]  # positionSide replaces reduceOnly in hedge mode
        return await self._signed_request("POST", "/fapi/v1/order", params)

    # ─── V8: Pre-Trade TP/SL Dry-Run Validator ────────────────────────

    async def can_place_tp_sl(
        self,
        symbol: str,
        side: str,          # BUY or SELL
        entry_price: float,
        tp_price: float,
        sl_price: float,
        precision: PrecisionInfo,
        is_hedge_mode: bool = False,
        quantity: float = 0.0,
    ) -> tuple[bool, str]:
        """
        V8: Pre-trade dry-run validation.
        Validates TP/SL prices BEFORE opening the entry order.
        Returns (can_proceed: bool, reason: str).

        STRICT RULE: If this returns False, the trade is SKIPPED entirely.
        No position is opened. No fees lost.
        """
        try:
            current_price = await self.get_market_price(symbol)
        except Exception as e:
            return False, f"Cannot fetch market price: {e}"

        # --- Side logic check ---
        if side == "BUY":  # LONG
            if tp_price <= current_price:
                return False, (
                    f"LONG TP {tp_price:.6f} is NOT above current price "
                    f"{current_price:.6f} — invalid TP"
                )
            if sl_price >= current_price:
                return False, (
                    f"LONG SL {sl_price:.6f} is NOT below current price "
                    f"{current_price:.6f} — invalid SL"
                )
        else:  # SHORT
            if tp_price >= current_price:
                return False, (
                    f"SHORT TP {tp_price:.6f} is NOT below current price "
                    f"{current_price:.6f} — invalid TP"
                )
            if sl_price <= current_price:
                return False, (
                    f"SHORT SL {sl_price:.6f} is NOT above current price "
                    f"{current_price:.6f} — invalid SL"
                )

        # --- Tick-size rounding check ---
        if precision.tick_size > 0:
            tp_rounded = self.format_price(tp_price, precision.price_precision)
            sl_rounded = self.format_price(sl_price, precision.price_precision)
            # Verify that rounding doesn't flip the direction
            tp_f = float(tp_rounded)
            sl_f = float(sl_rounded)
            if side == "BUY" and (tp_f <= current_price or sl_f >= current_price):
                return False, (
                    f"Tick-size rounding invalidated TP/SL for LONG: "
                    f"tp_rounded={tp_f} sl_rounded={sl_f} price={current_price}"
                )
            if side == "SELL" and (tp_f >= current_price or sl_f <= current_price):
                return False, (
                    f"Tick-size rounding invalidated TP/SL for SHORT: "
                    f"tp_rounded={tp_f} sl_rounded={sl_f} price={current_price}"
                )

        # --- Hedge Mode quantity check ---
        if is_hedge_mode and quantity <= 0:
            return False, "Hedge Mode requires quantity > 0 for TP/SL — cannot guarantee placement"

        # --- Min notional check ---
        if quantity > 0:
            notional = quantity * entry_price
            if notional < precision.min_notional:
                return False, (
                    f"Notional {notional:.2f} below min {precision.min_notional} "
                    f"— TP/SL quantity would be rejected"
                )

        logger.info(
            f"  ✅ V8 Pre-trade validation PASSED for {symbol} {side}: "
            f"price={current_price:.6f} tp={tp_price:.6f} sl={sl_price:.6f} "
            f"hedge_mode={is_hedge_mode}"
        )
        return True, "ok"

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
        V7: Enhanced retry TP/SL placement up to 3 times + 1 hail-mary.
        - Recalculates precision on -1111 errors (bad precision)
        - Adds 2s exchange-lag wait between retries
        Returns (order_result, error_message).
        """
        last_error = ""
        current_precision = precision

        for attempt in range(1, TP_SL_MAX_RETRIES + 1):
            try:
                result = await place_func(symbol, side, stop_price, current_precision)
                logger.info(
                    f"  ✅ {order_type} placed on attempt {attempt}/{TP_SL_MAX_RETRIES}: "
                    f"orderId=#{result.get('orderId')} stopPrice={result.get('stopPrice')} "
                    f"type={result.get('type')} closePosition={result.get('closePosition')}"
                )
                return result, ""
            except Exception as e:
                last_error = str(e)
                error_str = str(e)
                logger.error(
                    f"  ❌ {order_type} attempt {attempt}/{TP_SL_MAX_RETRIES} FAILED "
                    f"for {symbol}: {error_str}"
                )

                # V9: On ANY precision/parameter error, force fresh precision from exchange
                if any(code in error_str for code in ["-1111", "-1106", "-1102", "precision", "parameter"]):
                    try:
                        logger.info(f"  🔄 V9: Re-fetching precision for {symbol} after error")
                        global _exchange_info_cache, _exchange_info_ts
                        _exchange_info_ts = 0.0  # Force cache invalidation
                        current_precision = await self.get_precision(symbol)
                        logger.info(
                            f"  🔄 Fresh precision: tick={current_precision.tick_size} "
                            f"price_prec={current_precision.price_precision}"
                        )
                    except Exception as pe:
                        logger.warning(f"  ⚠️ Precision re-fetch failed: {pe}")

                if attempt < TP_SL_MAX_RETRIES:
                    await asyncio.sleep(2.0)

        # Final hail-mary: fresh precision + 3s wait
        try:
            logger.info(f"  🔄 V9 Hail-mary {order_type} for {symbol}: fresh precision + 3s wait")
            await asyncio.sleep(3.0)
            _exchange_info_ts = 0.0
            fresh_precision = await self.get_precision(symbol)
            result = await place_func(symbol, side, stop_price, fresh_precision)
            logger.info(
                f"  ✅ {order_type} placed on hail-mary: #{result.get('orderId')} "
                f"stopPrice={result.get('stopPrice')}"
            )
            return result, ""
        except Exception as e:
            last_error = str(e)
            logger.error(
                f"  ❌ {order_type} PROTECTION FAILED for {symbol} after all retries: {last_error}"
            )

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

    # ─── V7: Emergency Close Position ─────────────────────────────────

    async def _emergency_close_position(
        self, symbol: str, side: str, telegram_notifier=None,
    ) -> bool:
        """
        V7: Emergency market-close a position when TP/SL attachment fails.
        This prevents naked (unprotected) positions from remaining open.

        Args:
            symbol: Trading pair
            side: Original trade side (BUY/SELL) — close side is opposite
            telegram_notifier: Optional TelegramNotifier for alerts

        Returns: True if position was successfully closed
        """
        close_side = "SELL" if side == "BUY" else "BUY"
        logger.warning(
            f"  🚨 V9 EMERGENCY CLOSE: Attempting to close {symbol} "
            f"position (close_side={close_side}) due to TP/SL failure"
        )

        # Detect hedge mode for emergency close
        try:
            is_hedge = await self.detect_position_mode()
        except Exception:
            is_hedge = False

        for attempt in range(1, 4):  # 3 attempts
            try:
                # Get current position to know exact quantity
                position = await self.get_position_for_symbol(symbol)
                if not position:
                    logger.info(
                        f"  ✅ Emergency close: No position found for {symbol} "
                        f"(may have already been closed)"
                    )
                    return True

                pos_amt = abs(float(position.get("positionAmt", 0)))
                pos_side = position.get("positionSide", "BOTH")  # LONG/SHORT/BOTH
                if pos_amt == 0:
                    logger.info(f"  ✅ Emergency close: Position amount is 0 for {symbol}")
                    return True

                precision = await self.get_precision(symbol)
                qty_str = self.format_quantity(pos_amt, precision.quantity_precision)

                # Build emergency close params (hedge-mode-aware)
                params: dict = {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": qty_str,
                }
                if is_hedge and pos_side in ("LONG", "SHORT"):
                    params["positionSide"] = pos_side  # hedge mode needs positionSide
                else:
                    params["reduceOnly"] = "true"  # one-way mode uses reduceOnly

                result = await self._signed_request("POST", "/fapi/v1/order", params)
                close_order_id = result.get("orderId")

                logger.warning(
                    f"  🚨 EMERGENCY CLOSE EXECUTED: {symbol} "
                    f"qty={pos_amt} order=#{close_order_id} (attempt {attempt})"
                )

                # Cancel any remaining open orders
                try:
                    await self._signed_request(
                        "DELETE", "/fapi/v1/allOpenOrders",
                        {"symbol": symbol},
                    )
                    logger.info(f"  ✅ Cancelled all open orders for {symbol} after emergency close")
                except Exception as ce:
                    logger.warning(f"  ⚠️ Failed to cancel open orders for {symbol}: {ce}")

                return True

            except Exception as e:
                logger.error(
                    f"  ❌ Emergency close attempt {attempt}/3 failed for {symbol}: {e}"
                )
                if attempt < 3:
                    await asyncio.sleep(2.0)

        logger.critical(
            f"  🔥 CRITICAL: Emergency close FAILED for {symbol} after 3 attempts! "
            f"MANUAL INTERVENTION REQUIRED!"
        )
        return False

    # ─── V4: Full Trade Flow (LIMIT→MARKET Fallback + Position Verify) ──

    async def execute_trade(self, params, telegram_notifier=None) -> OrderResult:
        """
        V9 Full trade execution flow:
        1. Get precision + detect Hedge/One-way mode
        2. Validate notional + min qty
        3. Pre-validate TP/SL BEFORE entry (SKIP if invalid — no naked positions)
        4. Set leverage + margin type
        5. Try LIMIT → MARKET fallback entry
        6. Verify position + get actual fill price
        7. Recalculate TP/SL from fill price (tick-snapped)
        8. Place SL + TP with 3-retry (hedge-mode-aware, no invalid timeInForce)
        9. Emergency close ONLY if TP/SL fails post-entry
        """
        symbol = params.symbol  # Defined BEFORE try so except block always has it
        logger.info(f"⚡ V8 Executing {params.side} on {symbol} (lev={params.leverage}x qty={params.quantity})...")

        try:
            precision = await self.get_precision(symbol)

            # Validate notional
            notional = params.quantity * params.entry_price
            if notional < precision.min_notional:
                raise ValueError(f"Notional {notional:.2f} below min {precision.min_notional}")
            if params.quantity < precision.min_qty:
                raise ValueError(f"Qty {params.quantity} below min {precision.min_qty}")

            # ── V8: Detect account position mode ─────────────────────
            is_hedge_mode = await self.detect_position_mode()
            position_side = "BOTH"  # One-way default
            if is_hedge_mode:
                position_side = "LONG" if params.side == "BUY" else "SHORT"
                logger.info(f"  🔀 V8 Hedge Mode: positionSide={position_side}")

            # ── V8: Pre-compute estimated TP/SL for dry-run check ────
            # Use entry_price as estimate (will be recalculated post-fill)
            est_price = params.entry_price
            if hasattr(params, 'tp_pct') and params.tp_pct > 0:
                tp_pct_decimal = params.tp_pct / 100.0
                sl_pct_decimal = params.sl_pct / 100.0
                if params.side == "BUY":
                    est_tp = round(est_price * (1 + tp_pct_decimal), precision.price_precision)
                    est_sl = round(est_price * (1 - sl_pct_decimal), precision.price_precision)
                else:
                    est_tp = round(est_price * (1 - tp_pct_decimal), precision.price_precision)
                    est_sl = round(est_price * (1 + sl_pct_decimal), precision.price_precision)
            else:
                est_tp = params.take_profit
                est_sl = params.stop_loss

            # ══════════════════════════════════════════════════════════
            # V8: PRE-TRADE TP/SL DRY-RUN VALIDATION
            # STRICT RULE: SKIP TRADE ENTIRELY if TP/SL cannot be placed.
            # No position opened. No fees. No emergency close needed.
            # ══════════════════════════════════════════════════════════
            can_proceed, validation_reason = await self.can_place_tp_sl(
                symbol=symbol,
                side=params.side,
                entry_price=est_price,
                tp_price=est_tp,
                sl_price=est_sl,
                precision=precision,
                is_hedge_mode=is_hedge_mode,
                quantity=params.quantity if is_hedge_mode else 0.0,
            )

            if not can_proceed:
                logger.warning(
                    f"  🛡️ V8 PRE-TRADE PROTECTION: Skipping {symbol} — "
                    f"TP/SL cannot be guaranteed: {validation_reason}"
                )
                return OrderResult(
                    success=False,
                    order_id=None,
                    symbol=symbol,
                    side=params.side,
                    quantity=params.quantity,
                    entry_price=params.entry_price,
                    error=f"PRE_TRADE_SKIP: {validation_reason}",
                    tp_sl_protection_failed=True,
                    emergency_closed=False,
                )

            # Configure margin + leverage
            await self.set_margin_type(symbol, "ISOLATED")
            await self.set_leverage(symbol, params.leverage)

            # ── LIMIT → MARKET fallback ───────────────────────────────
            order = None
            order_method = "MARKET"
            fill_price = 0.0

            if settings.ENABLE_LIMIT_FALLBACK:
                try:
                    book = await self.get_book_ticker(symbol)
                    limit_price = book["ask"] if params.side == "BUY" else book["bid"]

                    if limit_price > 0:
                        logger.info(f"  📝 Trying LIMIT order at {limit_price}...")
                        order = await self.place_limit_order(
                            symbol, params.side, params.quantity, limit_price, precision
                        )
                        limit_order_id = order["orderId"]
                        logger.info(f"  📝 LIMIT order placed: #{limit_order_id}")

                        await asyncio.sleep(settings.LIMIT_ORDER_WAIT_SECONDS)

                        order_status = await self.get_order_status(symbol, limit_order_id)
                        status = order_status.get("status", "")

                        if status == "FILLED":
                            order_method = "LIMIT"
                            fill_price = self._get_fill_price(order_status)
                            logger.info(f"  ✅ LIMIT order FILLED: fill_price={fill_price}")
                        else:
                            logger.info(f"  ⏳ LIMIT status={status} — cancelling, switching to MARKET")
                            try:
                                await self.cancel_order(symbol, limit_order_id)
                            except Exception as ce:
                                logger.warning(f"  Cancel failed: {ce}")
                                recheck = await self.get_order_status(symbol, limit_order_id)
                                if recheck.get("status") == "FILLED":
                                    order_method = "LIMIT"
                                    fill_price = self._get_fill_price(recheck)
                                    order = recheck

                            if order_method != "LIMIT":
                                order = None
                except Exception as le:
                    logger.warning(f"  LIMIT attempt failed: {le} — falling back to MARKET")
                    order = None

            # MARKET entry (primary or fallback)
            if order is None or order_method == "MARKET":
                order = await self.place_market_order(symbol, params.side, params.quantity, precision)
                order_method = "MARKET"
                fill_price = self._get_fill_price(order)
                logger.info(f"  ✅ MARKET order: #{order['orderId']} fill={fill_price}")

            order_id = order["orderId"]

            # ── Verify position + get actual fill price ───────────────
            position_data = await self._verify_position(symbol)
            if position_data:
                pos_entry = float(position_data.get("entryPrice", 0))
                if pos_entry > 0:
                    fill_price = pos_entry

            if fill_price <= 0:
                fill_price = params.entry_price
                logger.warning(f"  ⚠️ Could not determine fill price, using estimate: {fill_price}")

            # ── Recalculate TP/SL from ACTUAL fill price ──────────────
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
            qty_for_hedge = params.quantity  # Used when position_side is LONG/SHORT

            logger.info(
                f"  🛡️ V9 Bracket protection: {symbol} fill={fill_price} "
                f"SL={actual_sl} TP={actual_tp} side={close_side} "
                f"hedge={is_hedge_mode} position_side={position_side}"
            )

            # ── V9: SL with retry (hedge-mode-aware, tick-snapped) ─────
            async def _sl_placer(sym, s, price, prec):
                return await self.place_stop_loss(
                    sym, s, price, prec,
                    position_side=position_side,
                    quantity=qty_for_hedge if is_hedge_mode else 0.0,
                )

            sl_order, sl_error = await self._place_with_retry(
                _sl_placer, "STOP_LOSS",
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
                        position_side=position_side,
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
                        position_side=position_side,
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
                # ── FULL TP MODE (hedge-mode-aware) ───────────────────
                async def _tp_placer(sym, s, price, prec):
                    return await self.place_take_profit(
                        sym, s, price, prec,
                        position_side=position_side,
                        quantity=qty_for_hedge if is_hedge_mode else 0.0,
                    )

                tp_order, tp_error = await self._place_with_retry(
                    _tp_placer, "TAKE_PROFIT",
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

                # ═══════════════════════════════════════════════════════
                # V7: ATOMIC TP/SL PROTECTION — EMERGENCY CLOSE
                # No position is EVER allowed to exist without both TP + SL.
                # If either failed after all retries → close position immediately.
                # ═══════════════════════════════════════════════════════

                logger.warning(
                    f"  🚨 V7 ATOMIC PROTECTION: TP/SL incomplete for {symbol} — "
                    f"initiating emergency close to prevent naked position"
                )

                # Cancel any partial TP/SL orders that DID succeed
                try:
                    await self._signed_request(
                        "DELETE", "/fapi/v1/allOpenOrders",
                        {"symbol": symbol},
                    )
                    logger.info(f"  ✅ Cancelled all open orders for {symbol} before emergency close")
                except Exception as cancel_err:
                    logger.warning(f"  ⚠️ Failed to cancel open orders: {cancel_err}")

                # Emergency close the position
                close_success = await self._emergency_close_position(
                    symbol=symbol, side=params.side, telegram_notifier=telegram_notifier,
                )

                if telegram_notifier:
                    if close_success:
                        await telegram_notifier.send_emergency_close(
                            symbol=symbol,
                            side=params.side,
                            fill_price=fill_price,
                            sl_attached=sl_attached,
                            tp_attached=tp_attached_status,
                            error=error_detail,
                        )
                    else:
                        # CRITICAL: Position is naked AND couldn't be closed
                        await telegram_notifier.tp_sl_failed(
                            symbol=symbol, side=params.side,
                            sl_attached=sl_attached, tp_attached=tp_attached_status,
                            error=f"CRITICAL: Emergency close also failed! {error_detail}",
                        )

                return OrderResult(
                    success=False,
                    order_id=order_id,
                    symbol=symbol,
                    side=params.side,
                    quantity=params.quantity,
                    entry_price=params.entry_price,
                    fill_price=fill_price,
                    sl_attached=sl_attached,
                    tp_attached=tp_attached_status,
                    order_method=order_method,
                    error=f"V7_ATOMIC_PROTECTION: TP/SL failed → position emergency closed. {error_detail}",
                    tp_sl_protection_failed=True,
                    emergency_closed=close_success,
                )

            # ── TP/SL SUCCESS — Normal completion ────────────────────
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
                is_hedge_mode=is_hedge_mode,
                position_side=position_side,
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
