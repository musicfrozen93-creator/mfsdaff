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
