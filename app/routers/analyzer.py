"""

V18 Analyzer API — Backend-First Direct Signal Architecture



Endpoints:

  POST /analyze        — Single coin analysis

  POST /analyze-batch  — Batch analysis with ALL 4 engines

  POST /analyze-scalp  — Scalp-only batch (V18: auto-registers signals directly)

  POST /analyze-swing  — Swing-only batch  (V18: high-conf → direct signal, sub-threshold → watchlist)



V18 Signal Flow:

  Python Backend: AI detects setup → _register_signal_direct() → DB save → Telegram

  n8n: scheduling | lifecycle monitoring | TP/SL tracking | daily reports



N8N IS NOT USED FOR SIGNAL ROUTING — all signal creation happens here.

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

from app.modules.signal_tracker_v16 import signal_tracker_v16  # V16

from app.config import settings

from app.utils.serialization import clean_json_types



# V18: Backend-first signal registration — imported lazily to avoid circular imports

async def _post_signal_direct(coin: dict, regime: str, source: str) -> dict:

    """Thin wrapper that imports and calls _register_signal_direct."""

    logger.info(

        "[SIGNAL DEBUG] _post_signal_direct: symbol=%s action=%s conf=%s price=%s source=%s",

        coin.get("symbol"), coin.get("action"), coin.get("confidence"),

        coin.get("current_price"), source

    )

    from app.routers.executor import _register_signal_direct

    return await _register_signal_direct(

        symbol        = coin["symbol"],

        action        = coin.get("action", "HOLD"),

        confidence    = int(coin.get("confidence", 0)),

        current_price = float(coin.get("current_price", 0.0)),

        strategy_type = coin.get("strategy_type", "scalp_trend_pullback"),

        reason        = coin.get("reason", ""),

        atr           = float(coin.get("atr", 0.0)),

        atr_pct       = float(coin.get("atr_pct", 0.0)),

        spread_pct    = float(coin.get("spread_pct", 0.0)),

        rsi           = float(coin.get("rsi", 50.0)),

        regime        = regime,

        setup_grade   = coin.get("setup_grade", ""),

        source        = source,

    )


# ── DIAGNOSTIC MODE: Force-send signal bypassing ALL gates ────────────
import traceback as _traceback

async def _force_send_diagnostic(coin: dict, regime: str) -> dict:
    """
    DIAGNOSTIC ONLY — Bypasses ALL gates.
    Direct path: signal number → DB → Telegram.
    Used to prove the pipeline physically works.
    """
    from app.modules.signal_tracker_v16 import signal_tracker_v16
    from app.modules.telegram import TelegramNotifier
    from app.database import async_session
    from app.models.trading import Signal

    symbol = str(coin.get("symbol", "UNKNOWN")).upper()
    action = coin.get("action", "HOLD")
    # Force action to BUY if HOLD — we need to test the pipeline
    if action not in ("BUY", "SELL"):
        action = "BUY"
    conf = int(coin.get("confidence", 50))
    price = float(coin.get("current_price", 0.0))
    reason = f"🔧 DIAGNOSTIC FORCE-SEND | {str(coin.get('reason', ''))[:200]}"
    strategy = coin.get("strategy_type", "diagnostic_test")

    logger.warning("[DIAG-FORCE] ╔═══ FORCE SEND START: %s %s conf=%s price=%s", symbol, action, conf, price)

    if price <= 0:
        logger.error("[DIAG-FORCE] ╚═══ ABORT: price=0 for %s", symbol)
        return {"status": "error", "reason": "price=0"}

    # 1. Signal number
    try:
        signal_number = await signal_tracker_v16.get_next_signal_number()
        signal_id_label = f"DIAG-{signal_number:03d}"
        logger.warning("[DIAG-FORCE] ║ Step 1 OK: signal_number=%s label=%s", signal_number, signal_id_label)
    except Exception as e:
        logger.error("[DIAG-FORCE] ║ Step 1 FAILED: signal number: %s\n%s", e, _traceback.format_exc())
        return {"status": "error", "reason": f"signal_number: {e}"}

    # 2. Compute basic TP/SL (fixed 5% / 2%)
    tp_pct, sl_pct = 5.0, 2.0
    if action == "BUY":
        tp_price = price * 1.05
        sl_price = price * 0.98
    else:
        tp_price = price * 0.95
        sl_price = price * 1.02

    # 3. DB insert
    signal_id = None
    try:
        async with async_session() as session:
            db_signal = Signal(
                symbol=symbol, side=action, confidence=conf,
                reason=reason[:800], ai_called=True,
                strategy_type=strategy, regime=regime,
                signal_number=signal_number, entry_price=price,
                tp_price=round(tp_price, 8), sl_price=round(sl_price, 8),
                tp_pct=tp_pct, sl_pct=sl_pct,
                entry_zone_low=price * 0.99, entry_zone_high=price * 1.001,
                status="PENDING", atr=0.0, atr_pct=0.0, btc_bias="NEUTRAL",
            )
            session.add(db_signal)
            await session.flush()
            signal_id = db_signal.id
            await session.commit()
        logger.warning("[DIAG-FORCE] ║ Step 3 OK: DB insert signal_id=%s", signal_id)
    except Exception as e:
        logger.error("[DIAG-FORCE] ║ Step 3 FAILED: DB insert: %s\n%s", e, _traceback.format_exc())
        return {"status": "error", "reason": f"DB: {e}"}

    # 4. Telegram send
    telegram_sent = False
    try:
        tg = TelegramNotifier()
        telegram_sent = await tg.send_signal_alert(
            signal_number=signal_number, symbol=symbol, side=action,
            confidence=conf, entry_price=price,
            entry_zone_low=price * 0.99, entry_zone_high=price * 1.001,
            tp_price=round(tp_price, 8), sl_price=round(sl_price, 8),
            tp_pct=tp_pct, sl_pct=sl_pct,
            strategy_type=strategy, setup_grade="DIAG",
            regime=regime, btc_bias="NEUTRAL",
            reason=reason, atr_pct=0.0, risk_reward=2.5,
        )
        logger.warning("[DIAG-FORCE] ║ Step 4 OK: Telegram sent=%s", telegram_sent)
    except Exception as e:
        logger.error("[DIAG-FORCE] ║ Step 4 FAILED: Telegram: %s\n%s", e, _traceback.format_exc())

    logger.warning("[DIAG-FORCE] ╚═══ FORCE SEND COMPLETE: %s %s tg=%s db_id=%s", signal_id_label, symbol, telegram_sent, signal_id)

    return {
        "status": "signal_generated",
        "signal_id": signal_id,
        "signal_id_label": signal_id_label,
        "symbol": symbol,
        "side": action,
        "confidence": conf,
        "telegram_sent": telegram_sent,
        "source": "diagnostic_force",
    }



router = APIRouter()

logger = logging.getLogger(__name__)



# V16: Analysis cache (5-min TTL)

import time as _time

_analysis_cache: dict[str, dict]  = {}   # symbol → {"ts": float, "result": dict}

_CACHE_TTL_S = 300.0                     # 5 minutes



# V16: Quiet market timer

_last_quiet_sent: float = 0.0

_QUIET_INTERVAL_S = 900.0               # V19: restored to 15 min (was 3600s debug)



# V17: Symbol signal cooldown — suppress re-signalling same symbol within 20 min

# unless confidence improved >= 10pts

_symbol_signal_cooldown: dict[str, dict] = {}  # symbol -> {"ts": float, "confidence": int, "side": str}

_SIGNAL_COOLDOWN_S = 1200.0  # 20 minutes





def _is_cache_fresh(symbol: str) -> bool:

    """True if symbol was analyzed within the cache TTL."""

    entry = _analysis_cache.get(symbol)

    if not entry:

        return False

    return (_time.time() - entry["ts"]) < _CACHE_TTL_S





def _get_cached(symbol: str) -> dict | None:

    entry = _analysis_cache.get(symbol)

    return entry["result"] if entry else None





def _store_cache(symbol: str, result: dict) -> None:

    _analysis_cache[symbol] = {"ts": _time.time(), "result": result}





def _passes_prefilter(coin) -> bool:

    """

    V17 pre-filter: relaxed technical gate.

    Coin must pass >=2 of 4 criteria.

    V17 changes: abs_chg lowered 1.5%->0.8%, score threshold 55->45

    """

    score = 0

    abs_chg = abs(coin.price_change_pct)



    if abs_chg > 0.8:    # V17: lowered from 1.5 — catch slower-moving setups

        score += 1

    if abs_chg > 2.0:    # V17: lowered from 2.5

        score += 1

    if coin.spread_pct < 0.10:  # V17: relaxed from 0.08

        score += 1

    if getattr(coin, "score", 0) > 45:  # V17: lowered from 55

        score += 1



    return score >= 2  # V19: restored — keeps noise out of AI pipeline





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



            # V12: ONE batched Telegram message for new watchlist entries (replaces per-coin spam)

            try:

                telegram = TelegramNotifier()

                batch = [

                    {

                        "symbol": s.symbol,

                        "action": s.side,

                        "side": s.side,

                        "confidence": s.confidence,

                        "setup_type": s.setup_type,

                        "trigger_price": s.trigger_price,

                        "current_price": s.current_price,

                        "strategy_type": f"swing_{s.setup_type}",

                    }

                    for s in new_setups[:10]

                ]

                await telegram.send_swing_watchlist(batch)

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



    # Step 2: Scalp engine — with V16 pre-filter + cache

    engine = ScalpingEngine()

    scanner_data = {c.symbol: c for c in req.coins}

    semaphore = asyncio.Semaphore(12)  # Scalp = faster, higher concurrency



    # V16: Apply pre-filter first — skip AI for low-quality coins

    prefilter_passed  = [c for c in req.coins if _passes_prefilter(c)]

    prefilter_skipped = len(req.coins) - len(prefilter_passed)

    if prefilter_skipped > 0:

        logger.info(f"  V16 Pre-filter: {len(prefilter_passed)} pass / {prefilter_skipped} skipped")



    # V18: Raised from 8 → 15 — wider coverage for direct signal posting

    ai_batch = prefilter_passed[:15]

    cache_hits = 0



    async def analyze_scalp_coin(symbol: str, spread: float):

        nonlocal cache_hits

        # Check cache first

        if _is_cache_fresh(symbol):

            cache_hits += 1

            return symbol, _get_cached(symbol)

        async with semaphore:

            try:

                decision = await engine.analyze(

                    symbol, spread_pct=spread,

                    regime=regime, regime_weights=regime_weights,

                )

                result = engine.to_dict(decision)

                _store_cache(symbol, result)   # V16: cache result

                return symbol, result

            except Exception as e:

                logger.warning(f"Scalp analysis failed for {symbol}: {e}")

                return symbol, None



    tasks = [analyze_scalp_coin(c.symbol, c.spread_pct) for c in ai_batch]

    results = await asyncio.gather(*tasks)



    analyzed = []

    for symbol, ai_result in results:

        if ai_result is None:

            continue

        coin_info = scanner_data.get(symbol)

        analyzed.append({

            "symbol": symbol,

            "action": ai_result.get("action", "HOLD"),

            "confidence": int(ai_result.get("confidence", 0)),  # V18-debug: removed session_mult double-apply

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



    # ===================================================================
    # DIAGNOSTIC MODE - ALL filters BYPASSED
    # Proves: analyzer -> DB -> Telegram physically works
    # REMOVE after pipeline is confirmed working
    # ===================================================================

    all_signals = analyzed + sniper_signals

    # -- DIAGNOSTIC: Log complete analysis breakdown --
    hold_count = sum(1 for s in analyzed if s.get("action") == "HOLD")
    buy_count = sum(1 for s in analyzed if s.get("action") == "BUY")
    sell_count = sum(1 for s in analyzed if s.get("action") == "SELL")
    none_count = sum(1 for s in analyzed if s.get("action") in (None, ""))

    logger.warning(
        "=== [DIAG] ANALYSIS COMPLETE: %d coins analyzed | "
        "BUY=%d SELL=%d HOLD=%d NONE=%d | sniper=%d | regime=%s",
        len(analyzed), buy_count, sell_count, hold_count, none_count,
        len(sniper_signals), regime
    )

    # Log ALL analyzed coins at WARNING level (force visible)
    for i, coin_diag in enumerate(analyzed):
        try:
            logger.warning(
                "  [DIAG] #%02d %s: action=%s conf=%s grade=%s strat=%s price=%s | %s",
                i + 1,
                coin_diag.get("symbol", "?"),
                coin_diag.get("action", "?"),
                coin_diag.get("confidence", 0),
                coin_diag.get("setup_grade", "?"),
                coin_diag.get("strategy_type", "?"),
                coin_diag.get("current_price", 0),
                str(coin_diag.get("reason", ""))[:100],
            )
        except Exception:
            logger.warning("  [DIAG] #%02d FAILED TO LOG", i + 1)

    # -- Sort ALL by confidence (no filters, no cooldowns) --
    all_sorted = sorted(all_signals, key=lambda x: x.get("confidence", 0), reverse=True)

    # Split into candidates
    non_hold = [c for c in all_sorted if c.get("action") in ("BUY", "SELL")]
    logger.warning(
        "=== [DIAG] NON-HOLD candidates: %d (from %d total) | "
        "Top 5: %s",
        len(non_hold), len(all_sorted),
        ", ".join(f"{c.get('symbol','?')}({c.get('action','?')}/{c.get('confidence',0)})"
                 for c in non_hold[:5]) or "NONE"
    )

    # -- FORCE-SEND TOP 3 via diagnostic path --
    # Takes top 3 coins by confidence, regardless of action/confidence
    # Forces BUY if HOLD. Bypasses ALL gates.
    force_candidates = all_sorted[:3]
    signals_posted = 0
    signal_results = []

    for fc in force_candidates:
        try:
            logger.warning(
                "=== [DIAG] FORCE-SENDING: %s action=%s conf=%s price=%s",
                fc.get("symbol"), fc.get("action"), fc.get("confidence"), fc.get("current_price")
            )
            result = await _force_send_diagnostic(fc, regime)
            logger.warning(
                "=== [DIAG] FORCE-SEND RESULT: %s status=%s tg=%s signal_id=%s",
                fc.get("symbol"), result.get("status"), result.get("telegram_sent"), result.get("signal_id")
            )
            if result.get("status") == "signal_generated":
                signals_posted += 1
                signal_results.append({
                    "symbol": fc.get("symbol", "?"),
                    "signal_id_label": result.get("signal_id_label"),
                    "confidence": result.get("confidence"),
                    "side": result.get("side"),
                    "source": "diagnostic_force",
                })
        except Exception as fe:
            logger.error(
                "=== [DIAG] FORCE-SEND EXCEPTION: %s: %s\n%s",
                fc.get("symbol"), fe, _traceback.format_exc()
            )

    # -- Also try normal path for any non-HOLD with conf >= 48 --
    tradeable = [c for c in non_hold if c.get("confidence", 0) >= 48][:3]
    for coin in tradeable:
        try:
            logger.warning(
                "=== [DIAG] NORMAL-PATH ATTEMPT: %s %s conf=%s",
                coin.get("symbol"), coin.get("action"), coin.get("confidence")
            )
            result = await _post_signal_direct(coin, regime, source="scalp_engine")
            logger.warning(
                "=== [DIAG] NORMAL-PATH RESULT: %s status=%s reason=%s",
                coin.get("symbol"), result.get("status"), str(result.get("reason", ""))[:100]
            )
            if result.get("status") == "signal_generated":
                signals_posted += 1
                signal_results.append({
                    "symbol": coin["symbol"],
                    "signal_id_label": result.get("signal_id_label"),
                    "confidence": result.get("confidence"),
                    "side": result.get("side"),
                })
        except Exception as se:
            logger.error(
                "=== [DIAG] NORMAL-PATH EXCEPTION: %s: %s\n%s",
                coin.get("symbol"), se, _traceback.format_exc()
            )

    logger.warning(
        "=== [DIAG] CYCLE COMPLETE: %d analyzed | %d force-sent | %d normal-path | "
        "%d total posted | regime=%s session=%s | prefilter=%d/%d cache_hits=%d",
        len(analyzed), len(force_candidates), len(tradeable),
        signals_posted, regime, session_mult,
        len(ai_batch), len(req.coins), cache_hits
    )

    has_signals = signals_posted > 0

    # Quiet market notification (every 15 min if no signals)
    if not has_signals:
        global _last_quiet_sent
        now_ts_q = _time.time()
        if (now_ts_q - _last_quiet_sent) >= _QUIET_INTERVAL_S:
            _last_quiet_sent = now_ts_q
            try:
                telegram_n = TelegramNotifier()
                await telegram_n.send_quiet_market(
                    active_signals=signal_tracker_v16.active_count()
                )
            except Exception as tq_err:
                logger.warning("  Quiet market telegram failed: %s", tq_err)

    return clean_json_types({
        "status":           "ok",
        "analyzed":         len(analyzed),
        "count":            len(all_sorted[:req.top_n]),
        "tradeable_count":  len(tradeable),
        "signals_posted":   signals_posted,
        "has_signals":      has_signals,
        "regime":           regime,
        "session_multiplier": session_mult,
        "scalp_watchlist":  [],
        "signals":          signal_results,
        "coins":            all_sorted[:req.top_n],
        "diagnostic_mode":  True,
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



    # Step 2b: V18 — Register watchlist-triggered setups as DIRECT SIGNALS
    wl_signals_posted = 0
    for setup in list(triggered_setups):
        try:
            result = await _post_signal_direct(setup, regime, source="swing_watchlist_trigger")
            if result.get("status") == "signal_generated":
                wl_signals_posted += 1
                logger.info(
                    "[SIGNAL GENERATED] %s %s %s conf=%s (watchlist-triggered)",
                    result.get("signal_id_label"), setup["symbol"], setup.get("action"), setup.get("confidence")
                )
            else:
                logger.info("[V18 WL-SKIP] %s: %s", setup["symbol"], result.get("reason", "")[:80])
        except Exception as e:
            logger.error("[V18 WL-TRIGGER] %s failed: %s", setup["symbol"], e)
    triggered_setups = []  # V18: handled as direct signals


    # Step 3: Scan top 20 for new swing setups

    # V18: High-confidence setups → direct signal registration

    #       Sub-threshold setups → watchlist (monitoring only)

    SWING_DIRECT_MIN = getattr(settings, "SWING_MIN_CONFIDENCE_EXECUTE", 72)

    swing_signals_posted = 0

    try:

        top_symbols = [c.symbol for c in req.coins[:20]]

        new_setups = await swing_engine.scan_multiple(top_symbols, regime)

        if new_setups:

            direct_setups   = [s for s in new_setups if s.confidence >= SWING_DIRECT_MIN]

            watchlist_setups = [s for s in new_setups if s.confidence < SWING_DIRECT_MIN]



            # Save sub-threshold to watchlist (future re-eval)

            if watchlist_setups:

                await swing_engine.save_to_watchlist(watchlist_setups)

                logger.info(

                    f"  🔭 {len(watchlist_setups)} swing setups saved to watchlist "

                    f"(conf < {SWING_DIRECT_MIN}%)"

                )



            # V18: Direct register high-confidence swing signals

            for s in direct_setups:

                coin_data = {

                    "symbol":        s.symbol,

                    "action":        s.side,

                    "confidence":    s.confidence,

                    "current_price": s.current_price,

                    "strategy_type": f"swing_{s.setup_type}",

                    "reason":        s.reason,

                    "atr":           0.0,

                    "atr_pct":       0.0,

                    "spread_pct":    0.0,

                    "rsi":           50.0,

                    "setup_grade":   "B",

                }

                try:

                    result = await _post_signal_direct(coin_data, regime, source="swing_engine")

                    if result.get("status") == "signal_generated":

                        swing_signals_posted += 1

                        logger.info(

                            f"  🌊 [V18 SWING SIGNAL] {result.get('signal_id_label')} "

                            f"{s.symbol} {s.side} conf={s.confidence}%"

                        )

                    else:

                        # Rejected by confidence/R:R gate — save to watchlist instead

                        await swing_engine.save_to_watchlist([s])

                        logger.info(

                            f"  [V18 SWING→WATCH] {s.symbol}: {result.get('reason', '')[:80]}"

                        )

                except Exception as se:

                    logger.error(f"  [V18 SWING POST] {s.symbol} failed: {se}")

                    await swing_engine.save_to_watchlist([s])



            new_watchlist_setups = [

                {

                    "symbol":        s.symbol,

                    "action":        s.side,

                    "side":          s.side,

                    "confidence":    s.confidence,

                    "setup_type":    s.setup_type,

                    "trigger_price": s.trigger_price,

                    "current_price": s.current_price,

                    "reason":        s.reason,

                    "strategy_type": f"swing_{s.setup_type}",

                }

                for s in watchlist_setups

            ]

    except Exception as e:

        logger.warning(f"Swing new scan failed: {e}")



    # Step 4: Cleanup stale watchlist entries

    try:

        await swing_engine.cleanup_old()

    except Exception:

        pass



    # Step 5: V18 — Watchlist Telegram REMOVED.
    # Sub-threshold setups saved to DB for future re-evaluation only.
    # No Telegram watchlist message — only real signals go to Telegram.
    if new_watchlist_setups:
        logger.info(
            "  [V18 WATCHLIST] %d sub-threshold setups in DB watchlist (no Telegram)",
            len(new_watchlist_setups)
        )
    all_watchlist = []  # V18: suppressed


    has_signals = swing_signals_posted > 0 or wl_signals_posted > 0



    logger.info(

        f"  V18 Swing result: {len(triggered_setups)} watchlist-triggered | "

        f"{swing_signals_posted} direct signals posted | "

        f"{len(new_watchlist_setups)} new watchlist entries | Regime: {regime}"

    )



    return clean_json_types({
        "status":              "ok",
        "analyzed":            len(req.coins),
        "signals_posted":      swing_signals_posted + wl_signals_posted,
        "direct_signals":      swing_signals_posted,
        "watchlist_triggered": wl_signals_posted,
        "new_watchlist":       len(new_watchlist_setups),
        "has_signals":         has_signals,
        "regime":              regime,
    })

