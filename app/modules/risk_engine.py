"""
V13 Dynamic Risk Engine — Smarter Leverage + Margin + ROI-Based TP/SL

Changes from V5.5:
  - Confidence-tiered leverage per mode (7/10/12/15x)
  - Balance-tier margin with confidence boosts (Strong+2%, Elite+3%)
  - Dynamic scalp TP by confidence (15/18/20/22% ROI)
  - ROI% converted to price% via leverage (makes TP meaningful at any leverage)
  - ATR volatility dampener: cap leverage at 10x when ATR%>3%
  - Fee/slippage profitability filter
  - Better <$30 account sizing (15% margin, max $4)

Margin Tiers:
  <$10:       hard skip (MIN_TRADE_BALANCE)
  $10-$30:    15% margin, max $4
  $30-$50:    12% margin, max $6
  $50-$200:    8% margin
  $200-$1000:  5% margin
  $1000+:      5% margin

Confidence Boost (applied AFTER tier calc, Patch 5 — explicit caps):
  Grade C (72-84):  no boost — base only
  Grade B (85-88):  +2% margin  (hard cap: tier_base_pct + 2%, never >15% abs)
  Grade A (89+):    +3% margin  (hard cap: tier_base_pct + 3%, never >15% abs)

Leverage (V13 tiers):
  SCALP:  72-76=7x | 77-82=10x | 83-88=12x | 89+=15x
  SWING:  75-80=7x | 81-86=10x | 87+=12x
  SNIPER: 90+=15x max
  ATR%>3% → cap at 10x

TP/SL (ROI %  → converted to price % by dividing by leverage):
  SCALP TP ROI: 72-76=15% | 77-82=18% | 83-88=20% | 89+=22%
  SCALP SL ROI: 9% (fixed)
  SWING TP ROI: 35% | SL ROI: 12%
  SNIPER TP ROI: 50% | SL ROI: 15%
"""

import logging
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ── Fee/slippage constants (round-trip) ──────────────────────────────
ROUND_TRIP_COST_PCT = (settings.V13_TAKER_FEE_PCT * 2) + settings.V13_SLIPPAGE_EST_PCT


@dataclass
class TradeParameters:
    symbol: str
    side: str                 # BUY | SELL
    leverage: int
    position_size_usdt: float
    safe_margin: float
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    risk_pct: float
    confidence: int
    approved: bool = True
    reject_reason: str = ""
    setup_grade: str = "C"
    tp_pct: float = 0.0       # price TP% for display
    sl_pct: float = 0.0       # price SL% for display
    tp_roi_pct: float = 0.0   # ROI TP% for display (V13)
    sl_roi_pct: float = 0.0   # ROI SL% for display (V13)
    margin_pct: float = 0.0   # margin as % of balance (V13)
    is_elite: bool = False
    # Partial TP (preserved)
    partial_tp_enabled: bool = False
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_qty_pct: float = 0.40
    tp2_qty_pct: float = 0.30
    trail_qty_pct: float = 0.30
    # V13 fee filter
    net_roi_after_fees: float = 0.0
    fee_filtered: bool = False


class RiskEngine:
    """
    V13 Dynamic Risk Management — ROI-based TP/SL, confidence-tiered leverage,
    balance-tier margin with explicit confidence boosts.
    """

    # ─── V13 Margin Tier ─────────────────────────────────────────────

    @staticmethod
    def get_margin_pct(balance: float, confidence: int) -> tuple[float, float]:
        """
        Returns (margin_pct, max_margin_usdt).
        Patch 1: better <$30 sizing.
        Patch 5: explicit confidence boost caps.

        Grade C (72-84): base only
        Grade B (85-88): +V13_BOOST_STRONG_ADD_PCT (capped at abs 15%)
        Grade A (89+):   +V13_BOOST_ELITE_ADD_PCT  (capped at abs 15%)
        """
        # Determine base tier %
        if balance < settings.V13_MIN_TRADE_BALANCE:
            return 0.0, 0.0   # Will be caught by hard skip

        if balance < 30.0:
            base_pct = settings.V13_MARGIN_UNDER30_PCT   # 15%
            abs_cap  = settings.V13_MARGIN_UNDER30_MAX   # $4
        elif balance < 50.0:
            base_pct = settings.V13_MARGIN_30_50_PCT     # 12%
            abs_cap  = settings.V13_MARGIN_30_50_MAX     # $6
        elif balance < 200.0:
            base_pct = settings.V13_MARGIN_50_200_PCT    # 8%
            abs_cap  = balance * (base_pct / 100.0)
        elif balance < 1000.0:
            base_pct = settings.V13_MARGIN_200_1000_PCT  # 5%
            abs_cap  = balance * (base_pct / 100.0)
        else:
            base_pct = settings.V13_MARGIN_OVER1000_PCT  # 5%
            abs_cap  = balance * (base_pct / 100.0)

        # Confidence boost (Patch 5 — explicit, capped)
        if confidence >= 89:
            boost = settings.V13_BOOST_ELITE_ADD_PCT   # +3%
        elif confidence >= 85:
            boost = settings.V13_BOOST_STRONG_ADD_PCT  # +2%
        else:
            boost = 0.0

        final_pct = min(base_pct + boost, settings.V13_MARGIN_ABSOLUTE_CAP_PCT)
        max_margin = min(balance * (final_pct / 100.0), abs_cap + balance * (boost / 100.0))
        # Absolute hard cap: never more than 15% of balance
        max_margin = min(max_margin, balance * (settings.V13_MARGIN_ABSOLUTE_CAP_PCT / 100.0))

        return final_pct, max_margin

    # ─── V13 Leverage ─────────────────────────────────────────────────

    @staticmethod
    def get_leverage(
        confidence: int,
        strategy_type: str = "trend_pullback",
        atr_pct: float = 0.0,
    ) -> int:
        """
        V13 confidence-tiered leverage per mode.
        ATR volatility dampener: if ATR%>V13_VOLATILE_ATR_THRESHOLD → cap at V13_VOLATILE_LEVERAGE_CAP.
        """
        is_swing   = strategy_type.startswith("swing")
        is_sniper  = strategy_type.startswith("sniper")

        if is_swing:
            if confidence >= 87:
                lev = 12
            elif confidence >= 81:
                lev = 10
            else:
                lev = 7
            cap = settings.V13_SWING_LEVERAGE_MAX

        elif is_sniper:
            if confidence >= 90:
                lev = 15
            elif confidence >= 85:
                lev = 12
            else:
                lev = 10
            cap = settings.V13_SNIPER_LEVERAGE_MAX

        else:  # scalp (default)
            if confidence >= 89:
                lev = 15
            elif confidence >= 83:
                lev = 12
            elif confidence >= 77:
                lev = 10
            else:
                lev = 7
            cap = settings.V13_SCALP_LEVERAGE_MAX

        lev = min(lev, cap)

        # ATR volatility dampener
        if atr_pct > settings.V13_VOLATILE_ATR_THRESHOLD:
            lev = min(lev, settings.V13_VOLATILE_LEVERAGE_CAP)
            logger.info(
                f"  [V13] Volatility dampener: ATR%={atr_pct:.2f}% > {settings.V13_VOLATILE_ATR_THRESHOLD}% "
                f"→ leverage capped at {lev}x"
            )

        return lev

    # ─── V13 TP/SL (ROI% → price%) ────────────────────────────────────

    @staticmethod
    def get_tp_sl_roi(
        confidence: int,
        strategy_type: str = "trend_pullback",
    ) -> tuple[float, float]:
        """
        Returns (tp_roi_pct, sl_roi_pct) — position ROI percentages.
        Patch 2: dynamic scalp TP by confidence.
        """
        is_swing  = strategy_type.startswith("swing")
        is_sniper = strategy_type.startswith("sniper")

        if is_swing:
            return settings.V13_SWING_TP_ROI, settings.V13_SWING_SL_ROI

        if is_sniper:
            return settings.V13_SNIPER_TP_ROI, settings.V13_SNIPER_SL_ROI

        # Scalp — dynamic TP by confidence
        if confidence >= 89:
            tp_roi = settings.V13_SCALP_TP_ROI_89   # 22%
        elif confidence >= 83:
            tp_roi = settings.V13_SCALP_TP_ROI_83   # 20%
        elif confidence >= 77:
            tp_roi = settings.V13_SCALP_TP_ROI_77   # 18%
        else:
            tp_roi = settings.V13_SCALP_TP_ROI_72   # 15%

        return tp_roi, settings.V13_SCALP_SL_ROI

    @staticmethod
    def roi_to_price_pct(roi_pct: float, leverage: int) -> float:
        """Convert position ROI% to price movement %.  price_pct = roi_pct / leverage."""
        if leverage <= 0:
            return roi_pct
        return roi_pct / leverage

    # ─── V13 Fee Filter (Patch 4) ─────────────────────────────────────

    @staticmethod
    def check_fee_filter(tp_roi_pct: float, leverage: int) -> tuple[bool, float, str]:
        """
        Patch 4: Reject trade if net ROI after round-trip fees < V13_MIN_NET_ROI_AFTER_FEES.

        Round-trip cost as ROI% = (taker_fee*2 + slippage) * leverage
        Because fees are on notional, at 10x leverage a 0.13% price move = 1.3% ROI hit.

        Returns: (passed, net_roi_pct, reason)
        """
        if not settings.V13_FEE_FILTER_ENABLED:
            return True, tp_roi_pct, ""

        fee_roi_cost = ROUND_TRIP_COST_PCT * leverage   # fee impact in ROI terms
        net_roi = tp_roi_pct - fee_roi_cost

        if net_roi < settings.V13_MIN_NET_ROI_AFTER_FEES:
            reason = (
                f"Fee filter: TP ROI={tp_roi_pct:.1f}% "
                f"- fees={fee_roi_cost:.1f}% (at {leverage}x) "
                f"= net {net_roi:.1f}% < min {settings.V13_MIN_NET_ROI_AFTER_FEES}%"
            )
            return False, net_roi, reason

        return True, net_roi, ""

    # ─── Setup Grade (preserved from V5.5) ────────────────────────────

    @staticmethod
    def determine_setup_grade(confidence: int, volume_spike: bool = False) -> str:
        if confidence >= 89:
            return "A"
        elif confidence >= 85:
            return "B"
        else:
            return "C"

    # ─── Main Calculate ────────────────────────────────────────────────

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
        max_leverage_override: int = 0,   # 0 = use V13 auto
        risk_pct_override: float = None,  # kept for compat, ignored in V13
        volume_spike: bool = False,
        size_multiplier: float = 1.0,
        strategy_type: str = "trend_pullback",
    ) -> TradeParameters:
        """
        V13 full trade parameter calculation.
        """
        setup_grade = self.determine_setup_grade(confidence, volume_spike)
        is_elite    = setup_grade == "A"

        # ── 1. Hard balance floor ──────────────────────────────────────
        if account_balance < settings.V13_MIN_TRADE_BALANCE:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=0, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=0, confidence=confidence,
                approved=False,
                reject_reason=f"Balance ${account_balance:.2f} below V13 minimum ${settings.V13_MIN_TRADE_BALANCE}",
                setup_grade=setup_grade,
            )

        # ── 2. Leverage ────────────────────────────────────────────────
        leverage = self.get_leverage(confidence, strategy_type, atr_pct)
        if max_leverage_override > 0:
            leverage = min(leverage, max_leverage_override)
        if leverage <= 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=0,
                position_size_usdt=0, safe_margin=0, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=0, confidence=confidence,
                approved=False, reject_reason="Confidence too low for leverage",
                setup_grade=setup_grade,
            )

        # ── 3. Margin ─────────────────────────────────────────────────
        margin_pct, max_margin = self.get_margin_pct(account_balance, confidence)
        safe_margin = min(account_balance * (margin_pct / 100.0), max_margin)

        # Apply daily-guard / consecutive-loss multiplier
        if size_multiplier < 1.0:
            safe_margin *= size_multiplier
            logger.info(f"  [V13] Size multiplier={size_multiplier:.2f} → margin=${safe_margin:.2f}")

        # ── 4. TP/SL as ROI% → price% ─────────────────────────────────
        tp_roi_pct, sl_roi_pct = self.get_tp_sl_roi(confidence, strategy_type)
        tp_price_pct = self.roi_to_price_pct(tp_roi_pct, leverage)
        sl_price_pct = self.roi_to_price_pct(sl_roi_pct, leverage)

        # ── 5. Fee/slippage filter (Patch 4) ──────────────────────────
        fee_ok, net_roi, fee_reason = self.check_fee_filter(tp_roi_pct, leverage)
        if not fee_ok:
            logger.info(f"  [V13 FEE FILTER] {symbol}: {fee_reason}")
            return TradeParameters(
                symbol=symbol, side=side, leverage=leverage,
                position_size_usdt=0, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=margin_pct / 100.0, confidence=confidence,
                approved=False, reject_reason=f"Fee filter: {fee_reason}",
                setup_grade=setup_grade, fee_filtered=True,
            )

        # ── 6. Position size ──────────────────────────────────────────
        position_size_usdt = safe_margin * leverage
        effective_min_notional = max(min_notional, settings.MIN_POSITION_USDT)

        if position_size_usdt < effective_min_notional:
            required_margin = effective_min_notional / leverage
            if required_margin <= max_margin * 1.5:
                position_size_usdt = effective_min_notional
                safe_margin = required_margin
            else:
                return TradeParameters(
                    symbol=symbol, side=side, leverage=leverage,
                    position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                    entry_price=entry_price, stop_loss=0, take_profit=0,
                    risk_reward=0, risk_pct=margin_pct / 100.0, confidence=confidence,
                    approved=False,
                    reject_reason=(
                        f"Position ${position_size_usdt:.2f} below min ${effective_min_notional}. "
                        f"Bumping would exceed safe margin."
                    ),
                    setup_grade=setup_grade,
                )

        # ── 7. Quantity ────────────────────────────────────────────────
        if entry_price <= 0:
            return TradeParameters(
                symbol=symbol, side=side, leverage=leverage,
                position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=0,
                entry_price=entry_price, stop_loss=0, take_profit=0,
                risk_reward=0, risk_pct=margin_pct / 100.0, confidence=confidence,
                approved=False, reject_reason="Invalid entry price",
                setup_grade=setup_grade,
            )

        import math
        raw_qty = position_size_usdt / entry_price
        if step_size > 0:
            raw_qty = math.floor(raw_qty / step_size) * step_size
        quantity = round(raw_qty, quantity_precision)

        if quantity <= 0 and min_qty > 0:
            quantity = min_qty
            position_size_usdt = quantity * entry_price

        if min_qty > 0 and quantity < min_qty:
            bumped_notional = min_qty * entry_price
            bumped_margin   = bumped_notional / leverage if leverage > 0 else bumped_notional
            if bumped_margin <= max_margin * 1.5:
                quantity = min_qty
                position_size_usdt = bumped_notional
                safe_margin = bumped_margin
            else:
                return TradeParameters(
                    symbol=symbol, side=side, leverage=leverage,
                    position_size_usdt=position_size_usdt, safe_margin=safe_margin, quantity=quantity,
                    entry_price=entry_price, stop_loss=0, take_profit=0,
                    risk_reward=0, risk_pct=margin_pct / 100.0, confidence=confidence,
                    approved=False,
                    reject_reason=f"Qty {quantity} below min {min_qty}, bumping exceeds margin cap",
                    setup_grade=setup_grade,
                )

        # ── 8. Price levels ────────────────────────────────────────────
        if side == "BUY":
            take_profit = entry_price * (1 + tp_price_pct)
            stop_loss   = entry_price * (1 - sl_price_pct)
        else:
            take_profit = entry_price * (1 - tp_price_pct)
            stop_loss   = entry_price * (1 + sl_price_pct)

        sl_distance = abs(entry_price - stop_loss)
        tp_distance = abs(take_profit - entry_price)
        rr = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0

        take_profit = round(take_profit, price_precision)
        stop_loss   = round(stop_loss,   price_precision)

        # ── 9. Partial TP (preserved from V5.5) ───────────────────────
        partial_tp_enabled, tp1_price, tp2_price = self.calculate_partial_tp(
            entry_price=entry_price,
            take_profit=take_profit,
            side=side,
            confidence=confidence,
            strategy_type=strategy_type,
            setup_grade=setup_grade,
            price_precision=price_precision,
        )

        actual_margin_pct = (safe_margin / account_balance * 100) if account_balance > 0 else 0

        logger.info(
            f"  [V13 Risk] {symbol} {side} | bal=${account_balance:.2f} "
            f"margin={actual_margin_pct:.1f}%/${safe_margin:.2f} lev={leverage}x "
            f"pos=${position_size_usdt:.2f} | "
            f"TP_ROI={tp_roi_pct:.1f}% SL_ROI={sl_roi_pct:.1f}% "
            f"TP_price={tp_price_pct*100:.2f}% SL_price={sl_price_pct*100:.2f}% "
            f"RR={rr} net_ROI={net_roi:.1f}% grade={setup_grade}"
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
            risk_pct=round(margin_pct / 100.0, 4),
            confidence=confidence,
            approved=True,
            setup_grade=setup_grade,
            tp_pct=round(tp_price_pct * 100, 2),
            sl_pct=round(sl_price_pct * 100, 2),
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            margin_pct=round(actual_margin_pct, 2),
            is_elite=is_elite,
            partial_tp_enabled=partial_tp_enabled,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp1_qty_pct=settings.PARTIAL_TP1_PCT,
            tp2_qty_pct=settings.PARTIAL_TP2_PCT,
            trail_qty_pct=settings.PARTIAL_TRAIL_PCT,
            net_roi_after_fees=round(net_roi, 2),
        )

    # ─── Partial TP (preserved from V5.5) ─────────────────────────────

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
        if not settings.PARTIAL_TP_ENABLED:
            return False, 0.0, 0.0

        is_swing      = strategy_type.startswith("swing")
        is_breakout   = "breakout" in strategy_type
        is_strong     = setup_grade in ("A", "B") and confidence >= settings.PARTIAL_TP_MIN_CONFIDENCE

        if not (is_swing or is_breakout or is_strong):
            return False, 0.0, 0.0

        tp_distance  = abs(take_profit - entry_price)
        tp1_distance = tp_distance * settings.PARTIAL_TP1_DISTANCE

        if side == "BUY":
            tp1_price = round(entry_price + tp1_distance, price_precision)
        else:
            tp1_price = round(entry_price - tp1_distance, price_precision)

        return True, tp1_price, take_profit
