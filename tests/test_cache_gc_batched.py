"""v1.7.2 Stream A: connection pragmas (ISS-075), batched esi_cache GC
(ISS-073) and the admin purge's time comparison (ISS-071)."""
import asyncio
import base64
import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.cache as cache_mod
import app.db.models as models
from app.db.cache import ESICache, cache_gc, cache_gc_run
from app.db.models import Base, User, get_db


# ── ISS-075: pragmas ────────────────────────────────────────────────────────

class _RecordingConn:
    def __init__(self):
        self.statements = []

    def cursor(self):
        outer = self

        class _Cur:
            def execute(self, sql, *a):
                outer.statements.append(sql)

            def close(self):
                pass
        return _Cur()


def test_connect_hook_sets_busy_timeout_first_and_never_touches_journal_mode():
    conn = _RecordingConn()
    models._sqlite_pragmas(conn, None)
    assert conn.statements[0] == "PRAGMA busy_timeout=10000"
    assert not any("journal_mode" in s for s in conn.statements)


def test_ensure_wal_leaves_a_fresh_file_in_wal():
    path = tempfile.mkdtemp(prefix="vigilant-wal-") + "/fresh.db"
    assert models.ensure_wal(f"sqlite+aiosqlite:///{path}") == "wal"
    c = sqlite3.connect(path)
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        c.close()


def test_ensure_wal_ignores_non_sqlite_urls():
    assert models.ensure_wal("postgresql://example/none") == ""


def test_pooled_connection_reads_busy_timeout_10000():
    async def go():
        async with models.engine.connect() as conn:
            return (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    try:
        assert asyncio.run(go()) == 10000
    finally:
        asyncio.run(models.engine.dispose())


# ── ISS-073: batched GC ─────────────────────────────────────────────────────

NOW = datetime(2026, 6, 15, 12, 0, 0)  # naive UTC, pinned


@pytest.fixture
def gc_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    models.ensure_wal(f"sqlite+aiosqlite:///{tmp.name}")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(setup())

    monkeypatch.setattr(models, "AsyncSessionLocal", SessionLocal)
    monkeypatch.setattr(cache_mod, "_gc_running", False)
    pauses = []

    async def pause():
        pauses.append(1)
    monkeypatch.setattr(cache_mod, "_between_gc_batches", pause)

    class Env:
        pass
    env = Env()
    env.engine, env.SessionLocal, env.pauses = engine, SessionLocal, pauses
    yield env
    asyncio.run(engine.dispose())


def _seed(env, expired, live, expired_at=NOW - timedelta(hours=1), live_at=NOW + timedelta(hours=6)):
    async def go():
        async with env.SessionLocal() as db:
            for i in range(expired):
                db.add(ESICache(key=f"old{i}", data="{}", expires_at=expired_at))
            for i in range(live):
                db.add(ESICache(key=f"live{i}", data="{}", expires_at=live_at))
            await db.commit()
    asyncio.run(go())


def _keys(env):
    async def go():
        async with env.SessionLocal() as db:
            return {r[0] for r in (await db.execute(text("SELECT key FROM esi_cache"))).all()}
    return asyncio.run(go())


def test_gc_deletes_expired_in_batches_and_keeps_live_rows(gc_env):
    _seed(gc_env, expired=25, live=3)
    res = asyncio.run(cache_gc_run(now=NOW, batch_rows=10))
    assert (res.removed, res.complete) == (25, True)
    assert _keys(gc_env) == {"live0", "live1", "live2"}
    assert len(gc_env.pauses) == 2  # 10 + 10 + 5: paused after the two full batches


def test_gc_returns_int_through_the_cache_gc_wrapper(gc_env):
    _seed(gc_env, expired=4, live=1)
    assert asyncio.run(cache_gc(now=NOW)) == 4


def test_gc_time_cap_leaves_the_rest_for_the_next_run(gc_env):
    _seed(gc_env, expired=25, live=0)
    res = asyncio.run(cache_gc_run(now=NOW, batch_rows=10, max_seconds=0))
    assert res.removed == 10 and res.complete is False
    assert len(_keys(gc_env)) == 15
    res2 = asyncio.run(cache_gc_run(now=NOW, batch_rows=10))
    assert res2.removed == 15 and res2.complete is True


def test_gc_failure_mid_run_counts_committed_batches_and_logs(gc_env, monkeypatch, caplog):
    _seed(gc_env, expired=25, live=0)
    real = gc_env.SessionLocal
    opened = []

    def flaky():
        opened.append(1)
        if len(opened) == 2:
            raise RuntimeError("database is locked")
        return real()
    monkeypatch.setattr(models, "AsyncSessionLocal", flaky)
    with caplog.at_level(logging.WARNING, logger="app.db.cache"):
        res = asyncio.run(cache_gc_run(now=NOW, batch_rows=10))
    assert res.removed == 10 and res.complete is False
    assert any("ESI cache GC stopped" in r.message for r in caplog.records)


def test_a_second_gc_does_not_overlap_a_running_one(gc_env, monkeypatch):
    _seed(gc_env, expired=3, live=0)
    monkeypatch.setattr(cache_mod, "_gc_running", True)
    res = asyncio.run(cache_gc_run(now=NOW))
    assert res.skipped and res.removed == 0 and len(_keys(gc_env)) == 3


def test_a_writer_can_commit_between_batches(gc_env, monkeypatch):
    """The seam fires between short transactions; a write there is not blocked."""
    _seed(gc_env, expired=25, live=0)
    wrote = []

    async def writer_in_gap():
        async with gc_env.SessionLocal() as db:
            await db.execute(text(
                "INSERT INTO esi_cache (key, data, expires_at) VALUES ('mid', '{}', '2099-01-01 00:00:00')"))
            await db.commit()
        wrote.append(1)
    monkeypatch.setattr(cache_mod, "_between_gc_batches", writer_in_gap)
    asyncio.run(cache_gc_run(now=NOW, batch_rows=10))
    assert wrote and "mid" in _keys(gc_env)


def test_inner_select_uses_the_expires_at_index(gc_env):
    async def plan():
        async with gc_env.SessionLocal() as db:
            rows = (await db.execute(text(
                "EXPLAIN QUERY PLAN SELECT rowid FROM esi_cache WHERE expires_at < :now LIMIT :n"),
                {"now": "2026-06-15 12:00:00.000000", "n": 5000})).all()
            return " ".join(str(r[-1]) for r in rows)
    assert "ix_esi_cache_expires_at" in asyncio.run(plan())


def test_scheduler_wrapper_logs_the_removed_count(gc_env, monkeypatch, caplog):
    import app.routes.dashboard as dash
    _seed(gc_env, expired=3, live=0, expired_at=datetime(2020, 1, 1))
    with caplog.at_level(logging.INFO):
        asyncio.run(dash._run_cache_gc())
    assert any("ESI cache GC removed 3 expired rows" in r.message for r in caplog.records)


# ── ISS-071: admin purge ────────────────────────────────────────────────────

ADMIN_ID = 910
CSRF = "test-csrf-token"


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        return base if tz else base.replace(tzinfo=None)


@pytest.fixture
def admin_client(gc_env, monkeypatch):
    import app.main as main
    import app.routes.admin as admin_mod

    async def seed():
        async with gc_env.SessionLocal() as db:
            db.add(User(id=ADMIN_ID, role="admin", is_admin=True))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with gc_env.SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    monkeypatch.setattr(cache_mod, "datetime", _FixedDT)
    monkeypatch.setattr(admin_mod, "datetime", _FixedDT)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": ADMIN_ID, "csrf_token": CSRF}).encode())
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", signer.sign(data).decode())
    c.headers.update({"X-CSRF-Token": CSRF})
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def test_admin_purge_keeps_rows_that_expire_later_the_same_day(gc_env, admin_client):
    # Clock pinned at 2026-06-15 12:00 UTC. Stored values are naive,
    # space-separated; a string compare against 'T...+00:00' would call the
    # 18:00 row expired.
    async def go():
        async with gc_env.SessionLocal() as db:
            db.add(ESICache(key="expired", data="{}", expires_at=datetime(2026, 6, 15, 11, 0)))
            db.add(ESICache(key="later-today", data="{}", expires_at=datetime(2026, 6, 15, 18, 0)))
            db.add(ESICache(key="tomorrow", data="{}", expires_at=datetime(2026, 6, 16, 1, 0)))
            await db.commit()
    asyncio.run(go())
    r = admin_client.post("/admin/action/cache-purge")
    assert r.status_code == 200
    assert "Purged 1 expired cache entries." in r.text
    assert _keys(gc_env) == {"later-today", "tomorrow"}


def _audit_details(env):
    async def go():
        async with env.SessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT detail FROM admin_audit_log WHERE event_type = 'admin_cache_purge'"))).all()
            return [r[0] for r in rows]
    return asyncio.run(go())


def test_admin_purge_writes_no_audit_row_when_another_cleanup_is_running(gc_env, admin_client, monkeypatch):
    monkeypatch.setattr(cache_mod, "_gc_running", True)
    r = admin_client.post("/admin/action/cache-purge")
    assert r.status_code == 200
    assert "already running" in r.text
    assert _audit_details(gc_env) == []


def test_admin_purge_audit_notes_an_early_stop(gc_env, admin_client, monkeypatch):
    real = cache_mod.cache_gc_run

    async def capped(now=None, max_seconds=None, batch_rows=None):
        return await real(now, 0, 10)  # stop after one 10-row batch
    monkeypatch.setattr(cache_mod, "cache_gc_run", capped)
    _seed(gc_env, expired=25, live=0, expired_at=datetime(2026, 6, 15, 11, 0))
    r = admin_client.post("/admin/action/cache-purge")
    assert "more remain" in r.text
    assert _audit_details(gc_env) == ["Purged 10 expired cache entries (stopped early; more remain)"]


def test_admin_purge_audit_for_a_complete_run(gc_env, admin_client):
    _seed(gc_env, expired=2, live=0, expired_at=datetime(2026, 6, 15, 11, 0))
    admin_client.post("/admin/action/cache-purge")
    assert _audit_details(gc_env) == ["Purged 2 expired cache entries"]
