"""ISS-061: expired d-scans are purged daily, in batches, and dscan_results
has the indexes that purge, the user's own list and user removal need.

Before this, an expired d-scan was only deleted when someone opened its link
again, so every paste ever made stayed in the database; and nothing indexed
user_id or expires_at.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models as models_mod
from app.db.models import Base, DScanResult, _create_missing_indexes, create_wallet_snapshot_index_background
from app.routes import dscan

EXPIRES_IX = "ix_dscan_results_expires_at"
USER_IX = "ix_dscan_results_user_created"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


async def _engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add(SessionLocal, n: int, *, expired: bool, user_id: int = 1, prefix: str = "s"):
    async with SessionLocal() as db:
        for i in range(n):
            created = NOW - timedelta(days=3 if expired else 0, minutes=i)
            db.add(DScanResult(
                id=f"{prefix}{'x' if expired else 'l'}{i}"[:12], paste_data="Test paste",
                parsed_json="[]", user_id=user_id, created_at=created,
                expires_at=(NOW - timedelta(hours=1)) if expired else (NOW + timedelta(hours=1)),
            ))
        await db.commit()


async def _ids(SessionLocal) -> list[str]:
    async with SessionLocal() as db:
        return sorted((await db.execute(select(DScanResult.id))).scalars().all())


async def _index_names(engine) -> set[str]:
    async with engine.connect() as conn:
        return {r[0] for r in (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dscan_results'")))}


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(dscan, "DSCAN_PURGE_PAUSE_SECONDS", 0)


def test_the_scheduler_runs_the_purge():
    import inspect
    from app.routes.dashboard import _background_scheduler
    src = inspect.getsource(_background_scheduler)
    assert "purge_expired_dscans()" in src
    assert "_last_dscan_purge" in src


def test_expired_rows_go_and_live_ones_stay(no_pause):
    async def go():
        engine, SessionLocal = await _engine()
        try:
            await _add(SessionLocal, 3, expired=True)
            await _add(SessionLocal, 2, expired=False)
            finished = await dscan.purge_expired_dscans(now=NOW, session_factory=SessionLocal)
            return finished, await _ids(SessionLocal)
        finally:
            await engine.dispose()

    finished, left = _run(go())
    assert finished is True
    assert left == ["sl0", "sl1"]


def test_the_purge_is_batched_and_resumes_past_its_cap(monkeypatch, no_pause):
    monkeypatch.setattr(dscan, "DSCAN_PURGE_BATCH", 2)
    monkeypatch.setattr(dscan, "DSCAN_PURGE_MAX_BATCHES", 2)
    commits = []

    async def go():
        engine, SessionLocal = await _engine()

        class CountingSession:
            def __init__(self):
                self._s = SessionLocal()

            async def __aenter__(self):
                db = await self._s.__aenter__()
                real_commit = db.commit

                async def commit():
                    commits.append(1)
                    await real_commit()
                db.commit = commit
                return db

            async def __aexit__(self, *exc):
                return await self._s.__aexit__(*exc)

        try:
            await _add(SessionLocal, 5, expired=True)
            await _add(SessionLocal, 1, expired=False)
            first = await dscan.purge_expired_dscans(now=NOW, session_factory=CountingSession)
            mid = await _ids(SessionLocal)
            second = await dscan.purge_expired_dscans(now=NOW, session_factory=CountingSession)
            return first, mid, second, await _ids(SessionLocal)
        finally:
            await engine.dispose()

    first, mid, second, end = _run(go())
    # 5 expired, 2 per batch, 2 batches per call: 4 go, the cap stops it, and
    # the scheduler's next tick finishes the job.
    assert first is False
    assert len(mid) == 2
    assert second is True
    assert end == ["sl0"]
    # One transaction per batch: 2 in the first call, 1 in the second.
    assert len(commits) == 3


def test_the_purge_waits_for_its_index(no_pause):
    async def go():
        engine, SessionLocal = await _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP INDEX {EXPIRES_IX}"))
            await _add(SessionLocal, 2, expired=True)
            finished = await dscan.purge_expired_dscans(now=NOW, session_factory=SessionLocal)
            return finished, await _ids(SessionLocal)
        finally:
            await engine.dispose()

    finished, left = _run(go())
    assert finished is False
    assert len(left) == 2


def test_the_indexes_are_built_after_startup_on_an_existing_table(monkeypatch):
    async def go():
        engine, _ = await _engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP INDEX {EXPIRES_IX}"))
                await conn.execute(text(f"DROP INDEX {USER_IX}"))
            # The blocking startup pass leaves them alone (prod row count unknown)...
            async with engine.begin() as conn:
                await conn.run_sync(_create_missing_indexes)
            after_startup = await _index_names(engine)
            # ...and the background builder adds them.
            monkeypatch.setattr(models_mod, "engine", engine)
            await create_wallet_snapshot_index_background(delay=0)
            return after_startup, await _index_names(engine)
        finally:
            await engine.dispose()

    after_startup, after_builder = _run(go())
    assert not ({EXPIRES_IX, USER_IX} & after_startup)
    assert {EXPIRES_IX, USER_IX} <= after_builder


def test_a_fresh_install_gets_the_indexes_straight_away():
    async def go():
        engine, _ = await _engine()
        try:
            return await _index_names(engine)
        finally:
            await engine.dispose()
    assert {EXPIRES_IX, USER_IX} <= _run(go())


def test_deferred_index_ddl_matches_the_declared_indexes():
    declared = {ix.name: [c.name for c in ix.columns]
                for t in Base.metadata.tables.values() for ix in t.indexes}
    for name, ddl in models_mod._DEFERRED_INDEX_DDL.items():
        assert name in declared, name
        cols = ddl.split("(", 1)[1].rstrip(")").replace(" ", "").split(",")
        assert cols == declared[name], (name, cols, declared[name])


async def _plan(engine, sql: str, params) -> str:
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)).fetchall()
    return " | ".join(r[3] for r in rows)


def _compiled(stmt):
    c = stmt.compile(dialect=sqlite.dialect())
    return str(c), tuple(1 for _ in c.positiontup)


def test_the_users_list_query_uses_the_user_index():
    # Same statement as app/routes/dscan.py:intel_page.
    stmt = (select(DScanResult)
            .where(DScanResult.user_id == 1, DScanResult.expires_at > NOW)
            .order_by(DScanResult.created_at.desc())
            .limit(50))

    async def go():
        engine, _ = await _engine()
        try:
            return await _plan(engine, *_compiled(stmt))
        finally:
            await engine.dispose()

    plan = _run(go())
    assert USER_IX in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_user_removal_and_the_purge_use_an_index():
    purge_ids = (select(DScanResult.id).where(DScanResult.expires_at < NOW)
                 .limit(dscan.DSCAN_PURGE_BATCH))

    async def go():
        engine, _ = await _engine()
        try:
            removal = await _plan(engine, "DELETE FROM dscan_results WHERE user_id = ?", (1,))
            purge = await _plan(engine, *_compiled(purge_ids))
            return removal, purge
        finally:
            await engine.dispose()

    removal, purge = _run(go())
    assert USER_IX in removal, removal
    assert EXPIRES_IX in purge, purge
