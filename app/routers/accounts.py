"""
V6 Accounts API — Multi-Account CRUD with Encrypted API Keys + Auth Protection

Endpoints:
  POST   /accounts          — Create account (admin only)
  GET    /accounts          — List all accounts (admin: all, user: own)
  GET    /accounts/{id}     — Get single account (admin or owner)
  PUT    /accounts/{id}     — Update account (admin or owner)
  DELETE /accounts/{id}     — Deactivate account (admin or owner)
  POST   /accounts/{id}/test — Test API connection (admin or owner)

SECURITY:
  - All routes require JWT auth
  - Admin can access all accounts
  - Normal users can only access their own accounts
  - API keys are NEVER returned in responses — only masked display
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from sqlalchemy import select

from app.database import async_session
from app.models.user import User, Account, ApiConnection, Balance
from app.models.system import Setting
from app.modules.crypto_utils import encrypt_api_key, decrypt_api_key, mask_api_key
from app.modules.executor import BinanceExecutor
from app.utils.auth import get_current_user, get_current_admin

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateAccountRequest(BaseModel):
    label: str = "Default"
    api_key: str
    api_secret: str
    user_id: Optional[int] = None  # Admin can specify; normal user auto-uses self
    email: Optional[str] = None
    username: Optional[str] = None


class UpdateAccountRequest(BaseModel):
    label: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    is_active: Optional[bool] = None


# ═══════════════════════════════════════════════════════════════════════
# HELPER: Ownership check
# ═══════════════════════════════════════════════════════════════════════

async def _verify_account_access(session, account_id: int, current_user: dict) -> "Account":
    """Verify caller has access to this account. Returns the Account or raises 403."""
    result = await session.execute(
        select(Account).where(Account.id == account_id)
    )
    acc = result.scalar_one_or_none()

    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # Admin can access any account
    if current_user.get("is_admin", False):
        return acc

    # Normal user: must own the account
    if acc.user_id != current_user.get("user_id"):
        logger.warning(
            f"SKIPPED: Unauthorized Account Access Blocked — "
            f"user_id={current_user.get('user_id')} tried to access account_id={account_id}"
        )
        raise HTTPException(status_code=403, detail="Access denied — not your account")

    return acc


# ═══════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════

@router.post("/accounts")
async def create_account(req: CreateAccountRequest, current_user: dict = Depends(get_current_user)):
    """Create a new trading account with encrypted API keys."""
    try:
        async with async_session() as session:
            # Determine which user to create account for
            if current_user.get("is_admin") and req.user_id:
                # Admin creating for a specific user
                target_user_id = req.user_id
                result = await session.execute(
                    select(User).where(User.id == target_user_id)
                )
                user = result.scalar_one_or_none()
                if not user:
                    raise HTTPException(status_code=404, detail="Target user not found")
            else:
                # User creating for themselves
                target_user_id = current_user.get("user_id")
                result = await session.execute(
                    select(User).where(User.id == target_user_id)
                )
                user = result.scalar_one_or_none()
                if not user:
                    # Create user record if it doesn't exist (backward compat)
                    user = User(
                        email=req.email or current_user.get("email"),
                        username=req.username or req.label,
                    )
                    session.add(user)
                    await session.flush()
                    target_user_id = user.id

            # Create account
            account = Account(user_id=target_user_id, label=req.label)
            session.add(account)
            await session.flush()

            # Encrypt and store API keys
            api_conn = ApiConnection(
                account_id=account.id,
                api_key_encrypted=encrypt_api_key(req.api_key),
                api_secret_encrypted=encrypt_api_key(req.api_secret),
            )
            session.add(api_conn)

            # Create balance record
            balance = Balance(account_id=account.id)
            session.add(balance)

            # Create default settings
            setting = Setting(account_id=account.id)
            session.add(setting)

            await session.commit()

            logger.info(f"✅ Account created: #{account.id} '{req.label}' for user #{target_user_id}")

            return {
                "status": "ok",
                "account_id": account.id,
                "label": req.label,
                "api_key_masked": mask_api_key(req.api_key),
                "message": "Account created successfully. API keys encrypted.",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Account creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts")
async def list_accounts(current_user: dict = Depends(get_current_user)):
    """List accounts — admin sees all, normal user sees own only."""
    try:
        async with async_session() as session:
            query = select(Account).where(Account.is_active == True)

            # Normal users can only see their own accounts
            if not current_user.get("is_admin", False):
                query = query.where(Account.user_id == current_user.get("user_id"))

            result = await session.execute(query)
            accounts = result.scalars().all()

            account_list = []
            for acc in accounts:
                # NEVER return raw API keys — only masked display
                api_masked = "Not connected"
                if acc.api_connection:
                    try:
                        plain_key = decrypt_api_key(acc.api_connection.api_key_encrypted)
                        api_masked = mask_api_key(plain_key)
                    except Exception:
                        api_masked = "Decryption error"

                balance_val = acc.balance.balance_usdt if acc.balance else 0.0

                account_list.append({
                    "id": acc.id,
                    "label": acc.label,
                    "is_active": acc.is_active,
                    "bot_enabled": acc.bot_enabled,
                    "api_key_masked": api_masked,
                    "balance_usdt": balance_val,
                    "api_valid": acc.api_valid,
                    "last_error": acc.last_error,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                })

            return {"status": "ok", "count": len(account_list), "accounts": account_list}

    except Exception as e:
        logger.error(f"List accounts failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/{account_id}")
async def get_account(account_id: int, current_user: dict = Depends(get_current_user)):
    """Get single account details (admin or owner)."""
    try:
        async with async_session() as session:
            acc = await _verify_account_access(session, account_id, current_user)

            api_masked = "Not connected"
            if acc.api_connection:
                try:
                    plain_key = decrypt_api_key(acc.api_connection.api_key_encrypted)
                    api_masked = mask_api_key(plain_key)
                except Exception:
                    api_masked = "Decryption error"

            return {
                "status": "ok",
                "account": {
                    "id": acc.id,
                    "label": acc.label,
                    "is_active": acc.is_active,
                    "bot_enabled": acc.bot_enabled,
                    "api_key_masked": api_masked,
                    "api_valid": acc.api_valid,
                    "last_error": acc.last_error,
                    "balance_usdt": acc.balance.balance_usdt if acc.balance else 0.0,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                },
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, req: UpdateAccountRequest, current_user: dict = Depends(get_current_user)):
    """Update account label, API keys, or active status (admin or owner)."""
    try:
        async with async_session() as session:
            acc = await _verify_account_access(session, account_id, current_user)

            if req.label is not None:
                acc.label = req.label
            if req.is_active is not None:
                acc.is_active = req.is_active

            if req.api_key and req.api_secret and acc.api_connection:
                acc.api_connection.api_key_encrypted = encrypt_api_key(req.api_key)
                acc.api_connection.api_secret_encrypted = encrypt_api_key(req.api_secret)

            await session.commit()

            return {"status": "ok", "message": f"Account #{account_id} updated"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/accounts/{account_id}")
async def deactivate_account(account_id: int, current_user: dict = Depends(get_current_user)):
    """Soft-deactivate an account (admin or owner)."""
    try:
        async with async_session() as session:
            acc = await _verify_account_access(session, account_id, current_user)

            acc.is_active = False
            acc.bot_enabled = False
            if acc.api_connection:
                acc.api_connection.is_active = False
            await session.commit()

            return {"status": "ok", "message": f"Account #{account_id} deactivated"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: int, current_user: dict = Depends(get_current_user)):
    """Test API connection (admin or owner). Keys are NEVER returned."""
    try:
        async with async_session() as session:
            acc = await _verify_account_access(session, account_id, current_user)

            if not acc.api_connection:
                raise HTTPException(status_code=404, detail="API connection not found")

            api_key = decrypt_api_key(acc.api_connection.api_key_encrypted)
            api_secret = decrypt_api_key(acc.api_connection.api_secret_encrypted)

            executor = BinanceExecutor(api_key=api_key, secret_key=api_secret)
            balance = await executor.get_account_balance()

            # Update balance in DB
            if acc.balance:
                acc.balance.balance_usdt = balance
                acc.balance.available_balance = balance
            else:
                bal = Balance(account_id=acc.id, balance_usdt=balance, available_balance=balance)
                session.add(bal)

            acc.api_connection.last_verified_at = datetime.utcnow()
            acc.api_valid = True
            acc.last_error = None
            acc.last_sync = datetime.utcnow()
            await session.commit()

            return {
                "status": "ok",
                "message": "API connection verified",
                "balance_usdt": balance,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"API test failed for account #{account_id}: {e}")
        # Record the error
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Account).where(Account.id == account_id)
                )
                acc = result.scalar_one_or_none()
                if acc:
                    acc.api_valid = False
                    acc.last_error = str(e)[:255]
                    await session.commit()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
