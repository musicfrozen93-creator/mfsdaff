"""
Trading ORM Models — Signals, Trades, Positions, Skips
V5: + SwingWatchlist, DailyStats, StrategyResult, NewsEventCache
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text, JSON,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY | SELL
    confidence = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    indicators_json = Column(JSON, nullable=True)
    ai_response_json = Column(JSON, nullable=True)
    ai_called = Column(Boolean, default=False)
    ai_tokens_used = Column(Integer, default=0)
    ai_model = Column(String(50), nullable=True)
    ai_latency_ms = Column(Integer, default=0)
    ai_fallback = Column(Boolean, default=False)
    # V5: strategy + regime tracking
    strategy_type = Column(String(30), nullable=True)   # trend_pullback | breakout_momentum | range_reversal | swing_* | sniper_*
    regime = Column(String(30), nullable=True)           # TRENDING_BULL | TRENDING_BEAR | SIDEWAYS_RANGE | etc.
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    trades = relationship("Trade", back_populates="signal", lazy="selectin")
    trade_skips = relationship("TradeSkip", back_populates="signal", lazy="selectin")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY | SELL
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    position_size_usdt = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False)
    take_profit = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    risk_pct = Column(Float, nullable=True)
    confidence = Column(Integer, nullable=True)
    order_id = Column(String(50), nullable=True)
    sl_order_id = Column(String(50), nullable=True)
    tp_order_id = Column(String(50), nullable=True)
    status = Column(String(20), default="open", nullable=False)  # open | closed | cancelled
    close_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    close_reason = Column(String(50), nullable=True)  # tp_hit | sl_hit | manual | error
    # V5: strategy + regime tracking
    strategy_type = Column(String(30), nullable=True)
    regime = Column(String(30), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True)

    # Relationships
    signal = relationship("Signal", back_populates="trades")
    account = relationship("Account", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False)
    unrealized_pnl = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="positions")


class TradeSkip(Base):
    __tablename__ = "trade_skips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    symbol = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False)
    category = Column(String(50), nullable=True)  # low_balance | risk_limit | min_notional | existing_position
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    signal = relationship("Signal", back_populates="trade_skips")
    account = relationship("Account", back_populates="trade_skips")


# ═══════════════════════════════════════════════════════════════════════
# V5 NEW MODELS
# ═══════════════════════════════════════════════════════════════════════

class SwingWatchlist(Base):
    """V5: Swing setup memory system — stores promising setups for delayed execution."""
    __tablename__ = "swing_watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)           # BUY | SELL
    setup_type = Column(String(30), nullable=False)     # trend_continuation | breakout_base | major_reversal
    confidence = Column(Integer, nullable=False)
    trigger_price = Column(Float, nullable=False)
    invalidation_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    regime_at_creation = Column(String(30), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="watching", nullable=False)  # watching | triggered | executed | invalidated | expired
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyStats(Base):
    """V5: Per-account daily aggregated stats for tracking and learning."""
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    trades_count = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    total_pnl_pct = Column(Float, default=0.0)
    best_trade_pnl = Column(Float, default=0.0)
    worst_trade_pnl = Column(Float, default=0.0)
    regime_distribution = Column(JSON, nullable=True)       # {"TRENDING_BULL": 3, "SIDEWAYS": 1}
    strategy_distribution = Column(JSON, nullable=True)     # {"trend_pullback": 2, "breakout": 1}
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StrategyResult(Base):
    """V5: Per-strategy performance tracking for adaptive learning."""
    __tablename__ = "strategy_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_type = Column(String(30), nullable=False, index=True)
    symbol = Column(String(20), nullable=True)
    side = Column(String(10), nullable=True)
    confidence = Column(Integer, nullable=True)
    regime = Column(String(30), nullable=True)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    won = Column(Boolean, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class NewsEventCache(Base):
    """V5: Cache for news events to avoid duplicate processing."""
    __tablename__ = "news_events_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(30), nullable=False)             # cryptopanic | binance | coingecko
    event_id = Column(String(100), nullable=False, index=True)  # External ID or hash
    title = Column(Text, nullable=True)
    symbols = Column(JSON, nullable=True)                   # ["BTC", "ETH"]
    sentiment = Column(String(20), nullable=True)           # positive | negative | neutral
    impact_score = Column(Float, default=0.0)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

