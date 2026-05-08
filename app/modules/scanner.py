"""
V3 Market Scanner Module — Smart Multi-Factor Coin Selection

Scans top coins by multiple quality factors, not just volume.
Scores each coin by: volume, spread, trend clarity, volatility quality,
momentum, breakout potential, orderbook quality, recent behavior.

Returns top 15 candidates for deep analysis.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# V3: Known manipulated / illiquid pairs to always exclude
BLACKLISTED_PAIRS = {
    "LUNA2USDT", "USTCUSDT", "SRMUSDT", "FTTUSDT",
    "BTSUSDT", "SCUSDT", "CKBUSDT", "STPTUSDT",
}


@dataclass
class CoinCandidate:
    symbol: str
    price: float
    volume_24h: float
    price_change_pct: float
    bid: float
    ask: float
    spread_pct: float
    score: float = 0.0
    trend_strength: float = 0.0
    # V3: Multi-factor scores
    volume_score: float = 0.0
    spread_score: float = 0.0
    trend_score: float = 0.0
    volatility_score: float = 0.0
    momentum_score: float = 0.0
    liquidity_score: float = 0.0


class MarketScanner:
    """
    V3 Scanner — Multi-factor scoring replaces volume-only ranking.
    Returns top candidates sorted by composite quality score.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url
        self.excluded = set(settings.EXCLUDED_COINS or []) | BLACKLISTED_PAIRS

    async def get_all_tickers(self) -> list[dict]:
        """Fetch 24h ticker stats for all USDT perpetual futures"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/fapi/v1/ticker/24hr")
            resp.raise_for_status()
            tickers = resp.json()
        return [t for t in tickers if t["symbol"].endswith("USDT")]

    async def get_all_book_tickers(self) -> dict[str, dict]:
        """
        Fetch best bid/ask for ALL symbols in a single API call.
        Returns a dict keyed by symbol for O(1) lookups.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/fapi/v1/ticker/bookTicker")
            resp.raise_for_status()
            data = resp.json()
        return {item["symbol"]: item for item in data}

    def passes_filters(self, ticker: dict) -> Optional[CoinCandidate]:
        """Apply basic filters. Returns None if coin fails."""
        symbol = ticker["symbol"]

        if symbol in self.excluded:
            return None

        try:
            price = float(ticker["lastPrice"])
            volume = float(ticker["quoteVolume"])
            change_pct = float(ticker["priceChangePercent"])
            high = float(ticker.get("highPrice", 0))
            low = float(ticker.get("lowPrice", 0))
            weighted_avg = float(ticker.get("weightedAvgPrice", 0))
        except (ValueError, KeyError):
            return None

        if price <= 0:
            return None

        if volume < settings.MIN_VOLUME_24H:
            return None

        abs_change = abs(change_pct)
        if abs_change < settings.MIN_PRICE_CHANGE:
            return None

        # V3: Skip extreme wicks / manipulated candles
        if high > 0 and low > 0:
            range_pct = ((high - low) / low) * 100
            if range_pct > 30:  # 30%+ daily range = dangerous
                return None

        return CoinCandidate(
            symbol=symbol,
            price=price,
            volume_24h=volume,
            price_change_pct=change_pct,
            bid=0.0,
            ask=0.0,
            spread_pct=0.0,
        )

    def enrich_with_spread(
        self, candidate: CoinCandidate, book_tickers: dict[str, dict]
    ) -> Optional[CoinCandidate]:
        """
        Add spread data from pre-fetched book tickers.
        Returns None if spread too wide or book data missing.
        """
        book = book_tickers.get(candidate.symbol)
        if not book:
            return None

        try:
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            spread_pct = ((ask - bid) / bid) * 100 if bid > 0 else 999

            if spread_pct > settings.MAX_SPREAD_PCT:
                return None

            candidate.bid = bid
            candidate.ask = ask
            candidate.spread_pct = round(spread_pct, 4)
            return candidate

        except (ValueError, KeyError) as e:
            logger.warning(f"Failed to enrich {candidate.symbol}: {e}")
            return None

    # ─── V3: Multi-Factor Scoring ─────────────────────────────────────

    def compute_composite_score(self, candidate: CoinCandidate) -> float:
        """
        V3: Score each coin by multiple quality dimensions (0-100 scale).
        
        Weights:
          - Volume:     25% — higher volume = better liquidity
          - Spread:     15% — tighter spread = less slippage
          - Trend:      15% — clear direction = better signals
          - Volatility: 15% — moderate volatility = good for trading
          - Momentum:   15% — recent move strength
          - Liquidity:  15% — bid/ask depth proxy
        """
        # Volume score (log-scaled, 0-100)
        import math
        vol_log = math.log10(max(candidate.volume_24h, 1))
        vol_norm = min((vol_log - 6) / 4, 1.0)  # 1M=6, 10B=10
        candidate.volume_score = max(vol_norm * 100, 0)

        # Spread score (lower = better)
        if candidate.spread_pct <= 0.01:
            candidate.spread_score = 100
        elif candidate.spread_pct <= 0.05:
            candidate.spread_score = 85
        elif candidate.spread_pct <= 0.10:
            candidate.spread_score = 65
        elif candidate.spread_pct <= 0.15:
            candidate.spread_score = 40
        else:
            candidate.spread_score = 10

        # Trend clarity score (higher abs change = clearer trend)
        abs_change = abs(candidate.price_change_pct)
        if 1.5 <= abs_change <= 8:
            candidate.trend_score = 80 + min(abs_change * 2, 20)
        elif 8 < abs_change <= 15:
            candidate.trend_score = 70
        elif abs_change > 15:
            candidate.trend_score = 30  # Too wild
        else:
            candidate.trend_score = max(abs_change * 30, 10)

        # Volatility quality (moderate = ideal for scalping)
        # Using price change as volatility proxy
        if 2 <= abs_change <= 6:
            candidate.volatility_score = 90
        elif 1 <= abs_change < 2:
            candidate.volatility_score = 70
        elif 6 < abs_change <= 10:
            candidate.volatility_score = 60
        else:
            candidate.volatility_score = 30

        # Momentum score (direction + magnitude)
        if abs_change > 3:
            candidate.momentum_score = min(abs_change * 12, 100)
        else:
            candidate.momentum_score = abs_change * 20

        # Liquidity proxy (volume per spread — higher = more liquid)
        if candidate.spread_pct > 0:
            liq_ratio = candidate.volume_24h / (candidate.spread_pct * 1_000_000)
            candidate.liquidity_score = min(liq_ratio * 10, 100)
        else:
            candidate.liquidity_score = 50

        # Weighted composite
        composite = (
            candidate.volume_score * 0.25
            + candidate.spread_score * 0.15
            + candidate.trend_score * 0.15
            + candidate.volatility_score * 0.15
            + candidate.momentum_score * 0.15
            + candidate.liquidity_score * 0.15
        )

        candidate.score = round(composite, 2)
        return candidate.score

    # ─── Main Scan ────────────────────────────────────────────────────

    async def scan(self, top_n: int = 100) -> list[dict]:
        """
        V3 Multi-factor scan:
        1. Get all USDT pairs
        2. Filter by volume/volatility/blacklist
        3. Batch-fetch book tickers (single API call)
        4. Enrich with spread, filter wide spreads
        5. Compute composite quality score
        6. Sort by composite score → return top N
        """
        logger.info("🔍 V3 multi-factor market scan starting...")

        # Fetch tickers and book tickers in parallel
        tickers_task = self.get_all_tickers()
        book_tickers_task = self.get_all_book_tickers()
        tickers, book_tickers = await asyncio.gather(tickers_task, book_tickers_task)

        logger.info(f"Total USDT pairs fetched: {len(tickers)}")

        # First-pass filter
        candidates = [c for t in tickers if (c := self.passes_filters(t)) is not None]
        logger.info(f"Candidates after basic filters: {len(candidates)}")

        # Enrich with spread data (no extra API calls — already batch-fetched)
        valid = []
        for candidate in candidates:
            enriched = self.enrich_with_spread(candidate, book_tickers)
            if enriched is not None:
                valid.append(enriched)

        logger.info(f"Candidates after spread filter: {len(valid)}")

        if not valid:
            logger.warning("No valid candidates after spread filter")
            return []

        # V3: Compute composite quality score
        for candidate in valid:
            self.compute_composite_score(candidate)

        # V3: Sort by composite score (NOT just volume)
        valid.sort(key=lambda x: x.score, reverse=True)
        top_coins = valid[:top_n]

        logger.info(
            f"📊 Returning top {len(top_coins)} coins by quality score: "
            f"{[f'{c.symbol}({c.score:.0f})' for c in top_coins[:10]]}"
            f"{'...' if len(top_coins) > 10 else ''}"
        )

        results = []
        for c in top_coins:
            results.append({
                "symbol": c.symbol,
                "price": c.price,
                "volume_24h": c.volume_24h,
                "price_change_pct": c.price_change_pct,
                "spread_pct": c.spread_pct,
                "bid": c.bid,
                "ask": c.ask,
                "score": c.score,
                # V3: Additional scoring detail
                "volume_score": c.volume_score,
                "spread_score": c.spread_score,
                "trend_score": c.trend_score,
                "volatility_score": c.volatility_score,
                "momentum_score": c.momentum_score,
                "liquidity_score": c.liquidity_score,
            })

        return results
