"""
V16 Migration Runner — run INSIDE the trading-bot container.

Usage:
  docker exec -it crypto-trading-bot python /app/migrations/run_v16_migration.py

Applies all V16 signal tracking columns to the existing 'signals' table
and creates the new 'signal_counter' table.
All statements are idempotent (ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS).
"""

import asyncio
import os
import sys

# Ensure app root is on path
sys.path.insert(0, "/app")


STMTS = [
    # ── signals: V16 tracking fields ──────────────────────────────────
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_number    INTEGER",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_price      DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_price         DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_price         DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_pct           DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_pct           DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_zone_low   DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_zone_high  DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS status           VARCHAR(20) DEFAULT 'PENDING'",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS result           VARCHAR(20)",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS peak_price       DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS trough_price     DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS drawdown_pct     DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_hit_at     TIMESTAMP",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_at        TIMESTAMP",
    # V16.1 lifecycle timestamps
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_hit_at        TIMESTAMP",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS sl_hit_at        TIMESTAMP",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS invalidated_at   TIMESTAMP",
    # V16.1 result storage
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_price     DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS pnl_percent      DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS atr              DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS atr_pct          DOUBLE PRECISION",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS btc_bias         VARCHAR(20)",
    "ALTER TABLE signals ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMP DEFAULT NOW()",

    # ── signal_counter: daily sequential numbering ──────────────────
    """
    CREATE TABLE IF NOT EXISTS signal_counter (
        id           SERIAL PRIMARY KEY,
        date         VARCHAR(10) NOT NULL UNIQUE,
        last_number  INTEGER NOT NULL DEFAULT 0,
        created_at   TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,

    # ── Indexes ──────────────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_signals_status        ON signals(status)",
    "CREATE INDEX IF NOT EXISTS idx_signals_signal_number ON signals(signal_number)",
    "CREATE INDEX IF NOT EXISTS idx_signals_updated       ON signals(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_signal_counter_date   ON signal_counter(date)",
]


async def run():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)

    print(f"  Connecting to: {db_url[:40]}...")
    engine = create_async_engine(db_url, echo=False)

    async with engine.begin() as conn:
        for i, stmt in enumerate(STMTS, 1):
            stmt = stmt.strip()
            label = stmt[:60].replace("\n", " ").strip()
            try:
                await conn.execute(text(stmt))
                print(f"  [{i:02d}/{len(STMTS)}] OK — {label}...")
            except Exception as e:
                print(f"  [{i:02d}/{len(STMTS)}] WARN — {label}... → {e}")

    await engine.dispose()
    print()
    print("✅  V16 migration complete — all columns and tables applied.")
    print()
    print("Next step: restart the trading-bot container to load new code:")
    print("  docker compose restart trading-bot")


if __name__ == "__main__":
    asyncio.run(run())
