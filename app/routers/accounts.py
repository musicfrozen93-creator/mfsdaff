"""
V2 Accounts API — Multi-Account CRUD with Encrypted API Keys

Endpoints:
  POST   /accounts          — Create account with encrypted API keys
  GET    /accounts          — List all accounts (keys masked)
  GET    /accounts/{id}     — Get single account
  PUT    /accounts/{id}     — Update account
  DELETE /accounts/{id}     — Deactivate account
  POST   /accounts/{id}/test — Test API connection
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from sqlalchemy import select

from app.database import async_session
from app.models.user import User, Account, ApiConnection, Balance
from app.models.system import Setting
from app.modules.crypto_utils import encrypt_api_key, decrypt_api_key, mask_api_key
from app.modules.executor import BinanceExecutor

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateAccountRequest(BaseModel):
    label: str = "Default"
    api_key: str
    api_secret: str
    email: Optional[str] = None
    username: Optional[str] = None


class UpdateAccountRequest(BaseModel):
    label: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    is_active: Optional[bool] = None


@router.post("/accounts")
async def create_account(req: CreateAccountRequest):
    """Create a new trading account with encrypted API keys."""
    try:
        async with async_session() as session:
            # Create or find user
            user = None
            if req.email or req.username:
                result = await session.execute(
                    select(User).where(
                        (User.email == req.email) | (User.username == req.username)
                    )
                )
                user = result.scalar_one_or_none()

            if not user:
                user = User(email=req.email, username=req.username or req.label)
                session.add(user)
                await session.flush()

            # Create account
            account = Account(user_id=user.id, label=req.label)
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

            logger.info(f"✅ Account created: #{account.id} '{req.label}'")

            return {
                "status": "ok",
                "account_id": account.id,
                "label": req.label,
                "api_key_masked": mask_api_key(req.api_key),
                "message": "Account created successfully. API keys encrypted.",
            }

    except Exception as e:
        logger.error(f"Account creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts")
async def list_accounts():
    """List all accounts with masked API keys."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.is_active == True)
            )
            accounts = result.scalars().all()

            account_list = []
            for acc in accounts:
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
                    "api_key_masked": api_masked,
                    "balance_usdt": balance_val,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                })

            return {"status": "ok", "count": len(account_list), "accounts": account_list}

    except Exception as e:
        logger.error(f"List accounts failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/{account_id}")
async def get_account(account_id: int):
    """Get single account details."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            acc = result.scalar_one_or_none()

            if not acc:
                raise HTTPException(status_code=404, detail="Account not found")

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
                    "api_key_masked": api_masked,
                    "balance_usdt": acc.balance.balance_usdt if acc.balance else 0.0,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                },
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, req: UpdateAccountRequest):
    """Update account label, API keys, or active status."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if not acc:
                raise HTTPException(status_code=404, detail="Account not found")

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
async def deactivate_account(account_id: int):
    """Soft-deactivate an account (not deleted)."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if not acc:
                raise HTTPException(status_code=404, detail="Account not found")

            acc.is_active = False
            if acc.api_connection:
                acc.api_connection.is_active = False
            await session.commit()

            return {"status": "ok", "message": f"Account #{account_id} deactivated"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: int):
    """Test API connection for an account — verifies keys and fetches balance."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id)
            )
            acc = result.scalar_one_or_none()
            if not acc or not acc.api_connection:
                raise HTTPException(status_code=404, detail="Account or API connection not found")

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
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
