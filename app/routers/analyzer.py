"""
V5.5 Analyzer API — Multi-Strategy Orchestrator

Endpoints:
  POST /analyze       — Single coin analysis
  POST /analyze-batch — Batch analysis with ALL 4 engines

V5.5 Flow:
  1. Detect market regime (ENGINE D)
  2. Apply session multiplier
  3. Scalp analysis with 3 sub-strategies (ENGINE A)
  4. Swing watchlist update + new scan (ENGINE B)
  5. Sniper/news scan with quality filter (ENGINE C)
  6. Run engine conflict resolver
  7. Apply bad symbol cooldowns
  8. Merge and rank all signals with strategy weight adjustments
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from app.modules.ai_engine import ScalpingEngine
from app.modules.orderbook import OrderBookAnalyzer
from app.modules.market_regime import MarketRegimeRouter
from app.modules.swing_engine import SwingEngine
from app.modules.sniper_engine import SniperEngine
from app.modules.strategy_tracker import strategy_tracker
from app.modules.telegram import TelegramNotifier
from app.config import settings
from app.utils.serialization import clean_json_types

router = APIRouter()
logger = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    symbol: str
    price_change_pct: float = 0.0
    volume_24h: float = 0.0
    score: float = 0.0
    spread_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0


class CoinInput(BaseModel):
    symbol: str
    price: float = 0.0
    volume_24h: float = 0.0
    price_change_pct: float = 0.0
    spread_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    score: float = 0.0


class BatchAnalyzeRequest(BaseModel):
    coins: List[CoinInput]
    top_n: int = 10


@router.post("/analyze")
async def analyze_coin(req: AnalyzeRequest):
    """Single coin analysis with V5 multi-strategy engine."""
    try:
        engine = ScalpingEngine()

        # Optional: orderbook analysis
        ob_data = None
        try:
            ob_analyzer = OrderBookAnalyzer()
            price = req.bid if req.bid > 0 else 0
            if price > 0:
                ob_result = await ob_analyzer.analyze(req.symbol, price)
                ob_data = ob_analyzer.to_dict(ob_result)
        except Exception as e:
            logger.warning(f"Orderbook analysis skipped for {req.symbol}: {e}")

        decision = await engine.analyze(
            req.symbol,
            spread_pct=req.spread_pct,
            orderbook_data=ob_data,
        )
        result = engine.to_dict(decision)

        return clean_json_types({
            "status": "ok",
            "symbol": req.symbol,
            "ai_decision": result,
        })

    except Exception as e:
        logger.error(f"Analysis failed for {req.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze-batch")
async def analyze_batch(req: BatchAnalyzeRequest):
    """
    V5 Multi-strategy batch analysis:
    1. Detect market regime
    2. Run scalp analysis on all coins (3 sub-strategies)
    3. Run swing watchlist update + scan top coins for new setups
    4. Run sniper scan (news + funding + volume)
    5. Merge all signals, return ranked results
    """
    if not req.coins:
        return {"status": "ok", "count": 0, "analyzed": 0, "has_signals": False, "coins": []}

    logger.info(f"📊 V5.5 batch analyzing {len(req.coins)} coins (top {req.top_n})...")

    # ═══════════════════════════════════════════════════════════════════
    # STEP 1: Detect market regime + session multiplier
    # ═══════════════════════════════════════════════════════════════════
    regime_router = MarketRegimeRouter()
    session_mult = regime_router.get_session_multiplier()
    logger.info(f"  ⏰ Session multiplier: {session_mult}")
    try:
        regime_data = await regime_router.detect_regime()
        regime = regime_data.regime
        regime_weights = regime_data.strategy_weights
        regime_desc = regime_data.description
        logger.info(f"  🌍 Regime: {regime} — {regime_desc}")
    except Exception as e:
        logger.warning(f"Regime detection failed: {e}")
        regime = "SIDEWAYS_RANGE"
        regime_weights = regime_router._get_strategy_weights("SIDEWAYS_RANGE")
        regime_desc = "Regime detection failed — using default"

    # ═══════════════════════════════════════════════════════════════════
    # STEP 2: Scalp analysis with regime-aware strategy selection
    # ═══════════════════════════════════════════════════════════════════
    engine = ScalpingEngine()
    scanner_data = {c.symbol: c for c in req.coins}
    semaphore = asyncio.Semaphore(10)

    async def analyze_with_limit(symbol: str, spread: float):
        async with semaphore:
            try:
                decision = await engine.analyze(
                    symbol, spread_pct=spread,
                    regime=regime, regime_weights=regime_weights,
                )
                d = engine.to_dict(decision)
                # V12: Log exact rejection reason for HOLD signals — scalp transparency
                if d.get("action") == "HOLD":
                    logger.info(
                        f"  [SCALP SKIP] {symbol}: HOLD | conf={d.get('confidence',0)} | "
                        f"reason={d.get('reason','?')[:120]}"
                    )
                return symbol, d
            except Exception as e:
                logger.warning(f"Analysis failed for {symbol}: {e}")
                return symbol, None

    tasks = [
        analyze_with_limit(coin.symbol, coin.spread_pct)
        for coin in req.coins
    ]
    results = await asyncio.gather(*tasks)

    analyzed = []
    for symbol, ai_result in results:
        if ai_result is None:
            continue

        coin_info = scanner_data.get(symbol)
        analyzed.append({
            "symbol": symbol,
            "action": ai_result.get("action", "HOLD"),
            "confidence": ai_result.get("confidence", 0),
            "reason": ai_result.get("reason", ""),
            "current_price": ai_result.get("current_price", 0.0),
            "rsi": ai_result.get("rsi", 50.0),
            "trend": ai_result.get("trend", "NEUTRAL"),
            "htf_trend": ai_result.get("htf_trend", "NEUTRAL"),
            "atr": ai_result.get("atr", 0.0),
            "atr_pct": ai_result.get("atr_pct", 0.0),
            "vwap": ai_result.get("vwap", 0.0),
            "volume_spike": ai_result.get("volume_spike", False),
            "candle_type": ai_result.get("candle_type", "DOJI"),
            "is_choppy": ai_result.get("is_choppy", False),
            "ai_called": ai_result.get("ai_called", False),
            "ai_fallback": ai_result.get("ai_fallback", False),
            "spread_pct": coin_info.spread_pct if coin_info else 0.0,
            "volume_24h": coin_info.volume_24h if coin_info else 0.0,
            # V3 fields
            "setup_grade": ai_result.get("setup_grade", "C"),
            "macd_crossover": ai_result.get("macd_crossover", "NONE"),
            "bb_position": ai_result.get("bb_position", "MID"),
            "is_pullback": ai_result.get("is_pullback", False),
            "is_chase": ai_result.get("is_chase", False),
            "conditions_passed": ai_result.get("conditions_passed", 0),
            "conditions_total": ai_result.get("conditions_total", 10),
            # V5 fields
            "strategy_type": ai_result.get("strategy_type", "trend_pullback"),
            "regime": regime,
        })

    # ═══════════════════════════════════════════════════════════════════
    # STEP 3: Swing watchlist update + new scan
    # ═══════════════════════════════════════════════════════════════════
    swing_signals = []
    try:
        swing_engine = SwingEngine()

        # Update existing watchlist entries
        triggered = await swing_engine.update_watchlist()
        for setup in triggered:
            swing_signals.append({
                "symbol": setup["symbol"],
                "action": setup["side"],
                "confidence": setup["confidence"],
                "reason": setup.get("reason", "Swing setup triggered"),
                "current_price": setup.get("current_price", 0.0),
                "strategy_type": setup.get("strategy_type", "swing_triggered"),
                "regime": regime,
                "setup_grade": "B",
                "atr_pct": 0.0,
                "spread_pct": 0.0,
            })

        # Scan top coins for new swing setups
        top_symbols = [c.symbol for c in req.coins[:20]]
        new_setups = await swing_engine.scan_multiple(top_symbols, regime)
        if new_setups:
            await swing_engine.save_to_watchlist(new_setups)
            logger.info(f"  🔭 {len(new_setups)} new swing setups saved to watchlist")

            # V7: Telegram alerts for new watchlist entries (best 3 only)
            try:
                telegram = TelegramNotifier()
                for setup in new_setups[:3]:
                    await telegram.send_watchlisted(
                        symbol=setup.symbol,
                        side=setup.side,
                        setup_type=setup.setup_type,
                        confidence=setup.confidence,
                        trigger_price=setup.trigger_price,
                        current_price=setup.current_price,
                        reason=setup.reason,
                    )
            except Exception as tw_err:
                logger.debug(f"  Watchlist telegram failed (non-critical): {tw_err}")

        # Cleanup old entries
        await swing_engine.cleanup_old()

    except Exception as e:
        logger.warning(f"Swing engine error: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # STEP 4: Sniper scan (news + funding + volume)
    # ═══════════════════════════════════════════════════════════════════
    sniper_signals = []
    try:
        sniper = SniperEngine()
        all_symbols = [c.symbol for c in req.coins[:30]]
        sniper_setups = await sniper.full_scan(all_symbols)

        for setup in sniper_setups:
            if setup.confidence >= settings.MIN_CONFIDENCE:
                sniper_signals.append({
                    "symbol": setup.symbol,
                    "action": setup.side,
                    "confidence": setup.confidence,
                    "reason": setup.reason,
                    "current_price": setup.current_price,
                    "strategy_type": setup.strategy_type or f"sniper_{setup.setup_type}",
                    "regime": regime,
                    "setup_grade": "B",
                    "atr_pct": 0.0,
                    "spread_pct": 0.0,
                })

    except Exception as e:
        logger.warning(f"Sniper engine error: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # STEP 5: V5.5 Conflict resolution + filtering
    # ═══════════════════════════════════════════════════════════════════

    # Run engine conflict resolver
    resolved = regime_router.resolve_conflicts(
        regime=regime,
        scalp_signals=analyzed,
        swing_signals=swing_signals,
        sniper_signals=sniper_signals,
    )

    # Apply session multiplier to confidence
    for sig in resolved:
        raw_conf = sig.get("confidence", 0)
        sig["confidence"] = int(raw_conf * session_mult)

    # Apply strategy weight adjustments
    try:
        weight_adjustments = await strategy_tracker.get_strategy_weight_adjustments(days=14)
        for sig in resolved:
            st = sig.get("strategy_type", "")
            if st in weight_adjustments:
                adj = weight_adjustments[st]
                sig["confidence"] = int(sig.get("confidence", 0) * adj)
    except Exception as e:
        logger.warning(f"Strategy weight adjustment failed: {e}")

    # Bad symbol cooldown check
    filtered = []
    for sig in resolved:
        sym = sig.get("symbol", "")
        action = sig.get("action", "HOLD")
        if action in ("BUY", "SELL"):
            cooled, reason = await strategy_tracker.check_symbol_cooldown(sym)
            if cooled:
                logger.info(f"  🧳 Skipping {sym}: {reason}")
                continue
        filtered.append(sig)

    # Sort by confidence descending
    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    top_coins = filtered[:req.top_n]

    tradeable = [
        c for c in top_coins
        if c.get("action") not in ("HOLD", None) and c.get("confidence", 0) >= settings.MIN_CONFIDENCE
    ]

    # Limit to MAX_TRADES_PER_CYCLE
    tradeable = tradeable[:settings.MAX_TRADES_PER_CYCLE]

    logger.info(
        f"  V5.5 Result: {len(analyzed)} scalp | {len(swing_signals)} swing | "
        f"{len(sniper_signals)} sniper | Resolved: {len(resolved)} | "
        f"After cooldown: {len(filtered)} | Tradeable: {len(tradeable)} | "
        f"Regime: {regime} | Session: {session_mult}"
    )

    has_signals = len(tradeable) > 0

    summary = "No signals met trade criteria" if not tradeable else (
        f"{len(tradeable)} tradeable: " +
        ", ".join(f"{c['symbol']}({c.get('strategy_type','?')}/{c['confidence']})" for c in tradeable[:5])
    )

    return clean_json_types({
        "status": "ok",
        "analyzed": len(analyzed),
        "count": len(top_coins),
        "tradeable_count": len(tradeable),
        "has_signals": has_signals,
        "summary": summary,
        "regime": regime,
        "regime_description": regime_desc,
        "session_multiplier": session_mult,
        "swing_watchlist_triggered": len(swing_signals),
        "sniper_setups": len(sniper_signals),
        "conflicts_blocked": len(resolved) < (len(analyzed) + len(swing_signals) + len(sniper_signals)),
        "coins": top_coins,
    })


# ═══════════════════════════════════════════════════════════════════
# V11: /analyze-scalp — Fast scalp-only endpoint (no swing engine)
# ═══════════════════════════════════════════════════════════════════

class ScalpBatchRequest(BaseModel):
    coins: List[CoinInput]
    top_n: int = 15


@router.post("/analyze-scalp")
async def analyze_scalp_batch(req: ScalpBatchRequest):
    """
    V11 Scalp-only batch analysis endpoint.
    Used exclusively by scalp_engine_workflow (10-min trigger).

    Flow:
      1. Detect regime (for strategy weight adjustments)
      2. Run ScalpingEngine on all coins (1m/3m/5m/15m aware)
      3. Run SniperEngine (fast momentum plays)
      4. No SwingEngine — no 4H fetches, no watchlist DB writes
      5. Return ranked scalp + sniper signals with grouped watchlist
    """
    if not req.coins:
        return {"status": "ok", "count": 0, "analyzed": 0, "has_signals": False, "coins": [], "scalp_watchlist": []}

    logger.info(f"⚡ V11 Scalp batch: {len(req.coins)} coins...")

    # Step 1: Regime (lightweight — just weights, no full analysis)
    regime_router = MarketRegimeRouter()
    session_mult = regime_router.get_session_multiplier()
    try:
        regime_data = await regime_router.detect_regime()
        regime = regime_data.regime
        regime_weights = regime_data.strategy_weights
    except Exception as e:
        logger.warning(f"Regime detection failed: {e}")
        regime = "SIDEWAYS_RANGE"
        regime_weights = regime_router._get_strategy_weights("SIDEWAYS_RANGE")

    # Step 2: Scalp engine
    engine = ScalpingEngine()
    scanner_data = {c.symbol: c for c in req.coins}
    semaphore = asyncio.Semaphore(12)  # Scalp = faster, higher concurrency

    async def analyze_scalp_coin(symbol: str, spread: float):
        async with semaphore:
            try:
                decision = await engine.analyze(
                    symbol, spread_pct=spread,
                    regime=regime, regime_weights=regime_weights,
                )
                return symbol, engine.to_dict(decision)
            except Exception as e:
                logger.warning(f"Scalp analysis failed for {symbol}: {e}")
                return symbol, None

    tasks = [analyze_scalp_coin(c.symbol, c.spread_pct) for c in req.coins]
    results = await asyncio.gather(*tasks)

    analyzed = []
    for symbol, ai_result in results:
        if ai_result is None:
            continue
        coin_info = scanner_data.get(symbol)
        analyzed.append({
            "symbol": symbol,
            "action": ai_result.get("action", "HOLD"),
            "confidence": int(ai_result.get("confidence", 0) * session_mult),
            "reason": ai_result.get("reason", ""),
            "current_price": ai_result.get("current_price", 0.0),
            "rsi": ai_result.get("rsi", 50.0),
            "trend": ai_result.get("trend", "NEUTRAL"),
            "htf_trend": ai_result.get("htf_trend", "NEUTRAL"),
            "atr": ai_result.get("atr", 0.0),
            "atr_pct": ai_result.get("atr_pct", 0.0),
            "vwap": ai_result.get("vwap", 0.0),
            "volume_spike": ai_result.get("volume_spike", False),
            "candle_type": ai_result.get("candle_type", "DOJI"),
            "is_choppy": ai_result.get("is_choppy", False),
            "setup_grade": ai_result.get("setup_grade", "C"),
            "macd_crossover": ai_result.get("macd_crossover", "NONE"),
            "bb_position": ai_result.get("bb_position", "MID"),
            "is_pullback": ai_result.get("is_pullback", False),
            "is_chase": ai_result.get("is_chase", False),
            "spread_pct": coin_info.spread_pct if coin_info else 0.0,
            "volume_24h": coin_info.volume_24h if coin_info else 0.0,
            "strategy_type": ai_result.get("strategy_type", "scalp_trend_pullback"),
            "regime": regime,
        })

    # Step 3: Sniper engine (fast momentum plays)
    sniper_signals = []
    try:
        sniper = SniperEngine()
        all_symbols = [c.symbol for c in req.coins[:30]]
        sniper_setups = await sniper.full_scan(all_symbols)
        for setup in sniper_setups:
            if setup.confidence >= settings.SCALP_MIN_CONFIDENCE:
                sniper_signals.append({
                    "symbol": setup.symbol,
                    "action": setup.side,
                    "confidence": setup.confidence,
                    "reason": setup.reason,
                    "current_price": setup.current_price,
                    "strategy_type": setup.strategy_type or f"sniper_{setup.setup_type}",
                    "regime": regime,
                    "setup_grade": "B",
                    "atr_pct": 0.0,
                    "spread_pct": 0.0,
                })
    except Exception as e:
        logger.warning(f"Sniper engine error: {e}")

    # Step 4: Combine + filter
    all_signals = analyzed + sniper_signals

    # Apply strategy weight adjustments
    try:
        weight_adjustments = await strategy_tracker.get_strategy_weight_adjustments(days=14)
        for sig in all_signals:
            st = sig.get("strategy_type", "")
            if st in weight_adjustments:
                adj = weight_adjustments[st]
                is_scalp = st.startswith(("scalp", "sniper"))
                effective_adj = max(adj, 1.0) if is_scalp else adj
                sig["confidence"] = int(min(sig.get("confidence", 0) * effective_adj, 100))
    except Exception as e:
        logger.warning(f"Strategy weight adjustment failed: {e}")

    # Symbol cooldown filter
    filtered = []
    for sig in all_signals:
        if sig.get("action") in ("BUY", "SELL"):
            cooled, _ = await strategy_tracker.check_symbol_cooldown(sig.get("symbol", ""))
            if cooled:
                continue
        filtered.append(sig)

    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # Split: tradeable vs watchlist
    SCALP_MIN = settings.SCALP_MIN_CONFIDENCE       # 65
    SCALP_WATCH = settings.SCALP_WATCHLIST_CONFIDENCE  # 55

    tradeable = [
        c for c in filtered
        if c.get("action") not in ("HOLD", None) and c.get("confidence", 0) >= SCALP_MIN
    ][:settings.MAX_TRADES_PER_CYCLE]

    scalp_watchlist = [
        c for c in filtered
        if c.get("action") not in ("HOLD", None)
        and SCALP_WATCH <= c.get("confidence", 0) < SCALP_MIN
    ]

    logger.info(
        f"  V11 Scalp result: {len(analyzed)} scalp | {len(sniper_signals)} sniper | "
        f"Tradeable: {len(tradeable)} | Watchlist: {len(scalp_watchlist)} | "
        f"Regime: {regime} | Session: {session_mult}"
    )

    return clean_json_types({
        "status": "ok",
        "analyzed": len(analyzed),
        "count": len(filtered[:req.top_n]),
        "tradeable_count": len(tradeable),
        "has_signals": len(tradeable) > 0,
        "regime": regime,
        "session_multiplier": session_mult,
        "scalp_watchlist": scalp_watchlist,
        "coins": tradeable if tradeable else filtered[:req.top_n],
    })


# ═══════════════════════════════════════════════════════════════════
# V11: /analyze-swing — Swing-only endpoint (no scalp engine)
# ═══════════════════════════════════════════════════════════════════

class SwingBatchRequest(BaseModel):
    coins: List[CoinInput]
    top_n: int = 10


@router.post("/analyze-swing")
async def analyze_swing_batch(req: SwingBatchRequest):
    """
    V11 Swing-only batch analysis endpoint.
    Used exclusively by swing_engine_workflow (15-min trigger).

    Flow:
      1. Detect regime
      2. Re-evaluate existing swing watchlist entries (may trigger)
      3. Scan top 20 coins for NEW swing setups (15m/1h/4h)
      4. Save new setups to watchlist DB
      5. Return triggered setups (ready to execute) + full watchlist state
      6. Send ONE grouped Telegram swing watchlist message
    """
    if not req.coins:
        return {"status": "ok", "count": 0, "has_signals": False, "coins": [], "swing_watchlist": []}

    logger.info(f"🌊 V11 Swing batch: {len(req.coins)} coins...")

    # Step 1: Regime
    regime_router = MarketRegimeRouter()
    try:
        regime_data = await regime_router.detect_regime()
        regime = regime_data.regime
    except Exception as e:
        logger.warning(f"Regime detection failed: {e}")
        regime = "SIDEWAYS_RANGE"

    swing_engine = SwingEngine()
    triggered_setups = []
    new_watchlist_setups = []

    # Step 2: Re-evaluate watchlist
    try:
        triggered = await swing_engine.update_watchlist()
        for setup in triggered:
            triggered_setups.append({
                "symbol": setup["symbol"],
                "action": setup["side"],
                "confidence": setup["confidence"],
                "reason": setup.get("reason", "Swing setup triggered"),
                "current_price": setup.get("current_price", 0.0),
                "strategy_type": setup.get("strategy_type", "swing_triggered"),
                "regime": regime,
                "setup_grade": "B",
                "atr_pct": 0.0,
                "spread_pct": 0.0,
            })
    except Exception as e:
        logger.warning(f"Swing watchlist update failed: {e}")

    # Step 3: Scan top 20 for new swing setups
    try:
        top_symbols = [c.symbol for c in req.coins[:20]]
        new_setups = await swing_engine.scan_multiple(top_symbols, regime)
        if new_setups:
            await swing_engine.save_to_watchlist(new_setups)
            logger.info(f"  🔭 {len(new_setups)} new swing setups saved to watchlist")
            new_watchlist_setups = [
                {
                    "symbol": s.symbol,
                    "action": s.side,
                    "side": s.side,
                    "confidence": s.confidence,
                    "setup_type": s.setup_type,
                    "trigger_price": s.trigger_price,
                    "current_price": s.current_price,
                    "reason": s.reason,
                    "strategy_type": f"swing_{s.setup_type}",
                }
                for s in new_setups
            ]
    except Exception as e:
        logger.warning(f"Swing new scan failed: {e}")

    # Step 4: Cleanup stale watchlist entries
    try:
        await swing_engine.cleanup_old()
    except Exception:
        pass

    # Step 5: Send grouped swing watchlist Telegram (one message)
    all_watchlist = new_watchlist_setups  # show newly found setups
    if all_watchlist:
        try:
            telegram = TelegramNotifier()
            await telegram.send_swing_watchlist(all_watchlist)
        except Exception as tw_err:
            logger.debug(f"Swing watchlist telegram failed: {tw_err}")

    has_signals = len(triggered_setups) > 0

    logger.info(
        f"  V11 Swing result: {len(triggered_setups)} triggered | "
        f"{len(new_watchlist_setups)} new setups | Regime: {regime}"
    )

    return clean_json_types({
        "status": "ok",
        "analyzed": len(req.coins),
        "count": len(triggered_setups),
        "tradeable_count": len(triggered_setups),
        "has_signals": has_signals,
        "regime": regime,
        "swing_watchlist_triggered": len(triggered_setups),
        "new_swing_setups": len(new_watchlist_setups),
        "swing_watchlist": new_watchlist_setups,
        "coins": triggered_setups,
    })
