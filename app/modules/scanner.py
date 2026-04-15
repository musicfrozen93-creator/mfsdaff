"""
Market Scanner Module — Scalping Mode
Scans top coins by volume, selects top 10 → top 5 → random 1
"""

import asyncio
import logging
import random
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
    Scans Binance Futures for scalping candidates.
    Top coins by volume → top 10 → top 5 → random 1.
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

    async def get_book_ticker(self, symbol: str) -> dict:
        """Fetch best bid/ask for spread calculation"""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/ticker/bookTicker",
                params={"symbol": symbol},
            )
            resp.raise_for_status()
            return resp.json()

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

    async def enrich_with_spread(self, candidate: CoinCandidate) -> Optional[CoinCandidate]:
        """Add spread data; returns None if spread too wide."""
        try:
            book = await self.get_book_ticker(candidate.symbol)
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            spread_pct = ((ask - bid) / bid) * 100 if bid > 0 else 999

            if spread_pct > settings.MAX_SPREAD_PCT:
                return None

            candidate.bid = bid
            candidate.ask = ask
            candidate.spread_pct = round(spread_pct, 4)
            return candidate

        except Exception as e:
            logger.warning(f"Failed to enrich {candidate.symbol}: {e}")
            return None

    async def scan(self, top_n: int = 1) -> list[dict]:
        """
        Scalping scan:
        1. Get all USDT pairs
        2. Filter by volume/volatility
        3. Sort by volume → top 10
        4. From top 10, pick top 5
        5. Random pick 1
        """
        logger.info("🔍 Starting scalping market scan...")

        tickers = await self.get_all_tickers()
        logger.info(f"Total USDT pairs fetched: {len(tickers)}")

        # First-pass filter
        candidates = [c for t in tickers if (c := self.passes_filters(t)) is not None]
        logger.info(f"Candidates after basic filters: {len(candidates)}")

        # Sort by volume descending → top 10
        candidates.sort(key=lambda x: x.volume_24h, reverse=True)
        top_10 = candidates[:10]
        logger.info(f"Top 10 by volume: {[c.symbol for c in top_10]}")

        # Enrich with spread data
        tasks = [self.enrich_with_spread(c) for c in top_10]
        enriched = await asyncio.gather(*tasks)
        valid = [c for c in enriched if c is not None]

        if not valid:
            logger.warning("No valid candidates after spread filter")
            return []

        # From valid, pick top 5
        top_5 = valid[:5]
        logger.info(f"Top 5 candidates: {[c.symbol for c in top_5]}")

        # Random pick 1
        chosen = random.choice(top_5)
        logger.info(f"🎯 Randomly selected: {chosen.symbol}")

        result = {
            "symbol": chosen.symbol,
            "price": chosen.price,
            "volume_24h": chosen.volume_24h,
            "price_change_pct": chosen.price_change_pct,
            "spread_pct": chosen.spread_pct,
            "bid": chosen.bid,
            "ask": chosen.ask,
            "score": chosen.score,
        }

        return [result]
