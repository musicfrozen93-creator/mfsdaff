"""
V6 SaaS Foundation — Database Upgrade Migration

Adds:
  - User auth fields: password_hash, is_banned, is_admin, last_login
  - Account bot control: bot_enabled, last_sync, api_valid, last_error
  - Subscription extensions: plan_name, price, start_date, end_date, added_by_admin, notes
  - AuditLog extensions: admin_email, target_user_id
  - New payments table

All operations are idempotent (IF NOT EXISTS).
Safe for existing live databases with data.

Revision ID: 001_saas_upgrade
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '001_saas_upgrade'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """Apply V6 SaaS database upgrades."""

    # ── Users: auth fields ───────────────────────────────────────────
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP")

    # ── Accounts: bot control fields ─────────────────────────────────
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS bot_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_sync TIMESTAMP")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS api_valid BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT")

    # ── Subscriptions: SaaS fields ───────────────────────────────────
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_name VARCHAR(100)")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS price DOUBLE PRECISION")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS start_date TIMESTAMP")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS end_date TIMESTAMP")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS added_by_admin BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS notes TEXT")

    # ── AuditLogs: enhanced tracking ─────────────────────────────────
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS admin_email VARCHAR(255)")
    op.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS target_user_id INTEGER")

    # ── Payments: new table ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount DOUBLE PRECISION NOT NULL,
            verified_by_admin BOOLEAN NOT NULL DEFAULT FALSE,
            notes TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at)")


def downgrade():
    """Rollback V6 SaaS database upgrades."""

    # Drop payments table
    op.execute("DROP TABLE IF EXISTS payments CASCADE")

    # Remove audit_logs extensions
    op.execute("ALTER TABLE audit_logs DROP COLUMN IF EXISTS admin_email")
    op.execute("ALTER TABLE audit_logs DROP COLUMN IF EXISTS target_user_id")

    # Remove subscription extensions
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS plan_name")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS price")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS start_date")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS end_date")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS added_by_admin")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS notes")

    # Remove account extensions
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS bot_enabled")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS last_sync")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS api_valid")
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS last_error")

    # Remove user extensions
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_hash")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_banned")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_login")
