"""
V6 Admin API — Protected endpoints for admin management.

All endpoints require JWT auth with is_admin=True.
Every state-changing action creates an AuditLog entry.

Endpoints:
  POST   /admin/login                    — Admin login → JWT
  POST   /admin/create-user              — Create new user
  GET    /admin/users                    — List all users
  GET    /admin/user/{user_id}           — Full user detail
  POST   /admin/ban-user                 — Toggle ban status
  POST   /admin/add-subscription         — Create subscription
  POST   /admin/extend-subscription      — Extend subscription end_date
  POST   /admin/add-payment              — Record manual payment
  GET    /admin/payments                 — List all payments
  GET    /admin/trades                   — List trades with filters
  GET    /admin/subscriptions/expired    — List expired subscriptions
  GET    /admin/subscriptions/expiring-soon — List expiring within 3 days
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, and_, or_

from app.database import async_session
from app.models.user import User, Account
from app.models.system import Subscription, AuditLog
from app.models.trading import Trade
from app.models.payment import Payment
from app.utils.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_admin,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ═══════════════════════════════════════════════════════════════════════

class AdminLoginRequest(BaseModel):
    email: str
    password: str


class CreateUserRequest(BaseModel):
    email: str
    username: str
    password: str


class BanUserRequest(BaseModel):
    user_id: int
    banned: bool  # True = ban, False = unban


class AddSubscriptionRequest(BaseModel):
    user_id: int
    plan_name: str = "basic"
    price: float = 0.0
    days: int = 30
    notes: Optional[str] = None


class ExtendSubscriptionRequest(BaseModel):
    subscription_id: int
    extra_days: int = 30
    notes: Optional[str] = None


class AddPaymentRequest(BaseModel):
    user_id: int
    amount: float
    verified_by_admin: bool = True
    notes: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
# HELPER: Create audit log
# ═══════════════════════════════════════════════════════════════════════

async def _audit_log(session, admin_email: str, action: str, target_user_id: int = None, details: dict = None):
    """Create an audit log entry."""
    log = AuditLog(
        admin_email=admin_email,
        target_user_id=target_user_id,
        action=action,
        details_json=details or {},
    )
    session.add(log)


# ═══════════════════════════════════════════════════════════════════════
# AUTH: ADMIN LOGIN
# ═══════════════════════════════════════════════════════════════════════

@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest):
    """Admin login — returns JWT token."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.email == req.email)
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                )

            # Check banned
            if user.is_banned:
                logger.warning(f"BLOCKED: Banned user '{req.email}' attempted login")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is banned. Contact support.",
                )

            # Check admin
            if not user.is_admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin access required",
                )

            # Verify password
            if not user.password_hash or not verify_password(req.password, user.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                )

            # Update last login
            user.last_login = datetime.now(timezone.utc)
            await session.commit()

            # Create JWT
            token = create_access_token({
                "user_id": user.id,
                "email": user.email,
                "is_admin": user.is_admin,
            })

            logger.info(f"✅ Admin login: {req.email}")

            return {
                "status": "ok",
                "access_token": token,
                "token_type": "bearer",
                "admin_email": user.email,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")


# ═══════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@router.post("/admin/create-user")
async def create_user(req: CreateUserRequest, admin: dict = Depends(get_current_admin)):
    """Create a new user account."""
    try:
        async with async_session() as session:
            # Check email uniqueness
            existing = await session.execute(
                select(User).where(
                    or_(User.email == req.email, User.username == req.username)
                )
            )
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=400,
                    detail="User with this email or username already exists",
                )

            user = User(
                email=req.email,
                username=req.username,
                password_hash=hash_password(req.password),
                is_active=True,
                is_banned=False,
                is_admin=False,
            )
            session.add(user)
            await session.flush()

            await _audit_log(
                session,
                admin_email=admin["email"],
                action="user_created",
                target_user_id=user.id,
                details={"email": req.email, "username": req.username},
            )

            await session.commit()

            logger.info(f"✅ User created by admin: {req.email}")

            return {
                "status": "ok",
                "user_id": user.id,
                "email": req.email,
                "username": req.username,
                "message": "User created successfully",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create user error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/users")
async def list_users(admin: dict = Depends(get_current_admin)):
    """List all users with summary info."""
    try:
        async with async_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

            user_list = []
            for u in users:
                # Get latest subscription status
                sub_status = "none"
                sub_plan = "none"
                sub_end = None
                if u.subscriptions:
                    latest_sub = max(u.subscriptions, key=lambda s: s.created_at)
                    sub_status = latest_sub.status
                    sub_plan = latest_sub.plan_name or latest_sub.plan
                    sub_end = (latest_sub.end_date or latest_sub.expires_at)

                user_list.append({
                    "id": u.id,
                    "email": u.email,
                    "username": u.username,
                    "is_active": u.is_active,
                    "is_banned": u.is_banned,
                    "is_admin": u.is_admin,
                    "accounts_count": len(u.accounts) if u.accounts else 0,
                    "subscription_status": sub_status,
                    "subscription_plan": sub_plan,
                    "subscription_end": sub_end.isoformat() if sub_end else None,
                    "last_login": u.last_login.isoformat() if u.last_login else None,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                })

            return {"status": "ok", "count": len(user_list), "users": user_list}

    except Exception as e:
        logger.error(f"List users error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/user/{user_id}")
async def get_user_detail(user_id: int, admin: dict = Depends(get_current_admin)):
    """Get full user detail with accounts, subscriptions, payments."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            accounts_list = []
            for acc in (user.accounts or []):
                accounts_list.append({
                    "id": acc.id,
                    "label": acc.label,
                    "is_active": acc.is_active,
                    "bot_enabled": acc.bot_enabled,
                    "api_valid": acc.api_valid,
                    "last_error": acc.last_error,
                    "balance_usdt": acc.balance.balance_usdt if acc.balance else 0.0,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                })

            subs_list = []
            for sub in (user.subscriptions or []):
                subs_list.append({
                    "id": sub.id,
                    "plan": sub.plan_name or sub.plan,
                    "status": sub.status,
                    "price": sub.price,
                    "start_date": sub.start_date.isoformat() if sub.start_date else None,
                    "end_date": (sub.end_date or sub.expires_at).isoformat() if (sub.end_date or sub.expires_at) else None,
                    "notes": sub.notes,
                    "created_at": sub.created_at.isoformat() if sub.created_at else None,
                })

            payments_list = []
            for pay in (user.payments or []):
                payments_list.append({
                    "id": pay.id,
                    "amount": pay.amount,
                    "verified_by_admin": pay.verified_by_admin,
                    "notes": pay.notes,
                    "created_at": pay.created_at.isoformat() if pay.created_at else None,
                })

            return {
                "status": "ok",
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                    "is_active": user.is_active,
                    "is_banned": user.is_banned,
                    "is_admin": user.is_admin,
                    "last_login": user.last_login.isoformat() if user.last_login else None,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "accounts": accounts_list,
                    "subscriptions": subs_list,
                    "payments": payments_list,
                },
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/ban-user")
async def ban_user(req: BanUserRequest, admin: dict = Depends(get_current_admin)):
    """Ban or unban a user. Banned users cannot login or trade."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.id == req.user_id)
            )
            user = result.scalar_one_or_none()

            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            if user.is_admin:
                raise HTTPException(status_code=400, detail="Cannot ban admin users")

            user.is_banned = req.banned

            # If banning, also disable bot on all accounts
            if req.banned:
                for acc in (user.accounts or []):
                    acc.bot_enabled = False

            action = "user_banned" if req.banned else "user_unbanned"
            await _audit_log(
                session,
                admin_email=admin["email"],
                action=action,
                target_user_id=req.user_id,
                details={"banned": req.banned},
            )

            await session.commit()

            status_text = "banned" if req.banned else "unbanned"
            logger.info(f"✅ User #{req.user_id} {status_text} by admin {admin['email']}")

            return {"status": "ok", "message": f"User #{req.user_id} {status_text}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════
# SUBSCRIPTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@router.post("/admin/add-subscription")
async def add_subscription(req: AddSubscriptionRequest, admin: dict = Depends(get_current_admin)):
    """Create a new subscription for a user."""
    try:
        async with async_session() as session:
            # Verify user exists
            user_result = await session.execute(
                select(User).where(User.id == req.user_id)
            )
            user = user_result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            now = datetime.now(timezone.utc)
            end_date = now + timedelta(days=req.days)

            sub = Subscription(
                user_id=req.user_id,
                plan=req.plan_name,
                plan_name=req.plan_name,
                status="active",
                price=req.price,
                start_date=now,
                end_date=end_date,
                expires_at=end_date,
                added_by_admin=True,
                notes=req.notes,
            )
            session.add(sub)

            # Enable bot on all user accounts
            for acc in (user.accounts or []):
                acc.bot_enabled = True

            await _audit_log(
                session,
                admin_email=admin["email"],
                action="subscription_added",
                target_user_id=req.user_id,
                details={
                    "plan_name": req.plan_name,
                    "days": req.days,
                    "price": req.price,
                    "end_date": end_date.isoformat(),
                },
            )

            await session.commit()

            logger.info(
                f"✅ Subscription added: user #{req.user_id} "
                f"plan={req.plan_name} days={req.days} by {admin['email']}"
            )

            return {
                "status": "ok",
                "message": f"Subscription added for user #{req.user_id}",
                "subscription": {
                    "plan_name": req.plan_name,
                    "start_date": now.isoformat(),
                    "end_date": end_date.isoformat(),
                    "days": req.days,
                },
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/extend-subscription")
async def extend_subscription(req: ExtendSubscriptionRequest, admin: dict = Depends(get_current_admin)):
    """Extend an existing subscription's end date."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Subscription).where(Subscription.id == req.subscription_id)
            )
            sub = result.scalar_one_or_none()

            if not sub:
                raise HTTPException(status_code=404, detail="Subscription not found")

            now = datetime.now(timezone.utc)
            current_end = sub.end_date or sub.expires_at or now
            # If already expired, extend from now; if still active, extend from current end
            if current_end.replace(tzinfo=timezone.utc) < now:
                new_end = now + timedelta(days=req.extra_days)
            else:
                new_end = current_end + timedelta(days=req.extra_days)

            sub.end_date = new_end
            sub.expires_at = new_end
            sub.status = "active"
            if req.notes:
                sub.notes = (sub.notes or "") + f"\n[Extended +{req.extra_days}d by admin: {req.notes}]"

            # Re-enable bot on user accounts
            user_result = await session.execute(
                select(User).where(User.id == sub.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user:
                for acc in (user.accounts or []):
                    acc.bot_enabled = True

            await _audit_log(
                session,
                admin_email=admin["email"],
                action="subscription_extended",
                target_user_id=sub.user_id,
                details={
                    "subscription_id": req.subscription_id,
                    "extra_days": req.extra_days,
                    "new_end_date": new_end.isoformat(),
                },
            )

            await session.commit()

            logger.info(
                f"✅ Subscription #{req.subscription_id} extended by {req.extra_days} days "
                f"→ {new_end.isoformat()} by {admin['email']}"
            )

            return {
                "status": "ok",
                "message": f"Subscription extended by {req.extra_days} days",
                "new_end_date": new_end.isoformat(),
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/subscriptions/expired")
async def list_expired_subscriptions(admin: dict = Depends(get_current_admin)):
    """List all expired subscriptions with user info."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Subscription).where(Subscription.status == "expired")
            )
            subs = result.scalars().all()

            items = []
            for sub in subs:
                user_result = await session.execute(
                    select(User).where(User.id == sub.user_id)
                )
                user = user_result.scalar_one_or_none()
                items.append({
                    "subscription_id": sub.id,
                    "user_id": sub.user_id,
                    "email": user.email if user else None,
                    "username": user.username if user else None,
                    "plan": sub.plan_name or sub.plan,
                    "end_date": (sub.end_date or sub.expires_at).isoformat() if (sub.end_date or sub.expires_at) else None,
                    "notes": sub.notes,
                })

            return {"status": "ok", "count": len(items), "expired_subscriptions": items}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/subscriptions/expiring-soon")
async def list_expiring_soon(admin: dict = Depends(get_current_admin)):
    """List active subscriptions expiring within 3 days."""
    try:
        async with async_session() as session:
            now = datetime.now(timezone.utc)
            three_days = now + timedelta(days=3)

            result = await session.execute(
                select(Subscription).where(
                    and_(
                        Subscription.status == "active",
                    )
                )
            )
            subs = result.scalars().all()

            items = []
            for sub in subs:
                expiry = sub.end_date or sub.expires_at
                if expiry and expiry.replace(tzinfo=timezone.utc) <= three_days:
                    user_result = await session.execute(
                        select(User).where(User.id == sub.user_id)
                    )
                    user = user_result.scalar_one_or_none()
                    remaining = (expiry.replace(tzinfo=timezone.utc) - now).total_seconds()
                    items.append({
                        "subscription_id": sub.id,
                        "user_id": sub.user_id,
                        "email": user.email if user else None,
                        "username": user.username if user else None,
                        "plan": sub.plan_name or sub.plan,
                        "end_date": expiry.isoformat(),
                        "hours_remaining": round(remaining / 3600, 1),
                    })

            return {"status": "ok", "count": len(items), "expiring_subscriptions": items}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════
# PAYMENT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@router.post("/admin/add-payment")
async def add_payment(req: AddPaymentRequest, admin: dict = Depends(get_current_admin)):
    """Record a manual payment (e.g., from WhatsApp)."""
    try:
        async with async_session() as session:
            # Verify user exists
            user_result = await session.execute(
                select(User).where(User.id == req.user_id)
            )
            if not user_result.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="User not found")

            payment = Payment(
                user_id=req.user_id,
                amount=req.amount,
                verified_by_admin=req.verified_by_admin,
                notes=req.notes,
            )
            session.add(payment)

            await _audit_log(
                session,
                admin_email=admin["email"],
                action="payment_added",
                target_user_id=req.user_id,
                details={"amount": req.amount, "notes": req.notes},
            )

            await session.commit()

            logger.info(f"✅ Payment recorded: user #{req.user_id} amount={req.amount} by {admin['email']}")

            return {
                "status": "ok",
                "message": f"Payment of {req.amount} recorded for user #{req.user_id}",
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/payments")
async def list_payments(admin: dict = Depends(get_current_admin)):
    """List all payments."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Payment).order_by(Payment.created_at.desc())
            )
            payments = result.scalars().all()

            items = []
            for pay in payments:
                user_result = await session.execute(
                    select(User).where(User.id == pay.user_id)
                )
                user = user_result.scalar_one_or_none()
                items.append({
                    "id": pay.id,
                    "user_id": pay.user_id,
                    "email": user.email if user else None,
                    "amount": pay.amount,
                    "verified_by_admin": pay.verified_by_admin,
                    "notes": pay.notes,
                    "created_at": pay.created_at.isoformat() if pay.created_at else None,
                })

            return {"status": "ok", "count": len(items), "payments": items}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════
# TRADE HISTORY
# ═══════════════════════════════════════════════════════════════════════

@router.get("/admin/trades")
async def list_trades(
    limit: int = 50,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    status_filter: Optional[str] = None,
    admin: dict = Depends(get_current_admin),
):
    """List trades with optional filters."""
    try:
        async with async_session() as session:
            query = select(Trade).order_by(Trade.created_at.desc())

            if account_id:
                query = query.where(Trade.account_id == account_id)
            if symbol:
                query = query.where(Trade.symbol == symbol.upper())
            if status_filter:
                query = query.where(Trade.status == status_filter)

            query = query.limit(min(limit, 200))

            result = await session.execute(query)
            trades = result.scalars().all()

            items = []
            for t in trades:
                items.append({
                    "id": t.id,
                    "account_id": t.account_id,
                    "symbol": t.symbol,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "close_price": t.close_price,
                    "quantity": t.quantity,
                    "leverage": t.leverage,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct,
                    "status": t.status,
                    "strategy_type": t.strategy_type,
                    "close_reason": t.close_reason,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                })

            return {"status": "ok", "count": len(items), "trades": items}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
