"""
V2 Dynamic Risk Engine — Balance-Based Tiers + Confidence Leverage

NO fixed trade sizes. Everything is calculated from live account balance.

Balance Risk Tiers:
  $20-$100   → 8% risk
  $101-$300  → 6% risk
  $301-$1000 → 4% risk
  $1000+     → 2% risk

Leverage by Confidence:
  65-79  → 5x
  80-89  → 8x
  90-94  → 10x
  95+    → 12x
  <70    → NO TRADE

TP/SL by Confidence + ATR:
  Low (65-79)   → TP 5%, SL 2%
  Med (80-89)   → TP 10%, SL 5%
  High (90+)    → TP 15%, SL 6%
"""

import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TradeParameters:
    symbol: str
    side: str                 # BUY | SELL
    leverage: int
    position_size_usdt: float
    safe_margin: float
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
    V2 Dynamic Risk Management — deterministic, balance-based, no randomness.
    """

    # ─── Balance Risk Tiers ───────────────────────────────────────────

    @staticmethod
    def get_risk_pct(balance: float) -> float:
        """Returns risk percentage based on account balance tier."""
        if balance <= 0:
            return 0.0
        elif balance <= 100:
            return 0.08   # 8%
        elif balance <= 300:
            return 0.06   # 6%
        elif balance <= 1000:
            return 0.04   # 4%
        else:
            return 0.02   # 2%

    # ─── Safe Margin Caps ─────────────────────────────────────────────

    @staticmethod
    def get_max_margin(balance: float) -> float:
        """
        Maximum margin allowed for a single trade.
        Protects small accounts from over-exposure.
        """
        if balance <= 30:
            return min(balance * 0.10, 2.0)
        elif balance <= 50:
            return min(balance * 0.08, 4.0)
        elif balance <= 100:
            return min(balance * 0.07, 7.0)
        elif balance <= 300:
            return min(balance * 0.05, 15.0)
        elif balance <= 1000:
            return min(balance * 0.04, 40.0)
        else:
            return min(balance * 0.02, 100.0)

    # ─── Leverage by Confidence ───────────────────────────────────────

    @staticmethod
    def get_leverage(confidence: int, max_leverage: int = 12) -> int:
        """Deterministic leverage based on confidence level."""
        if confidence < 65:
            return 0  # NO TRADE
        elif confidence < 80:
            return min(5, max_leverage)
        elif confidence < 90:
            return min(8, max_leverage)
        elif confidence < 95:
            return min(10, max_leverage)
        else:
            return min(12, max_leverage)

    # ─── TP/SL Percentages ────────────────────────────────────────────

    @staticmethod
    def get_tp_sl_pct(confidence: int, atr_pct: float = 0.0) -> tuple[float, float]:
        """
        Returns (tp_pct, sl_pct) as decimals based on confidence + ATR.
        Higher ATR → wider SL for safety.
        """
        if confidence >= 90:
            tp_pct = 0.15   # 15%
            sl_pct = 0.06   # 6%
        elif confidence >= 80:
            tp_pct = 0.10   # 10%
            sl_pct = 0.05   # 5%
        else:
            tp_pct = 0.05   # 5%
            sl_pct = 0.02   # 2%

        # Widen SL slightly for volatile coins
        if atr_pct > 1.0:
            volatility_factor = 1 + (atr_pct * 0.1)
            sl_pct *= min(volatility_factor, 1.5)  # Cap at 1.5x widening

        return tp_pct, sl_pct

    # ─── Main Calculation ─────────────────────────────────────────────

    def calculate(
        self,
        symbol: str,
        side: str,
        confidence: int,
        entry_price: float,
        atr_pct: float,
        account_balance: float,
        min_notional: float = 5.0,
        min_qty: float = 0.0,
        step_size: float = 0.0,
        quantity_precision: int = 3,
        price_precision: int = 4,
        max_leverage_override: int = 12,
        risk_pct_override: float = None,
    ) -> TradeParameters:
        """
        Compute full trade parameters with V2 dynamic risk management.

        Flow:
        1. Check minimum confidence
        2. Get risk % from balance tier
        3. Calculate safe_margin = balance * risk_pct
        4. Apply margin cap
        5. Get leverage from confidence
        6. Calculate position_size = safe_margin * leverage
        7. Validate symbol minimums
        8. Calculate TP/SL
        """

        # ── Confidence gate ──────────────────────────────────────────
        if confidence < settings.MIN_CONFIDENCE:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=0, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=0, confidence=confidence,
                approved=False,
                reject_reason=f"Confidence {confidence} below minimum {settings.MIN_CONFIDENCE}",
            )

        # ── Risk percentage ──────────────────────────────────────────
        risk_pct = risk_pct_override if risk_pct_override else self.get_risk_pct(account_balance)

        # ── Safe margin ──────────────────────────────────────────────
        safe_margin = account_balance * risk_pct
        max_margin = self.get_max_margin(account_balance)
        safe_margin = min(safe_margin, max_margin)

        # ── Leverage ─────────────────────────────────────────────────
        leverage = self.get_leverage(confidence, max_leverage_override)
        if leverage == 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                approved=False,
                reject_reason=f"Confidence {confidence} too low for any leverage",
            )

        # ── Position size ────────────────────────────────────────────
        position_size_usdt = safe_margin * leverage

        # ── Symbol minimum validation ────────────────────────────────
        if position_size_usdt < min_notional:
            # Try bumping to minimum
            required_margin = min_notional / leverage
            if required_margin <= max_margin * 1.2:  # Allow 20% over cap for minimums
                position_size_usdt = min_notional
                safe_margin = required_margin
                logger.info(f"  Bumped to min notional: ${min_notional} (margin=${safe_margin:.2f})")
            else:
                return TradeParameters(
                    symbol=symbol, side=side, leverage=leverage,
                    position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                    entry_price=entry_price, stop_loss=0, take_profit=0,
                    risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                    approved=False,
                    reject_reason=(
                        f"Position ${position_size_usdt:.2f} below min notional ${min_notional}. "
                        f"Bumping would exceed safe margin cap ${max_margin:.2f}"
                    ),
                )

        # ── Quantity calculation ──────────────────────────────────────
        if entry_price <= 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=leverage,
                position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                approved=False, reject_reason="Invalid entry price",
            )

        raw_quantity = position_size_usdt / entry_price

        # Apply step size rounding if provided
        if step_size > 0:
            raw_quantity = int(raw_quantity / step_size) * step_size

        quantity = round(raw_quantity, quantity_precision)

        # Validate min qty
        if min_qty > 0 and quantity < min_qty:
            return TradeParameters(
                symbol=symbol, side=side, leverage=leverage,
                position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=quantity,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                approved=False,
                reject_reason=f"Quantity {quantity} below symbol minimum {min_qty}",
            )

        # ── TP/SL ────────────────────────────────────────────────────
        tp_pct, sl_pct = self.get_tp_sl_pct(confidence, atr_pct)

        if side == "BUY":
            take_profit = entry_price * (1 + tp_pct)
            stop_loss = entry_price * (1 - sl_pct)
        else:
            take_profit = entry_price * (1 - tp_pct)
            stop_loss = entry_price * (1 + sl_pct)

        # Risk/reward ratio
        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        rr = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0

        take_profit = round(take_profit, price_precision)
        stop_loss = round(stop_loss, price_precision)

        logger.info(
            f"  Risk V2: bal=${account_balance:.2f} risk={risk_pct*100:.1f}% "
            f"margin=${safe_margin:.2f} lev={leverage}x pos=${position_size_usdt:.2f} "
            f"qty={quantity} TP={tp_pct*100:.1f}% SL={sl_pct*100:.1f}% RR={rr}"
        )

        return TradeParameters(
            symbol=symbol,
            side=side,
            leverage=leverage,
            position_size_usdt=round(position_size_usdt, 2),
            safe_margin=round(safe_margin, 2),
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=rr,
            risk_pct=round(risk_pct, 4),
            confidence=confidence,
            approved=True,
        )
