"""
V5/V7/V9 Schema Catch-Up Migration

Adds every column and table that ORM models define but that the live database
may be missing because only the base schema.sql + 001_saas_upgrade.py were
applied previously.

All statements use IF NOT EXISTS / IF NOT EXISTS — fully idempotent.
Safe to run against a live database with existing data.

Changes:
  trades       — strategy_type, regime
  signals      — strategy_type, regime  (guard: may already exist)
  accounts     — bot_enabled, last_sync, api_valid, last_error  (guard)
  New tables   — strategy_registry, trade_memory, daily_pnl_logs,
                 open_positions (V9), swing_watchlist, daily_stats,
                 strategy_results, news_events_cache

Revision ID: 002_v5_strategy_columns
Replaces:    manual ALTER TABLE in schema.sql that was never applied
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '002_v5_strategy_columns'
down_revision = '001_saas_upgrade'
branch_labels = None
depends_on = None


def upgrade():
    """Apply V5 / V7 / V9 schema additions."""

    # ────────────────────────────────────────────────────────────────
    # trades — V5 strategy tracking (THE PRIMARY FIX)
    # ────────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(100)")
    op.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS regime VARCHAR(100)")

    # ────────────────────────────────────────────────────────────────
    # signals — V5 strategy tracking (idempotent guard)
    # ────────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(100)")
    op.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime VARCHAR(100)")

    # ────────────────────────────────────────────────────────────────
    # accounts — V6 bot control fields (idempotent guard)
    # These are also in 001_saas_upgrade but guard anyway
    # ────────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS bot_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_sync TIMESTAMP")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_valid BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT")

    # ────────────────────────────────────────────────────────────────
    # swing_watchlist — V5
    # ────────────────────────────────────────────────────────────────
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_swing_symbol ON swing_watchlist(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_swing_status ON swing_watchlist(status)")

    # ────────────────────────────────────────────────────────────────
    # daily_stats — V5
    # ────────────────────────────────────────────────────────────────
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_account ON daily_stats(account_id)")

    # ────────────────────────────────────────────────────────────────
    # strategy_results — V5
    # ────────────────────────────────────────────────────────────────
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_strategy_results_type ON strategy_results(strategy_type)")

    # ────────────────────────────────────────────────────────────────
    # news_events_cache — V5
    # ────────────────────────────────────────────────────────────────
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_news_event_id ON news_events_cache(event_id)")

    # ────────────────────────────────────────────────────────────────
    # strategy_registry — V7
    # ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS strategy_registry (
            id SERIAL PRIMARY KEY,
            strategy_id VARCHAR(50) UNIQUE NOT NULL,
            method VARCHAR(20) NOT NULL,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            weight DOUBLE PRECISION DEFAULT 1.0,
            total_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate DOUBLE PRECISION DEFAULT 0.0,
            avg_pnl DOUBLE PRECISION DEFAULT 0.0,
            total_pnl DOUBLE PRECISION DEFAULT 0.0,
            profit_factor DOUBLE PRECISION DEFAULT 0.0,
            max_drawdown DOUBLE PRECISION DEFAULT 0.0,
            best_regime VARCHAR(30),
            best_symbols JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_strategy_registry_id ON strategy_registry(strategy_id)")

    # ────────────────────────────────────────────────────────────────
    # trade_memory — V7
    # ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_memory (
            id SERIAL PRIMARY KEY,
            strategy_id VARCHAR(50) NOT NULL,
            method VARCHAR(20) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            market_regime VARCHAR(30),
            side VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION,
            exit_price DOUBLE PRECISION,
            tp_result VARCHAR(20),
            sl_result VARCHAR(20),
            pnl_pct DOUBLE PRECISION,
            won BOOLEAN,
            duration_minutes INTEGER,
            btc_trend VARCHAR(20),
            confidence INTEGER,
            confidence_breakdown JSONB,
            setup_grade VARCHAR(5),
            emergency_closed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_memory_strategy ON trade_memory(strategy_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_memory_symbol ON trade_memory(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_memory_created ON trade_memory(created_at)")

    # ────────────────────────────────────────────────────────────────
    # daily_pnl_logs — V7
    # ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS daily_pnl_logs (
            id SERIAL PRIMARY KEY,
            account_id INTEGER NOT NULL,
            date VARCHAR(10) NOT NULL,
            starting_balance DOUBLE PRECISION DEFAULT 0.0,
            ending_balance DOUBLE PRECISION DEFAULT 0.0,
            total_pnl DOUBLE PRECISION DEFAULT 0.0,
            total_pnl_pct DOUBLE PRECISION DEFAULT 0.0,
            trades_count INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            best_trade_pnl DOUBLE PRECISION DEFAULT 0.0,
            worst_trade_pnl DOUBLE PRECISION DEFAULT 0.0,
            max_consecutive_losses INTEGER DEFAULT 0,
            was_stopped BOOLEAN DEFAULT FALSE,
            stop_reason VARCHAR(200),
            was_safe_mode BOOLEAN DEFAULT FALSE,
            regime_distribution JSONB,
            strategy_distribution JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_daily_pnl_account ON daily_pnl_logs(account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_daily_pnl_date ON daily_pnl_logs(date)")

    # ────────────────────────────────────────────────────────────────
    # open_positions — V9 Position Manager
    # ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            id SERIAL PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            trade_id INTEGER REFERENCES trades(id),
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            quantity DOUBLE PRECISION NOT NULL,
            leverage INTEGER NOT NULL DEFAULT 1,
            position_size_usdt DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            strategy_type VARCHAR(30),
            timeframe VARCHAR(10),
            confidence INTEGER DEFAULT 0,
            regime VARCHAR(30),
            tp_price DOUBLE PRECISION NOT NULL,
            sl_price DOUBLE PRECISION NOT NULL,
            tp_pct DOUBLE PRECISION DEFAULT 0.0,
            sl_pct DOUBLE PRECISION DEFAULT 0.0,
            trailing_active BOOLEAN DEFAULT FALSE,
            trailing_sl_price DOUBLE PRECISION,
            trailing_trigger_pct DOUBLE PRECISION DEFAULT 0.0,
            highest_price DOUBLE PRECISION,
            lowest_price DOUBLE PRECISION,
            entry_order_id VARCHAR(50),
            is_hedge_mode BOOLEAN DEFAULT FALSE,
            position_side VARCHAR(10) DEFAULT 'BOTH',
            status VARCHAR(20) NOT NULL DEFAULT 'open',
            close_price DOUBLE PRECISION,
            close_reason VARCHAR(30),
            pnl_usdt DOUBLE PRECISION,
            pnl_pct DOUBLE PRECISION,
            last_checked_at TIMESTAMP,
            last_price DOUBLE PRECISION,
            check_count INTEGER DEFAULT 0,
            opened_at TIMESTAMP NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_open_positions_account ON open_positions(account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_open_positions_symbol  ON open_positions(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_open_positions_status  ON open_positions(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_open_positions_opened  ON open_positions(opened_at)")


def downgrade():
    """Rollback V5/V7/V9 schema additions."""

    # Drop new tables
    op.execute("DROP TABLE IF EXISTS open_positions CASCADE")
    op.execute("DROP TABLE IF EXISTS daily_pnl_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS trade_memory CASCADE")
    op.execute("DROP TABLE IF EXISTS strategy_registry CASCADE")
    op.execute("DROP TABLE IF EXISTS news_events_cache CASCADE")
    op.execute("DROP TABLE IF EXISTS strategy_results CASCADE")
    op.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    op.execute("DROP TABLE IF EXISTS swing_watchlist CASCADE")

    # Remove strategy columns from trades
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS strategy_type")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS regime")

    # Remove strategy columns from signals
    op.execute("ALTER TABLE signals DROP COLUMN IF EXISTS strategy_type")
    op.execute("ALTER TABLE signals DROP COLUMN IF EXISTS regime")
