"""
V5.5 Dynamic Risk Engine — Optimized TP/SL + ATR-Adaptive Stops

NO fixed trade sizes. Everything is calculated from live account balance.

Balance Risk Tiers (PRESERVED from V2):
  $20-$100   → 8% risk
  $101-$300  → 6% risk
  $301-$1000 → 4% risk
  $1000+     → 2% risk

V5.5 Leverage (10x REMOVED — max 8x elite scalp only):
  Scalp:  4x / 5x / 6x / 8x-elite
  Swing:  2x / 3x / 4x / 5x-elite
  Sniper: 3x / 3x / 4x / 4x-max

V5.5 TP/SL (wider TP, ATR-dynamic SL):
  Scalp:  TP 2.5-6%, SL max(1-2%, 0.8-1.5×ATR)
  Swing:  TP 5-12%, SL 2-4%
  Sniper: TP 2-5%, SL 1-2%
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
    # V5.5 Partial TP additions
    partial_tp_enabled: bool = False
    tp1_price: float = 0.0     # First partial TP price
    tp2_price: float = 0.0     # Second partial TP price (= full TP)
    tp1_qty_pct: float = 0.40  # 40% of position at TP1
    tp2_qty_pct: float = 0.30  # 30% of position at TP2
    trail_qty_pct: float = 0.30  # 30% trails with BE stop


class RiskEngine:
    """
    V5.5 Dynamic Risk Management — ATR-adaptive, strategy-aware, balance-based.
    Position sizing PRESERVED from V2. TP/SL and leverage optimized for V5.5.
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

    # ─── V5.5 Leverage by Strategy + Confidence ────────────────────────

    @staticmethod
    def get_leverage(
        confidence: int, is_elite: bool = False,
        max_leverage: int = 8, strategy_type: str = "trend_pullback",
    ) -> int:
        """
        V5.5 Deterministic leverage — 10x REMOVED, max 8x elite scalp.
        Scalp: 4/5/6/8. Swing: 2/3/4/5. Sniper: 3/3/4/4.
        """
        if confidence < 70:
            return 0  # NO TRADE

        # Strategy-specific leverage tiers
        if strategy_type.startswith("swing"):
            if is_elite and confidence >= 95:
                return min(5, max_leverage)
            elif confidence >= 91:
                return min(4, max_leverage)
            elif confidence >= 81:
                return min(3, max_leverage)
            else:
                return min(2, max_leverage)

        elif strategy_type.startswith("sniper"):
            # Sniper: capped at 4x always
            if confidence >= 91:
                return min(4, max_leverage)
            elif confidence >= 81:
                return min(4, max_leverage)
            else:
                return min(3, max_leverage)

        else:
            # Scalp: 8x max for elite only, no 10x ever
            if is_elite and confidence >= 95:
                return min(8, max_leverage)
            elif confidence >= 91:
                return min(6, max_leverage)
            elif confidence >= 81:
                return min(5, max_leverage)
            else:
                return min(4, max_leverage)

    # ─── V5.5 TP/SL — Wider TP + ATR-Dynamic SL ──────────────────────

    @staticmethod
    def get_tp_sl_pct(
        confidence: int,
        atr_pct: float = 0.0,
        is_elite: bool = False,
        setup_grade: str = "C",
        strategy_type: str = "trend_pullback",
    ) -> tuple[float, float]:
        """
        V5.5: Wider scalp TP (2.5-6%), ATR-adaptive SL (min 1% floor).
        SL = max(base_sl, atr_pct × multiplier) — prevents wick stop-outs.
        """
        if strategy_type.startswith("swing"):
            # Swing: unchanged — wider targets for bigger moves
            if confidence >= 91:
                tp_pct = 0.12   # 12%
                sl_pct = 0.04   # 4%
            elif confidence >= 81:
                tp_pct = 0.08   # 8%
                sl_pct = 0.03   # 3%
            else:
                tp_pct = 0.05   # 5%
                sl_pct = 0.02   # 2%

        elif strategy_type.startswith("sniper"):
            # Sniper: unchanged — medium targets
            if confidence >= 91:
                tp_pct = 0.05   # 5%
                sl_pct = 0.02   # 2%
            elif confidence >= 81:
                tp_pct = 0.03   # 3%
                sl_pct = 0.015  # 1.5%
            else:
                tp_pct = 0.02   # 2%
                sl_pct = 0.01   # 1%

        else:
            # Scalp: V5.5 WIDER TP to capture real profit after fees
            if is_elite and confidence >= 95:
                tp_pct = 0.06   # 6% (was 2.5%)
                base_sl = 0.02  # 2% base
                atr_mult = 1.5
            elif confidence >= 91:
                tp_pct = 0.045  # 4.5% (was 2%)
                base_sl = 0.018 # 1.8% base
                atr_mult = 1.2
            elif confidence >= 81:
                tp_pct = 0.035  # 3.5% (was 1.5%)
                base_sl = 0.015 # 1.5% base
                atr_mult = 1.0
            else:
                tp_pct = 0.025  # 2.5% (was 1%)
                base_sl = 0.012 # 1.2% base
                atr_mult = 0.8

            # V5.5 ATR-dynamic SL: max(base, atr × multiplier, 1% floor)
            atr_sl = (atr_pct / 100.0) * atr_mult if atr_pct > 0 else 0.0
            sl_pct = max(base_sl, atr_sl, 0.01)  # 1% absolute floor
            # Cap SL so it never exceeds 50% of TP (maintain R:R ≥ 2)
            sl_pct = min(sl_pct, tp_pct * 0.5)
            return tp_pct, sl_pct

        # Swing/Sniper: standard ATR widening (unchanged)
        if atr_pct > 1.0:
            volatility_factor = 1 + (atr_pct * 0.1)
            sl_pct *= min(volatility_factor, 1.5)

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
        # V5 params
        strategy_type: str = "trend_pullback",
    ) -> TradeParameters:
        """
        Compute full trade parameters with V5 strategy-aware risk management.

        Flow:
        1. Check minimum confidence
        2. Get risk % from balance tier (PRESERVED)
        3. Calculate safe_margin = balance * risk_pct
        4. Apply margin cap (PRESERVED)
        5. Get leverage from strategy + confidence (V5 tiers)
        6. Calculate position_size = safe_margin * leverage
        7. Apply size_multiplier (daily guard / consecutive loss reduction)
        8. Validate symbol minimums
        9. Calculate TP/SL (V5 strategy-based)
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

        # ── Leverage (V5 strategy tiers) ──────────────────────────────
        leverage = self.get_leverage(confidence, is_elite, max_leverage_override, strategy_type)
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

        # ── V4: Apply minimum position floor ─────────────────────────
        effective_min_notional = max(min_notional, settings.MIN_POSITION_USDT)

        # ── Symbol minimum validation (IMPROVED V4) ──────────────────
        if position_size_usdt < effective_min_notional:
            # Try bumping to minimum
            required_margin = effective_min_notional / leverage
            if required_margin <= max_margin * 1.5:  # V4: Allow 50% over cap (was 20%)
                old_size = position_size_usdt
                position_size_usdt = effective_min_notional
                safe_margin = required_margin
                logger.info(
                    f"  V4 size bump: ${old_size:.2f} -> ${effective_min_notional:.2f} "
                    f"(min_notional=${min_notional}, floor=${settings.MIN_POSITION_USDT}) "
                    f"margin=${safe_margin:.2f}"
                )
            else:
                return TradeParameters(
                    symbol=symbol, side=side, leverage=leverage,
                    position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                    entry_price=entry_price, stop_loss=0, take_profit=0,
                    risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                    approved=False,
                    reject_reason=(
                        f"Position ${position_size_usdt:.2f} below min ${effective_min_notional}. "
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

        # V4: If step_size rounding dropped quantity to 0, bump to min_qty
        if quantity <= 0 and min_qty > 0:
            quantity = min_qty
            position_size_usdt = quantity * entry_price
            logger.info(
                f"  V4: Quantity was 0 after rounding, bumped to min_qty={min_qty} "
                f"(notional=${position_size_usdt:.2f})"
            )

        # Validate min qty
        if min_qty > 0 and quantity < min_qty:
            # V4: Try bumping to min_qty instead of rejecting
            bumped_notional = min_qty * entry_price
            bumped_margin = bumped_notional / leverage if leverage > 0 else bumped_notional
            if bumped_margin <= max_margin * 1.5:
                quantity = min_qty
                position_size_usdt = bumped_notional
                safe_margin = bumped_margin
                logger.info(
                    f"  V4: Bumped quantity to min_qty={min_qty} "
                    f"(notional=${position_size_usdt:.2f}, margin=${safe_margin:.2f})"
                )
            else:
                return TradeParameters(
                    symbol=symbol, side=side, leverage=leverage,
                    position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=quantity,
                    entry_price=entry_price, stop_loss=0, take_profit=0,
                    risk_reward=0, risk_pct=risk_pct, confidence=confidence,
                    approved=False,
                    reject_reason=f"Quantity {quantity} below symbol minimum {min_qty}, bumping exceeds margin cap",
                    setup_grade=setup_grade,
                )

        # ── V5 TP/SL (Strategy-Based) ─────────────────────────────────
        tp_pct, sl_pct = self.get_tp_sl_pct(confidence, atr_pct, is_elite, setup_grade, strategy_type)

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

        # ── V5.5 Partial TP Calculation ─────────────────────────────
        partial_tp_enabled, tp1_price, tp2_price = self.calculate_partial_tp(
            entry_price=entry_price,
            take_profit=take_profit,
            side=side,
            confidence=confidence,
            strategy_type=strategy_type,
            setup_grade=setup_grade,
            price_precision=price_precision,
        )

        logger.info(
            f"  Risk V5: bal=${account_balance:.2f} risk={risk_pct*100:.1f}% "
            f"margin=${safe_margin:.2f} lev={leverage}x pos=${position_size_usdt:.2f} "
            f"qty={quantity} TP={tp_pct*100:.1f}% SL={sl_pct*100:.1f}% RR={rr} "
            f"grade={setup_grade} strategy={strategy_type}"
            f"{' | PARTIAL_TP' if partial_tp_enabled else ''}"
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
            partial_tp_enabled=partial_tp_enabled,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp1_qty_pct=settings.PARTIAL_TP1_PCT,
            tp2_qty_pct=settings.PARTIAL_TP2_PCT,
            trail_qty_pct=settings.PARTIAL_TRAIL_PCT,
        )

    # ─── V5.5: Partial Take Profit Calculation ───────────────────────

    @staticmethod
    def calculate_partial_tp(
        entry_price: float,
        take_profit: float,
        side: str,
        confidence: int,
        strategy_type: str,
        setup_grade: str,
        price_precision: int = 4,
    ) -> tuple[bool, float, float]:
        """
        V5.5: Determine if partial TP should be used and calculate levels.

        Partial TP activates for:
          - Swing trades (any grade)
          - Breakout scalps with Grade A or B
          - Any strategy with confidence >= 85 and Grade A or B

        TP1 = 50% of the distance to full TP (close 40% of position)
        TP2 = full TP level (close 30% of position)
        Remaining 30% trails via break-even stop.

        Returns: (enabled, tp1_price, tp2_price)
        """
        if not settings.PARTIAL_TP_ENABLED:
            return False, 0.0, 0.0

        # Determine eligibility
        is_swing = strategy_type.startswith("swing")
        is_breakout = "breakout" in strategy_type
        is_strong_setup = setup_grade in ("A", "B") and confidence >= settings.PARTIAL_TP_MIN_CONFIDENCE

        if not (is_swing or is_breakout or is_strong_setup):
            return False, 0.0, 0.0

        # Calculate TP1 at halfway to full TP
        tp_distance = abs(take_profit - entry_price)
        tp1_distance = tp_distance * settings.PARTIAL_TP1_DISTANCE

        if side == "BUY":
            tp1_price = round(entry_price + tp1_distance, price_precision)
        else:
            tp1_price = round(entry_price - tp1_distance, price_precision)

        # TP2 = the full TP level
        tp2_price = take_profit

        logger.info(
            f"  📊 Partial TP: TP1=${tp1_price} (40% close) | "
            f"TP2=${tp2_price} (30% close) | Trail 30%"
        )

        return True, tp1_price, tp2_price
