"""
Payment ORM Model — Simple admin-side payment records.
No payment gateway integration. Admin records manual WhatsApp payments.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, Boolean, DateTime, ForeignKey, Text,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    verified_by_admin = Column(Boolean, default=False, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="payments")
