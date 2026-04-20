"""
User & Account ORM Models
V6: + password_hash, is_banned, is_admin, last_login on User
     + bot_enabled, last_sync, api_valid, last_error on Account
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text,
)
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=True)
    username = Column(String(100), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=True)  # V6: bcrypt hash, NULL for legacy users
    is_active = Column(Boolean, default=True, nullable=False)
    is_banned = Column(Boolean, default=False, nullable=False)  # V6: banned users can't login or trade
    is_admin = Column(Boolean, default=False, nullable=False)   # V6: admin flag
    last_login = Column(DateTime, nullable=True)                # V6: last login timestamp
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    accounts = relationship("Account", back_populates="user", lazy="selectin")
    subscriptions = relationship("Subscription", back_populates="user", lazy="selectin")
    payments = relationship("Payment", back_populates="user", lazy="selectin")
    audit_logs = relationship("AuditLog", back_populates="user", lazy="selectin")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    label = Column(String(100), nullable=False, default="Default")
    is_active = Column(Boolean, default=True, nullable=False)
    bot_enabled = Column(Boolean, default=True, nullable=False)   # V6: admin can disable bot per-account
    last_sync = Column(DateTime, nullable=True)                   # V6: last balance sync
    api_valid = Column(Boolean, default=True, nullable=False)     # V6: API key validity flag
    last_error = Column(Text, nullable=True)                      # V6: last error message
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="accounts")
    api_connection = relationship("ApiConnection", back_populates="account", uselist=False, lazy="selectin")
    balance = relationship("Balance", back_populates="account", uselist=False, lazy="selectin")
    trades = relationship("Trade", back_populates="account", lazy="selectin")
    positions = relationship("Position", back_populates="account", lazy="selectin")
    settings = relationship("Setting", back_populates="account", uselist=False, lazy="selectin")
    trade_skips = relationship("TradeSkip", back_populates="account", lazy="selectin")


class ApiConnection(Base):
    __tablename__ = "api_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)
    exchange = Column(String(50), default="binance", nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    api_secret_encrypted = Column(Text, nullable=False)
    permissions = Column(String(255), default="futures_only", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    account = relationship("Account", back_populates="api_connection")


class Balance(Base):
    __tablename__ = "balances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)
    balance_usdt = Column(Float, default=0.0, nullable=False)
    available_balance = Column(Float, default=0.0, nullable=False)
    total_margin_used = Column(Float, default=0.0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="balance")
