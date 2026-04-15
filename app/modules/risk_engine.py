"""
Dynamic Risk Engine for Scalping
Computes leverage, position size, SL/TP based on confidence level.
Randomized within ranges for natural trading behavior.
"""

import logging
import random
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TradeParameters:
    symbol: str
    side: str                 # BUY | SELL
    leverage: int
    position_size_usdt: float
    quantity: float           # In base asset
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    risk_pct: float
    confidence: int
    approved: bool = True
    reject_reason: str = ""


class RiskEngine:
    """
    Dynamic risk management for scalping.
    Adjusts trade size, leverage, TP/SL based on confidence level.
    """

    def _get_confidence_tier(self, confidence: int) -> str:
        if confidence >= 85:
            return "HIGH"
        elif confidence >= 75:
            return "MEDIUM"
        else:
            return "LOW"

    def _calc_trade_size_pct(self, tier: str) -> float:
        """Trade size as % of account balance, randomized within range"""
        if tier == "HIGH":
            return random.uniform(0.05, 0.08)
        elif tier == "MEDIUM":
            return random.uniform(0.03, 0.05)
        else:
            return 0.03

    def _calc_leverage(self, tier: str) -> int:
        """Dynamic leverage based on confidence tier"""
        if tier == "HIGH":
            return random.randint(6, 8)
        elif tier == "MEDIUM":
            return random.randint(3, 5)
        else:
            return 3

    def _calc_tp_sl_pct(self, tier: str, atr_pct: float = 0.0) -> tuple:
        """
        Returns (tp_pct, sl_pct) as decimals.
        Uses ATR if available, otherwise static ranges.
        """
        if atr_pct > 0.5:
            # ATR-based dynamic TP/SL
            if tier == "HIGH":
                tp_pct = atr_pct * random.uniform(2.5, 4.0) / 100
                sl_pct = atr_pct * random.uniform(1.0, 1.5) / 100
            elif tier == "MEDIUM":
                tp_pct = atr_pct * random.uniform(1.5, 2.5) / 100
                sl_pct = atr_pct * random.uniform(0.8, 1.2) / 100
            else:
                tp_pct = atr_pct * random.uniform(1.0, 1.5) / 100
                sl_pct = atr_pct * random.uniform(0.7, 1.0) / 100
        else:
            # Static fallback ranges
            if tier == "HIGH":
                tp_pct = random.uniform(0.08, 0.15)
                sl_pct = random.uniform(0.04, 0.05)
            elif tier == "MEDIUM":
                tp_pct = random.uniform(0.04, 0.06)
                sl_pct = random.uniform(0.02, 0.03)
            else:
                tp_pct = random.uniform(0.03, 0.04)
                sl_pct = 0.02

        return tp_pct, sl_pct

    def calculate(
        self,
        symbol: str,
        side: str,
        confidence: int,
        entry_price: float,
        atr_pct: float,
        account_balance: float,
        quantity_precision: int = 3,
        price_precision: int = 4,
    ) -> TradeParameters:
        """Compute full trade parameters with dynamic risk management"""

        if confidence < settings.MIN_CONFIDENCE:
            return TradeParameters(
                symbol=symbol, side=side, leverage=1,
                position_size_usdt=0, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=0, confidence=confidence,
                approved=False,
                reject_reason=f"Confidence {confidence} below minimum {settings.MIN_CONFIDENCE}",
            )

        tier = self._get_confidence_tier(confidence)
        trade_size_pct = self._calc_trade_size_pct(tier)
        leverage = self._calc_leverage(tier)
        tp_pct, sl_pct = self._calc_tp_sl_pct(tier, atr_pct)

        # Position sizing
        capital_at_risk = account_balance * trade_size_pct
        position_size_usdt = capital_at_risk * leverage

        # TP/SL prices
        if side == "BUY":
            take_profit = entry_price * (1 + tp_pct)
            stop_loss = entry_price * (1 - sl_pct)
        else:
            take_profit = entry_price * (1 - tp_pct)
            stop_loss = entry_price * (1 + sl_pct)

        # Quantity
        raw_quantity = position_size_usdt / entry_price if entry_price > 0 else 0
        quantity = round(raw_quantity, quantity_precision)

        # Risk/reward ratio
        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        rr = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0

        logger.info(
            f"  Risk: tier={tier} | lev={leverage}x | size={trade_size_pct*100:.1f}% | "
            f"pos={position_size_usdt:.2f} USDT | qty={quantity} | "
            f"TP={tp_pct*100:.1f}% | SL={sl_pct*100:.1f}% | RR={rr}"
        )

        return TradeParameters(
            symbol=symbol,
            side=side,
            leverage=leverage,
            position_size_usdt=round(position_size_usdt, 2),
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=round(stop_loss, price_precision),
            take_profit=round(take_profit, price_precision),
            risk_reward=rr,
            risk_pct=round(trade_size_pct, 4),
            confidence=confidence,
            approved=True,
        )
