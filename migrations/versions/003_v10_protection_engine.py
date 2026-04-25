"""
V10 Two-Engine Architecture — Protection Engine lifecycle columns

Adds protection tracking columns to the trades table so the Protection Engine
(position_manager.py) can manage lifecycle state independently of the Entry Engine.

Changes:
  trades  — protection_status, virtual_sl, virtual_tp, managed_by, opened_at

All statements are fully idempotent (IF NOT EXISTS).
Safe to run against a live database with existing data.

Revision ID: 003_v10_protection_engine
Previous:    002_v5_strategy_columns
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '003_v10_protection_engine'
down_revision = '002_v5_strategy_columns'
branch_labels = None
depends_on = None


def upgrade():
    """Apply V10 Protection Engine schema additions."""

    # ────────────────────────────────────────────────────────────────
    # trades — V10 Protection Engine lifecycle tracking
    # ────────────────────────────────────────────────────────────────

    # protection_status: PENDING (just opened by entry engine)
    #                    ACTIVE  (Protection Engine is now managing)
    #                    CLOSED  (Protection Engine closed the trade)
    op.execute(
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS protection_status "
        "VARCHAR(30) DEFAULT 'PENDING'"
    )

    # virtual_sl / virtual_tp: The SL/TP prices the Protection Engine
    # will use. These mirror stop_loss / take_profit but are owned by
    # the Protection Engine — it can update them (trailing, break-even).
    op.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS virtual_sl FLOAT")
    op.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS virtual_tp FLOAT")

    # managed_by: identifies which engine closed the trade
    # Values: 'external_engine' | 'manual' | 'emergency'
    op.execute(
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS managed_by "
        "VARCHAR(50) DEFAULT 'external_engine'"
    )

    # opened_at: explicit open timestamp for the Protection Engine
    # (created_at already exists but this is clearer for the PM loop)
    op.execute(
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS opened_at "
        "TIMESTAMP DEFAULT NOW()"
    )

    # Index: Protection Engine queries open trades by protection_status
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_protection "
        "ON trades(protection_status)"
    )


def downgrade():
    """Rollback V10 Protection Engine schema additions."""
    op.execute("DROP INDEX IF EXISTS idx_trades_protection")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS opened_at")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS managed_by")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS virtual_tp")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS virtual_sl")
    op.execute("ALTER TABLE trades DROP COLUMN IF EXISTS protection_status")
