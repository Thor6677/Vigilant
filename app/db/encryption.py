"""Transparent at-rest encryption for sensitive database fields."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

_fernet = None


def _derive_key() -> bytes:
    settings = get_settings()
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        settings.secret_key.encode(),
        b"vigilant-token-encryption-salt",
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(dk)


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_key())
    return _fernet


class EncryptedText(TypeDecorator):
    """SQLAlchemy column type that encrypts on write and decrypts on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return get_fernet().decrypt(value.encode()).decode()
        except (InvalidToken, Exception):
            # Still plaintext (pre-migration) — return as-is
            return value
