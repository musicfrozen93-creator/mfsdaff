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


# ═══════════════════════════════════════════════════════════════════════
# V7 NEW MODELS — Adaptive Learning System
# ═══════════════════════════════════════════════════════════════════════

class StrategyRegistry(Base):
    """
    V7: Strategy catalog with performance tracking and adaptive weights.
    Contains 9 starter strategies that the learning engine ranks.
    """
    __tablename__ = "strategy_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(50), unique=True, nullable=False, index=True)  # scalp_trend_pullback
    method = Column(String(20), nullable=False)                     # scalp | swing | snipe
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    weight = Column(Float, default=1.0)                            # Adaptive weight (0.3 - 1.3)
    total_trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    avg_pnl = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    best_regime = Column(String(30), nullable=True)                # Best-performing regime
    best_symbols = Column(JSON, nullable=True)                     # Top 5 symbols
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TradeMemory(Base):
    """
    V7: Extended trade result storage for learning engine.
    Stores richer data than StrategyResult for advanced analysis.
    """
    __tablename__ = "trade_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(50), nullable=False, index=True)   # Links to strategy_registry.strategy_id
    method = Column(String(20), nullable=False)                    # scalp | swing | snipe
    symbol = Column(String(20), nullable=False, index=True)
    market_regime = Column(String(30), nullable=True)
    side = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    tp_result = Column(String(20), nullable=True)                  # hit | missed | partial
    sl_result = Column(String(20), nullable=True)                  # hit | missed
    pnl_pct = Column(Float, nullable=True)
    won = Column(Boolean, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    btc_trend = Column(String(20), nullable=True)
    confidence = Column(Integer, nullable=True)
    confidence_breakdown = Column(JSON, nullable=True)             # V7 pillar scores
    setup_grade = Column(String(5), nullable=True)
    emergency_closed = Column(Boolean, default=False)              # V7: was it emergency closed?
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class DailyPnlLog(Base):
    """
    V7: Daily P&L log per account.
    Persists daily_guard state to DB for historical tracking and restart recovery.
    """
    __tablename__ = "daily_pnl_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)          # YYYY-MM-DD
    starting_balance = Column(Float, default=0.0)
    ending_balance = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    total_pnl_pct = Column(Float, default=0.0)
    trades_count = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    best_trade_pnl = Column(Float, default=0.0)
    worst_trade_pnl = Column(Float, default=0.0)
    max_consecutive_losses = Column(Integer, default=0)
    was_stopped = Column(Boolean, default=False)
    stop_reason = Column(String(200), nullable=True)
    was_safe_mode = Column(Boolean, default=False)
    regime_distribution = Column(JSON, nullable=True)              # {"TRENDING_BULL": 3, ...}
    strategy_distribution = Column(JSON, nullable=True)            # {"scalp_trend_pullback": 5, ...}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════════
# V9 Position Manager — Open Position Tracking
# ═══════════════════════════════════════════════════════════════════════

class OpenPosition(Base):
    """
    V9: Live position tracking for the Position Manager bot.

    Written at trade OPEN by the main bot.
    Read every loop iteration by position_manager.py.
    Updated to status='closed' when TP/SL/trailing triggers.

    Strategy-aware: stores the exact TP/SL prices computed by RiskEngine
    at trade open time — no re-calculation needed later.
    """
    __tablename__ = "open_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ─────────────────────────────────────────────────────
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)  # link to trades table
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)             # BUY | SELL

    # ── Entry data ───────────────────────────────────────────────────
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    leverage = Column(Integer, default=1)
    position_size_usdt = Column(Float, default=0.0)

    # ── Strategy context (preserved from signal engine) ───────────────
    strategy_type = Column(String(30), nullable=True)    # scalp_trend_pullback | swing_* | sniper_*
    timeframe = Column(String(10), nullable=True)        # 1m | 5m | 15m | 4h
    confidence = Column(Integer, default=0)
    regime = Column(String(30), nullable=True)

    # ── TP/SL prices (computed by RiskEngine at open — exact formulas) ─
    tp_price = Column(Float, nullable=False)
    sl_price = Column(Float, nullable=False)
    tp_pct = Column(Float, default=0.0)   # e.g. 3.5 (percent)
    sl_pct = Column(Float, default=0.0)   # e.g. 1.5 (percent)

    # ── Trailing stop (optional, activated after BE trigger) ──────────
    trailing_active = Column(Boolean, default=False)
    trailing_sl_price = Column(Float, nullable=True)    # Current trailing SL price
    trailing_trigger_pct = Column(Float, default=0.0)   # % profit to activate trailing
    highest_price = Column(Float, nullable=True)        # Peak price seen (for trailing calc)
    lowest_price = Column(Float, nullable=True)         # Trough price seen (for trailing calc)

    # ── Binance execution IDs (may be None if broker protection disabled) ─
    entry_order_id = Column(String(50), nullable=True)

    # ── Hedge mode flags (needed for correct reduceOnly / positionSide) ─
    is_hedge_mode = Column(Boolean, default=False)
    position_side = Column(String(10), default="BOTH")   # BOTH | LONG | SHORT

    # ── Status ───────────────────────────────────────────────────────
    status = Column(String(20), default="open", nullable=False, index=True)
    # open | closed | error
    close_price = Column(Float, nullable=True)
    close_reason = Column(String(30), nullable=True)     # tp_hit | sl_hit | trailing_exit | manual
    pnl_usdt = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)

    # ── Monitoring metadata ───────────────────────────────────────────
    last_checked_at = Column(DateTime, nullable=True)
    last_price = Column(Float, nullable=True)            # Last known market price
    check_count = Column(Integer, default=0)

    # ── Timestamps ───────────────────────────────────────────────────
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True)

