"""
Admin Seed Script — Creates admin user on startup from environment variables.

Reads ADMIN_EMAIL and ADMIN_PASSWORD from config.
If admin does not exist → creates one with is_admin=True.
If admin already exists → skips (no overwrite).
"""

import logging

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.user import User
from app.utils.auth import hash_password

logger = logging.getLogger(__name__)


async def seed_admin():
    """Create admin account from environment variables if it doesn't exist."""
    admin_email = settings.ADMIN_EMAIL
    admin_password = settings.ADMIN_PASSWORD

    if not admin_email or not admin_password:
        logger.warning(
            "⚠️ ADMIN_EMAIL or ADMIN_PASSWORD not set in environment. "
            "Skipping admin seed. Set these in .env to create admin account."
        )
        return

    if len(admin_password) < 8:
        logger.warning("⚠️ ADMIN_PASSWORD is too short (min 8 chars). Skipping admin seed.")
        return

    try:
        async with async_session() as session:
            # Check if admin already exists
            result = await session.execute(
                select(User).where(User.email == admin_email)
            )
            existing = result.scalar_one_or_none()

            if existing:
                # Ensure is_admin flag is set (in case it was created before V6)
                if not existing.is_admin:
                    existing.is_admin = True
                    await session.commit()
                    logger.info(f"✅ Existing user '{admin_email}' upgraded to admin")
                else:
                    logger.info(f"ℹ️ Admin account '{admin_email}' already exists — skipping seed")
                return

            # Create new admin user
            admin_user = User(
                email=admin_email,
                username="admin",
                password_hash=hash_password(admin_password),
                is_active=True,
                is_banned=False,
                is_admin=True,
            )
            session.add(admin_user)
            await session.commit()

            logger.info(f"✅ Admin account created: {admin_email}")

    except Exception as e:
        logger.error(f"❌ Admin seed failed: {e}")
