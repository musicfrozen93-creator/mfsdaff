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

router = APIRouter()
logger = logging.getLogger(__name__)

# V16: Analysis cache (5-min TTL)
import time as _time
_analysis_cache: dict[str, dict]  = {}   # symbol → {"ts": float, "result": dict}
_CACHE_TTL_S = 300.0                     # 5 minutes

# V16: Quiet market timer
_last_quiet_sent: float = 0.0
_QUIET_INTERVAL_S = 900.0               # 15 minutes

# V17: Symbol signal cooldown — suppress re-signalling same symbol within 20 min
# unless confidence improved >= 10pts
_symbol_signal_cooldown: dict[str, dict] = {}  # symbol -> {"ts": float, "confidence": int, "side": str}
_SIGNAL_COOLDOWN_S = 1200.0  # 20 minutes

# V17: Watchlist spam suppression — same symbol+side suppressed for 30 min
_watchlist_post_ts: dict[str, float] = {}  # "SYMBOL_SIDE" -> last_post_ts
_WATCHLIST_COOLDOWN_S = 1800.0  # 30 minutes


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

    return score >= 2


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
    now_ts = _time.time()
    for sig in all_signals:
        sym = sig.get("symbol", "")
        action = sig.get("action", "HOLD")
        conf = sig.get("confidence", 0)
        if action in ("BUY", "SELL"):
            # Platform cooldown
            cooled, _ = await strategy_tracker.check_symbol_cooldown(sym)
            if cooled:
                continue
            # V17: Per-symbol signal cooldown
            cooldown_entry = _symbol_signal_cooldown.get(sym)
            if cooldown_entry:
                age = now_ts - cooldown_entry["ts"]
                conf_improvement = conf - cooldown_entry["confidence"]
                same_side = cooldown_entry["side"] == action
                if age < _SIGNAL_COOLDOWN_S and same_side and conf_improvement < 10:
                    logger.debug(
                        f"  [COOLDOWN] {sym} {action} suppressed: "
                        f"age={age:.0f}s conf_delta={conf_improvement:+d}"
                    )
                    continue
            # Update cooldown
            _symbol_signal_cooldown[sym] = {"ts": now_ts, "confidence": conf, "side": action}
        filtered.append(sig)

    filtered.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # ── V18: Split + direct-post signals ──────────────────────────────
    SCALP_MIN   = settings.SCALP_MIN_CONFIDENCE         # 70
    SCALP_WATCH = settings.SCALP_WATCHLIST_CONFIDENCE   # 55

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
        "[SIGNAL DEBUG] /analyze-scalp: tradeable=%s watchlist=%s filtered=%s regime=%s session_mult=%s",
        len(tradeable), len(scalp_watchlist), len(filtered), regime, session_mult
    )
    if not tradeable:
        logger.warning("[SIGNAL DEBUG] tradeable[] is EMPTY - no scalp signals will be posted this cycle")

    # V18: Auto-register every tradeable scalp signal directly in Python.
    # No n8n routing needed — backend posts to DB + Telegram immediately.
    signals_posted = 0
    signal_results = []
    for coin in tradeable:
        try:
            result = await _post_signal_direct(coin, regime, source="scalp_engine")
            if result.get("status") == "signal_generated":
                signals_posted += 1
                signal_results.append({
                    "symbol": coin["symbol"],
                    "signal_id_label": result.get("signal_id_label"),
                    "confidence": result.get("confidence"),
                    "side": result.get("side"),
                })
                logger.info(
                    f"  ⚡ [V18 SCALP SIGNAL] {result.get('signal_id_label')} "
                    f"{coin['symbol']} {coin.get('action')} conf={result.get('confidence')}%"
                )
            else:
                logger.info(
                    f"  [V18 SCALP SKIP] {coin['symbol']}: {result.get('status')} — "
                    f"{result.get('reason', '')[:80]}"
                )
        except Exception as se:
            logger.error(f"  [V18 SCALP POST] {coin['symbol']} failed: {se}")

    logger.info(
        f"  V18 Scalp result: {len(analyzed)} analyzed | {len(sniper_signals)} sniper | "
        f"Tradeable: {len(tradeable)} | Signals posted: {signals_posted} | "
        f"Watchlist: {len(scalp_watchlist)} | Regime: {regime} | "
        f"Session: {session_mult} | Pre-filter: {len(ai_batch)}/{len(req.coins)} | "
        f"Cache hits: {cache_hits}"
    )

    has_signals = signals_posted > 0

    # V16: Quiet market timer — send Telegram at most once per 15 min if no signals
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
                logger.debug(f"  Quiet market telegram failed (non-critical): {tq_err}")

    return clean_json_types({
        "status":           "ok",
        "analyzed":         len(analyzed),
        "count":            len(filtered[:req.top_n]),
        "tradeable_count":  len(tradeable),
        "signals_posted":   signals_posted,   # V18: how many went to DB+Telegram
        "has_signals":      has_signals,
        "regime":           regime,
        "session_multiplier": session_mult,
        "scalp_watchlist":  scalp_watchlist,
        "signals":          signal_results,   # V18: signal ID labels for n8n logging
        "coins":            tradeable if tradeable else filtered[:req.top_n],
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

    # Step 5: Send grouped swing watchlist Telegram (one message) with spam suppression
    all_watchlist = new_watchlist_setups
    if all_watchlist:
        now_ts = _time.time()
        # V17: Filter out recently posted symbol+side pairs
        to_post = []
        for s in all_watchlist:
            key = f"{s['symbol']}_{s['side']}"
            last_ts = _watchlist_post_ts.get(key, 0)
            if (now_ts - last_ts) >= _WATCHLIST_COOLDOWN_S:
                to_post.append(s)
                _watchlist_post_ts[key] = now_ts
            else:
                logger.debug(
                    f"  [WATCHLIST SUPPRESS] {s['symbol']} {s['side']} "
                    f"posted {(now_ts-last_ts)/60:.0f}m ago"
                )
        if to_post:
            try:
                telegram = TelegramNotifier()
                await telegram.send_swing_watchlist(to_post)
            except Exception as tw_err:
                logger.debug(f"Swing watchlist telegram failed: {tw_err}")

    has_signals = len(triggered_setups) > 0 or swing_signals_posted > 0

    logger.info(
        f"  V18 Swing result: {len(triggered_setups)} watchlist-triggered | "
        f"{swing_signals_posted} direct signals posted | "
        f"{len(new_watchlist_setups)} new watchlist entries | Regime: {regime}"
    )

    return clean_json_types({
        "status":                   "ok",
        "analyzed":                 len(req.coins),
        "count":                    len(triggered_setups) + swing_signals_posted,
        "tradeable_count":          len(triggered_setups) + swing_signals_posted,
        "signals_posted":           swing_signals_posted,   # V18
        "has_signals":              has_signals,
        "regime":                   regime,
        "swing_watchlist_triggered": len(triggered_setups),
        "new_swing_setups":         len(new_watchlist_setups),
        "swing_watchlist":          new_watchlist_setups,
        "coins":                    triggered_setups,
    })
