"""
V13 Dynamic Risk Engine — Fixed TP/SL math + Safe Margin Tiers

TP/SL Formula (FIXED):
  price_move_pct = target_roi_pct / leverage / 100   (decimal, e.g. 0.018)
  LONG:  TP = entry * (1 + move)   SL = entry * (1 - sl_move)
  SHORT: TP = entry * (1 - move)   SL = entry * (1 + sl_move)

Margin Tiers (V13 Final):
  <$30:       Normal 5% / Strong 7% / Elite 10%  — hard cap $2.50 / $2.50 / $3.00
  $30-$100:   Normal 6% / Strong 8% / Elite 10%  — hard cap $10
  $100-$500:  Normal 5% / Strong 7% / Elite  9%  — hard cap $35
  $500+:      Normal 4% / Strong 6% / Elite  8%  — hard cap balance*pct

TP/SL ROI Targets:
  SCALP  Normal (72-79):  TP +12% ROI / SL -6%
  SCALP  Strong (80-89):  TP +18% ROI / SL -8%
  SCALP  Elite  (90+):    TP +25% ROI / SL -10%
  SWING  Normal (75-84):  TP +20% ROI / SL -8%
  SWING  Strong (85-89):  TP +35% ROI / SL -12%
  SWING  Elite  (90+):    TP +50% ROI / SL -15%
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
        Returns (margin_pct, max_margin_usdt) using strict per-tier rules.

        Grade mapping:
          Normal  = confidence 72-79  (no boost beyond base)
          Strong  = confidence 80-89  (+mid tier %)
          Elite   = confidence 90+    (+top tier %)

        Tiers:
          <$30:      Normal 5% / Strong 7% / Elite 10%  — cap $2.50 / $2.50 / $3.00
          $30-$100:  Normal 6% / Strong 8% / Elite 10%  — cap $10
          $100-$500: Normal 5% / Strong 7% / Elite  9%  — cap $35
          $500+:     Normal 4% / Strong 6% / Elite  8%  — uncapped (balance*pct)
        """
        if balance < settings.V13_MIN_TRADE_BALANCE:
            return 0.0, 0.0  # Hard skip handled upstream

        # Grade from confidence
        is_elite  = confidence >= 90
        is_strong = confidence >= 80 and not is_elite
        # Normal = everything below 80

        if balance < 30.0:
            if is_elite:
                pct, cap = 10.0, 3.00
            elif is_strong:
                pct, cap = 7.0,  2.50
            else:
                pct, cap = 5.0,  2.50

        elif balance < 100.0:
            if is_elite:
                pct, cap = 10.0, 10.0
            elif is_strong:
                pct, cap = 8.0,  10.0
            else:
                pct, cap = 6.0,  10.0

        elif balance < 500.0:
            if is_elite:
                pct, cap = 9.0,  35.0
            elif is_strong:
                pct, cap = 7.0,  35.0
            else:
                pct, cap = 5.0,  35.0

        else:  # $500+
            if is_elite:
                pct, cap = 8.0,  balance * 0.08
            elif is_strong:
                pct, cap = 6.0,  balance * 0.06
            else:
                pct, cap = 4.0,  balance * 0.04

        # Margin = min(balance * pct%, hard_dollar_cap)
        margin = min(balance * (pct / 100.0), cap)
        return pct, margin

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
        strategy_type: str = "scalp_trend_pullback",
    ) -> tuple[float, float]:
        """
        Returns (tp_roi_pct, sl_roi_pct).

        SCALP:
          Normal  (72-79): TP +12% / SL -6%
          Strong  (80-89): TP +18% / SL -8%
          Elite   (90+):   TP +25% / SL -10%

        SWING:
          Normal  (75-84): TP +20% / SL -8%
          Strong  (85-89): TP +35% / SL -12%
          Elite   (90+):   TP +50% / SL -15%
        """
        is_swing  = strategy_type.startswith("swing")
        is_sniper = strategy_type.startswith("sniper")

        if is_sniper:
            # Sniper always elite targets
            if confidence >= 90:
                return 50.0, 15.0
            return 35.0, 12.0

        if is_swing:
            if confidence >= 90:
                return 50.0, 15.0
            elif confidence >= 85:
                return 35.0, 12.0
            else:
                return 20.0, 8.0

        # Scalp (default)
        if confidence >= 90:
            return 25.0, 10.0
        elif confidence >= 80:
            return 18.0, 8.0
        else:
            return 12.0, 6.0

    @staticmethod
    def roi_to_price_pct(roi_pct: float, leverage: int) -> float:
        """
        Convert position ROI% to price movement as a DECIMAL FACTOR.

        Formula: price_factor = roi_pct / leverage / 100

        Example: 18% ROI at 10x leverage
          = 18 / 10 / 100 = 0.018  (1.8% coin price move)

        Usage:
          TP (LONG)  = entry * (1 + 0.018)   # +1.8% price
          SL (LONG)  = entry * (1 - 0.009)   # -0.9% price

        CRITICAL: The previous implementation returned roi_pct/leverage (= 1.8)
        which was used as entry*(1+1.8) = 280% move. That was the TP/SL bug.
        """
        if leverage <= 0:
            return roi_pct / 100.0
        return roi_pct / leverage / 100.0

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

    # ─── V16: Multi-TP Calculator ─────────────────────────────────────

    @staticmethod
    def calculate_multi_tp(
        entry_price: float,
        take_profit: float,
        stop_loss: float,
        side: str,
        price_precision: int = 6,
    ) -> dict:
        """
        V16: Generate TP1/TP2/TP3 price levels with ROI percentages.

        Returns dict with tp1/tp2/tp3 prices, ROI%, and close percentages.
        """
        if not settings.V16_MULTI_TP_ENABLED or entry_price <= 0 or take_profit <= 0:
            return {"enabled": False}

        tp_distance = abs(take_profit - entry_price)
        sl_distance = abs(entry_price - stop_loss) if stop_loss > 0 else tp_distance * 0.5

        if side == "BUY":
            tp1 = round(entry_price + tp_distance * settings.V16_TP1_DISTANCE_PCT, price_precision)
            tp2 = round(entry_price + tp_distance * settings.V16_TP2_DISTANCE_PCT, price_precision)
            tp3 = round(entry_price + tp_distance * settings.V16_TP3_DISTANCE_PCT, price_precision)
            sl_roi = round(-sl_distance / entry_price * 100, 2) if entry_price > 0 else 0
            tp1_roi = round((tp1 - entry_price) / entry_price * 100, 2)
            tp2_roi = round((tp2 - entry_price) / entry_price * 100, 2)
            tp3_roi = round((tp3 - entry_price) / entry_price * 100, 2)
        else:
            tp1 = round(entry_price - tp_distance * settings.V16_TP1_DISTANCE_PCT, price_precision)
            tp2 = round(entry_price - tp_distance * settings.V16_TP2_DISTANCE_PCT, price_precision)
            tp3 = round(entry_price - tp_distance * settings.V16_TP3_DISTANCE_PCT, price_precision)
            sl_roi = round(-sl_distance / entry_price * 100, 2) if entry_price > 0 else 0
            tp1_roi = round((entry_price - tp1) / entry_price * 100, 2)
            tp2_roi = round((entry_price - tp2) / entry_price * 100, 2)
            tp3_roi = round((entry_price - tp3) / entry_price * 100, 2)

        return {
            "enabled": True,
            "tp1_price": tp1,
            "tp2_price": tp2,
            "tp3_price": tp3,
            "tp1_roi_pct": tp1_roi,
            "tp2_roi_pct": tp2_roi,
            "tp3_roi_pct": tp3_roi,
            "sl_roi_pct": sl_roi,
            "tp1_close_pct": settings.V16_TP1_CLOSE_PCT,
            "tp2_close_pct": settings.V16_TP2_CLOSE_PCT,
            "tp3_close_pct": settings.V16_TP3_CLOSE_PCT,
            "stop_loss": stop_loss,
        }

    @staticmethod
    def compute_tp_sl_from_ideal_entry(
        ideal_entry: float,
        side: str,
        tp_roi_pct: float,
        sl_roi_pct: float,
        leverage: int,
    ) -> tuple:
        """
        V16: Compute TP/SL from ideal entry price instead of market price.
        Returns (take_profit, stop_loss) price levels.
        """
        if ideal_entry <= 0:
            return 0.0, 0.0

        tp_price_pct = tp_roi_pct / leverage / 100.0 if leverage > 0 else tp_roi_pct / 100.0
        sl_price_pct = sl_roi_pct / leverage / 100.0 if leverage > 0 else sl_roi_pct / 100.0

        if side == "BUY":
            tp = round(ideal_entry * (1 + tp_price_pct), 6)
            sl = round(ideal_entry * (1 - sl_price_pct), 6)
        else:
            tp = round(ideal_entry * (1 - tp_price_pct), 6)
            sl = round(ideal_entry * (1 + sl_price_pct), 6)

        return tp, sl
