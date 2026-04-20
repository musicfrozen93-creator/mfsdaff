"""
Subscription Guard — Pre-trade subscription validation + auto-expiry system.

Used by the executor to check if an account's owner has a valid subscription
before executing any trade. Also provides auto-expiry checking.

This module does NOT modify the trading engine logic — it only provides
validation functions that are called at the pre-check stage.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, Account
from app.models.system import Subscription

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# PRE-TRADE SUBSCRIPTION CHECK
# ═══════════════════════════════════════════════════════════════════════

async def check_account_eligible(session: AsyncSession, account_id: int) -> dict:
    """
    Check if an account is eligible to trade:
    1. Account must be active and bot_enabled
    2. Account owner must not be banned
    3. Account owner must have an active, non-expired subscription

    Returns:
        {"eligible": True/False, "reason": "...", "user_id": int}
    """
    try:
        # Load account with user
        result = await session.execute(
            select(Account).where(Account.id == account_id)
        )
        account = result.scalar_one_or_none()

        if not account:
            return {"eligible": False, "reason": "Account not found", "user_id": None}

        if not account.is_active:
            return {"eligible": False, "reason": "Account deactivated", "user_id": account.user_id}

        if not account.bot_enabled:
            return {"eligible": False, "reason": "Bot disabled for account", "user_id": account.user_id}

        # Load user
        user_result = await session.execute(
            select(User).where(User.id == account.user_id)
        )
        user = user_result.scalar_one_or_none()

        if not user:
            return {"eligible": False, "reason": "Account owner not found", "user_id": account.user_id}

        # Check banned status
        if user.is_banned:
            logger.warning(f"SKIPPED: User Banned — user_id={user.id} account_id={account_id}")
            return {"eligible": False, "reason": "User Banned", "user_id": user.id}

        if not user.is_active:
            return {"eligible": False, "reason": "User account deactivated", "user_id": user.id}

        # Admin users always eligible (bypass subscription check)
        if user.is_admin:
            return {"eligible": True, "reason": "Admin bypass", "user_id": user.id}

        # Check subscription
        now = datetime.now(timezone.utc)
        sub_result = await session.execute(
            select(Subscription)
            .where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.status == "active",
                )
            )
            .order_by(Subscription.created_at.desc())
        )
        subscription = sub_result.scalar_one_or_none()

        if not subscription:
            logger.warning(
                f"SKIPPED: Subscription Expired — user_id={user.id} "
                f"account_id={account_id} (no active subscription)"
            )
            return {"eligible": False, "reason": "Subscription Expired", "user_id": user.id}

        # Check end_date (V6 field) or falls back to expires_at
        expiry = subscription.end_date or subscription.expires_at
        if expiry and expiry.replace(tzinfo=timezone.utc) < now:
            logger.warning(
                f"SKIPPED: Subscription Expired — user_id={user.id} "
                f"account_id={account_id} expired_at={expiry.isoformat()}"
            )
            # Auto-expire the subscription
            subscription.status = "expired"
            account.bot_enabled = False
            await session.commit()
            return {"eligible": False, "reason": "Subscription Expired", "user_id": user.id}

        return {"eligible": True, "reason": "Active subscription", "user_id": user.id}

    except Exception as e:
        logger.error(f"Subscription check error for account {account_id}: {e}")
        # On error, allow trade to proceed (don't block live trading on check failure)
        return {"eligible": True, "reason": f"Check error (allowing): {e}", "user_id": None}


# ═══════════════════════════════════════════════════════════════════════
# AUTO-EXPIRY CHECKER — runs on app startup and can be called periodically
# ═══════════════════════════════════════════════════════════════════════

async def run_subscription_expiry_check():
    """
    Check all active subscriptions and expire any that have passed their end_date.
    Also disables bot_enabled on affected accounts.

    Safe to call repeatedly — idempotent.
    """
    from app.database import async_session as get_session

    try:
        async with get_session() as session:
            now = datetime.now(timezone.utc)

            # Find active subscriptions past their expiry
            result = await session.execute(
                select(Subscription).where(
                    and_(
                        Subscription.status == "active",
                    )
                )
            )
            subscriptions = result.scalars().all()

            expired_count = 0
            for sub in subscriptions:
                expiry = sub.end_date or sub.expires_at
                if expiry and expiry.replace(tzinfo=timezone.utc) < now:
                    sub.status = "expired"
                    expired_count += 1

                    # Disable bot on all user accounts
                    acc_result = await session.execute(
                        select(Account).where(Account.user_id == sub.user_id)
                    )
                    accounts = acc_result.scalars().all()
                    for acc in accounts:
                        acc.bot_enabled = False

                    logger.info(
                        f"🔴 Subscription expired: user_id={sub.user_id} "
                        f"plan={sub.plan_name or sub.plan} expired_at={expiry.isoformat()}"
                    )

            if expired_count > 0:
                await session.commit()
                logger.info(f"✅ Subscription expiry check: {expired_count} subscriptions expired")
            else:
                logger.info("✅ Subscription expiry check: no expired subscriptions found")

    except Exception as e:
        logger.error(f"❌ Subscription expiry check failed: {e}")
