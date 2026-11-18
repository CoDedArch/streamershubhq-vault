"""Encryption at rest for personal data (phone and mobile money numbers), and masking for display."""

import base64
import hashlib
import hmac
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    raw = settings.SH_DATA_KEY or f"sh-data::{settings.SECRET_KEY}"
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def lookup_hash(value: str) -> str:
    """A keyed hash for finding a row by an encrypted value (an investor's phone, say)."""
    return hmac.new(settings.SECRET_KEY.encode(), normalise_contact(value).encode(), hashlib.sha256).hexdigest()


def normalise_contact(value: str) -> str:
    value = (value or "").strip().lower()
    if "@" in value:
        return value
    digits = "".join(c for c in value if c.isdigit())
    if digits.startswith("0") and len(digits) == 10:  # 024 123 4567 -> 233241234567
        digits = "233" + digits[1:]
    return digits


def digits_tail(value: Optional[str], n: int) -> Optional[str]:
    digits = "".join(c for c in (value or "") if c.isdigit())
    return digits[-n:] if digits else None
