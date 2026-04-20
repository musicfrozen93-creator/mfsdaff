"""
V5.5 Sniper / News Engine (ENGINE C)

Scans external data sources for trade catalysts:
  1. CryptoPanic — crypto news sentiment (free API)
  2. Binance funding rates — squeeze detection
  3. Volume anomaly detection — sudden explosions
  4. Fear & Greed Index — extreme readings

V5.5 Changes:
  - Minimum 2 confirmations required (cross-validation)
  - Headline spam filter (clickbait/vague terms removed)
  - Confidence capped at 80 (never overrides proven strategies)
  - Higher vote threshold (5+)
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.config import settings
from app.database import async_session
from app.models.trading import NewsEventCache

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Cache for funding rates (refresh every 15 min)
_funding_cache: Optional[dict] = None
_funding_cache_ts: float = 0.0


@dataclass
class SniperSetup:
    symbol: str
    side: str               # BUY | SELL
    setup_type: str         # news_breakout | panic_dump_reversal | funding_squeeze | volume_explosion
    confidence: int         # 0-100
    reason: str
    current_price: float = 0.0
    trigger: str = ""       # What triggered this setup
    strategy_type: str = "" # Filled by engine


class SniperEngine:
    """
    V5.5 Sniper/News Engine — quality-filtered catalyst detection.
    Requires 2+ confirmations. Confidence capped at 80.
    """

    # V5.5: Headlines containing these terms are ignored (low quality)
    SPAM_TERMS = [
        "could", "might", "rumor", "speculation", "prediction",
        "influencer", "whale alert", "moon", "100x", "to the moon",
        "opinion", "analyst says", "expect", "should",
    ]

    def __init__(self):
        self.base_url = settings.binance_base_url
        self.cryptopanic_key = settings.CRYPTOPANIC_API_KEY

    # ─── 1. CryptoPanic News ─────────────────────────────────────────

    async def scan_news(self) -> list[SniperSetup]:
        """
        Fetch recent crypto news from CryptoPanic free API.
        Returns setups for strong sentiment news.
        """
        setups = []

        if not self.cryptopanic_key:
            logger.debug("  CryptoPanic API key not set — skipping news scan")
            return setups

        try:
            url = "https://cryptopanic.com/api/v1/posts/"
            params = {
                "auth_token": self.cryptopanic_key,
                "currencies": "BTC,ETH,SOL,XRP,DOGE,BNB,ADA,AVAX,MATIC,DOT",
                "filter": "important",
                "public": "true",
            }

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    logger.warning(f"CryptoPanic API returned {resp.status_code}")
                    return setups
                data = resp.json()

            results = data.get("results", [])

            for post in results[:10]:
                event_id = str(post.get("id", ""))
                title = post.get("title", "")
                currencies = [c.get("code", "") for c in post.get("currencies", [])]
                votes = post.get("votes", {})

                # Check if already processed
                if await self._is_processed(event_id):
                    continue

                # Sentiment from votes
                positive = votes.get("positive", 0)
                negative = votes.get("negative", 0)
                total_votes = positive + negative

                if total_votes < 5:
                    continue  # V5.5: Not enough engagement (was 3)

                # V5.5: Spam headline filter
                title_lower = title.lower()
                if any(spam in title_lower for spam in self.SPAM_TERMS):
                    logger.debug(f"  Skipping spam headline: {title[:60]}")
                    continue

                sentiment_ratio = positive / total_votes if total_votes > 0 else 0.5

                # Strong positive news → potential long
                if sentiment_ratio > 0.75 and total_votes >= 5:
                    for currency in currencies:
                        symbol = f"{currency}USDT"
                        setups.append(SniperSetup(
                            symbol=symbol,
                            side="BUY",
                            setup_type="news_breakout",
                            confidence=min(65 + int(sentiment_ratio * 20), 85),
                            reason=f"Positive news: {title[:80]}",
                            trigger=f"CryptoPanic sentiment={sentiment_ratio:.0%} votes={total_votes}",
                            strategy_type="sniper_news_breakout",
                        ))

                # Strong negative news → potential short or reversal buy
                elif sentiment_ratio < 0.25 and total_votes >= 5:
                    for currency in currencies:
                        symbol = f"{currency}USDT"
                        setups.append(SniperSetup(
                            symbol=symbol,
                            side="SELL",
                            setup_type="news_breakout",
                            confidence=min(60 + int((1 - sentiment_ratio) * 20), 80),
                            reason=f"Negative news: {title[:80]}",
                            trigger=f"CryptoPanic sentiment={sentiment_ratio:.0%} votes={total_votes}",
                            strategy_type="sniper_news_breakout",
                        ))

                # Cache as processed
                await self._cache_event(event_id, title, currencies,
                    "positive" if sentiment_ratio > 0.5 else "negative", sentiment_ratio)

        except Exception as e:
            logger.warning(f"News scan failed: {e}")

        return setups

    # ─── 2. Funding Rate Squeeze ──────────────────────────────────────

    async def scan_funding_rates(self) -> list[SniperSetup]:
        """
        Detect extreme funding rates = potential squeeze.
        Very positive funding → shorts get paid, longs over-leveraged → potential dump
        Very negative funding → longs get paid, shorts over-leveraged → potential squeeze up
        """
        global _funding_cache, _funding_cache_ts

        setups = []
        now = time.time()

        try:
            # Use cache if fresh
            if _funding_cache and (now - _funding_cache_ts) < 900:
                funding_data = _funding_cache
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(f"{self.base_url}/fapi/v1/premiumIndex")
                    resp.raise_for_status()
                    funding_data = resp.json()
                    _funding_cache = funding_data
                    _funding_cache_ts = now

            for item in funding_data:
                symbol = item.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue

                funding_rate = float(item.get("lastFundingRate", 0))
                mark_price = float(item.get("markPrice", 0))

                # Extreme positive funding (> 0.1%) → shorts squeeze potential
                # Actually: very positive funding means longs pay shorts
                # Longs are overleveraged → potential SHORT or wait for dump then BUY
                if funding_rate > 0.001:  # > 0.1%
                    setups.append(SniperSetup(
                        symbol=symbol,
                        side="SELL",
                        setup_type="funding_squeeze",
                        confidence=min(60 + int(abs(funding_rate) * 5000), 82),
                        reason=f"Extreme positive funding {funding_rate*100:.3f}% — longs overleveraged",
                        current_price=mark_price,
                        trigger=f"Funding rate={funding_rate*100:.3f}%",
                        strategy_type="sniper_funding_squeeze",
                    ))

                # Extreme negative funding (< -0.05%) → longs squeeze potential
                elif funding_rate < -0.0005:
                    setups.append(SniperSetup(
                        symbol=symbol,
                        side="BUY",
                        setup_type="funding_squeeze",
                        confidence=min(60 + int(abs(funding_rate) * 5000), 82),
                        reason=f"Extreme negative funding {funding_rate*100:.3f}% — shorts overleveraged",
                        current_price=mark_price,
                        trigger=f"Funding rate={funding_rate*100:.3f}%",
                        strategy_type="sniper_funding_squeeze",
                    ))

        except Exception as e:
            logger.warning(f"Funding rate scan failed: {e}")

        # Return top 5 most extreme only
        setups.sort(key=lambda x: x.confidence, reverse=True)
        return setups[:5]

    # ─── 3. Volume Anomaly Detection ──────────────────────────────────

    async def scan_volume_anomalies(self, symbols: list[str]) -> list[SniperSetup]:
        """
        Detect sudden volume explosions in recent candles.
        Volume > 3x average = potential breakout catalyst.
        """
        setups = []
        semaphore = asyncio.Semaphore(5)

        async def check_symbol(symbol: str):
            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"{self.base_url}/fapi/v1/klines",
                            params={"symbol": symbol, "interval": "15m", "limit": 30},
                        )
                        resp.raise_for_status()
                        candles = resp.json()

                    if len(candles) < 20:
                        return None

                    volumes = [float(c[5]) for c in candles]
                    closes = [float(c[4]) for c in candles]

                    avg_vol = sum(volumes[:-3]) / len(volumes[:-3])
                    recent_vol = sum(volumes[-3:]) / 3

                    if avg_vol <= 0:
                        return None

                    vol_ratio = recent_vol / avg_vol

                    if vol_ratio >= 3.0:
                        # Determine direction from price
                        price_change = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0
                        side = "BUY" if price_change > 0 else "SELL"

                        return SniperSetup(
                            symbol=symbol,
                            side=side,
                            setup_type="volume_explosion",
                            confidence=min(60 + int(vol_ratio * 5), 85),
                            reason=f"Volume explosion {vol_ratio:.1f}x | price change {price_change:+.2f}%",
                            current_price=closes[-1],
                            trigger=f"Volume ratio={vol_ratio:.1f}x",
                            strategy_type="sniper_volume_explosion",
                        )

                except Exception:
                    pass
                return None

        tasks = [check_symbol(s) for s in symbols[:30]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, SniperSetup):
                setups.append(r)

        setups.sort(key=lambda x: x.confidence, reverse=True)
        return setups[:5]

    # ─── 4. Fear & Greed Index ────────────────────────────────────────

    async def get_fear_greed(self) -> Optional[dict]:
        """
        Fetch Fear & Greed Index from Alternative.me (free API).
        Returns: {"value": 25, "classification": "Extreme Fear", "timestamp": "..."}
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.alternative.me/fng/?limit=1")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if "data" in data and len(data["data"]) > 0:
                    entry = data["data"][0]
                    return {
                        "value": int(entry["value"]),
                        "classification": entry["value_classification"],
                        "timestamp": entry["timestamp"],
                    }
        except Exception as e:
            logger.debug(f"Fear & Greed fetch failed: {e}")
        return None

    # ─── Full Scan ────────────────────────────────────────────────────

    async def full_scan(self, symbols: list[str]) -> list[SniperSetup]:
        """
        Run all sniper scans and merge results.
        Returns ranked list of actionable sniper setups.
        """
        if not settings.SNIPER_ENABLED:
            logger.info("  🔇 Sniper engine disabled")
            return []

        logger.info("🎯 Running sniper scan...")

        # Run all scans concurrently
        news_task = self.scan_news()
        funding_task = self.scan_funding_rates()
        volume_task = self.scan_volume_anomalies(symbols)
        fng_task = self.get_fear_greed()

        results = await asyncio.gather(
            news_task, funding_task, volume_task, fng_task,
            return_exceptions=True,
        )

        all_setups = []

        # News results
        if isinstance(results[0], list):
            all_setups.extend(results[0])
            logger.info(f"  📰 News: {len(results[0])} setups")

        # Funding results
        if isinstance(results[1], list):
            all_setups.extend(results[1])
            logger.info(f"  💰 Funding: {len(results[1])} setups")

        # Volume results
        if isinstance(results[2], list):
            all_setups.extend(results[2])
            logger.info(f"  📊 Volume: {len(results[2])} setups")

        # Fear & Greed context
        fng = results[3] if not isinstance(results[3], Exception) else None
        if fng:
            logger.info(f"  😱 Fear & Greed: {fng['value']} ({fng['classification']})")
            # Extreme fear = boost BUY setups, extreme greed = boost SELL setups
            if fng["value"] < 20:  # Extreme fear
                for s in all_setups:
                    if s.side == "BUY":
                        s.confidence = min(s.confidence + 5, 90)
                        s.reason += f" | F&G={fng['value']} (Extreme Fear)"
            elif fng["value"] > 80:  # Extreme greed
                for s in all_setups:
                    if s.side == "SELL":
                        s.confidence = min(s.confidence + 5, 90)
                        s.reason += f" | F&G={fng['value']} (Extreme Greed)"

        # Sort by confidence
        all_setups.sort(key=lambda x: x.confidence, reverse=True)

        logger.info(f"  🎯 Raw sniper setups: {len(all_setups)}")

        # V5.5: Cross-validation — require 2+ confirmations per symbol
        confirmed_setups = self._cross_validate(all_setups)
        logger.info(f"  ✅ Confirmed setups (2+ sources): {len(confirmed_setups)}")

        # V5.5: Cap confidence at 80 (sniper should never outprioritize proven strategies)
        for s in confirmed_setups:
            s.confidence = min(s.confidence, 80)

        return confirmed_setups[:10]

    def _cross_validate(self, setups: list[SniperSetup]) -> list[SniperSetup]:
        """
        V5.5: Require minimum 2 independent confirmations per symbol.
        A news signal alone is not enough — it must be backed by volume or funding.
        """
        # Group setups by symbol + side
        from collections import defaultdict
        groups = defaultdict(list)
        for s in setups:
            key = f"{s.symbol}_{s.side}"
            groups[key].append(s)

        confirmed = []
        for key, group in groups.items():
            # Count unique source types
            source_types = set(s.setup_type for s in group)
            if len(source_types) >= 2:
                # 2+ different sources confirm — take the highest confidence one
                best = max(group, key=lambda x: x.confidence)
                sources_str = ", ".join(source_types)
                best.reason += f" | Confirmed by {len(source_types)} sources: {sources_str}"
                best.confidence = min(best.confidence + 5, 80)  # Small boost for cross-validation
                confirmed.append(best)
            elif len(group) == 1 and group[0].setup_type == "volume_explosion" and group[0].confidence >= 75:
                # Exception: very strong volume explosions can stand alone
                confirmed.append(group[0])

        confirmed.sort(key=lambda x: x.confidence, reverse=True)
        return confirmed

    # ─── Cache Helpers ────────────────────────────────────────────────

    async def _is_processed(self, event_id: str) -> bool:
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(NewsEventCache).where(NewsEventCache.event_id == event_id)
                )
                return result.scalar_one_or_none() is not None
        except Exception:
            return False

    async def _cache_event(self, event_id: str, title: str, symbols: list,
                           sentiment: str, impact: float):
        try:
            async with async_session() as session:
                entry = NewsEventCache(
                    source="cryptopanic",
                    event_id=event_id,
                    title=title[:500],
                    symbols=symbols,
                    sentiment=sentiment,
                    impact_score=impact,
                    processed=True,
                )
                session.add(entry)
                await session.commit()
        except Exception as e:
            logger.debug(f"Failed to cache news event: {e}")
