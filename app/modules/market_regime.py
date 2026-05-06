"""
V5.5 Market Regime Router (ENGINE D)

Determines current market mode before each cycle:
  1. TRENDING_BULL   — BTC strong uptrend
  2. TRENDING_BEAR   — BTC strong downtrend
  3. SIDEWAYS_RANGE  — BTC ranging / choppy
  4. BREAKOUT_EXPANSION — BTC breaking out of range
  5. HIGH_VOLATILITY — Extreme moves, dangerous
  6. DEAD_MARKET     — Very low activity

V5.5 Additions:
  - Engine conflict resolver (blocks trades when engines disagree)
  - Session-based activity multiplier (London/NY boost)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Cache regime for 10 minutes to avoid redundant BTC analysis
_regime_cache: Optional[dict] = None
_regime_cache_ts: float = 0.0
REGIME_CACHE_TTL = 600  # 10 minutes


@dataclass
class MarketRegime:
    regime: str              # TRENDING_BULL | TRENDING_BEAR | SIDEWAYS_RANGE | BREAKOUT_EXPANSION | HIGH_VOLATILITY | DEAD_MARKET
    confidence: int          # 0-100 how confident in the classification
    btc_trend: str           # BULLISH | BEARISH | NEUTRAL
    btc_atr_pct: float       # BTC volatility %
    btc_bb_width: float      # Bollinger Band width — squeeze vs expansion
    btc_ema_dist: float      # EMA20-EMA50 distance %
    btc_volume_ratio: float  # current vs average volume
    strategy_weights: dict   # {"scalp_trend": 1.0, "scalp_breakout": 0.5, ...}
    description: str


class MarketRegimeRouter:
    """
    V5 Market Regime Router — analyzes BTC to determine market state
    and route strategy priorities.
    """

    def __init__(self):
        self.base_url = settings.binance_base_url

    # ─── Helpers ──────────────────────────────────────────────────────

    def _ema(self, values: np.ndarray, period: int) -> np.ndarray:
        k = 2 / (period + 1)
        result = np.zeros(len(values))
        result[0] = values[0]
        for i in range(1, len(values)):
            result[i] = values[i] * k + result[i - 1] * (1 - k)
        return result

    def _atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            tr_list.append(tr)
        tr_arr = np.array(tr_list)
        if len(tr_arr) < period:
            return float(np.mean(tr_arr)) if len(tr_arr) > 0 else 0.0
        return float(np.mean(tr_arr[-period:]))

    def _bollinger_width(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0) -> float:
        if len(closes) < period:
            return 0.0
        middle = np.mean(closes[-period:])
        std = np.std(closes[-period:])
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        if middle > 0:
            return float((upper - lower) / middle * 100)
        return 0.0

    async def _fetch_btc_candles(self, interval: str = "1h", limit: int = 100) -> list:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/fapi/v1/klines",
                params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()

    # ─── V16: BTC Directional Bias ────────────────────────────────────

    # Cache bias for 3 minutes (more responsive than 10-min regime cache)
    _btc_bias_cache: dict = {}
    _btc_bias_cache_ts: float = 0.0
    BTC_BIAS_CACHE_TTL: float = 180.0  # 3 minutes

    async def get_btc_directional_bias(self) -> dict:
        """
        V16: Multi-timeframe BTC directional bias filter.

        Checks:
          1. EMA50 / EMA200 on 1h (macro trend)
          2. 1m / 5m / 15m EMA9 vs EMA21 alignment
          3. HH/HL structure (bullish) vs LH/LL structure (bearish)
          4. ATR-based volatility spike detection

        Returns:
          {
            "bias": "BULLISH" | "BEARISH" | "NEUTRAL",
            "volatility_high": bool,
            "long_confidence_multiplier": float,   # apply to LONG signals
            "short_confidence_multiplier": float,  # apply to SHORT signals
            "reason": str,
          }
        """
        import time as _time
        now = _time.time()
        if self._btc_bias_cache and (now - self._btc_bias_cache_ts) < self.BTC_BIAS_CACHE_TTL:
            return self._btc_bias_cache

        try:
            import asyncio as _asyncio
            raw_1h, raw_15m, raw_5m, raw_1m = await _asyncio.gather(
                self._fetch_btc_candles("1h", 210),
                self._fetch_btc_candles("15m", 50),
                self._fetch_btc_candles("5m", 50),
                self._fetch_btc_candles("1m", 30),
            )

            def _closes(raw): return np.array([float(k[4]) for k in raw])
            def _highs(raw):  return np.array([float(k[2]) for k in raw])
            def _lows(raw):   return np.array([float(k[3]) for k in raw])

            # ── 1H: EMA50 / EMA200 macro trend ───────────────────────
            c1h = _closes(raw_1h)
            ema50  = self._ema(c1h, 50)[-1]
            ema200 = self._ema(c1h, 200)[-1]
            price_1h = c1h[-1]
            macro_bull = ema50 > ema200 and price_1h > ema50
            macro_bear = ema50 < ema200 and price_1h < ema50

            # ── Multi-TF EMA9 vs EMA21 alignment ─────────────────────
            def tf_trend(raw):
                c = _closes(raw)
                e9  = self._ema(c, 9)[-1]
                e21 = self._ema(c, 21)[-1]
                if e9 > e21 and c[-1] > e9:
                    return "BULL"
                elif e9 < e21 and c[-1] < e9:
                    return "BEAR"
                return "NEUTRAL"

            t15 = tf_trend(raw_15m)
            t5  = tf_trend(raw_5m)
            t1  = tf_trend(raw_1m)
            bull_count = sum(1 for t in [t15, t5, t1] if t == "BULL")
            bear_count = sum(1 for t in [t15, t5, t1] if t == "BEAR")

            # ── HH/HL vs LH/LL structure (15m) ───────────────────────
            h15 = _highs(raw_15m)[-10:]
            l15 = _lows(raw_15m)[-10:]
            hh_hl = h15[-1] > h15[-3] and l15[-1] > l15[-3]   # Higher High + Higher Low
            lh_ll = h15[-1] < h15[-3] and l15[-1] < l15[-3]   # Lower High + Lower Low

            # ── ATR volatility spike (1h) ─────────────────────────────
            atr_1h = self._atr(_highs(raw_1h), _lows(raw_1h), c1h, 14)
            atr_pct_1h = (atr_1h / price_1h) * 100 if price_1h > 0 else 0
            volatility_high = atr_pct_1h > 3.0

            # ── 5-min BTC move: >1.5% in last 5 minutes = volatile warning ───────
            c5m = _closes(raw_5m)
            btc_5m_move = abs((c5m[-1] - c5m[-2]) / c5m[-2] * 100) if len(c5m) >= 2 else 0.0
            btc_5m_volatile = btc_5m_move > 1.5
            if btc_5m_volatile:
                logger.warning(
                    f"  ⚠️ V17 BTC 5m spike: {btc_5m_move:.2f}% — reducing confidence (not pausing)"
                )  # V17: no longer hard-pauses signals, just warns

            # ── Decide bias ───────────────────────────────────────────
            bull_score = (2 if macro_bull else 0) + bull_count + (1 if hh_hl else 0)
            bear_score = (2 if macro_bear else 0) + bear_count + (1 if lh_ll else 0)

            if bull_score >= 4:
                bias = "BULLISH"
                long_mult  = 1.0
                short_mult = 0.85   # V17: reduced penalty from 0.75 — SHORT still valid in bull
                reason = f"BTC bullish: macro={'bull' if macro_bull else 'bear'} TF={bull_count}/3 bull HH/HL={hh_hl}"
            elif bear_score >= 4:
                bias = "BEARISH"
                long_mult  = 0.85   # V17: reduced penalty from 0.75 — LONG still valid in bear
                short_mult = 1.0
                reason = f"BTC bearish: macro={'bear' if macro_bear else 'bull'} TF={bear_count}/3 bear LH/LL={lh_ll}"
            else:
                bias = "NEUTRAL"
                long_mult  = 1.0
                short_mult = 1.0
                reason = f"BTC neutral: bull_score={bull_score} bear_score={bear_score}"

            if volatility_high:
                reason += f" | HIGH VOLATILITY (ATR%={atr_pct_1h:.2f})"
            if btc_5m_volatile:
                reason += f" | BTC 5m SPIKE +{btc_5m_move:.2f}%"

            # V17: is_unstable flag — ATR > 2% means reduce confidence
            is_unstable = atr_pct_1h > 2.0

            result = {
                "bias": bias,
                "volatility_high": volatility_high,
                "btc_5m_volatile": btc_5m_volatile,
                "btc_5m_move_pct": round(btc_5m_move, 3),
                "long_confidence_multiplier": long_mult,
                "short_confidence_multiplier": short_mult,
                "reason": reason,
                "bull_score": bull_score,
                "bear_score": bear_score,
                "atr_pct_1h": round(atr_pct_1h, 3),
                "is_unstable": is_unstable,
                # V17: confidence reduction for volatile markets
                "volatility_confidence_adj": -5 if btc_5m_volatile else (0 if not is_unstable else -3),
            }
            logger.info(f"  🔶 V16 BTC Bias: {bias} | {reason}")
            self._btc_bias_cache = result
            self._btc_bias_cache_ts = now
            return result

        except Exception as e:
            logger.warning(f"V16 BTC bias detection failed: {e} — using NEUTRAL")
            result = {
                "bias": "NEUTRAL", "volatility_high": False,
                "btc_5m_volatile": False, "btc_5m_move_pct": 0.0,
                "long_confidence_multiplier": 1.0, "short_confidence_multiplier": 1.0,
                "reason": f"Detection failed: {str(e)[:60]}", "bull_score": 0, "bear_score": 0,
                "atr_pct_1h": 0.0,
            }
            self._btc_bias_cache = result
            self._btc_bias_cache_ts = now
            return result

    # ─── Main Detection ───────────────────────────────────────────────


    async def detect_regime(self, force_refresh: bool = False) -> MarketRegime:
        """
        Detect current market regime from BTC data.
        Cached for 10 minutes to reduce API calls.
        """
        global _regime_cache, _regime_cache_ts

        now = time.time()
        if not force_refresh and _regime_cache and (now - _regime_cache_ts) < REGIME_CACHE_TTL:
            logger.info(f"  📊 Regime cache hit: {_regime_cache['regime']} (age={int(now - _regime_cache_ts)}s)")
            return MarketRegime(**_regime_cache)

        logger.info("🌍 Detecting market regime from BTC data...")

        try:
            # Fetch BTC 1H candles
            raw_1h = await self._fetch_btc_candles("1h", 100)
            if len(raw_1h) < 50:
                return self._default_regime("Insufficient BTC data")

            closes = np.array([float(k[4]) for k in raw_1h])
            highs = np.array([float(k[2]) for k in raw_1h])
            lows = np.array([float(k[3]) for k in raw_1h])
            volumes = np.array([float(k[5]) for k in raw_1h])

            current_price = float(closes[-1])

            # EMA 20 vs 50
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            ema20_val = float(ema20[-1])
            ema50_val = float(ema50[-1])
            ema_dist = abs(ema20_val - ema50_val) / ema50_val * 100 if ema50_val > 0 else 0.0

            # Trend direction
            if ema20_val > ema50_val and current_price > ema20_val:
                btc_trend = "BULLISH"
            elif ema20_val < ema50_val and current_price < ema20_val:
                btc_trend = "BEARISH"
            else:
                btc_trend = "NEUTRAL"

            # ATR % (volatility)
            atr = self._atr(highs, lows, closes, 14)
            atr_pct = (atr / current_price) * 100 if current_price > 0 else 0.0

            # Bollinger Band width (squeeze detection)
            bb_width = self._bollinger_width(closes, 20, 2.0)

            # Volume ratio (current vs 20-period average)
            avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes))
            cur_vol = float(volumes[-1])
            volume_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

            # EMA convergence history — were EMAs squeezing then expanding?
            ema_dist_5_ago = abs(float(ema20[-6]) - float(ema50[-6])) / float(ema50[-6]) * 100 if float(ema50[-6]) > 0 else 0.0
            expanding = ema_dist > ema_dist_5_ago * 1.5

            # BB width history — was it squeezing then expanding?
            bb_width_10_ago = self._bollinger_width(closes[:-10], 20, 2.0) if len(closes) > 30 else bb_width
            bb_expanding = bb_width > bb_width_10_ago * 1.3

            # ── Classify Regime ──────────────────────────────────────
            regime, confidence, description = self._classify(
                btc_trend=btc_trend,
                ema_dist=ema_dist,
                atr_pct=atr_pct,
                bb_width=bb_width,
                volume_ratio=volume_ratio,
                expanding=expanding,
                bb_expanding=bb_expanding,
            )

            # Strategy weights
            strategy_weights = self._get_strategy_weights(regime)

            result = MarketRegime(
                regime=regime,
                confidence=confidence,
                btc_trend=btc_trend,
                btc_atr_pct=round(atr_pct, 4),
                btc_bb_width=round(bb_width, 4),
                btc_ema_dist=round(ema_dist, 4),
                btc_volume_ratio=round(volume_ratio, 2),
                strategy_weights=strategy_weights,
                description=description,
            )

            # Cache
            _regime_cache = {
                "regime": result.regime,
                "confidence": result.confidence,
                "btc_trend": result.btc_trend,
                "btc_atr_pct": result.btc_atr_pct,
                "btc_bb_width": result.btc_bb_width,
                "btc_ema_dist": result.btc_ema_dist,
                "btc_volume_ratio": result.btc_volume_ratio,
                "strategy_weights": result.strategy_weights,
                "description": result.description,
            }
            _regime_cache_ts = now

            logger.info(
                f"  📊 Regime: {regime} (conf={confidence}) | "
                f"BTC trend={btc_trend} ATR%={atr_pct:.2f} BB_W={bb_width:.2f} "
                f"EMA_dist={ema_dist:.3f}% vol_ratio={volume_ratio:.1f}x"
            )
            return result

        except Exception as e:
            logger.error(f"Regime detection failed: {e}")
            return self._default_regime(f"Detection error: {str(e)[:80]}")

    def _classify(
        self,
        btc_trend: str,
        ema_dist: float,
        atr_pct: float,
        bb_width: float,
        volume_ratio: float,
        expanding: bool,
        bb_expanding: bool,
    ) -> tuple[str, int, str]:
        """Classify market into one of 6 regimes."""

        # 1. HIGH VOLATILITY — extreme ATR or massive BB expansion
        if atr_pct > 3.0 or (bb_width > 8.0 and volume_ratio > 2.0):
            return "HIGH_VOLATILITY", 85, f"Extreme volatility: ATR%={atr_pct:.2f}, BB={bb_width:.1f}"

        # 2. DEAD MARKET — tiny ATR, low volume, narrow BB
        if atr_pct < 0.3 and volume_ratio < 0.6 and bb_width < 1.5:
            return "DEAD_MARKET", 80, f"Dead market: ATR%={atr_pct:.2f}, vol={volume_ratio:.1f}x"

        # 3. BREAKOUT EXPANSION — EMAs expanding + BB expanding + volume surge
        if expanding and bb_expanding and volume_ratio > 1.3:
            return "BREAKOUT_EXPANSION", 75, f"Breakout expansion: vol={volume_ratio:.1f}x, BB expanding"

        # 4. TRENDING BULL — clear uptrend with decent separation
        if btc_trend == "BULLISH" and ema_dist > 0.3:
            conf = min(90, 60 + int(ema_dist * 20))
            return "TRENDING_BULL", conf, f"Bullish trend: EMA dist={ema_dist:.3f}%"

        # 5. TRENDING BEAR — clear downtrend
        if btc_trend == "BEARISH" and ema_dist > 0.3:
            conf = min(90, 60 + int(ema_dist * 20))
            return "TRENDING_BEAR", conf, f"Bearish trend: EMA dist={ema_dist:.3f}%"

        # 6. SIDEWAYS RANGE — EMAs close, moderate volatility
        # V17: Allow more trade types in SIDEWAYS (was too restrictive)
        return "SIDEWAYS_RANGE", 70, f"Sideways range: EMA dist={ema_dist:.3f}%, ATR%={atr_pct:.2f}"

    def _get_strategy_weights(self, regime: str) -> dict:
        """Return strategy priority weights for the current regime."""
        weights = {
            "TRENDING_BULL": {
                "scalp_trend_pullback": 1.0,
                "scalp_breakout": 0.7,
                "scalp_range_reversal": 0.2,
                "swing": 1.0,
                "sniper": 0.5,
            },
            "TRENDING_BEAR": {
                "scalp_trend_pullback": 1.0,
                "scalp_breakout": 0.7,
                "scalp_range_reversal": 0.2,
                "swing": 0.8,
                "sniper": 0.5,
            },
            "SIDEWAYS_RANGE": {
                "scalp_trend_pullback": 0.7,   # V17: raised from 0.3 — trend plays valid in range
                "scalp_breakout": 0.5,          # V17: raised from 0.3
                "scalp_range_reversal": 1.0,
                "swing": 0.6,                   # V17: raised from 0.4
                "sniper": 0.4,                  # V17: raised from 0.3
            },
            "BREAKOUT_EXPANSION": {
                "scalp_trend_pullback": 0.5,
                "scalp_breakout": 1.0,
                "scalp_range_reversal": 0.1,
                "swing": 0.6,
                "sniper": 1.0,
            },
            "HIGH_VOLATILITY": {
                "scalp_trend_pullback": 0.2,
                "scalp_breakout": 0.3,
                "scalp_range_reversal": 0.1,
                "swing": 0.1,
                "sniper": 0.8,
            },
            "DEAD_MARKET": {
                "scalp_trend_pullback": 0.1,
                "scalp_breakout": 0.1,
                "scalp_range_reversal": 0.2,
                "swing": 0.3,
                "sniper": 0.1,
            },
        }
        return weights.get(regime, weights["SIDEWAYS_RANGE"])

    def _default_regime(self, reason: str) -> MarketRegime:
        """Fallback regime when detection fails."""
        return MarketRegime(
            regime="SIDEWAYS_RANGE",
            confidence=30,
            btc_trend="NEUTRAL",
            btc_atr_pct=0.0,
            btc_bb_width=0.0,
            btc_ema_dist=0.0,
            btc_volume_ratio=1.0,
            strategy_weights=self._get_strategy_weights("SIDEWAYS_RANGE"),
            description=f"Fallback regime: {reason}",
        )

    def get_size_multiplier(self, regime: str) -> float:
        """Regime-based position size adjustment."""
        multipliers = {
            "TRENDING_BULL": 1.0,
            "TRENDING_BEAR": 0.9,
            "SIDEWAYS_RANGE": 0.8,
            "BREAKOUT_EXPANSION": 0.9,
            "HIGH_VOLATILITY": 0.5,
            "DEAD_MARKET": 0.3,
        }
        return multipliers.get(regime, 0.7)

    def should_trade(self, regime: str) -> bool:
        """Check if trading is recommended in this regime."""
        return regime != "DEAD_MARKET"

    def to_dict(self, regime: MarketRegime) -> dict:
        return {
            "regime": regime.regime,
            "confidence": regime.confidence,
            "btc_trend": regime.btc_trend,
            "btc_atr_pct": regime.btc_atr_pct,
            "btc_bb_width": regime.btc_bb_width,
            "btc_ema_dist": regime.btc_ema_dist,
            "btc_volume_ratio": regime.btc_volume_ratio,
            "strategy_weights": regime.strategy_weights,
            "description": regime.description,
        }

    # ─── V5.5: Engine Conflict Resolver ──────────────────────────────

    def resolve_conflicts(
        self,
        regime: str,
        scalp_signals: list[dict],
        swing_signals: list[dict],
        sniper_signals: list[dict],
    ) -> list[dict]:
        """
        V5.5: Master conflict resolver. If engines disagree on direction
        for the same symbol, block that symbol.

        Priority by regime:
          TRENDING      → Swing > Scalp trend > skip others
          BREAKOUT      → Sniper > Breakout scalp > skip range
          RANGE         → Range scalp ONLY
          HIGH_VOL      → Sniper only (if confirmed)
          DEAD_MARKET   → Nothing
        """
        if regime == "DEAD_MARKET":
            logger.info("  🚫 Conflict resolver: DEAD_MARKET — blocking all trades")
            return []

        # Build direction map per symbol: symbol -> {"BUY": [engines], "SELL": [engines]}
        direction_map = {}
        all_signals = []

        for sig in scalp_signals:
            sym = sig.get("symbol", "")
            action = sig.get("action", "HOLD")
            if action in ("BUY", "SELL") and sig.get("confidence", 0) >= 70:
                direction_map.setdefault(sym, {"BUY": [], "SELL": []})
                direction_map[sym][action].append("scalp")
                all_signals.append(sig)

        for sig in swing_signals:
            sym = sig.get("symbol", "")
            action = sig.get("action", "HOLD")
            if action in ("BUY", "SELL") and sig.get("confidence", 0) >= 70:
                direction_map.setdefault(sym, {"BUY": [], "SELL": []})
                direction_map[sym][action].append("swing")
                all_signals.append(sig)

        for sig in sniper_signals:
            sym = sig.get("symbol", "")
            action = sig.get("action", "HOLD")
            if action in ("BUY", "SELL") and sig.get("confidence", 0) >= 70:
                direction_map.setdefault(sym, {"BUY": [], "SELL": []})
                direction_map[sym][action].append("sniper")
                all_signals.append(sig)

        # Detect conflicts
        blocked_symbols = set()
        for sym, dirs in direction_map.items():
            has_buy = len(dirs["BUY"]) > 0
            has_sell = len(dirs["SELL"]) > 0
            if has_buy and has_sell:
                logger.info(
                    f"  ⚠️ Conflict: {sym} BUY={dirs['BUY']} vs SELL={dirs['SELL']} — BLOCKING"
                )
                blocked_symbols.add(sym)

        # Apply regime-specific filtering
        allowed = []
        for sig in all_signals:
            sym = sig.get("symbol", "")
            strategy = sig.get("strategy_type", "")

            if sym in blocked_symbols:
                continue

            # HIGH_VOL: only allow sniper
            if regime == "HIGH_VOLATILITY" and not strategy.startswith("sniper"):
                continue

            # SIDEWAYS: only block pure breakout scalp — range+trend scalps still valid
            # V12: removed over-restriction that blocked ALL scalps in sideways
            if regime == "SIDEWAYS_RANGE" and not strategy.startswith("swing"):
                if strategy in ("breakout_momentum", "scalp_breakout"):  # Only block breakout
                    logger.debug(f"  Sideways: blocking breakout scalp {sym}")
                    continue
                # range_reversal, trend_pullback, and all other scalps = ALLOWED

            allowed.append(sig)

        if blocked_symbols:
            logger.info(f"  🚫 Blocked {len(blocked_symbols)} conflicting symbols: {blocked_symbols}")

        return allowed

    # ─── V5.5: Session Activity Filter ───────────────────────────────

    @staticmethod
    def get_session_multiplier() -> float:
        """
        V5.5: Returns confidence multiplier based on current trading session.
        London/NY = full priority, dead hours = reduced.
        """
        now_utc = datetime.now(timezone.utc)
        hour = now_utc.hour

        # London + NY overlap (13:00-16:00 UTC) = best liquidity
        if 13 <= hour < 16:
            return 1.1  # Slight boost
        # London session (08:00-16:00 UTC)
        elif 8 <= hour < 16:
            return 1.0
        # New York session (13:00-21:00 UTC)
        elif 13 <= hour < 21:
            return 1.0
        # Asia session (00:00-08:00 UTC) — floor raised to 0.92 to prevent confidence kills
        elif 0 <= hour < 8:
            return 0.92
        # Dead hours (21:00-00:00 UTC) — floor raised to 0.90
        else:
            return 0.90
