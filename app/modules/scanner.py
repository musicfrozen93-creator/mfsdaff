"""
Market Scanner Module — Multi-Coin Mode
Scans top coins by volume, returns up to 100 candidates
that pass quality filters (volume, spread, price change).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


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


class MarketScanner:
    """
    Scans Binance Futures for trading candidates.
    Returns up to 100 coins that pass quality filters,
    sorted by 24h volume descending.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url
        self.excluded = set(settings.EXCLUDED_COINS)

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
            change_pct = abs(float(ticker["priceChangePercent"]))
        except (ValueError, KeyError):
            return None

        if price <= 0:
            return None

        if volume < settings.MIN_VOLUME_24H:
            return None

        if change_pct < settings.MIN_PRICE_CHANGE:
            return None

        return CoinCandidate(
            symbol=symbol,
            price=price,
            volume_24h=volume,
            price_change_pct=change_pct,
            bid=0.0,
            ask=0.0,
            spread_pct=0.0,
            score=volume,  # Score by volume for ranking
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

    async def scan(self, top_n: int = 100) -> list[dict]:
        """
        Multi-coin scan:
        1. Get all USDT pairs
        2. Filter by volume/volatility
        3. Batch-fetch book tickers (single API call)
        4. Enrich with spread, filter wide spreads
        5. Sort by volume descending → return top N (default 100)
        """
        logger.info("🔍 Starting multi-coin market scan...")

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

        # Sort by volume descending → take top N
        valid.sort(key=lambda x: x.volume_24h, reverse=True)
        top_coins = valid[:top_n]

        logger.info(
            f"📊 Returning top {len(top_coins)} coins: "
            f"{[c.symbol for c in top_coins[:10]]}{'...' if len(top_coins) > 10 else ''}"
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
            })

        return results
