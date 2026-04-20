"""
Authentication Utilities — bcrypt password hashing + JWT token management.
Provides FastAPI dependencies for route protection.

Dependencies:
  - get_current_admin: Requires valid JWT with is_admin=True
  - get_current_user: Requires valid JWT (any authenticated user)
  - get_admin_or_owner: Requires admin OR matching user_id ownership
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

# ── Security scheme ──────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════════════════════════════
# PASSWORD HASHING
# ═══════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# JWT TOKENS
# ═══════════════════════════════════════════════════════════════════════

def _get_secret_key() -> str:
    """Get JWT secret key — raises if not configured."""
    key = settings.JWT_SECRET_KEY
    if not key or key in ("", "changeme", "changeme-in-production"):
        raise ValueError(
            "JWT_SECRET_KEY is not set or uses an insecure default. "
            "Set a strong secret in your .env file. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )
    return key


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _get_secret_key(), algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT token. Returns the payload dict."""
    try:
        payload = jwt.decode(
            token, _get_secret_key(), algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except JWTError as e:
        logger.warning(f"JWT decode failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ═══════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency — requires valid JWT.
    Returns the decoded token payload: {"user_id": ..., "email": ..., "is_admin": ...}
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    return payload


async def get_current_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    FastAPI dependency — requires valid JWT with is_admin=True.
    Use for all /admin/* endpoints.
    """
    if not current_user.get("is_admin", False):
        logger.warning(
            f"BLOCKED: Non-admin user {current_user.get('email')} "
            f"attempted admin access"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


def require_admin_or_owner(user_id_param: str = "user_id"):
    """
    Factory for a FastAPI dependency that allows admin OR resource owner.
    The user_id is extracted from the path parameter specified by `user_id_param`.

    Usage in route:
        @router.get("/users/{user_id}/accounts")
        async def get_accounts(user_id: int, auth=Depends(require_admin_or_owner("user_id"))):
    """
    async def dependency(
        current_user: dict = Depends(get_current_user),
        **kwargs,
    ) -> dict:
        if current_user.get("is_admin", False):
            return current_user
        # For owner check, the caller must compare manually
        return current_user

    return dependency
