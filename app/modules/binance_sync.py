"""
V12 Binance Sync Module — Binance as Truth Source

Responsibilities:
  1. get_binance_live_positions()  — fetch all live positions from Binance /fapi/v2/positionRisk
  2. get_live_count_for_symbol()   — how many live positions exist for a given symbol
  3. sync_db_with_binance()        — full DB reconciliation:
       - Case A: DB open, Binance missing  → mark DB trade closed (ghost)
       - Case B: Binance open, DB missing  → create new DB record (orphan recovery)
       - Case C: Both match               → update last_price in DB
  4. count_all_live_positions()    — total count across all accounts

Architecture:
  - Position Manager calls sync_db_with_binance() on every BINANCE_SYNC_INTERVAL tick
  - execute_multi_account calls get_binance_live_positions() INSTEAD of DB count
  - DB open_positions is treated as a mirror/analytics log, not the gate
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.modules.crypto_utils import decrypt_api_key
from app.models.trading import OpenPosition, Trade
from app.models.user import Account, ApiConnection

logger = logging.getLogger(__name__)

# Lazy DB session factory (shared with position_manager)
_engine = None
_AsyncSessionFactory = None


def _get_session_factory():
    global _engine, _AsyncSessionFactory
    if _AsyncSessionFactory is None:
        _engine = create_async_engine(settings.DATABASE_URL, echo=False)
        _AsyncSessionFactory = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _AsyncSessionFactory


# ── Core: Fetch live Binance positions ────────────────────────────────

async def get_binance_live_positions(api_key: str, api_secret: str) -> list[dict]:
    """
    Fetch all open positions from Binance /fapi/v2/positionRisk.
    Returns only positions where positionAmt != 0.

    Returns list of dicts with keys:
      symbol, positionAmt, entryPrice, positionSide, unrealizedProfit, leverage
    """
    import hashlib, hmac, time
    from urllib.parse import urlencode
    import httpx

    base = settings.binance_base_url
    params = {
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    }
    query = urlencode(params)
    sig = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    headers = {"X-MBX-APIKEY": api_key}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/fapi/v2/positionRisk", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        live = [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return live
    except Exception as e:
        logger.error(f"[BinanceSync] Failed to fetch live positions: {e}")
        return []


async def count_all_live_positions() -> int:
    """
    V12: Fetch live position count from ALL active accounts (combined).
    Used by execute_multi_account to check actual slots before entry.
    """
    factory = _get_session_factory()
    total_symbols: set[str] = set()

    try:
        async with factory() as session:
            stmt = (
                select(Account, ApiConnection)
                .join(ApiConnection, ApiConnection.account_id == Account.id)
                .where(Account.is_active == True)
                .where(Account.bot_enabled == True)
                .where(ApiConnection.is_active == True)
            )
            result = await session.execute(stmt)
            rows = result.all()

        tasks = []
        for acc, conn in rows:
            if not conn.api_key_encrypted or not conn.api_secret_encrypted:
                continue
            try:
                ak = decrypt_api_key(conn.api_key_encrypted)
                ask = decrypt_api_key(conn.api_secret_encrypted)
                tasks.append(get_binance_live_positions(ak, ask))
            except Exception:
                continue

        if not tasks:
            # Fallback: use master key
            if settings.BINANCE_API_KEY:
                tasks.append(get_binance_live_positions(settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, list):
                for pos in res:
                    total_symbols.add(pos.get("symbol", ""))

    except Exception as e:
        logger.error(f"[BinanceSync] count_all_live_positions failed: {e}")

    return len(total_symbols)


async def get_live_count_for_symbol(symbol: str, api_key: str, api_secret: str) -> int:
    """How many live Binance positions exist for this symbol on this account."""
    positions = await get_binance_live_positions(api_key, api_secret)
    return sum(1 for p in positions if p.get("symbol") == symbol)


# ── Core: DB Sync ─────────────────────────────────────────────────────

async def sync_db_with_binance(api_key: str, api_secret: str, account_id: int) -> dict:
    """
    V12 Full DB ↔ Binance reconciliation for a single account.

    Case A — Ghost DB trade (DB=open, Binance=missing):
        Marks DB trade as closed with close_reason = BINANCE_GHOST_CLOSE_REASON

    Case B — Orphan Binance position (Binance=open, DB=missing):
        Creates a new OpenPosition row with source='binance_sync'

    Case C — Both match:
        Updates last_price in DB from Binance unrealizedProfit data

    Returns: {"ghosts": int, "orphans": int, "synced": int}
    """
    result = {"ghosts": 0, "orphans": 0, "synced": 0, "errors": []}
    factory = _get_session_factory()
    now = datetime.now(timezone.utc)

    # 1. Fetch live Binance positions for this account
    live_positions = await get_binance_live_positions(api_key, api_secret)
    live_symbols: dict[str, dict] = {}
    for p in live_positions:
        sym = p.get("symbol", "")
        if sym:
            live_symbols[sym] = p

    # 2. Fetch DB open positions for this account
    try:
        async with factory() as session:
            db_result = await session.execute(
                select(OpenPosition).where(
                    OpenPosition.account_id == account_id,
                    OpenPosition.status == "open",
                )
            )
            db_positions = db_result.scalars().all()
    except Exception as e:
        logger.error(f"[BinanceSync] DB fetch failed for account {account_id}: {e}")
        result["errors"].append(str(e))
        return result

    db_symbols = {p.symbol: p for p in db_positions}

    # ── Case A: Ghost DB trades (DB open, Binance missing) ────────────
    for sym, db_pos in db_symbols.items():
        if sym not in live_symbols:
            logger.warning(
                f"[BinanceSync] GHOST TRADE: {sym} account={account_id} "
                f"pos_id={db_pos.id} — in DB but NOT on Binance → marking closed"
            )
            try:
                # Fix: open_position update in first session
                async with factory() as session:
                    pos = await session.get(OpenPosition, db_pos.id)
                    if pos and pos.status == "open":
                        pos.status = "closed"
                        pos.close_reason = settings.BINANCE_GHOST_CLOSE_REASON
                        pos.closed_at = now
                        pos.last_checked_at = now
                        await session.commit()

                # Fix: Trade update in a SEPARATE fresh session (avoids committed-session bug)
                if db_pos.trade_id:
                    async with factory() as trade_session:
                        trade = await trade_session.get(Trade, db_pos.trade_id)
                        if trade and trade.status == "open":
                            trade.status = "closed"
                            trade.close_reason = settings.BINANCE_GHOST_CLOSE_REASON
                            trade.closed_at = now
                            await trade_session.commit()

                result["ghosts"] += 1
                logger.info(f"[BinanceSync] ✅ Ghost closed: {sym} (pos_id={db_pos.id})")
            except Exception as e:
                logger.error(f"[BinanceSync] Failed to close ghost {sym}: {e}")
                result["errors"].append(f"ghost_{sym}: {e}")

    # ── Case B: Orphan Binance positions (Binance open, DB missing) ───
    if settings.BINANCE_CREATE_MISSING_RECORDS:
        for sym, live_pos in live_symbols.items():
            if sym not in db_symbols:
                amt = float(live_pos.get("positionAmt", 0))
                entry = float(live_pos.get("entryPrice", 0))
                mark = float(live_pos.get("markPrice", entry))
                leverage = int(live_pos.get("leverage", 1))
                pos_side = live_pos.get("positionSide", "BOTH")
                side = "BUY" if amt > 0 else "SELL"

                # V12: Generate emergency TP/SL so PM can manage orphaned positions
                if entry > 0:
                    if side == "BUY":
                        emerg_tp = round(entry * 1.030, 8)
                        emerg_sl = round(entry * 0.985, 8)
                    else:
                        emerg_tp = round(entry * 0.970, 8)
                        emerg_sl = round(entry * 1.015, 8)
                    emerg_tp_pct, emerg_sl_pct = 3.0, 1.5
                else:
                    emerg_tp = emerg_sl = 0.0
                    emerg_tp_pct = emerg_sl_pct = 0.0

                logger.warning(
                    f"[BinanceSync] ORPHAN POSITION: {sym} account={account_id} "
                    f"side={side} entry={entry} amt={amt} — on Binance but NOT in DB → creating record "
                    f"emergency TP={emerg_tp} SL={emerg_sl}"
                )
                try:
                    async with factory() as session:
                        new_pos = OpenPosition(
                            account_id=account_id,
                            trade_id=None,
                            symbol=sym,
                            side=side,
                            entry_price=entry,
                            quantity=abs(amt),
                            leverage=leverage,
                            position_size_usdt=abs(amt) * entry,
                            strategy_type="binance_sync",
                            trade_mode="scalp",
                            timeframe="unknown",
                            confidence=0,
                            regime="",
                            tp_price=emerg_tp,
                            sl_price=emerg_sl,
                            tp_pct=emerg_tp_pct,
                            sl_pct=emerg_sl_pct,
                            is_hedge_mode=(pos_side in ("LONG", "SHORT")),
                            position_side=pos_side,
                            status="open",
                            last_price=mark,
                            highest_price=max(entry, mark),
                            lowest_price=min(entry, mark),
                            opened_at=now,
                            entry_reason="binance_sync_recovery",
                        )
                        session.add(new_pos)
                        await session.commit()
                    result["orphans"] += 1
                    logger.info(f"[BinanceSync] ✅ Orphan recovered: {sym} (account={account_id}) TP={emerg_tp} SL={emerg_sl}")
                except Exception as e:
                    logger.error(f"[BinanceSync] Failed to create orphan record {sym}: {e}")
                    result["errors"].append(f"orphan_{sym}: {e}")

    # ── Case C: Both match — update last_price ─────────────────────────
    for sym, db_pos in db_symbols.items():
        if sym in live_symbols:
            live = live_symbols[sym]
            try:
                unrealized = float(live.get("unrealizedProfit", 0))
                notional = abs(float(live.get("positionAmt", 0))) * float(live.get("markPrice", db_pos.entry_price))
                async with factory() as session:
                    pos = await session.get(OpenPosition, db_pos.id)
                    if pos and pos.status == "open":
                        try:
                            mark_price = float(live.get("markPrice", 0))
                            if mark_price > 0:
                                pos.last_price = mark_price
                            pos.last_checked_at = now
                            await session.commit()
                        except Exception:
                            pass
                result["synced"] += 1
            except Exception as e:
                logger.debug(f"[BinanceSync] last_price update failed for {sym}: {e}")

    # V12: Always emit a cycle summary log (not just when ghosts/orphans found)
    logger.info(
        f"[BinanceSync ⏰ {now.strftime('%H:%M:%S')}] account={account_id} "
        f"binance_live={len(live_symbols)} db_open={len(db_symbols)} | "
        f"ghosts_closed={result['ghosts']} orphans_recovered={result['orphans']} "
        f"synced={result['synced']} errors={len(result['errors'])}"
    )

    return result


async def sync_all_accounts() -> dict:
    """
    V12: Sync ALL active accounts with Binance.
    Called by Position Manager on BINANCE_SYNC_INTERVAL ticks.
    """
    factory = _get_session_factory()
    total = {"ghosts": 0, "orphans": 0, "synced": 0, "errors": []}

    try:
        async with factory() as session:
            stmt = (
                select(Account, ApiConnection)
                .join(ApiConnection, ApiConnection.account_id == Account.id)
                .where(Account.is_active == True)
                .where(Account.bot_enabled == True)
                .where(ApiConnection.is_active == True)
            )
            result = await session.execute(stmt)
            rows = result.all()
    except Exception as e:
        logger.error(f"[BinanceSync] sync_all_accounts DB query failed: {e}")
        return total

    tasks = []
    account_infos = []
    for acc, conn in rows:
        if not conn.api_key_encrypted or not conn.api_secret_encrypted:
            continue
        try:
            ak = decrypt_api_key(conn.api_key_encrypted)
            ask = decrypt_api_key(conn.api_secret_encrypted)
            tasks.append(sync_db_with_binance(ak, ask, acc.id))
            account_infos.append(acc.id)
        except Exception as e:
            logger.warning(f"[BinanceSync] Decrypt failed for account {acc.id}: {e}")

    # Fallback: master key
    if not tasks and settings.BINANCE_API_KEY:
        tasks.append(sync_db_with_binance(settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY, 0))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, dict):
            total["ghosts"] += r.get("ghosts", 0)
            total["orphans"] += r.get("orphans", 0)
            total["synced"] += r.get("synced", 0)
            total["errors"].extend(r.get("errors", []))

    return total
