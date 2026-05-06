"""
V4 Configuration — Multi-Account Scalping Bot
All settings loaded from environment variables with safe defaults.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # ── Database ──────────────────────────────────────────────────────
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://botuser:botpass@localhost:5432/trading_bot",
    )

    # ── Binance (Master account — used for scanning) ─────────────────
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")
    BINANCE_TESTNET: bool = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

    # ── OpenAI ───────────────────────────────────────────────────────
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # ── Encryption ───────────────────────────────────────────────────
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    # ── Admin Account ────────────────────────────────────────────────
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # ── JWT Authentication ───────────────────────────────────────────
    # REQUIRED: No default — must be set in .env for security
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))  # 8 hours

    # ── Telegram ─────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Trading parameters ───────────────────────────────────────────
    MIN_CONFIDENCE: int = int(os.getenv("MIN_CONFIDENCE", "60"))  # V12: lowered from 70 to 60 — allows scalp grades C/D

    # ── Scanner filters ──────────────────────────────────────────────
    MIN_VOLUME_24H: float = 3_000_000.0
    MIN_PRICE_CHANGE: float = 1.0
    MAX_SPREAD_PCT: float = 0.15       # V3: tightened from 0.3
    MIN_PRICE: float = 0.0001
    MAX_PRICE: float = 100000.0
    EXCLUDED_COINS: list = None

    # ── Trade limits ─────────────────────────────────────────────────
    DAILY_MAX_TRADES: int = int(os.getenv("DAILY_MAX_TRADES", "100"))
    HOURLY_MAX_TRADES: int = int(os.getenv("HOURLY_MAX_TRADES", "10"))
    COIN_COOLDOWN_MINUTES: int = int(os.getenv("COIN_COOLDOWN_MINUTES", "30"))
    MAX_COIN_REPEATS_PER_HOUR: int = int(os.getenv("MAX_COIN_REPEATS_PER_HOUR", "2"))

    # ── Risk controls ────────────────────────────────────────────────
    MAX_VOLATILITY_PCT: float = float(os.getenv("MAX_VOLATILITY_PCT", "5.0"))
    # V7: Updated daily PnL limits (user config: +4%/+6%/-3%)
    DAILY_PROFIT_LIMIT_PCT: float = float(os.getenv("DAILY_PROFIT_LIMIT_PCT", "6.0"))   # V7: hard stop (was 7%)
    DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "-3.0"))      # V7: max loss (was -8%)
    LOSS_COOLDOWN_COUNT: int = int(os.getenv("LOSS_COOLDOWN_COUNT", "3"))
    LOSS_COOLDOWN_MINUTES: int = int(os.getenv("LOSS_COOLDOWN_MINUTES", "15"))
    DRAWDOWN_PAUSE_PCT: float = float(os.getenv("DRAWDOWN_PAUSE_PCT", "-5.0"))          # V7: tighter (was -10%)

    # ── V7 Daily Guard (per-account) ─────────────────────────────────
    DAILY_SAFE_MODE_PCT: float = float(os.getenv("DAILY_SAFE_MODE_PCT", "4.0"))         # V7: lock at +4% (was +5%)
    DAILY_LOSS_REDUCE_PCT: float = float(os.getenv("DAILY_LOSS_REDUCE_PCT", "2.0"))     # V7: reduce size at -2%
    CONSECUTIVE_LOSS_REDUCE_THRESHOLD: int = int(os.getenv("CONSECUTIVE_LOSS_REDUCE_THRESHOLD", "2"))
    CONSECUTIVE_LOSS_PAUSE_THRESHOLD: int = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_THRESHOLD", "3"))  # V7: pause at 3 (was 4)
    CONSECUTIVE_LOSS_PAUSE_MINUTES: int = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_MINUTES", "60"))

    # ── V3 Pre-entry checks ──────────────────────────────────────────
    # V12: relaxed from 0.10% to 0.20% — scalps on volatile coins need wider spread tolerance
    MAX_SPREAD_ENTRY_PCT: float = float(os.getenv("MAX_SPREAD_ENTRY_PCT", "0.20"))

    # ── V4 Order execution ───────────────────────────────────────────
    LIMIT_ORDER_WAIT_SECONDS: int = int(os.getenv("LIMIT_ORDER_WAIT_SECONDS", "3"))
    MIN_POSITION_USDT: float = float(os.getenv("MIN_POSITION_USDT", "6.0"))
    ENABLE_LIMIT_FALLBACK: bool = os.getenv("ENABLE_LIMIT_FALLBACK", "true").lower() == "true"

    # V12: raised from 3 to 10 — prevents scalps being cut off when swing takes the slots
    MAX_TRADES_PER_CYCLE: int = int(os.getenv("MAX_TRADES_PER_CYCLE", "10"))


    # ── V5 Scalp TP/SL (tighter for scalping) ───────────────────────
    SCALP_TP_PCT: float = float(os.getenv("SCALP_TP_PCT", "2.0"))
    SCALP_SL_PCT: float = float(os.getenv("SCALP_SL_PCT", "1.0"))

    # ── V5 Swing TP/SL ──────────────────────────────────────────────
    SWING_TP_PCT: float = float(os.getenv("SWING_TP_PCT", "8.0"))
    SWING_SL_PCT: float = float(os.getenv("SWING_SL_PCT", "3.0"))
    SWING_MIN_CONFIDENCE: int = int(os.getenv("SWING_MIN_CONFIDENCE", "80"))
    SWING_EXECUTE_CONFIDENCE: int = int(os.getenv("SWING_EXECUTE_CONFIDENCE", "85"))
    SWING_WATCHLIST_MAX: int = int(os.getenv("SWING_WATCHLIST_MAX", "20"))
    SWING_EXPIRY_HOURS: int = int(os.getenv("SWING_EXPIRY_HOURS", "72"))

    # ── V5 Sniper TP/SL ─────────────────────────────────────────────
    SNIPER_TP_PCT: float = float(os.getenv("SNIPER_TP_PCT", "3.0"))
    SNIPER_SL_PCT: float = float(os.getenv("SNIPER_SL_PCT", "1.5"))
    SNIPER_ENABLED: bool = os.getenv("SNIPER_ENABLED", "true").lower() == "true"

    # ── V5.5 Partial Take Profit ─────────────────────────────────────
    PARTIAL_TP_ENABLED: bool = os.getenv("PARTIAL_TP_ENABLED", "true").lower() == "true"
    PARTIAL_TP1_PCT: float = float(os.getenv("PARTIAL_TP1_PCT", "0.40"))   # 40% of position at TP1
    PARTIAL_TP2_PCT: float = float(os.getenv("PARTIAL_TP2_PCT", "0.30"))   # 30% of position at TP2
    PARTIAL_TRAIL_PCT: float = float(os.getenv("PARTIAL_TRAIL_PCT", "0.30"))  # 30% trails
    PARTIAL_TP1_DISTANCE: float = float(os.getenv("PARTIAL_TP1_DISTANCE", "0.50"))  # TP1 at 50% of full TP distance
    PARTIAL_TP_MIN_CONFIDENCE: int = int(os.getenv("PARTIAL_TP_MIN_CONFIDENCE", "85"))  # Only for strong setups

    # ── V5.5 Break-Even Stop ─────────────────────────────────────────
    BREAK_EVEN_ENABLED: bool = os.getenv("BREAK_EVEN_ENABLED", "true").lower() == "true"
    BREAK_EVEN_TRIGGER_PCT: float = float(os.getenv("BREAK_EVEN_TRIGGER_PCT", "3.0"))  # Move SL to BE at +3% ROI
    BREAK_EVEN_BUFFER_PCT: float = float(os.getenv("BREAK_EVEN_BUFFER_PCT", "0.1"))   # Small buffer above entry

    # ── V5 External APIs (free) ──────────────────────────────────────
    CRYPTOPANIC_API_KEY: str = os.getenv("CRYPTOPANIC_API_KEY", "")

    # ── V7 Settings ──────────────────────────────────────────────────────
    V7_MAX_LEVERAGE: int = int(os.getenv("V7_MAX_LEVERAGE", "7"))             # Max leverage cap
    V7_PER_COIN_COOLDOWN_LOSSES: int = int(os.getenv("V7_PER_COIN_COOLDOWN_LOSSES", "3"))
    V7_PER_COIN_COOLDOWN_HOURS: int = int(os.getenv("V7_PER_COIN_COOLDOWN_HOURS", "48"))
    V7_PER_COIN_COOLDOWN_DAYS: int = int(os.getenv("V7_PER_COIN_COOLDOWN_DAYS", "7"))
    # V7: Confidence thresholds
    V7_MIN_CONFIDENCE: int = int(os.getenv("V7_MIN_CONFIDENCE", "60"))        # Minimum to trade (weighted score)
    V7_CONFIDENCE_ELITE_THRESHOLD: int = int(os.getenv("V7_CONFIDENCE_ELITE_THRESHOLD", "90"))  # Elite tier
    # V7: Dynamic leverage tiers (mapped to confidence)
    V7_LEVERAGE_LOW: int = int(os.getenv("V7_LEVERAGE_LOW", "3"))             # Confidence 60-69 → 3x
    V7_LEVERAGE_MID: int = int(os.getenv("V7_LEVERAGE_MID", "5"))             # Confidence 70-84 → 5x
    V7_LEVERAGE_HIGH: int = int(os.getenv("V7_LEVERAGE_HIGH", "7"))           # Confidence 85+ → 7x
    # V7: Daily profit lock
    DAILY_PROFIT_LOCK_PCT: float = float(os.getenv("DAILY_PROFIT_LOCK_PCT", "3.0"))  # Lock gains at +3%

    # ── V10 Split Confidence System ────────────────────────────────────
    # V17: Lowered scalp from 75->70, swing from 80->72
    SCALP_MIN_CONFIDENCE: int = int(os.getenv("SCALP_MIN_CONFIDENCE", "70"))           # V17: was 75
    SCALP_WATCHLIST_CONFIDENCE: int = int(os.getenv("SCALP_WATCHLIST_CONFIDENCE", "55"))  # unchanged
    # Swing confidence gates
    SWING_MIN_CONFIDENCE_EXECUTE: int = int(os.getenv("SWING_MIN_CONFIDENCE_EXECUTE", "72"))  # V17: was 80
    SWING_WATCHLIST_CONFIDENCE: int = int(os.getenv("SWING_WATCHLIST_CONFIDENCE", "62"))      # V17: was 70

    # ── V10 Trigger Zone Tolerance ───────────────────────────────────────
    # Prevents missing near-price entries due to tick-level mismatch
    SCALP_TRIGGER_TOLERANCE: float = float(os.getenv("SCALP_TRIGGER_TOLERANCE", "0.002"))  # 0.2%
    SWING_TRIGGER_TOLERANCE: float = float(os.getenv("SWING_TRIGGER_TOLERANCE", "0.005"))  # 0.5%

    # ── V10 Concurrent Trade Limits ──────────────────────────────────────
    # V12: Set to 99999 — effectively unlimited. Binance live positions are the
    # truth source. DB counts are NOT used to block entries.
    MAX_CONCURRENT_SCALP_TRADES: int = int(os.getenv("MAX_CONCURRENT_SCALP_TRADES", "99999"))
    MAX_CONCURRENT_SWING_TRADES: int = int(os.getenv("MAX_CONCURRENT_SWING_TRADES", "99999"))

    # ── V10 Per-Coin Post-Close Cooldown ─────────────────────────────────
    # Cooldown applied after a position is CLOSED (not just opened)
    SCALP_CLOSE_COOLDOWN_MINUTES: int = int(os.getenv("SCALP_CLOSE_COOLDOWN_MINUTES", "10"))
    SWING_CLOSE_COOLDOWN_MINUTES: int = int(os.getenv("SWING_CLOSE_COOLDOWN_MINUTES", "60"))

    # ── V10 Position Manager Control ─────────────────────────────────────
    # Set PYTHON_PM_ENABLED=false once n8n PM is stable to disable Python PM
    PYTHON_PM_ENABLED: bool = os.getenv("PYTHON_PM_ENABLED", "true").lower() == "true"

    # ── V11 Smart Position Limits ─────────────────────────────────────────
    # V12: All position caps set to 99999 (unlimited).
    # Entry decisions use Binance LIVE positions, NOT DB counts.
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "99999"))
    # Block new entries if daily loss hits this %  (e.g. -8.0 = -8%)
    V11_DAILY_LOSS_GATE_PCT: float = float(os.getenv("V11_DAILY_LOSS_GATE_PCT", "-8.0"))
    # Lock new entries when daily profit reaches this % (e.g. 12.0 = +12%)
    V11_DAILY_PROFIT_LOCK_PCT: float = float(os.getenv("V11_DAILY_PROFIT_LOCK_PCT", "12.0"))
    # Block same symbol if already open: check Binance live, not DB
    MAX_SAME_SYMBOL_OPEN: int = int(os.getenv("MAX_SAME_SYMBOL_OPEN", "1"))

    # ── V12 Binance Truth Source ──────────────────────────────────────────
    # When True: use Binance live positions as truth for entry decisions.
    # DB open_positions is treated as mirror / analytics only.
    BINANCE_TRUTH_SOURCE: bool = os.getenv("BINANCE_TRUTH_SOURCE", "true").lower() == "true"
    # Auto-sync interval in seconds (PM syncs Binance → DB every N seconds)
    BINANCE_SYNC_INTERVAL: int = int(os.getenv("BINANCE_SYNC_INTERVAL", "60"))
    # If a DB trade is open but missing from Binance → mark as closed with reason
    BINANCE_GHOST_CLOSE_REASON: str = os.getenv("BINANCE_GHOST_CLOSE_REASON", "externally_closed")
    # Create DB records for Binance positions that don't exist in DB
    BINANCE_CREATE_MISSING_RECORDS: bool = os.getenv("BINANCE_CREATE_MISSING_RECORDS", "true").lower() == "true"

    # ── V11 Scalp TP/SL targets ──────────────────────────────────────────
    # Overrides SCALP_TP_PCT / SCALP_SL_PCT for the new scalp engine
    V11_SCALP_TP_MIN_PCT: float = float(os.getenv("V11_SCALP_TP_MIN_PCT", "4.0"))   # min TP%
    V11_SCALP_TP_MAX_PCT: float = float(os.getenv("V11_SCALP_TP_MAX_PCT", "10.0"))  # max TP%
    V11_SCALP_SL_MIN_PCT: float = float(os.getenv("V11_SCALP_SL_MIN_PCT", "2.0"))   # min SL%
    V11_SCALP_SL_MAX_PCT: float = float(os.getenv("V11_SCALP_SL_MAX_PCT", "4.0"))   # max SL%

    # ── V11 Stale Trade Detection ─────────────────────────────────────────
    # After this many hours with no TP/SL hit, trade is flagged as stale
    STALE_CLOSE_ENABLED: bool = os.getenv("STALE_CLOSE_ENABLED", "false").lower() == "true"
    SCALP_STALE_HOURS: int = int(os.getenv("SCALP_STALE_HOURS", "4"))
    SWING_STALE_HOURS: int = int(os.getenv("SWING_STALE_HOURS", "72"))

    # ── V11 Position Manager Reliability ─────────────────────────────────
    # Check candle hi/lo for intracandle TP every N position-manager ticks
    PM_CANDLE_CHECK_TICKS: int = int(os.getenv("PM_CANDLE_CHECK_TICKS", "30"))
    # Max close retry attempts before alerting manual action required
    PM_MAX_CLOSE_RETRIES: int = int(os.getenv("PM_MAX_CLOSE_RETRIES", "3"))
    # Seconds between retry attempts
    PM_CLOSE_RETRY_DELAY: int = int(os.getenv("PM_CLOSE_RETRY_DELAY", "5"))
    # Orphan sync: compare DB open_positions vs Binance on startup
    PM_ORPHAN_SYNC_ENABLED: bool = os.getenv("PM_ORPHAN_SYNC_ENABLED", "true").lower() == "true"

    # ── V13 Per-Mode Confidence Gates ────────────────────────────────────
    # V17: Lowered thresholds for all modes
    V13_SCALP_MIN_CONFIDENCE: int = int(os.getenv("V13_SCALP_MIN_CONFIDENCE", "68"))   # V17: was 72
    V13_SWING_MIN_CONFIDENCE: int = int(os.getenv("V13_SWING_MIN_CONFIDENCE", "70"))   # V17: was 75
    V13_SNIPER_MIN_CONFIDENCE: int = int(os.getenv("V13_SNIPER_MIN_CONFIDENCE", "78"))  # V17: was 82

    # ── V13 Leverage Caps (per mode) ─────────────────────────────────────
    V13_SCALP_LEVERAGE_MAX: int = int(os.getenv("V13_SCALP_LEVERAGE_MAX", "15"))
    V13_SWING_LEVERAGE_MAX: int = int(os.getenv("V13_SWING_LEVERAGE_MAX", "12"))
    V13_SNIPER_LEVERAGE_MAX: int = int(os.getenv("V13_SNIPER_LEVERAGE_MAX", "15"))
    # Reduce leverage if ATR% exceeds this threshold
    V13_VOLATILE_ATR_THRESHOLD: float = float(os.getenv("V13_VOLATILE_ATR_THRESHOLD", "3.0"))
    V13_VOLATILE_LEVERAGE_CAP: int = int(os.getenv("V13_VOLATILE_LEVERAGE_CAP", "10"))

    # ── V13 Balance-Tier Margin % (Patch 1: better <$30 sizing) ──────────
    # Balance < $10: hard skip (MIN_TRADE_BALANCE)
    V13_MIN_TRADE_BALANCE: float = float(os.getenv("V13_MIN_TRADE_BALANCE", "10.0"))
    # $10-$30:  15% margin, max $4
    V13_MARGIN_UNDER30_PCT: float = float(os.getenv("V13_MARGIN_UNDER30_PCT", "15.0"))
    V13_MARGIN_UNDER30_MAX: float = float(os.getenv("V13_MARGIN_UNDER30_MAX", "4.0"))
    # $30-$50:  12% margin, max $6
    V13_MARGIN_30_50_PCT: float = float(os.getenv("V13_MARGIN_30_50_PCT", "12.0"))
    V13_MARGIN_30_50_MAX: float = float(os.getenv("V13_MARGIN_30_50_MAX", "6.0"))
    # $50-$200: 8% margin
    V13_MARGIN_50_200_PCT: float = float(os.getenv("V13_MARGIN_50_200_PCT", "8.0"))
    # $200-$1000: 5% margin
    V13_MARGIN_200_1000_PCT: float = float(os.getenv("V13_MARGIN_200_1000_PCT", "5.0"))
    # $1000+: 5% margin
    V13_MARGIN_OVER1000_PCT: float = float(os.getenv("V13_MARGIN_OVER1000_PCT", "5.0"))

    # ── V13 Confidence Boost (Patch 5: explicit caps) ─────────────────────
    # Grade C (72-84): no boost — base only
    # Grade B / Strong (85-88): +2% margin, hard capped at tier_max + 2%
    V13_BOOST_STRONG_ADD_PCT: float = float(os.getenv("V13_BOOST_STRONG_ADD_PCT", "2.0"))
    # Grade A / Elite (89+): +3% margin, hard capped at tier_max + 3%
    V13_BOOST_ELITE_ADD_PCT: float = float(os.getenv("V13_BOOST_ELITE_ADD_PCT", "3.0"))
    # Absolute hard cap: no single trade uses more than this % of balance regardless of boosts
    V13_MARGIN_ABSOLUTE_CAP_PCT: float = float(os.getenv("V13_MARGIN_ABSOLUTE_CAP_PCT", "15.0"))

    # ── V13 TP/SL (ROI %, Patch 2: dynamic scalp TP by confidence) ───────
    # Scalp TP scales with confidence:
    V13_SCALP_TP_ROI_72: float = float(os.getenv("V13_SCALP_TP_ROI_72", "15.0"))   # conf 72-76
    V13_SCALP_TP_ROI_77: float = float(os.getenv("V13_SCALP_TP_ROI_77", "18.0"))   # conf 77-82
    V13_SCALP_TP_ROI_83: float = float(os.getenv("V13_SCALP_TP_ROI_83", "20.0"))   # conf 83-88
    V13_SCALP_TP_ROI_89: float = float(os.getenv("V13_SCALP_TP_ROI_89", "22.0"))   # conf 89+
    V13_SCALP_SL_ROI: float = float(os.getenv("V13_SCALP_SL_ROI", "9.0"))          # fixed
    # Swing / Sniper (fixed ROI targets)
    V13_SWING_TP_ROI: float = float(os.getenv("V13_SWING_TP_ROI", "35.0"))
    V13_SWING_SL_ROI: float = float(os.getenv("V13_SWING_SL_ROI", "12.0"))
    V13_SNIPER_TP_ROI: float = float(os.getenv("V13_SNIPER_TP_ROI", "50.0"))
    V13_SNIPER_SL_ROI: float = float(os.getenv("V13_SNIPER_SL_ROI", "15.0"))

    # ── V13 Break-Even ROI Triggers (per mode) ────────────────────────────
    V13_SCALP_BE_TRIGGER_ROI: float = float(os.getenv("V13_SCALP_BE_TRIGGER_ROI", "10.0"))
    V13_SWING_BE_TRIGGER_ROI: float = float(os.getenv("V13_SWING_BE_TRIGGER_ROI", "18.0"))
    V13_SNIPER_BE_TRIGGER_ROI: float = float(os.getenv("V13_SNIPER_BE_TRIGGER_ROI", "25.0"))
    # Fee buffer added to breakeven price to cover round-trip fees
    V13_FEE_BUFFER_PCT: float = float(os.getenv("V13_FEE_BUFFER_PCT", "0.12"))   # 0.12% round-trip

    # ── V13 Anti-Reverse Momentum Exit (Part 5) ───────────────────────────
    V13_MOMENTUM_EXIT_ENABLED: bool = os.getenv("V13_MOMENTUM_EXIT_ENABLED", "true").lower() == "true"
    # Minimum ROI that must have been achieved before momentum check activates
    V13_MOMENTUM_MIN_PEAK_ROI: float = float(os.getenv("V13_MOMENTUM_MIN_PEAK_ROI", "5.0"))
    # Exit if ROI retraces more than this % from peak
    V13_MOMENTUM_RETRACE_PCT: float = float(os.getenv("V13_MOMENTUM_RETRACE_PCT", "40.0"))
    # RSI threshold: exit long if RSI drops below this while in profit
    V13_MOMENTUM_RSI_EXIT_LONG: float = float(os.getenv("V13_MOMENTUM_RSI_EXIT_LONG", "45.0"))
    V13_MOMENTUM_RSI_EXIT_SHORT: float = float(os.getenv("V13_MOMENTUM_RSI_EXIT_SHORT", "55.0"))

    # ── V13 Fee/Slippage Profitability Filter (Patch 4) ──────────────────
    V13_FEE_FILTER_ENABLED: bool = os.getenv("V13_FEE_FILTER_ENABLED", "true").lower() == "true"
    V13_TAKER_FEE_PCT: float = float(os.getenv("V13_TAKER_FEE_PCT", "0.04"))       # 0.04% per side
    V13_SLIPPAGE_EST_PCT: float = float(os.getenv("V13_SLIPPAGE_EST_PCT", "0.05")) # estimated slippage
    # Skip trade if net ROI after fees < this value
    V13_MIN_NET_ROI_AFTER_FEES: float = float(os.getenv("V13_MIN_NET_ROI_AFTER_FEES", "3.0"))

    # ── V17 Engine Calibration Settings ───────────────────────────────────
    # Volume spike adaptive threshold
    V17_VOLUME_SPIKE_THRESHOLD: float = float(os.getenv("V17_VOLUME_SPIKE_THRESHOLD", "1.2"))    # base ratio (was 1.5)
    V17_VOLUME_ACCELERATION_PCT: float = float(os.getenv("V17_VOLUME_ACCELERATION_PCT", "1.1"))  # acceleration multiplier
    # Signal cooldown (suppress same symbol re-signal within N seconds)
    V17_SIGNAL_COOLDOWN_S: int = int(os.getenv("V17_SIGNAL_COOLDOWN_S", "1200"))   # 20 minutes
    V17_SIGNAL_COOLDOWN_CONF_IMPROVE: int = int(os.getenv("V17_SIGNAL_COOLDOWN_CONF_IMPROVE", "10"))  # override if +10pts
    # Watchlist spam suppression (same symbol+side)
    V17_WATCHLIST_COOLDOWN_S: int = int(os.getenv("V17_WATCHLIST_COOLDOWN_S", "1800"))  # 30 minutes
    # Trigger activation momentum bypass
    V17_TRIGGER_BYPASS_PCT: float = float(os.getenv("V17_TRIGGER_BYPASS_PCT", "0.5"))   # within 0.5% of zone
    # BTC bias multiplier (both directions)
    V17_BTC_AGAINST_MULT: float = float(os.getenv("V17_BTC_AGAINST_MULT", "0.85"))     # was 0.75
    # Confidence floor (absolute minimum to emit any signal)
    V17_CONFIDENCE_FLOOR: int = int(os.getenv("V17_CONFIDENCE_FLOOR", "58"))           # was 60
    # Signal tracker capacity
    V17_MAX_ACTIVE_SIGNALS: int = int(os.getenv("V17_MAX_ACTIVE_SIGNALS", "15"))       # was 5
    V17_OPPOSITE_MIN_CONFIDENCE: int = int(os.getenv("V17_OPPOSITE_MIN_CONFIDENCE", "70"))  # was 80

    # ── V13 Learning Engine (Patch 3) ────────────────────────────────────
    V13_LEARNING_TRADE_INTERVAL: int = int(os.getenv("V13_LEARNING_TRADE_INTERVAL", "20"))  # every 20 trades
    V13_LEARNING_WEEKLY_ENABLED: bool = os.getenv("V13_LEARNING_WEEKLY_ENABLED", "true").lower() == "true"
    V13_LEARNING_AUTO_DISABLE_WEIGHT: float = float(os.getenv("V13_LEARNING_AUTO_DISABLE_WEIGHT", "0.3"))

    def __post_init__(self):
        self.EXCLUDED_COINS = []

    @property
    def binance_base_url(self) -> str:
        if self.BINANCE_TESTNET:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"


settings = Settings()
