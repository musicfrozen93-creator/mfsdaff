-- =============================================================
-- V11 Trade Metadata Migration
-- Adds entry_reason, close_reason, stale tracking, and
-- close_attempt retry counter to trades + open_positions tables.
-- Run once: psql -d trading_bot -f 012_v11_trade_metadata.sql
-- =============================================================

-- ── trades table additions ────────────────────────────────────
ALTER TABLE trades ADD COLUMN IF NOT EXISTS entry_reason       TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_reason_v11   TEXT;       -- rename avoids conflict with existing close_reason col if any
ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_stale           BOOLEAN    DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_attempts     INTEGER    DEFAULT 0;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS stale_alerted_at   TIMESTAMPTZ;

-- Ensure strategy_type column exists (may already exist from v5)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_type      VARCHAR(60);

-- ── open_positions table additions ───────────────────────────
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS entry_reason       TEXT;
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS close_attempts     INTEGER    DEFAULT 0;
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS stale_alerted      BOOLEAN    DEFAULT FALSE;
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS last_candle_high   NUMERIC(24, 8);
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS last_candle_low    NUMERIC(24, 8);
ALTER TABLE open_positions ADD COLUMN IF NOT EXISTS candle_checked_at  TIMESTAMPTZ;

-- ── indexes ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_open_positions_strategy ON open_positions(strategy_type);
CREATE INDEX IF NOT EXISTS idx_trades_strategy         ON trades(strategy_type);
CREATE INDEX IF NOT EXISTS idx_open_positions_stale    ON open_positions(stale_alerted, status);

-- ── done ──────────────────────────────────────────────────────
DO $$
BEGIN
    RAISE NOTICE 'V11 migration complete: entry_reason, close_reason_v11, stale tracking, close_attempts, candle hi/lo columns added.';
END $$;
