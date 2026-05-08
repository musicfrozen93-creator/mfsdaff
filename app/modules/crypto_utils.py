"""
API Key Encryption / Decryption Utility
Uses Fernet (AES-128-CBC) symmetric encryption.
Keys are never stored in plaintext, never logged, never returned in API responses.
"""

import logging
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet() -> Fernet:
    """Lazy-init Fernet cipher from ENCRYPTION_KEY."""
    global _fernet
    if _fernet is None:
        key = settings.ENCRYPTION_KEY
        if not key:
            raise ValueError(
                "ENCRYPTION_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_api_key(plain_text: str) -> str:
    """Encrypt an API key. Returns base64-encoded ciphertext string."""
    if not plain_text:
        raise ValueError("Cannot encrypt empty API key")
    f = _get_fernet()
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_api_key(encrypted_text: str) -> str:
    """Decrypt an API key. Returns plaintext string."""
    if not encrypted_text:
        raise ValueError("Cannot decrypt empty value")
    try:
        f = _get_fernet()
        return f.decrypt(encrypted_text.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt API key — invalid token or wrong ENCRYPTION_KEY")
        raise ValueError("Decryption failed — check ENCRYPTION_KEY")


def mask_api_key(key: str) -> str:
    """Mask an API key for safe display: shows first 4 and last 4 chars."""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"
