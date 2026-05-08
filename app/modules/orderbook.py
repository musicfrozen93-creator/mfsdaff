"""
Order Book Analysis Module
Analyzes Level 2 order book for liquidity walls, support/resistance,
and optimal stop-loss placement
"""

import logging
from dataclasses import dataclass, field
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

WALL_THRESHOLD_MULTIPLIER = 5.0   # Orders >5x avg = liquidity wall
DEPTH_LEVELS = 50                  # Number of order book levels to fetch


@dataclass
class OrderBookAnalysis:
    symbol: str
    mid_price: float

    # Liquidity walls
    buy_walls: list[dict] = field(default_factory=list)   # [{price, size, distance_pct}]
    sell_walls: list[dict] = field(default_factory=list)

    # Support / resistance zones
    support_zones: list[float] = field(default_factory=list)
    resistance_zones: list[float] = field(default_factory=list)

    # Stop-loss suggestions
    suggested_long_sl: float = 0.0   # Place below nearest buy wall
    suggested_short_sl: float = 0.0  # Place above nearest sell wall

    # Order book imbalance (-100 = heavy sell, +100 = heavy buy)
    imbalance_score: float = 0.0

    # Summary
    bias: str = "NEUTRAL"  # BUY_PRESSURE | SELL_PRESSURE | NEUTRAL


class OrderBookAnalyzer:

    def __init__(self):
        self.base_url = settings.binance_base_url

    async def fetch_order_book(self, symbol: str, limit: int = DEPTH_LEVELS) -> dict:
        """Fetch L2 order book snapshot"""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/depth",
                params={"symbol": symbol, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    def find_walls(self, orders: list[list], threshold_mult: float = WALL_THRESHOLD_MULTIPLIER) -> list[dict]:
        """
        Identify liquidity walls: levels where size is significantly
        above the average size across all levels.
        """
        if not orders:
            return []

        sizes = [float(o[1]) for o in orders]
        avg_size = sum(sizes) / len(sizes)
        threshold = avg_size * threshold_mult

        walls = []
        for price_str, size_str in orders:
            price = float(price_str)
            size = float(size_str)
            if size >= threshold:
                walls.append({"price": price, "size": size, "multiple_of_avg": round(size / avg_size, 1)})

        return sorted(walls, key=lambda x: x["size"], reverse=True)

    def compute_imbalance(self, bids: list, asks: list, levels: int = 10) -> float:
        """
        Order book imbalance score:
        +100 = total bid pressure, -100 = total ask pressure
        Uses top N levels by volume.
        """
        top_bids = sorted(bids, key=lambda x: float(x[0]), reverse=True)[:levels]
        top_asks = sorted(asks, key=lambda x: float(x[0]))[:levels]

        bid_vol = sum(float(b[1]) for b in top_bids)
        ask_vol = sum(float(a[1]) for a in top_asks)
        total = bid_vol + ask_vol

        if total == 0:
            return 0.0
        return round(((bid_vol - ask_vol) / total) * 100, 2)

    async def analyze(self, symbol: str, current_price: float) -> OrderBookAnalysis:
        """Full order book analysis for a symbol"""
        logger.info(f"📖 Analyzing order book for {symbol}...")

        book = await self.fetch_order_book(symbol)
        bids = book.get("bids", [])  # [[price, qty], ...]
        asks = book.get("asks", [])

        result = OrderBookAnalysis(symbol=symbol, mid_price=current_price)

        # Find liquidity walls
        buy_walls  = self.find_walls(bids)
        sell_walls = self.find_walls(asks)

        # Annotate with distance from current price
        for wall in buy_walls:
            wall["distance_pct"] = round(((current_price - wall["price"]) / current_price) * 100, 3)
        for wall in sell_walls:
            wall["distance_pct"] = round(((wall["price"] - current_price) / current_price) * 100, 3)

        result.buy_walls  = buy_walls[:5]   # Top 5 buy walls
        result.sell_walls = sell_walls[:5]  # Top 5 sell walls

        # Support = prices with large buy walls below current price
        result.support_zones = sorted(
            [w["price"] for w in buy_walls if w["price"] < current_price],
            reverse=True,
        )[:3]

        # Resistance = prices with large sell walls above current price
        result.resistance_zones = sorted(
            [w["price"] for w in sell_walls if w["price"] > current_price],
        )[:3]

        # Stop-loss suggestions: place just below nearest support (long) / above resistance (short)
        if result.support_zones:
            result.suggested_long_sl = round(result.support_zones[0] * 0.998, 8)  # 0.2% below wall
        if result.resistance_zones:
            result.suggested_short_sl = round(result.resistance_zones[0] * 1.002, 8)

        # Imbalance score
        result.imbalance_score = self.compute_imbalance(bids, asks)
        if result.imbalance_score > 20:
            result.bias = "BUY_PRESSURE"
        elif result.imbalance_score < -20:
            result.bias = "SELL_PRESSURE"
        else:
            result.bias = "NEUTRAL"

        logger.info(
            f"  Buy walls: {len(buy_walls)} | Sell walls: {len(sell_walls)} | "
            f"Imbalance: {result.imbalance_score} | Bias: {result.bias}"
        )
        return result

    def to_dict(self, result: OrderBookAnalysis) -> dict:
        return {
            "symbol": result.symbol,
            "mid_price": result.mid_price,
            "imbalance_score": result.imbalance_score,
            "bias": result.bias,
            "buy_walls": result.buy_walls,
            "sell_walls": result.sell_walls,
            "support_zones": result.support_zones,
            "resistance_zones": result.resistance_zones,
            "suggested_long_sl": result.suggested_long_sl,
            "suggested_short_sl": result.suggested_short_sl,
        }
