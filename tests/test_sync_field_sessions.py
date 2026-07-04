"""Regression test for BUG-1: per-field AsyncSessionLocal in _sync_fields.

`_sync_fields` fans field fetchers out through `asyncio.gather`. The location
and assets fetchers write/commit on the session they're given (via
get_structure's structure-name caching). If they all share the one
request-scoped `db` AsyncSession, concurrent execute/commit on a single
session raises greenlet / InvalidRequestError non-deterministically and the
per-field handler silently marks the field failed.

The fix gives each gathered fetcher its own `AsyncSessionLocal()` session and
its own Character row (PK lookup by character_id). This test proves each
fetcher receives a distinct session and a distinct Character instance, and
that neither is the outer `db` / outer `char`.

Sync-style (no pytest-asyncio): a single manually-managed event loop, per
tests/test_activity_history.py. A temp *file* DB (not `:memory:`) is used so
the concurrently-gathered sessions each get their own real connection —
`:memory:` forces a single shared StaticPool connection, which both fails to
model the fix and can raise spuriously under concurrent gather.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    Character,
    CharacterAssetCache,
    CharacterDashboardCache,
)
import app.routes.dashboard as dash


CHAR_ID = 90000001


def _make_char() -> Character:
    return Character(
        character_id=CHAR_ID,
        character_name="Test Pilot",
        access_token="dummy-access",
        refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes="",
        user_id=None,
    )


def test_each_fetcher_gets_its_own_session(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    captured_sessions: list = []
    captured_chars: list = []
    outer_db_ref: list = []
    outer_char_ref: list = []

    def _make_fake_fetcher():
        async def _fetcher(characters, db):
            captured_sessions.append(db)
            captured_chars.append(characters[0])
            return {}
        return _fetcher

    # Only two fake, always-stale fields exist; no real scope requirement.
    monkeypatch.setattr(dash, "FIELD_CACHE_SECONDS", {"fake_a": 3600, "fake_b": 3600})
    monkeypatch.setattr(dash, "FIELD_SCOPES", {"fake_a": "", "fake_b": ""})
    monkeypatch.setattr(dash, "_FIELD_FETCHERS", {
        "fake_a": _make_fake_fetcher(),
        "fake_b": _make_fake_fetcher(),
    })
    # `col = _FIELD_DB_COLUMN[field]` runs unconditionally in the loop; the
    # fake fields must be present (→ None, unused since val is None).
    patched_cols = dict(dash._FIELD_DB_COLUMN)
    patched_cols.update({"fake_a": None, "fake_b": None})
    monkeypatch.setattr(dash, "_FIELD_DB_COLUMN", patched_cols)

    # Fetchers spin up their own AsyncSessionLocal() sessions.
    monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)

    async def _scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Seed a committed Character so each fetcher's fresh-session PK
        # lookup (scalar_one) succeeds.
        async with SessionLocal() as seed_db:
            seed_db.add(_make_char())
            await seed_db.commit()

        # Build the ORM objects _sync_fields expects, mirroring
        # _sync_task_inner: cache + asset_cache committed on the outer db.
        outer_db = SessionLocal()
        outer_db_ref.append(outer_db)
        char = (await outer_db.execute(
            select(Character).where(Character.character_id == CHAR_ID)
        )).scalar_one()
        outer_char_ref.append(char)
        cache = CharacterDashboardCache(character_id=CHAR_ID)
        asset_cache = CharacterAssetCache(character_id=CHAR_ID)
        outer_db.add(cache)
        outer_db.add(asset_cache)
        await outer_db.commit()

        try:
            await dash._sync_fields(CHAR_ID, char, cache, asset_cache, outer_db)
        finally:
            await outer_db.close()

    try:
        loop.run_until_complete(_scenario())
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        os.unlink(tmp.name)

    # Both fake fields ran.
    assert len(captured_sessions) == 2, captured_sessions
    assert len(captured_chars) == 2, captured_chars
    # Each fetcher got its own distinct session...
    assert captured_sessions[0] is not captured_sessions[1]
    # ...and none of them is the outer request-scoped session.
    outer_db = outer_db_ref[0]
    assert all(s is not outer_db for s in captured_sessions)
    # Each fetcher got a Character loaded in its own session — never the
    # outer `char` instance (attribute access on it from another session's
    # coroutine would trigger cross-session lazy-load errors).
    outer_char = outer_char_ref[0]
    assert all(c is not outer_char for c in captured_chars)
    assert all(c.character_id == CHAR_ID for c in captured_chars)
