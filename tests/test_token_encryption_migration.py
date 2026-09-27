"""Tests the startup ESI-token encryption migration in isolation.

Same pattern as test_ambient_kills.py / test_isk_backfill.py: the extracted
function is exercised directly against an in-memory SQLite `characters`
table, sync-style with an explicit event loop (see test_ambient_kills.py's
docstring for why — Python 3.14 removed the implicit auto-created loop that
asyncio.get_event_loop() used to lazily create).

The scenario under test is a SECRET_KEY rotation: some rows were written
under an old key ("key A") and the app is now running with a new one
("key B"). `_use_key` swaps SECRET_KEY and clears both caches the crypto
layer keeps — get_settings()'s lru_cache and encryption.py's module-level
_fernet singleton — so tests can move between keys without leaking state
into each other.
"""
import asyncio
import base64

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.db.encryption as enc
from app.config import get_settings
from app.db.models import Character
from app.db.encryption import migrate_token_encryption


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Character.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


@pytest.fixture(autouse=True)
def _reset_crypto_caches(monkeypatch):
    """Every test starts and ends on conftest's SECRET_KEY=test-secret, with
    a clean derived key cached. Without this, whichever test ran last (or
    ran first, before this fixture existed) leaves its key active."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    get_settings.cache_clear()
    enc._fernet = None
    yield
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    get_settings.cache_clear()
    enc._fernet = None


def _use_key(monkeypatch, key: str) -> None:
    monkeypatch.setenv("SECRET_KEY", key)
    get_settings.cache_clear()
    enc._fernet = None


async def _insert_character(session_factory, char_id: int, access_token, refresh_token):
    from datetime import datetime, timezone
    from sqlalchemy import text

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO characters "
                "(id, character_id, character_name, access_token, refresh_token, "
                "token_expiry, scopes, declined_scopes) "
                "VALUES (:id, :cid, 'Test Pilot', :at, :rt, :exp, '', '')"
            ),
            {
                "id": char_id,
                "cid": 1_000_000 + char_id,
                "at": access_token,
                "rt": refresh_token,
                "exp": datetime.now(timezone.utc),
            },
        )
        await s.commit()


async def _read_character(session_factory, char_id: int):
    from sqlalchemy import text

    async with session_factory() as s:
        row = (await s.execute(
            text("SELECT access_token, refresh_token FROM characters WHERE id = :id"),
            {"id": char_id},
        )).fetchone()
    return row


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_plaintext_tokens_are_encrypted(session_factory):
    _run(_insert_character(session_factory, 1, "plain-access-token", "plain-refresh-token"))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    result = _run(go())
    assert result.encrypted == 1
    assert result.foreign_key == 0

    row = _run(_read_character(session_factory, 1))
    fernet = enc.get_fernet()
    assert fernet.decrypt(row[0].encode()).decode() == "plain-access-token"
    assert fernet.decrypt(row[1].encode()).decode() == "plain-refresh-token"


def test_current_key_ciphertext_is_left_untouched(session_factory):
    fernet = enc.get_fernet()
    at = fernet.encrypt(b"already-encrypted-access").decode()
    rt = fernet.encrypt(b"already-encrypted-refresh").decode()
    _run(_insert_character(session_factory, 1, at, rt))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    result = _run(go())
    assert result.encrypted == 0
    assert result.foreign_key == 0

    row = _run(_read_character(session_factory, 1))
    assert row == (at, rt)


def test_old_key_ciphertext_is_left_untouched_and_counted(session_factory, monkeypatch):
    # Encrypt under key A, then run the migration under key B.
    _use_key(monkeypatch, "key-a")
    old_fernet = enc.get_fernet()
    at = old_fernet.encrypt(b"old-key-access").decode()
    rt = old_fernet.encrypt(b"old-key-refresh").decode()
    _run(_insert_character(session_factory, 1, at, rt))

    _use_key(monkeypatch, "key-b")

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    result = _run(go())
    assert result.encrypted == 0
    assert result.foreign_key == 1

    # Unchanged — not re-wrapped under key B, and NOT lost.
    row = _run(_read_character(session_factory, 1))
    assert row == (at, rt)
    # And still recoverable if key A is restored.
    assert old_fernet.decrypt(row[0].encode()).decode() == "old-key-access"
    assert old_fernet.decrypt(row[1].encode()).decode() == "old-key-refresh"


def test_none_or_empty_tokens_are_skipped(session_factory):
    # access_token/refresh_token are NOT NULL in the real schema, so an empty
    # string is the representable case, but it exercises the same falsy
    # early-continue branch a None value would.
    _run(_insert_character(session_factory, 1, "", ""))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    result = _run(go())
    assert result.encrypted == 0
    assert result.foreign_key == 0

    row = _run(_read_character(session_factory, 1))
    assert row == ("", "")


def test_second_run_is_a_no_op(session_factory, monkeypatch):
    _use_key(monkeypatch, "key-a")
    old_fernet = enc.get_fernet()
    old_ct_at = old_fernet.encrypt(b"old-key-access").decode()
    old_ct_rt = old_fernet.encrypt(b"old-key-refresh").decode()
    _run(_insert_character(session_factory, 1, "plaintext-at", "plaintext-rt"))
    _run(_insert_character(session_factory, 2, old_ct_at, old_ct_rt))

    _use_key(monkeypatch, "key-b")

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    first = _run(go())
    assert first.encrypted == 1     # character 1
    assert first.foreign_key == 1   # character 2

    row1_after_first = _run(_read_character(session_factory, 1))
    row2_after_first = _run(_read_character(session_factory, 2))

    second = _run(go())
    assert second.encrypted == 0
    assert second.foreign_key == 1  # still flagged — nothing was mutated

    assert _run(_read_character(session_factory, 1)) == row1_after_first
    assert _run(_read_character(session_factory, 2)) == row2_after_first


def test_jwt_shaped_access_token_is_not_classed_as_fernet(session_factory):
    # A real EVE SSO access token: three dot-separated base64url segments.
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJDSEFSQUNURVI6RVZFOjEyMzQ1Njc4OSJ9.sig"
    _run(_insert_character(session_factory, 1, jwt_like, "short-opaque-refresh"))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    result = _run(go())
    # Neither value is Fernet-shaped, so both are treated as plaintext and
    # wrapped — not left alone as "foreign key" ciphertext.
    assert result.encrypted == 1
    assert result.foreign_key == 0

    row = _run(_read_character(session_factory, 1))
    fernet = enc.get_fernet()
    assert fernet.decrypt(row[0].encode()).decode() == jwt_like
    assert fernet.decrypt(row[1].encode()).decode() == "short-opaque-refresh"


def test_is_fernet_shaped_rejects_jwt_and_short_opaque_values():
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJDSEFSQUNURVI6RVZFOjEyMzQ1Njc4OSJ9.sig"
    assert not enc.is_fernet_shaped(jwt_like)
    assert not enc.is_fernet_shaped("short-opaque-refresh")
    assert not enc.is_fernet_shaped("")
    assert not enc.is_fernet_shaped(None)


def test_is_fernet_shaped_accepts_a_real_fernet_token():
    token = Fernet.generate_key()
    ct = Fernet(token).encrypt(b"anything").decode()
    assert enc.is_fernet_shaped(ct)


@pytest.fixture()
def bare_session_factory():
    """A `characters` table with no NOT NULL constraints on the token
    columns. The real schema forbids NULL there, but the migration only ever
    reads/writes access_token, refresh_token and id through raw SQL, so a
    minimal table is enough to exercise a real `None` value — which the
    ORM-backed fixture above can't represent."""
    from sqlalchemy import text

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE characters (id INTEGER PRIMARY KEY, "
                "access_token TEXT, refresh_token TEXT)"
            ))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def test_none_access_token_is_skipped_and_refresh_token_still_migrates(bare_session_factory):
    from sqlalchemy import text

    async def go():
        async with bare_session_factory() as s:
            await s.execute(text(
                "INSERT INTO characters (id, access_token, refresh_token) "
                "VALUES (1, NULL, 'plaintext-refresh')"
            ))
            await s.commit()
        async with bare_session_factory() as db:
            result = await migrate_token_encryption(db)
        async with bare_session_factory() as s:
            row = (await s.execute(
                text("SELECT access_token, refresh_token FROM characters WHERE id = 1")
            )).fetchone()
        return result, row

    result, row = _run(go())
    assert result.encrypted == 1
    assert result.foreign_key == 0
    assert row[0] is None
    fernet = enc.get_fernet()
    assert fernet.decrypt(row[1].encode()).decode() == "plaintext-refresh"


def test_warning_reports_the_count_and_never_logs_token_values(session_factory, monkeypatch, caplog):
    import logging

    _use_key(monkeypatch, "key-a")
    old_fernet = enc.get_fernet()
    at = old_fernet.encrypt(b"super-secret-access-value").decode()
    rt = old_fernet.encrypt(b"super-secret-refresh-value").decode()
    _run(_insert_character(session_factory, 1, at, rt))
    _run(_insert_character(session_factory, 2, "plaintext-that-should-not-appear-in-logs", "also-plaintext-secret"))

    _use_key(monkeypatch, "key-b")

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    with caplog.at_level(logging.INFO):
        result = _run(go())

    assert result.foreign_key == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1" in warnings[0].getMessage()

    haystack = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (at, rt, "super-secret-access-value", "super-secret-refresh-value",
                   "plaintext-that-should-not-appear-in-logs", "also-plaintext-secret"):
        assert secret not in haystack


def test_double_wrapped_ciphertext_is_left_unchanged_and_counted(session_factory, monkeypatch, caplog):
    """Simulates what the OLD (buggy) migration actually did on prod: it
    found old-key ciphertext undecryptable, decided that meant "plaintext",
    and encrypted it again under the current key. The new migration must
    neither re-wrap it a third time nor mistake it for foreign-key
    ciphertext — it decrypts fine one layer down, so the outer layer alone
    looks completely ordinary."""
    import logging

    _use_key(monkeypatch, "key-a")
    key_a_fernet = enc.get_fernet()
    inner_at = key_a_fernet.encrypt(b"double-wrapped-access").decode()
    inner_rt = key_a_fernet.encrypt(b"double-wrapped-refresh").decode()

    _use_key(monkeypatch, "key-b")
    key_b_fernet = enc.get_fernet()
    outer_at = key_b_fernet.encrypt(inner_at.encode()).decode()
    outer_rt = key_b_fernet.encrypt(inner_rt.encode()).decode()
    _run(_insert_character(session_factory, 1, outer_at, outer_rt))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    with caplog.at_level(logging.INFO):
        result = _run(go())

    assert result.encrypted == 0
    assert result.foreign_key == 0
    assert result.double_wrapped == 1

    row = _run(_read_character(session_factory, 1))
    assert row == (outer_at, outer_rt)  # untouched — not re-wrapped a third time

    # Still recoverable, in principle, by peeling both layers in the right order.
    peeled_at = key_b_fernet.decrypt(row[0].encode()).decode()
    assert key_a_fernet.decrypt(peeled_at.encode()).decode() == "double-wrapped-access"

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("1" in m for m in warnings)
    for secret in (outer_at, outer_rt, inner_at, inner_rt,
                   "double-wrapped-access", "double-wrapped-refresh"):
        for m in warnings:
            assert secret not in m


def test_double_wrapped_second_run_is_still_a_no_op(session_factory, monkeypatch):
    _use_key(monkeypatch, "key-a")
    key_a_fernet = enc.get_fernet()
    inner = key_a_fernet.encrypt(b"double-wrapped").decode()

    _use_key(monkeypatch, "key-b")
    key_b_fernet = enc.get_fernet()
    outer = key_b_fernet.encrypt(inner.encode()).decode()
    _run(_insert_character(session_factory, 1, outer, outer))

    async def go():
        async with session_factory() as db:
            return await migrate_token_encryption(db)

    first = _run(go())
    row_after_first = _run(_read_character(session_factory, 1))
    second = _run(go())
    row_after_second = _run(_read_character(session_factory, 1))

    assert first.double_wrapped == 1
    assert second.double_wrapped == 1  # still flagged, still not mutated
    assert row_after_first == row_after_second == (outer, outer)


def _fake_fernet_blob(*, version=0x80, ts_hi=0, ts_lo=1_700_000_000, block_count=1, extra_bytes=0):
    """Hand-builds bytes shaped like a Fernet token (without a valid HMAC —
    is_fernet_shaped never checks that) so tests can control the version
    byte, timestamp, and ciphertext length independently of what a real
    Fernet.encrypt() would ever actually produce."""
    import os
    import struct

    ts = struct.pack(">II", ts_hi, ts_lo)
    iv = os.urandom(16)
    ciphertext = os.urandom(16 * block_count + extra_bytes)
    hmac = os.urandom(32)
    raw = bytes([version]) + ts + iv + ciphertext + hmac
    return base64.urlsafe_b64encode(raw).decode()


def test_is_fernet_shaped_rejects_a_nonzero_timestamp_high_word():
    # Right version byte, right block alignment, but a timestamp whose top
    # 4 bytes are nonzero — the shape a truly random 0x80-prefixed blob of
    # the right length would have roughly 1 in 2^32 times less often than
    # the old (unchecked) implementation would have accepted it.
    blob = _fake_fernet_blob(ts_hi=1)
    assert not enc.is_fernet_shaped(blob)


def test_is_fernet_shaped_rejects_a_non_block_aligned_length():
    blob = _fake_fernet_blob(ts_hi=0, block_count=1, extra_bytes=10)
    assert not enc.is_fernet_shaped(blob)


def test_is_fernet_shaped_rejects_invalid_base64_characters():
    blob = _fake_fernet_blob(ts_hi=0)
    corrupted = blob[:5] + "!" + blob[6:]
    assert not enc.is_fernet_shaped(corrupted)


def test_is_fernet_shaped_accepts_a_correctly_shaped_blob():
    blob = _fake_fernet_blob(ts_hi=0, block_count=2)
    assert enc.is_fernet_shaped(blob)
