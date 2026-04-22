"""
V7 Learning Engine — Adaptive Strategy System

Provides:
  1. Strategy Registry (9 strategies) — seeded on startup
  2. Pre-trade: get_best_strategy() — returns highest-ranked strategy for conditions
  3. Post-trade: record_trade() — stores result and updates strategy weights
  4. Analytics: get_strategy_rankings() — full ranking report
  
Weight System:
  - Default weight: 1.0
  - Winning strategies: up to 1.3
  - Losing strategies: down to 0.3
  - Minimum 5 trades before adjustment
  - Updated after each trade close
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, and_

from app.database import async_session
from app.models.trading import StrategyRegistry, TradeMemory, StrategyResult
from app.modules.strategy_tracker import strategy_tracker

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# V7 STRATEGY DEFINITIONS — 9 starter strategies
# ═══════════════════════════════════════════════════════════════════════

STRATEGY_DEFINITIONS = [
    # Scalp strategies (5m timeframe)
    {
        "strategy_id": "scalp_trend_pullback",
        "method": "scalp",
        "name": "Trend Pullback Scalp",
        "description": "EMA trend + pullback to support. Best in trending markets.",
    },
    {
        "strategy_id": "scalp_breakout_momentum",
        "method": "scalp",
        "name": "Breakout Momentum Scalp",
        "description": "Resistance break + volume spike + strong close. Best in expansion.",
    },
    {
        "strategy_id": "scalp_range_reversal",
        "method": "scalp",
        "name": "Range Reversal Scalp",
        "description": "Support bounce / resistance rejection at extremes. Best in ranges.",
    },
    # Swing strategies (4H timeframe)
    {
        "strategy_id": "swing_trend_continuation",
        "method": "swing",
        "name": "Swing Trend Continuation",
        "description": "Enter on pullbacks in strong 4H trends. Hold for larger targets.",
    },
    {
        "strategy_id": "swing_breakout_base",
        "method": "swing",
        "name": "Swing Breakout Base",
        "description": "Breakout from consolidation base with volume. Hold for expansion.",
    },
    {
        "strategy_id": "swing_major_reversal",
        "method": "swing",
        "name": "Swing Major Reversal",
        "description": "Major trend reversal at key levels. High R:R but lower win rate.",
    },
    # Sniper strategies (event-driven)
    {
        "strategy_id": "sniper_news_breakout",
        "method": "snipe",
        "name": "Sniper News Breakout",
        "description": "Fast entries on news catalysts. Requires multi-source confirmation.",
    },
    {
        "strategy_id": "sniper_funding_squeeze",
        "method": "snipe",
        "name": "Sniper Funding Squeeze",
        "description": "Exploit extreme funding rates for mean-reversion plays.",
    },
    {
        "strategy_id": "sniper_volume_explosion",
        "method": "snipe",
        "name": "Sniper Volume Explosion",
        "description": "Trade sudden volume anomalies (3x+ average) with momentum.",
    },
]


class LearningEngine:
    """
    V7 Adaptive Learning System.
    
    - Seeds strategy registry on startup
    - Ranks strategies by rolling performance
    - Records trade results to trade_memory
    - Updates strategy weights automatically
    """

    async def seed_strategy_registry(self):
        """
        Seed the strategy_registry table with 9 starter strategies.
        Only inserts if strategy_id doesn't already exist.
        """
        try:
            async with async_session() as session:
                for defn in STRATEGY_DEFINITIONS:
                    result = await session.execute(
                        select(StrategyRegistry).where(
                            StrategyRegistry.strategy_id == defn["strategy_id"]
                        )
                    )
                    existing = result.scalar_one_or_none()

                    if not existing:
                        entry = StrategyRegistry(
                            strategy_id=defn["strategy_id"],
                            method=defn["method"],
                            name=defn["name"],
                            description=defn["description"],
                            weight=1.0,
                        )
                        session.add(entry)
                        logger.info(f"  📋 Seeded strategy: {defn['strategy_id']}")

                await session.commit()
                logger.info("✅ Strategy registry seeded successfully")

        except Exception as e:
            logger.warning(f"Failed to seed strategy registry: {e}")

    async def get_best_strategy(
        self,
        method: str = "scalp",
        market_regime: str = "",
    ) -> Optional[dict]:
        """
        V7: Get the highest-ranked active strategy for the given method and regime.
        Uses rolling 20-trade performance score.
        
        Returns: {strategy_id, weight, rolling_score, win_rate, ...} or None
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(StrategyRegistry).where(
                        and_(
                            StrategyRegistry.method == method,
                            StrategyRegistry.is_active == True,
                        )
                    )
                )
                strategies = result.scalars().all()

                if not strategies:
                    return None

                # Get rolling scores for each strategy
                ranked = []
                for s in strategies:
                    rolling = await strategy_tracker.get_rolling_score(
                        s.strategy_id, window=20,
                    )

                    # Combine registry weight with rolling score
                    effective_score = rolling["score"] * (s.weight or 1.0)

                    # Regime bonus: if this strategy has a best_regime matching current
                    if market_regime and s.best_regime == market_regime:
                        effective_score *= 1.1

                    ranked.append({
                        "strategy_id": s.strategy_id,
                        "method": s.method,
                        "name": s.name,
                        "weight": round(s.weight or 1.0, 2),
                        "rolling_score": rolling["score"],
                        "effective_score": round(effective_score, 1),
                        "win_rate": rolling["win_rate"],
                        "avg_pnl": rolling["avg_pnl"],
                        "trades": rolling["trades"],
                        "profit_factor": rolling.get("profit_factor", 0),
                    })

                # Sort by effective score, highest first
                ranked.sort(key=lambda x: x["effective_score"], reverse=True)

                if ranked:
                    best = ranked[0]
                    logger.info(
                        f"  🏆 V7 Best strategy for {method}/{market_regime}: "
                        f"{best['strategy_id']} (score={best['effective_score']}, "
                        f"WR={best['win_rate']}%, weight={best['weight']})"
                    )
                    return best

        except Exception as e:
            logger.warning(f"Failed to get best strategy: {e}")

        return None

    async def record_trade(
        self,
        strategy_id: str,
        method: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float = 0.0,
        pnl_pct: float = 0.0,
        won: bool = False,
        market_regime: str = "",
        btc_trend: str = "",
        confidence: int = 0,
        confidence_breakdown: dict = None,
        setup_grade: str = "",
        duration_minutes: int = 0,
        tp_result: str = "",
        sl_result: str = "",
        emergency_closed: bool = False,
    ):
        """
        V7: Record a completed trade to trade_memory and update strategy registry.
        Called after each trade is closed.
        """
        try:
            async with async_session() as session:
                # 1. Store in trade_memory
                memory = TradeMemory(
                    strategy_id=strategy_id,
                    method=method,
                    symbol=symbol,
                    market_regime=market_regime,
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    tp_result=tp_result,
                    sl_result=sl_result,
                    pnl_pct=pnl_pct,
                    won=won,
                    duration_minutes=duration_minutes,
                    btc_trend=btc_trend,
                    confidence=confidence,
                    confidence_breakdown=confidence_breakdown,
                    setup_grade=setup_grade,
                    emergency_closed=emergency_closed,
                )
                session.add(memory)

                # 2. Update strategy registry stats
                reg_result = await session.execute(
                    select(StrategyRegistry).where(
                        StrategyRegistry.strategy_id == strategy_id
                    )
                )
                registry = reg_result.scalar_one_or_none()

                if registry:
                    registry.total_trades = (registry.total_trades or 0) + 1
                    if won:
                        registry.wins = (registry.wins or 0) + 1
                    else:
                        registry.losses = (registry.losses or 0) + 1

                    total = registry.total_trades
                    registry.win_rate = round(
                        (registry.wins or 0) / total * 100, 1
                    ) if total > 0 else 0

                    registry.total_pnl = round(
                        (registry.total_pnl or 0) + pnl_pct, 4
                    )
                    registry.avg_pnl = round(
                        registry.total_pnl / total, 4
                    ) if total > 0 else 0

                    # Update weight every 5 trades
                    if total >= 5 and total % 5 == 0:
                        new_weight = await self._calculate_weight(strategy_id)
                        registry.weight = new_weight
                        logger.info(
                            f"  📊 V7 Strategy weight updated: {strategy_id} → {new_weight}"
                        )

                    registry.updated_at = datetime.now(timezone.utc)

                await session.commit()

                logger.info(
                    f"  📝 V7 Trade recorded: {strategy_id} {symbol} "
                    f"{'WIN' if won else 'LOSS'} PnL={pnl_pct:+.2f}%"
                )

        except Exception as e:
            logger.warning(f"Failed to record trade to learning engine: {e}")

    async def _calculate_weight(self, strategy_id: str) -> float:
        """
        V7: Calculate adaptive weight for a strategy based on rolling performance.
        
        Weight range: 0.3 (kill) to 1.3 (boost)
        """
        rolling = await strategy_tracker.get_rolling_score(strategy_id, window=20)

        score = rolling["score"]
        win_rate = rolling["win_rate"]
        profit_factor = rolling.get("profit_factor", 1.0)

        # Base weight from rolling score
        if score >= 80:
            weight = 1.3
        elif score >= 65:
            weight = 1.1
        elif score >= 50:
            weight = 1.0
        elif score >= 35:
            weight = 0.7
        elif score >= 20:
            weight = 0.5
        else:
            weight = 0.3

        # Fine-tune with win rate
        if win_rate >= 65:
            weight = min(weight + 0.1, 1.3)
        elif win_rate < 35:
            weight = max(weight - 0.1, 0.3)

        # Profit factor adjustment
        if profit_factor >= 2.0:
            weight = min(weight + 0.05, 1.3)
        elif profit_factor < 0.5 and rolling["trades"] >= 10:
            weight = max(weight - 0.1, 0.3)

        return round(weight, 2)

    async def get_strategy_rankings(self) -> list[dict]:
        """
        V7: Get full strategy rankings with rolling scores.
        Used for the strategy report endpoint.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(StrategyRegistry).where(
                        StrategyRegistry.is_active == True
                    )
                )
                strategies = result.scalars().all()

                rankings = []
                for s in strategies:
                    rolling = await strategy_tracker.get_rolling_score(
                        s.strategy_id, window=20,
                    )
                    best_coins = await strategy_tracker.get_best_coin_pairings(
                        s.strategy_id, top_n=3,
                    )

                    rankings.append({
                        "strategy_id": s.strategy_id,
                        "method": s.method,
                        "name": s.name,
                        "is_active": s.is_active,
                        "weight": round(s.weight or 1.0, 2),
                        "total_trades": s.total_trades or 0,
                        "wins": s.wins or 0,
                        "losses": s.losses or 0,
                        "win_rate": round(s.win_rate or 0, 1),
                        "avg_pnl": round(s.avg_pnl or 0, 4),
                        "total_pnl": round(s.total_pnl or 0, 4),
                        "rolling_score": rolling["score"],
                        "rolling_win_rate": rolling["win_rate"],
                        "rolling_profit_factor": rolling.get("profit_factor", 0),
                        "best_coins": best_coins,
                    })

                # Sort by rolling score
                rankings.sort(key=lambda x: x["rolling_score"], reverse=True)
                return rankings

        except Exception as e:
            logger.warning(f"Failed to get strategy rankings: {e}")
            return []

    async def map_strategy_id(
        self, strategy_type: str, method: str = "scalp",
    ) -> str:
        """
        V7: Map an existing strategy_type (from ai_engine) to a strategy_id.
        Handles backward compatibility with V5 naming.
        """
        mapping = {
            # Scalp
            "trend_pullback": "scalp_trend_pullback",
            "breakout_momentum": "scalp_breakout_momentum",
            "range_reversal": "scalp_range_reversal",
            # Swing
            "trend_continuation": "swing_trend_continuation",
            "breakout_base": "swing_breakout_base",
            "major_reversal": "swing_major_reversal",
            # Sniper
            "news_breakout": "sniper_news_breakout",
            "funding_squeeze": "sniper_funding_squeeze",
            "volume_explosion": "sniper_volume_explosion",
        }

        # Direct match
        if strategy_type in mapping:
            return mapping[strategy_type]

        # Already a full strategy_id
        for defn in STRATEGY_DEFINITIONS:
            if defn["strategy_id"] == strategy_type:
                return strategy_type

        # Fallback: prefix with method
        return f"{method}_{strategy_type}"


# Singleton
learning_engine = LearningEngine()
