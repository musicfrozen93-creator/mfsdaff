"""
Trading ORM Models — Signals, Trades, Positions, Skips
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
