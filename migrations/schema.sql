-- ═══════════════════════════════════════════════════════════════════════
-- V2 Multi-Account Crypto Scalping Bot — PostgreSQL Schema
-- Run: psql -U botuser -d trading_bot -f schema.sql
-- ═══════════════════════════════════════════════════════════════════════

-- ── Users ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    username VARCHAR(100) UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ── Accounts ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label VARCHAR(100) NOT NULL DEFAULT 'Default',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id);

-- ── API Connections (encrypted keys) ────────────────────────────────
CREATE TABLE IF NOT EXISTS api_connections (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    exchange VARCHAR(50) NOT NULL DEFAULT 'binance',
    api_key_encrypted TEXT NOT NULL,
    api_secret_encrypted TEXT NOT NULL,
    permissions VARCHAR(255) NOT NULL DEFAULT 'futures_only',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_verified_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ── Balances ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS balances (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    balance_usdt DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    available_balance DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    total_margin_used DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ── Signals ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,
    confidence INTEGER NOT NULL,
    reason TEXT,
    indicators_json JSONB,
    ai_response_json JSONB,
    ai_called BOOLEAN DEFAULT FALSE,
    ai_tokens_used INTEGER DEFAULT 0,
    ai_model VARCHAR(50),
    ai_latency_ms INTEGER DEFAULT 0,
    ai_fallback BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

-- ── Trades ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    position_size_usdt DOUBLE PRECISION NOT NULL,
    leverage INTEGER NOT NULL,
    take_profit DOUBLE PRECISION,
    stop_loss DOUBLE PRECISION,
    risk_pct DOUBLE PRECISION,
    confidence INTEGER,
    order_id VARCHAR(50),
    sl_order_id VARCHAR(50),
    tp_order_id VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    close_price DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION,
    close_reason VARCHAR(50),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);

-- ── Positions ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    leverage INTEGER NOT NULL,
    unrealized_pnl DOUBLE PRECISION DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_positions_account ON positions(account_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

-- ── Trade Skips ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_skips (
    id SERIAL PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    account_id INTEGER REFERENCES accounts(id),
    symbol VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    category VARCHAR(50),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_skips_created ON trade_skips(created_at);

-- ── Settings (per-account overrides) ────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    risk_pct_override DOUBLE PRECISION,
    max_leverage INTEGER NOT NULL DEFAULT 12,
    enabled_symbols JSONB,
    auto_trade BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ── Subscriptions ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan VARCHAR(50) NOT NULL DEFAULT 'free',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    max_accounts INTEGER NOT NULL DEFAULT 1,
    expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ── Audit Logs ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    details_json JSONB,
    ip_address VARCHAR(45),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

-- ═══════════════════════════════════════════════════════════════════════
-- V5 Multi-Strategy Additions
-- ═══════════════════════════════════════════════════════════════════════

-- V5: Add strategy tracking to signals
ALTER TABLE signals ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(100);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime VARCHAR(100);

-- V5: Add strategy tracking to trades  ← PRIMARY FIX (was missing from live DB)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(100);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS regime VARCHAR(100);

-- ── Swing Watchlist ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS swing_watchlist (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,
    setup_type VARCHAR(30) NOT NULL,
    confidence INTEGER NOT NULL,
    trigger_price DOUBLE PRECISION NOT NULL,
    invalidation_price DOUBLE PRECISION NOT NULL,
    current_price DOUBLE PRECISION,
    regime_at_creation VARCHAR(30),
    notes TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'watching',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_swing_symbol ON swing_watchlist(symbol);
CREATE INDEX IF NOT EXISTS idx_swing_status ON swing_watchlist(status);
CREATE INDEX IF NOT EXISTS idx_swing_created ON swing_watchlist(created_at);

-- ── Daily Stats ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_stats (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date VARCHAR(10) NOT NULL,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl DOUBLE PRECISION DEFAULT 0.0,
    total_pnl_pct DOUBLE PRECISION DEFAULT 0.0,
    best_trade_pnl DOUBLE PRECISION DEFAULT 0.0,
    worst_trade_pnl DOUBLE PRECISION DEFAULT 0.0,
    regime_distribution JSONB,
    strategy_distribution JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
CREATE INDEX IF NOT EXISTS idx_daily_stats_account ON daily_stats(account_id);

-- ── Strategy Results ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategy_results (
    id SERIAL PRIMARY KEY,
    strategy_type VARCHAR(30) NOT NULL,
    symbol VARCHAR(20),
    side VARCHAR(10),
    confidence INTEGER,
    regime VARCHAR(30),
    entry_price DOUBLE PRECISION,
    exit_price DOUBLE PRECISION,
    pnl DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION,
    won BOOLEAN,
    duration_minutes INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_results_type ON strategy_results(strategy_type);
CREATE INDEX IF NOT EXISTS idx_strategy_results_created ON strategy_results(created_at);

-- ── News Events Cache ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_events_cache (
    id SERIAL PRIMARY KEY,
    source VARCHAR(30) NOT NULL,
    event_id VARCHAR(100) NOT NULL,
    title TEXT,
    symbols JSONB,
    sentiment VARCHAR(20),
    impact_score DOUBLE PRECISION DEFAULT 0.0,
    processed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_event_id ON news_events_cache(event_id);
CREATE INDEX IF NOT EXISTS idx_news_created ON news_events_cache(created_at);

-- ═══════════════════════════════════════════════════════════════════════
-- V6 SaaS Foundation — Database Upgrade
-- Safe: all ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS
-- ═══════════════════════════════════════════════════════════════════════

-- V6: Extend users table for auth
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;

-- V6: Extend accounts table for bot control
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS bot_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_sync TIMESTAMP;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_valid BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT;

-- V6: Extend subscriptions table for SaaS
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_name VARCHAR(100);
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS price DOUBLE PRECISION;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_date TIMESTAMP;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_date TIMESTAMP;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS added_by_admin BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS notes TEXT;

-- V6: Extend audit_logs table
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS admin_email VARCHAR(255);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS target_user_id INTEGER;

-- V6: Create payments table
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount DOUBLE PRECISION NOT NULL,
    verified_by_admin BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at);

-- ═══════════════════════════════════════════════════════════════════════
-- V9 Position Manager — open_positions tracking table
-- Written at trade OPEN. Monitored 24/7 by position_manager.py.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS open_positions (
    id SERIAL PRIMARY KEY,

    -- Identity
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    trade_id   INTEGER REFERENCES trades(id),
    symbol     VARCHAR(20) NOT NULL,
    side       VARCHAR(10) NOT NULL,          -- BUY | SELL

    -- Entry
    entry_price        DOUBLE PRECISION NOT NULL,
    quantity           DOUBLE PRECISION NOT NULL,
    leverage           INTEGER          NOT NULL DEFAULT 1,
    position_size_usdt DOUBLE PRECISION NOT NULL DEFAULT 0.0,

    -- Strategy context (preserved from signal engine)
    strategy_type VARCHAR(30),               -- scalp_trend_pullback | swing_* | sniper_*
    timeframe     VARCHAR(10),               -- 1m | 5m | 15m | 4h
    confidence    INTEGER DEFAULT 0,
    regime        VARCHAR(30),

    -- TP/SL prices (computed by RiskEngine at open — exact, not static)
    tp_price DOUBLE PRECISION NOT NULL,
    sl_price DOUBLE PRECISION NOT NULL,
    tp_pct   DOUBLE PRECISION DEFAULT 0.0,
    sl_pct   DOUBLE PRECISION DEFAULT 0.0,

    -- Trailing stop
    trailing_active      BOOLEAN          DEFAULT FALSE,
    trailing_sl_price    DOUBLE PRECISION,
    trailing_trigger_pct DOUBLE PRECISION DEFAULT 0.0,
    highest_price        DOUBLE PRECISION,
    lowest_price         DOUBLE PRECISION,

    -- Binance entry order
    entry_order_id VARCHAR(50),

    -- Hedge mode flags
    is_hedge_mode BOOLEAN     DEFAULT FALSE,
    position_side VARCHAR(10) DEFAULT 'BOTH',   -- BOTH | LONG | SHORT

    -- Status
    status        VARCHAR(20) NOT NULL DEFAULT 'open',  -- open | closed | error
    close_price   DOUBLE PRECISION,
    close_reason  VARCHAR(30),   -- tp_hit | sl_hit | trailing_exit | manual
    pnl_usdt      DOUBLE PRECISION,
    pnl_pct       DOUBLE PRECISION,

    -- Monitoring
    last_checked_at TIMESTAMP,
    last_price      DOUBLE PRECISION,
    check_count     INTEGER DEFAULT 0,

    -- Timestamps
    opened_at TIMESTAMP NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_open_positions_account ON open_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_open_positions_symbol  ON open_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_open_positions_status  ON open_positions(status);
CREATE INDEX IF NOT EXISTS idx_open_positions_opened  ON open_positions(opened_at);

-- =======================================================================
-- V10 Two-Engine Architecture — Protection Engine lifecycle columns
-- Added after stripping native TP/SL from the entry engine.
-- All statements are idempotent (IF NOT EXISTS).
-- =======================================================================

-- trades: Protection Engine lifecycle tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS protection_status VARCHAR(30) DEFAULT 'PENDING';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS virtual_sl        FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS virtual_tp        FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS managed_by        VARCHAR(50) DEFAULT 'external_engine';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS opened_at         TIMESTAMP DEFAULT NOW();

-- Index for fast Protection Engine queries
CREATE INDEX IF NOT EXISTS idx_trades_protection ON trades(protection_status);

-- =======================================================================
-- V16 Signal Engine — Pure signal tracking (no Binance execution)
-- All statements are idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
-- =======================================================================

-- signals: V16 tracking fields
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_number    INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_price      DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_price         DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_price         DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_pct           DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_pct           DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_zone_low   DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_zone_high  DOUBLE PRECISION;
-- Status lifecycle: PENDING → ENTRY_HIT → TP_HIT | SL_HIT | INVALIDATED | CANCELLED
ALTER TABLE signals ADD COLUMN IF NOT EXISTS status           VARCHAR(20) DEFAULT 'PENDING';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS result           VARCHAR(20);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS peak_price       DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS trough_price     DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS drawdown_pct     DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_hit_at     TIMESTAMP;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_at        TIMESTAMP;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS atr              DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS atr_pct          DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS btc_bias         VARCHAR(20);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMP DEFAULT NOW();

-- Indexes for signal tracker queries
CREATE INDEX IF NOT EXISTS idx_signals_status        ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_signal_number ON signals(signal_number);
CREATE INDEX IF NOT EXISTS idx_signals_updated       ON signals(updated_at);

-- V16: Daily signal counter — one row per date for sequential numbering
CREATE TABLE IF NOT EXISTS signal_counter (
    id           SERIAL PRIMARY KEY,
    date         VARCHAR(10) NOT NULL UNIQUE,   -- YYYY-MM-DD
    last_number  INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_counter_date ON signal_counter(date);

