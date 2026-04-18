"""
V3 Configuration — Multi-Account Scalping Bot
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

    # ── Telegram ─────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Trading parameters ───────────────────────────────────────────
    MIN_CONFIDENCE: int = int(os.getenv("MIN_CONFIDENCE", "70"))

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
    DAILY_PROFIT_LIMIT_PCT: float = float(os.getenv("DAILY_PROFIT_LIMIT_PCT", "7.0"))
    DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "-8.0"))
    LOSS_COOLDOWN_COUNT: int = int(os.getenv("LOSS_COOLDOWN_COUNT", "3"))
    LOSS_COOLDOWN_MINUTES: int = int(os.getenv("LOSS_COOLDOWN_MINUTES", "15"))
    DRAWDOWN_PAUSE_PCT: float = float(os.getenv("DRAWDOWN_PAUSE_PCT", "-10.0"))

    # ── V3 Daily Guard (per-account) ─────────────────────────────────
    DAILY_SAFE_MODE_PCT: float = float(os.getenv("DAILY_SAFE_MODE_PCT", "5.0"))
    DAILY_LOSS_REDUCE_PCT: float = float(os.getenv("DAILY_LOSS_REDUCE_PCT", "5.0"))
    CONSECUTIVE_LOSS_REDUCE_THRESHOLD: int = int(os.getenv("CONSECUTIVE_LOSS_REDUCE_THRESHOLD", "2"))
    CONSECUTIVE_LOSS_PAUSE_THRESHOLD: int = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_THRESHOLD", "4"))
    CONSECUTIVE_LOSS_PAUSE_MINUTES: int = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_MINUTES", "60"))

    # ── V3 Pre-entry checks ──────────────────────────────────────────
    MAX_SPREAD_ENTRY_PCT: float = float(os.getenv("MAX_SPREAD_ENTRY_PCT", "0.10"))

    def __post_init__(self):
        self.EXCLUDED_COINS = []

    @property
    def binance_base_url(self) -> str:
        if self.BINANCE_TESTNET:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"


settings = Settings()
