"""
System ORM Models — Settings, Subscriptions, Audit Logs
V6: Extended Subscription (plan_name, price, start_date, end_date, added_by_admin, notes)
    Extended AuditLog (admin_email, target_user_id)
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text, JSON,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)
    risk_pct_override = Column(Float, nullable=True)  # Override balance-based tier
    max_leverage = Column(Integer, default=12, nullable=False)
    enabled_symbols = Column(JSON, nullable=True)  # null = all symbols
    auto_trade = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("Account", back_populates="settings")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Original fields (kept for backward compatibility)
    plan = Column(String(50), default="free", nullable=False)  # free | basic | pro
    status = Column(String(20), default="active", nullable=False)  # active | expired | cancelled
    max_accounts = Column(Integer, default=1, nullable=False)
    expires_at = Column(DateTime, nullable=True)

    # V6: Extended subscription fields
    plan_name = Column(String(100), nullable=True)     # Human-readable plan name
    price = Column(Float, nullable=True)               # Subscription price
    start_date = Column(DateTime, nullable=True)       # When subscription started
    end_date = Column(DateTime, nullable=True)         # When subscription ends (for expiry check)
    added_by_admin = Column(Boolean, default=True, nullable=False)  # Admin added manually
    notes = Column(Text, nullable=True)                # Admin notes

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="subscriptions")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False)
    details_json = Column(JSON, nullable=True)
    ip_address = Column(String(45), nullable=True)

    # V6: Enhanced audit tracking
    admin_email = Column(String(255), nullable=True)    # Which admin performed the action
    target_user_id = Column(Integer, nullable=True)     # Target user of the action

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    user = relationship("User", back_populates="audit_logs")
