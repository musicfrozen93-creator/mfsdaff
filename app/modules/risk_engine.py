"""
V3 Dynamic Risk Engine — Balance-Based Tiers + Confidence Leverage

NO fixed trade sizes. Everything is calculated from live account balance.

Balance Risk Tiers (PRESERVED from V2):
  $20-$100   → 8% risk
  $101-$300  → 6% risk
  $301-$1000 → 4% risk
  $1000+     → 2% risk

V3 Leverage by Confidence:
  70-80  → 5x
  81-90  → 6x
  91+    → 8x
  Elite  → 10x (confidence 95+ with full confluence)
  <70    → NO TRADE

V3 TP/SL by Confidence (NOT leverage):
  70-80   → TP 5%, SL 2%
  81-90   → TP 7%, SL 3%
  91+     → TP 9%, SL 4%
  Elite   → TP 12%
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
    # V3 additions
    setup_grade: str = "C"    # A | B | C
    tp_pct: float = 0.0       # TP percentage for display
    sl_pct: float = 0.0       # SL percentage for display
    is_elite: bool = False     # Elite momentum setup


class RiskEngine:
    """
    V3 Dynamic Risk Management — deterministic, balance-based, no randomness.
    Position sizing PRESERVED from V2. TP/SL and leverage updated for V3.
    """

    # ─── Balance Risk Tiers (PRESERVED — DO NOT CHANGE) ──────────────

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

    # ─── Safe Margin Caps (PRESERVED — DO NOT CHANGE) ────────────────

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

    # ─── V3 Leverage by Confidence ───────────────────────────────────

    @staticmethod
    def get_leverage(confidence: int, is_elite: bool = False, max_leverage: int = 10) -> int:
        """
        V3 Deterministic leverage based on confidence level.
        More conservative than V2 — protects small accounts.
        """
        if confidence < 70:
            return 0  # NO TRADE
        elif confidence <= 80:
            return min(5, max_leverage)
        elif confidence <= 90:
            return min(6, max_leverage)
        elif is_elite and confidence >= 95:
            return min(10, max_leverage)  # Elite only
        else:
            return min(8, max_leverage)

    # ─── V3 TP/SL Percentages (Confidence-Based) ────────────────────

    @staticmethod
    def get_tp_sl_pct(
        confidence: int,
        atr_pct: float = 0.0,
        is_elite: bool = False,
        setup_grade: str = "C",
    ) -> tuple[float, float]:
        """
        V3: Returns (tp_pct, sl_pct) as decimals based on confidence + ATR.
        NOT leverage-based. Uses confidence + setup quality + market condition.
        """
        if confidence >= 91:
            tp_pct = 0.09   # 9%
            sl_pct = 0.04   # 4%
        elif confidence >= 81:
            tp_pct = 0.07   # 7%
            sl_pct = 0.03   # 3%
        else:
            tp_pct = 0.05   # 5%
            sl_pct = 0.02   # 2%

        # Elite momentum setups get extended TP
        if is_elite and confidence >= 95:
            tp_pct = 0.12   # 12%

        # ATR adjustment: widen SL slightly for volatile coins
        if atr_pct > 1.0:
            volatility_factor = 1 + (atr_pct * 0.1)
            sl_pct *= min(volatility_factor, 1.5)  # Cap at 1.5x widening

        return tp_pct, sl_pct

    # ─── Setup Grade ─────────────────────────────────────────────────

    @staticmethod
    def determine_setup_grade(confidence: int, volume_spike: bool = False) -> str:
        """
        A = Elite setup (confidence 91+, volume spike)
        B = Strong setup (confidence 81-90, or 91+ without volume)
        C = Standard setup (confidence 70-80)
        """
        if confidence >= 91 and volume_spike:
            return "A"
        elif confidence >= 81:
            return "B"
        else:
            return "C"

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
        max_leverage_override: int = 10,
        risk_pct_override: float = None,
        # V3 params
        volume_spike: bool = False,
        size_multiplier: float = 1.0,  # For daily guard reductions
    ) -> TradeParameters:
        """
        Compute full trade parameters with V3 dynamic risk management.

        Flow:
        1. Check minimum confidence
        2. Get risk % from balance tier (PRESERVED)
        3. Calculate safe_margin = balance * risk_pct
        4. Apply margin cap (PRESERVED)
        5. Get leverage from confidence (V3 tiers)
        6. Calculate position_size = safe_margin * leverage
        7. Apply size_multiplier (daily guard / consecutive loss reduction)
        8. Validate symbol minimums
        9. Calculate TP/SL (V3 confidence-based)
        """

        # ── Determine setup grade + elite status ─────────────────────
        setup_grade = self.determine_setup_grade(confidence, volume_spike)
        is_elite = setup_grade == "A" and confidence >= 95

        # ── Confidence gate ──────────────────────────────────────────
        if confidence < settings.MIN_CONFIDENCE:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=0, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=0, confidence=confidence,
                approved=False,
                reject_reason=f"Confidence {confidence} below minimum {settings.MIN_CONFIDENCE}",
                setup_grade=setup_grade,
            )

        # ── Risk percentage (PRESERVED) ──────────────────────────────
        risk_pct = risk_pct_override if risk_pct_override else self.get_risk_pct(account_balance)

        # ── Safe margin (PRESERVED) ──────────────────────────────────
        safe_margin = account_balance * risk_pct
        max_margin = self.get_max_margin(account_balance)
        safe_margin = min(safe_margin, max_margin)

        # ── V3: Apply size multiplier (daily guard / loss reduction) ──
        if size_multiplier < 1.0:
            safe_margin *= size_multiplier
            logger.info(f"  V3 size reduction: multiplier={size_multiplier:.2f} margin=${safe_margin:.2f}")

        # ── Leverage (V3 tiers) ──────────────────────────────────────
        leverage = self.get_leverage(confidence, is_elite, max_leverage_override)
        if leverage == 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                approved=False,
                reject_reason=f"Confidence {confidence} too low for any leverage",
                setup_grade=setup_grade,
            )

        # ── Position size (PRESERVED logic) ──────────────────────────
        position_size_usdt = safe_margin * leverage

        # ── Symbol minimum validation (PRESERVED) ────────────────────
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
                    setup_grade=setup_grade,
                )

        # ── Quantity calculation (PRESERVED) ─────────────────────────
        if entry_price <= 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=leverage,
                position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                approved=False, reject_reason="Invalid entry price",
                setup_grade=setup_grade,
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
                setup_grade=setup_grade,
            )

        # ── V3 TP/SL (Confidence-Based) ─────────────────────────────
        tp_pct, sl_pct = self.get_tp_sl_pct(confidence, atr_pct, is_elite, setup_grade)

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
            f"  Risk V3: bal=${account_balance:.2f} risk={risk_pct*100:.1f}% "
            f"margin=${safe_margin:.2f} lev={leverage}x pos=${position_size_usdt:.2f} "
            f"qty={quantity} TP={tp_pct*100:.1f}% SL={sl_pct*100:.1f}% RR={rr} "
            f"grade={setup_grade} elite={is_elite}"
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
            setup_grade=setup_grade,
            tp_pct=round(tp_pct * 100, 1),
            sl_pct=round(sl_pct * 100, 1),
            is_elite=is_elite,
        )
