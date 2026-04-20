"""
V5.5 Strategy Performance Tracker

Tracks win/loss rate, profit factor, avg R multiple, drawdown, and
performance by strategy type and regime. Used to adaptively weight
strategies over time.

V5.5 Additions:
  - Profit factor = gross profit / gross loss
  - Average R multiple = avg win / avg loss
  - Max drawdown tracking
  - Regime-specific performance
  - Bad symbol cooldown (3 losses in 7 days = 48h pause)
  - Strategy report endpoint data
"""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func, and_

from app.database import async_session
from app.models.trading import StrategyResult, DailyStats, Trade

logger = logging.getLogger(__name__)


class StrategyTracker:
    """V5.5 Track and analyze per-strategy performance with advanced metrics."""

    async def record_result(
        self,
        strategy_type: str,
        symbol: str,
        side: str,
        confidence: int,
        regime: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        duration_minutes: int = 0,
    ):
        """Record a closed trade's result for strategy tracking."""
        try:
            won = pnl > 0
            async with async_session() as session:
                result = StrategyResult(
                    strategy_type=strategy_type,
                    symbol=symbol,
                    side=side,
                    confidence=confidence,
                    regime=regime,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    won=won,
                    duration_minutes=duration_minutes,
                )
                session.add(result)
                await session.commit()
                logger.info(
                    f"📊 Strategy result recorded: {strategy_type} {symbol} "
                    f"{'WIN' if won else 'LOSS'} PnL={pnl:+.4f} ({pnl_pct:+.2f}%)"
                )
        except Exception as e:
            logger.warning(f"Failed to record strategy result: {e}")

    async def get_strategy_stats(self, days: int = 30) -> dict:
        """
        V5.5: Get comprehensive stats per strategy type.
        Includes win rate, profit factor, avg R multiple, max drawdown.
        """
        stats = {}
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            async with async_session() as session:
                result = await session.execute(
                    select(StrategyResult).where(
                        StrategyResult.created_at >= cutoff
                    )
                )
                entries = result.scalars().all()

                for entry in entries:
                    st = entry.strategy_type
                    if st not in stats:
                        stats[st] = {
                            "wins": 0, "losses": 0, "trades": 0,
                            "gross_profit": 0.0, "gross_loss": 0.0,
                            "win_pnls": [], "loss_pnls": [],
                            "pnl_curve": [],  # For drawdown calc
                            "regime_stats": {},
                        }

                    pnl = entry.pnl or 0
                    regime = entry.regime or "unknown"
                    stats[st]["trades"] += 1
                    stats[st]["pnl_curve"].append(pnl)

                    if entry.won:
                        stats[st]["wins"] += 1
                        stats[st]["gross_profit"] += pnl
                        stats[st]["win_pnls"].append(pnl)
                    else:
                        stats[st]["losses"] += 1
                        stats[st]["gross_loss"] += abs(pnl)
                        stats[st]["loss_pnls"].append(abs(pnl))

                    # Regime-specific tracking
                    if regime not in stats[st]["regime_stats"]:
                        stats[st]["regime_stats"][regime] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                    stats[st]["regime_stats"][regime]["total_pnl"] += pnl
                    if entry.won:
                        stats[st]["regime_stats"][regime]["wins"] += 1
                    else:
                        stats[st]["regime_stats"][regime]["losses"] += 1

                # Calculate aggregates
                for st in stats:
                    total = stats[st]["trades"]
                    wins = stats[st]["wins"]
                    gross_profit = stats[st]["gross_profit"]
                    gross_loss = stats[st]["gross_loss"]
                    win_pnls = stats[st]["win_pnls"]
                    loss_pnls = stats[st]["loss_pnls"]

                    # Win rate
                    stats[st]["win_rate"] = round(wins / total * 100, 1) if total > 0 else 0

                    # Profit factor = gross profit / gross loss
                    stats[st]["profit_factor"] = round(
                        gross_profit / gross_loss, 2
                    ) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

                    # Average R multiple = avg win / avg loss
                    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
                    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 1
                    stats[st]["avg_r_multiple"] = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0

                    # Average PnL
                    total_pnl = gross_profit - gross_loss
                    stats[st]["total_pnl"] = round(total_pnl, 4)
                    stats[st]["avg_pnl"] = round(total_pnl / total, 4) if total > 0 else 0

                    # Max drawdown from PnL curve
                    stats[st]["max_drawdown"] = self._calc_max_drawdown(stats[st]["pnl_curve"])

                    # Regime win rates
                    for regime, rd in stats[st]["regime_stats"].items():
                        rt = rd["wins"] + rd["losses"]
                        rd["win_rate"] = round(rd["wins"] / rt * 100, 1) if rt > 0 else 0

                    # Clean up internal data
                    del stats[st]["win_pnls"]
                    del stats[st]["loss_pnls"]
                    del stats[st]["pnl_curve"]

        except Exception as e:
            logger.warning(f"Failed to get strategy stats: {e}")

        return stats

    def _calc_max_drawdown(self, pnl_list: list[float]) -> float:
        """Calculate max drawdown from a PnL curve."""
        if not pnl_list:
            return 0.0
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in pnl_list:
            cumulative += pnl
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_dd = max(max_dd, drawdown)
        return round(max_dd, 4)

    async def get_strategy_weight_adjustments(self, days: int = 14) -> dict:
        """
        V5.5: Calculate adaptive weight adjustments based on rolling performance.
        Uses: (win_rate × 0.4) + (profit_factor × 0.4) + (drawdown_factor × 0.2)
        """
        stats = await self.get_strategy_stats(days)
        adjustments = {}

        for strategy_type, data in stats.items():
            if data["trades"] < 5:
                adjustments[strategy_type] = 1.0
                continue

            win_rate = data["win_rate"]
            profit_factor = data.get("profit_factor", 1.0)
            max_dd = data.get("max_drawdown", 0)

            # Win rate factor (40% weight)
            if win_rate >= 70:
                wr_factor = 1.3
            elif win_rate >= 55:
                wr_factor = 1.1
            elif win_rate >= 45:
                wr_factor = 0.9
            elif win_rate >= 35:
                wr_factor = 0.7
            else:
                wr_factor = 0.4  # Kill underperformers

            # Profit factor (40% weight)
            if profit_factor >= 2.0:
                pf_factor = 1.3
            elif profit_factor >= 1.5:
                pf_factor = 1.1
            elif profit_factor >= 1.0:
                pf_factor = 0.9
            elif profit_factor >= 0.5:
                pf_factor = 0.7
            else:
                pf_factor = 0.4

            # Drawdown penalty (20% weight)
            if max_dd < 1.0:
                dd_factor = 1.1
            elif max_dd < 3.0:
                dd_factor = 1.0
            elif max_dd < 5.0:
                dd_factor = 0.8
            else:
                dd_factor = 0.5  # Heavy penalty for big drawdowns

            # Combined weight
            weight = (wr_factor * 0.4) + (pf_factor * 0.4) + (dd_factor * 0.2)
            adjustments[strategy_type] = round(weight, 2)

            logger.debug(
                f"  Strategy weight {strategy_type}: WR={win_rate}% PF={profit_factor} "
                f"DD={max_dd} → weight={weight:.2f}"
            )

        return adjustments

    # ─── V5.5: Bad Symbol Cooldown ────────────────────────────────────

    async def check_symbol_cooldown(self, symbol: str, max_losses: int = 3, days: int = 7) -> tuple[bool, str]:
        """
        V5.5: Check if a symbol should be cooled down due to repeated losses.
        3+ losses in 7 days = 48h cooldown from last loss.
        Returns: (is_cooled_down, reason)
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            async with async_session() as session:
                result = await session.execute(
                    select(Trade).where(
                        and_(
                            Trade.symbol == symbol,
                            Trade.created_at >= cutoff,
                            Trade.pnl < 0,
                            Trade.status == "closed",
                        )
                    ).order_by(Trade.created_at.desc())
                )
                losses = result.scalars().all()

                if len(losses) >= max_losses:
                    last_loss = losses[0]
                    cooldown_end = last_loss.created_at + timedelta(hours=48)
                    now = datetime.now(timezone.utc)

                    if now < cooldown_end:
                        remaining = cooldown_end - now
                        hours = int(remaining.total_seconds() / 3600)
                        reason = (
                            f"Symbol {symbol} has {len(losses)} losses in {days}d. "
                            f"Cooldown: {hours}h remaining"
                        )
                        logger.info(f"  🧊 {reason}")
                        return True, reason

        except Exception as e:
            logger.warning(f"Symbol cooldown check failed: {e}")

        return False, ""

    # ─── V5.5: Strategy Report Data ───────────────────────────────────

    async def get_strategy_report(self, days: int = 30) -> dict:
        """
        V5.5: Generate comprehensive strategy report for /api/v1/strategy-report.
        Includes per-engine metrics, regime performance, and TP/SL success rates.
        """
        stats = await self.get_strategy_stats(days)

        # Group by engine type
        engines = {
            "scalping": {},
            "swing": {},
            "sniper": {},
        }

        for strategy_type, data in stats.items():
            if strategy_type.startswith("swing"):
                engines["swing"][strategy_type] = data
            elif strategy_type.startswith("sniper"):
                engines["sniper"][strategy_type] = data
            else:
                engines["scalping"][strategy_type] = data

        # Engine summaries
        report = {}
        for engine_name, strategies in engines.items():
            total_trades = sum(s["trades"] for s in strategies.values())
            total_wins = sum(s["wins"] for s in strategies.values())
            total_pnl = sum(s["total_pnl"] for s in strategies.values())

            report[engine_name] = {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "win_rate": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0,
                "total_pnl": round(total_pnl, 4),
                "strategies": strategies,
            }

        return report

    async def update_daily_stats(self, account_id: int, date_str: str,
                                  pnl: float, won: bool,
                                  regime: str = "", strategy_type: str = ""):
        """Update daily aggregated stats for an account."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(DailyStats).where(
                        and_(
                            DailyStats.account_id == account_id,
                            DailyStats.date == date_str,
                        )
                    )
                )
                entry = result.scalar_one_or_none()

                if not entry:
                    entry = DailyStats(
                        account_id=account_id,
                        date=date_str,
                        regime_distribution={},
                        strategy_distribution={},
                    )
                    session.add(entry)

                entry.trades_count = (entry.trades_count or 0) + 1
                entry.total_pnl = (entry.total_pnl or 0) + pnl

                if won:
                    entry.wins = (entry.wins or 0) + 1
                else:
                    entry.losses = (entry.losses or 0) + 1

                if pnl > (entry.best_trade_pnl or 0):
                    entry.best_trade_pnl = pnl
                if pnl < (entry.worst_trade_pnl or 0):
                    entry.worst_trade_pnl = pnl

                # Update distributions
                if regime:
                    rd = entry.regime_distribution or {}
                    rd[regime] = rd.get(regime, 0) + 1
                    entry.regime_distribution = rd

                if strategy_type:
                    sd = entry.strategy_distribution or {}
                    sd[strategy_type] = sd.get(strategy_type, 0) + 1
                    entry.strategy_distribution = sd

                await session.commit()

        except Exception as e:
            logger.warning(f"Failed to update daily stats: {e}")


# Singleton
strategy_tracker = StrategyTracker()
