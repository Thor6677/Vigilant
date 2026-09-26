"""Transparent at-rest encryption for sensitive database fields."""

import base64
import hashlib
import logging
import re
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

_fernet = None

# Fernet's on-wire layout: 1-byte version + 8-byte timestamp + 16-byte IV +
# ciphertext (a multiple of 16 bytes, at least one block) + 32-byte HMAC.
# 57 is everything except the ciphertext block(s); 73 is the floor with
# exactly one block.
_FERNET_FIXED_LEN = 57
_FERNET_MIN_LEN = 73
_FERNET_VERSION_BYTE = 0x80
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")
_URLSAFE_TO_STANDARD = str.maketrans("-_", "+/")


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


def is_fernet_shaped(value: str | None) -> bool:
    """True if `value` could plausibly be a Fernet token under SOME key.

    Checks shape only — it can't verify the HMAC without the key that
    produced it. That's exactly the point: this is how the startup migration
    and the token-refresh path tell "ciphertext encrypted under a different
    SECRET_KEY" apart from a genuinely plaintext or opaque token, without
    being able to decrypt it.

    This gates the refresh path's short-circuit (see app/esi/client.py), so a
    false positive there would lock a character with a perfectly valid,
    still-refreshable token out of refresh. A single check (version byte +
    minimum length) leaves roughly a 1-in-256 false-positive rate on a long
    random opaque token, so the shape check is layered:
      - strict url-safe-base64 (rejects stray characters `urlsafe_b64decode`
        would otherwise silently ignore, and bad padding);
      - the decoded length lands exactly on a whole number of AES blocks
        past the fixed-size fields (version + timestamp + IV + HMAC);
      - the version byte is 0x80;
      - the 8-byte big-endian timestamp's top 4 bytes are zero, i.e. the
        timestamp is before the year 2106.
    Combined, a random blob's odds of passing are about 1 in 2^32.
    """
    if not value:
        return False
    if not _B64URL_RE.fullmatch(value):
        return False
    try:
        decoded = base64.b64decode(value.translate(_URLSAFE_TO_STANDARD), validate=True)
    except Exception:
        return False
    if len(decoded) < _FERNET_MIN_LEN:
        return False
    if (len(decoded) - _FERNET_FIXED_LEN) % 16 != 0:
        return False
    if decoded[0] != _FERNET_VERSION_BYTE:
        return False
    if decoded[1:5] != b"\x00\x00\x00\x00":
        return False
    return True


@dataclass
class TokenMigrationResult:
    encrypted: int = 0        # characters that had a plaintext token wrapped
    foreign_key: int = 0      # characters whose token is ciphertext this key can't read
    double_wrapped: int = 0   # characters whose token decrypts to ANOTHER key's ciphertext


async def migrate_token_encryption(db: AsyncSession) -> TokenMigrationResult:
    """Encrypt any plaintext ESI tokens in-place; leave foreign ciphertext alone.

    A value is classified per column (access_token, refresh_token):
      - empty/None: skipped.
      - decrypts under the current key, and what comes out is itself
        Fernet-shaped: this row was double-wrapped by an earlier (buggy)
        version of this migration, which treated old-key ciphertext it
        couldn't read as plaintext and re-encrypted it — old-key ciphertext
        underneath current-key ciphertext. Left unchanged (unwrapping it
        would need the old key, which this migration doesn't have) and
        counted separately below.
      - decrypts under the current key otherwise: left as-is.
      - Fernet-shaped but does NOT decrypt: ciphertext from a previous
        SECRET_KEY, not plaintext. Re-encrypting it would wrap it under the
        new key without ever recovering the plaintext underneath — worse
        than leaving it, since restoring the old key would no longer help
        either. Left unchanged and counted for the warning below.
      - anything else: plaintext, so it gets encrypted.

    Either kind of leftover foreign ciphertext is what the refresh path's own
    short-circuit (app/esi/client.py) actually catches at use time: for a
    double-wrapped row, `EncryptedText.process_result_value` peels the outer
    (current-key) layer for free and hands back the inner old-key ciphertext
    as the ORM-level token value, which is exactly what that short-circuit is
    built to recognize.
    """
    fernet = get_fernet()
    rows = (await db.execute(
        text("SELECT id, access_token, refresh_token FROM characters")
    )).fetchall()

    result = TokenMigrationResult()
    for char_id, raw_at, raw_rt in rows:
        new_at, new_rt = raw_at, raw_rt
        needs_update = False
        foreign_key = False
        double_wrapped = False

        for column, raw in (("access_token", raw_at), ("refresh_token", raw_rt)):
            if not raw:
                continue
            try:
                decrypted = fernet.decrypt(raw.encode())
            except Exception:
                decrypted = None

            if decrypted is not None:
                try:
                    inner = decrypted.decode()
                except UnicodeDecodeError:
                    inner = None
                if inner is not None and is_fernet_shaped(inner):
                    double_wrapped = True
                continue  # decrypts under the current key — leave it either way

            if is_fernet_shaped(raw):
                foreign_key = True
                continue  # ciphertext from a different key — leave it
            encrypted = fernet.encrypt(raw.encode()).decode()
            if column == "access_token":
                new_at = encrypted
            else:
                new_rt = encrypted
            needs_update = True

        if needs_update:
            await db.execute(
                text("UPDATE characters SET access_token = :at, refresh_token = :rt WHERE id = :id"),
                {"at": new_at, "rt": new_rt, "id": char_id},
            )
            result.encrypted += 1
        if foreign_key:
            result.foreign_key += 1
        if double_wrapped:
            result.double_wrapped += 1

    if result.encrypted:
        await db.commit()

    if result.encrypted:
        logging.info("Encrypted tokens for %d characters.", result.encrypted)
    if result.foreign_key:
        logging.warning(
            "%d character(s) have a stored token that is ciphertext under a "
            "different SECRET_KEY and cannot be refreshed as-is; those "
            "characters need to be re-authorized.",
            result.foreign_key,
        )
    if result.double_wrapped:
        logging.warning(
            "%d character(s) hold a token re-encrypted over old-key "
            "ciphertext by an earlier version of this migration; they need "
            "to be re-authorized.",
            result.double_wrapped,
        )
    return result
