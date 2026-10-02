"""ISS-084: a sync that times out, is cancelled or fails commits only its
error status, never the results it was holding.

_sync_fields commits its results once, after the removal check (ISS-060) and
the narrowing check (ISS-079). When it stops anywhere before that commit, the
handlers in _sync_task_inner record the failure with a commit of their own.
That commit must carry the status alone. If it also carried what the session
had pending or had already flushed (a balance, a wallet snapshot, the roles
row), those would land without either check.

Each test stops the sync at the narrowing check made after its flush, the
last point before the real commit, when every result is already in the
session's open write transaction.
"""
import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 (populate Base.metadata)
import app.db.sde_models  # noqa: F401
import app.routes.dashboard as dash
from app.db.models import (
    Base, Character, CharacterAssetCache, CharacterDashboardCache, ensure_wal,
)

CID = 93200001
SEEN_SYNCED = json.dumps({"wallet": "2026-09-01T00:00:00+00:00"})


async def _wallet(chars, db):
    return {chars[0].character_id: (123.0, None)}


async def _assets(chars, db):
    return {chars[0].character_id: ([{"type_id": 34, "quantity": 5}], None)}


async def _roles(chars, db):
    return {chars[0].character_id: ({"roles": ["Director"]}, None)}


async def _skillqueue(chars, db):
    return {chars[0].character_id: ([{"skill_id": 3300, "finished_level": 5}], None)}


FETCHERS = {"wallet": _wallet, "assets": _assets, "roles": _roles, "skillqueue": _skillqueue}


@pytest.mark.parametrize("mode", ["timeout", "cancel", "error"])
def test_a_sync_stopped_after_its_flush_commits_only_its_status(monkeypatch, tmp_path, mode):
    url = f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}"
    ensure_wal(url)
    monkeypatch.setattr(dash, "FIELD_CACHE_SECONDS", {f: 0 for f in FETCHERS})
    monkeypatch.setattr(dash, "FIELD_SCOPES", {f: None for f in FETCHERS})
    monkeypatch.setattr(dash, "_FIELD_FETCHERS", FETCHERS)
    monkeypatch.setattr(dash, "_SYNC_TIMEOUT", 1)

    real_check = dash._drop_if_narrowed
    checks = []
    stopped = asyncio.Event()

    async def check(db, character_id, started_scopes):
        checks.append(character_id)
        if len(checks) == 2:    # the check made once the results are flushed
            stopped.set()
            if mode == "error":
                raise RuntimeError("ESI went away")
            await asyncio.sleep(30)
        return await real_check(db, character_id, started_scopes)
    monkeypatch.setattr(dash, "_drop_if_narrowed", check)

    async def go():
        engine = create_async_engine(url)
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with SessionLocal() as db:
                db.add(Character(character_id=CID, character_name="Pilot A", user_id=None,
                                 access_token="x", refresh_token="y",
                                 token_expiry=datetime(2099, 1, 1), scopes=""))
                db.add(CharacterDashboardCache(character_id=CID, wallet=500.0, skillqueue_json="[]",
                                               field_synced_json=SEEN_SYNCED))
                db.add(CharacterAssetCache(character_id=CID, assets_json="[]"))
                await db.commit()

            if mode == "cancel":
                task = asyncio.create_task(dash._sync_task_inner(CID))
                await asyncio.wait_for(stopped.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await asyncio.wait_for(dash._sync_task_inner(CID), 10)
            assert stopped.is_set()

            async with SessionLocal() as db:
                cache = (await db.execute(select(CharacterDashboardCache).where(
                    CharacterDashboardCache.character_id == CID))).scalar_one()
                row = (await db.execute(text(
                    "SELECT (SELECT count(*) FROM wallet_snapshots),"
                    " (SELECT count(*) FROM character_corp_roles),"
                    " (SELECT assets_json FROM character_asset_cache)"))).one()
            return cache, tuple(row)
        finally:
            await engine.dispose()

    cache, (snapshots, roles_rows, assets_json) = asyncio.run(go())
    assert (snapshots, roles_rows, assets_json) == (0, 0, "[]")
    assert (cache.wallet, cache.skillqueue_json, cache.field_synced_json) == (500.0, "[]", SEEN_SYNCED)
    assert cache.last_synced is None
    assert cache.sync_status == "error"
    assert cache.sync_error == {
        "timeout": "timeout after 1s",
        "cancel": "CancelledError: ",
        "error": "RuntimeError: ESI went away",
    }[mode]
