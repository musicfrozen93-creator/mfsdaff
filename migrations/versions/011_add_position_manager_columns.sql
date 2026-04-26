-- ================================================================
-- Migration 011: V10 Position Manager columns
-- Run once on your database.
-- All statements use IF NOT EXISTS / safe column additions.
-- ================================================================

-- 1. Ensure open_positions.status can hold 'closing' state
--    (VARCHAR(20) already in ORM, this is a safety check)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='open_positions' AND column_name='status'
    ) THEN
        ALTER TABLE open_positions ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'open';
    END IF;
END$$;

-- 2. Add opening_type to trades table (simpler classification than strategy_type)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS opening_type VARCHAR(10) DEFAULT 'swing';
-- Backfill from strategy_type
UPDATE trades
SET opening_type = CASE
    WHEN strategy_type ILIKE 'scalp%' THEN 'scalp'
    WHEN strategy_type ILIKE 'sniper%' THEN 'scalp'
    ELSE 'swing'
END
WHERE opening_type IS NULL OR opening_type = 'swing';

-- 3. Ensure managed_by default is set for all existing trades
UPDATE trades SET managed_by = 'python_pm' WHERE managed_by IS NULL;

-- 4. Index for fast open position queries by type (used by n8n PM every 5s)
CREATE INDEX IF NOT EXISTS idx_open_positions_status ON open_positions(status);
CREATE INDEX IF NOT EXISTS idx_open_positions_strategy ON open_positions(strategy_type);
CREATE INDEX IF NOT EXISTS idx_trades_opening_type ON trades(opening_type);
