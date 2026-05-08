"""
V9 Price Stream — WebSocket-based live price feed for Position Manager.

Architecture:
  - One shared WebSocket connection per batch of symbols (max 200/stream)
  - Subscribes to <symbol>@markPrice streams on Binance Futures
  - Falls back to REST polling if WebSocket fails
  - Thread-safe price cache accessible by the Position Manager loop

Usage:
    stream = PriceStream()
    await stream.start(["BTCUSDT", "ETHUSDT", ...])
    price = stream.get_price("BTCUSDT")
    await stream.stop()
"""

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
import websockets

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────
WS_BASE_FUTURES = "wss://fstream.binance.com/stream"
WS_BASE_TESTNET = "wss://stream.binancefuture.com/stream"
REST_PRICE_URL  = "https://fapi.binance.com/fapi/v1/ticker/price"
REST_TEST_URL   = "https://testnet.binancefuture.com/fapi/v1/ticker/price"
MAX_SYMBOLS_PER_STREAM = 100      # Binance limit per combined stream
RECONNECT_DELAY = 3.0             # seconds between reconnect attempts
REST_FALLBACK_INTERVAL = 2.0      # seconds between REST polling cycles


class PriceStream:
    """
    WebSocket price feed for Binance Futures mark prices.
    Automatically reconnects on disconnect.
    Falls back to REST polling if WebSocket cannot connect.
    """

    def __init__(self, testnet: bool = False):
        self.testnet = testnet
        self._prices: dict[str, float] = {}        # symbol → latest mark price
        self._timestamps: dict[str, float] = {}    # symbol → last update time (epoch)
        self._symbols: list[str] = []
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._rest_task: Optional[asyncio.Task] = None
        self._use_rest_fallback = False

    # ── Public API ─────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[float]:
        """Return latest cached mark price for symbol, or None if unknown."""
        return self._prices.get(symbol.upper())

    def get_age_seconds(self, symbol: str) -> float:
        """How many seconds ago was this price last updated."""
        ts = self._timestamps.get(symbol.upper(), 0.0)
        return time.time() - ts if ts > 0 else 9999.0

    def all_prices(self) -> dict[str, float]:
        """Return a snapshot of all cached prices."""
        return dict(self._prices)

    def is_stale(self, symbol: str, max_age: float = 10.0) -> bool:
        """Returns True if price data is older than max_age seconds."""
        return self.get_age_seconds(symbol) > max_age

    async def start(self, symbols: list[str]) -> None:
        """Start streaming prices for the given symbol list."""
        self._symbols = [s.upper() for s in symbols]
        self._running = True
        logger.info(f"🔌 PriceStream: starting for {len(self._symbols)} symbols")

        # Prime cache with REST prices immediately so we have data before WS connects
        await self._rest_snapshot()

        # Start WebSocket task
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        """Stop all streaming tasks."""
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        if self._rest_task and not self._rest_task.done():
            self._rest_task.cancel()
        logger.info("🔌 PriceStream: stopped")

    async def update_symbols(self, symbols: list[str]) -> None:
        """
        Hot-swap the symbol list (Position Manager calls this when a new
        trade opens or an existing one closes).
        """
        new_set = set(s.upper() for s in symbols)
        old_set = set(self._symbols)
        if new_set == old_set:
            return

        logger.info(
            f"🔌 PriceStream: updating symbols "
            f"(added={new_set - old_set}, removed={old_set - new_set})"
        )
        self._symbols = list(new_set)

        # Restart WS to pick up new subscriptions
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        await asyncio.sleep(0.5)
        self._ws_task = asyncio.create_task(self._ws_loop())

    # ── WebSocket Loop ─────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Main WebSocket connection loop with auto-reconnect."""
        base = WS_BASE_TESTNET if self.testnet else WS_BASE_FUTURES

        while self._running:
            if not self._symbols:
                await asyncio.sleep(1.0)
                continue

            # Build combined stream URL — chunks of MAX_SYMBOLS_PER_STREAM
            chunk = self._symbols[:MAX_SYMBOLS_PER_STREAM]
            streams = "/".join(f"{s.lower()}@markPrice" for s in chunk)
            url = f"{base}?streams={streams}"

            try:
                logger.info(f"🔌 PriceStream: connecting to WebSocket ({len(chunk)} symbols)...")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._use_rest_fallback = False
                    logger.info("🔌 PriceStream: WebSocket connected ✅")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            # Combined stream wraps in {"stream": "...", "data": {...}}
                            payload = data.get("data", data)
                            self._handle_mark_price(payload)
                        except Exception as pe:
                            logger.debug(f"  WS parse error: {pe}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"🔌 PriceStream: WebSocket error: {e} — "
                    f"switching to REST fallback, retry in {RECONNECT_DELAY}s"
                )
                self._use_rest_fallback = True
                # Activate REST fallback while we wait to reconnect
                if not self._rest_task or self._rest_task.done():
                    self._rest_task = asyncio.create_task(self._rest_fallback_loop())
                await asyncio.sleep(RECONNECT_DELAY)

        logger.info("🔌 PriceStream: WebSocket loop exited")

    def _handle_mark_price(self, payload: dict) -> None:
        """Parse a markPrice event and update the cache."""
        symbol = payload.get("s", "")
        price = float(payload.get("p", 0))   # "p" = mark price
        if symbol and price > 0:
            self._prices[symbol] = price
            self._timestamps[symbol] = time.time()

    # ── REST Fallback ──────────────────────────────────────────────────

    async def _rest_fallback_loop(self) -> None:
        """Poll REST endpoint every REST_FALLBACK_INTERVAL seconds as fallback."""
        logger.info("🔌 PriceStream: REST fallback activated")
        while self._running and self._use_rest_fallback:
            try:
                await self._rest_snapshot()
            except Exception as e:
                logger.debug(f"  REST fallback poll error: {e}")
            await asyncio.sleep(REST_FALLBACK_INTERVAL)
        logger.info("🔌 PriceStream: REST fallback deactivated")

    async def _rest_snapshot(self) -> None:
        """Bulk-fetch mark prices via REST for all tracked symbols."""
        if not self._symbols:
            return

        url = REST_TEST_URL if self.testnet else REST_PRICE_URL

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            tracked = set(self._symbols)
            now = time.time()

            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "")
                    if sym in tracked:
                        price = float(item.get("price", 0))
                        if price > 0:
                            self._prices[sym] = price
                            self._timestamps[sym] = now
            elif isinstance(data, dict):
                # Single symbol response
                sym = data.get("symbol", "")
                price = float(data.get("price", 0))
                if sym in tracked and price > 0:
                    self._prices[sym] = price
                    self._timestamps[sym] = now

        except Exception as e:
            logger.debug(f"  REST snapshot error: {e}")
