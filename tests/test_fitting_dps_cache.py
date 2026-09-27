"""Tests for the persistent per-fit DPS cache (T-069 follow-up to ISS-018).

/tools/fitting/saved/dps used to run calculate_fitting_stats for every
saved fit, concurrently, on every page view — a real 135-fit account
measured 175s of 100%-CPU recompute on one call. Every fit's DPS is now
cached on the row (dps_cached/dps_cache_key), keyed by everything that can
change the number (app.fitting.engine.dps_cache_key), and the endpoint
only recomputes a small batch of stale fits per call.

Coverage:
1. A call against many stale fits recomputes at most DPS_STALE_BATCH_SIZE
   of them and reports the remainder as "pending".
2. A second call against the same (now all-cached) fits recomputes
   nothing.
3. Editing a fit (its items_json changing) invalidates only that fit.
4. Bumping the engine version, or the SDE version stamp, invalidates
   every fit at once (both are cache-key inputs, not just the fit itself).
5. The user_fittings migration covers dps_cached/dps_cache_key too (the
   fourth assertion lives in test_fitting_char_import.py's migration test,
   which this file doesn't duplicate).
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
import app.fitting.engine as engine_mod
import app.routes.fitting as fitting_mod
from app.db.models import Base, User, UserFitting, get_db

USER_A = 8801
_CSRF = "test-csrf-token-dpscache-0123456789"
SHIP_TYPE_ID = 99001


def _client():
    import app.main as main
    return TestClient(main.app, base_url="https://testserver")


def _authed_client(user_id=USER_A):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = _client()
    client.cookies.set("vigilant_session", cookie)
    return client


def _items_json(tag: int) -> str:
    # Content just needs to differ between fits (and change on "edit") —
    # calculate_fitting_stats itself is mocked in every test here.
    return json.dumps([{"type_id": 1000 + tag, "slot": "high", "quantity": 1}])


def _seeded_db(n_fits: int, sde_stamp: str = "2026-01-01T00:00:00+00:00"):
    """Temp-file sqlite DB with `n_fits` UserFitting rows for USER_A, none
    ever computed, plus an sde_meta last_updated row. Returns
    (teardown, SessionLocal, [fit_id, ...])."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    fit_ids: list[int] = []

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A))
            db.add(sm.SDEMeta(key="last_updated", value=sde_stamp))
            for i in range(n_fits):
                fit = UserFitting(
                    user_id=USER_A, folder_id=None, name=f"Test Fit {i}",
                    ship_type_id=SHIP_TYPE_ID, items_json=_items_json(i),
                    implants_json="{}", boosters_json="{}",
                )
                db.add(fit)
            await db.commit()
            rows = (await db.execute(UserFitting.__table__.select().where(
                UserFitting.user_id == USER_A))).fetchall()
            fit_ids.extend(r.id for r in rows)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(engine.dispose())
        finally:
            loop2.close()

    return teardown, SessionLocal, fit_ids


def _run_async(SessionLocal, fn):
    async def _wrapped():
        async with SessionLocal() as db:
            return await fn(db)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _fake_stats(calls: dict):
    async def fake(db, ship_type_id, items, *args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return {"total_dps": 123.4}
    return fake


# ── 1. Batch size + pending count ───────────────────────────────────────────

def test_stale_batch_is_capped_and_pending_reported(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", _fake_stats(calls))

    teardown, SessionLocal, fit_ids = _seeded_db(n_fits=40)
    try:
        r = _authed_client().get("/tools/fitting/saved/dps")
        assert r.status_code == 200
        body = r.json()
        assert calls.get("n", 0) == fitting_mod.DPS_STALE_BATCH_SIZE
        assert len(body["dps"]) == fitting_mod.DPS_STALE_BATCH_SIZE
        assert body["pending"] == 40 - fitting_mod.DPS_STALE_BATCH_SIZE
        assert all(v == 123.4 for v in body["dps"].values())
    finally:
        teardown()


# ── 2. Second call serves cache ─────────────────────────────────────────────

def test_second_call_serves_cache_without_recompute(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", _fake_stats(calls))

    teardown, SessionLocal, fit_ids = _seeded_db(n_fits=5)
    try:
        client = _authed_client()
        r1 = client.get("/tools/fitting/saved/dps")
        body1 = r1.json()
        assert body1["pending"] == 0
        assert calls["n"] == 5

        r2 = client.get("/tools/fitting/saved/dps")
        body2 = r2.json()
        assert calls["n"] == 5   # unchanged — nothing recomputed
        assert body2["pending"] == 0
        assert body2["dps"] == body1["dps"]
    finally:
        teardown()


# ── 3. Editing a fit invalidates only that fit ──────────────────────────────

def test_editing_a_fit_invalidates_only_that_fit(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", _fake_stats(calls))

    teardown, SessionLocal, fit_ids = _seeded_db(n_fits=5)
    try:
        client = _authed_client()
        client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 5

        edited_id = fit_ids[0]

        async def _edit(db):
            await db.execute(
                UserFitting.__table__.update()
                .where(UserFitting.id == edited_id)
                .values(items_json=_items_json(999))
            )
            await db.commit()
        _run_async(SessionLocal, _edit)

        r = client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 6   # exactly one more fit computed
        assert r.json()["pending"] == 0
    finally:
        teardown()


# ── 4. Engine version / SDE stamp bump invalidates everything ──────────────

def test_engine_version_bump_invalidates_all(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", _fake_stats(calls))

    teardown, SessionLocal, fit_ids = _seeded_db(n_fits=5)
    try:
        client = _authed_client()
        client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 5

        monkeypatch.setattr(engine_mod, "FITTING_ENGINE_VERSION", "test-bumped")
        r = client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 10   # all 5 recomputed (batch size >= 5)
        assert r.json()["pending"] == 0
    finally:
        teardown()


def test_sde_version_bump_invalidates_all(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", _fake_stats(calls))

    teardown, SessionLocal, fit_ids = _seeded_db(n_fits=5)
    try:
        client = _authed_client()
        client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 5

        async def _bump_sde(db):
            await db.execute(
                sm.SDEMeta.__table__.update()
                .where(sm.SDEMeta.key == "last_updated")
                .values(value="2027-01-01T00:00:00+00:00")
            )
            await db.commit()
        _run_async(SessionLocal, _bump_sde)

        r = client.get("/tools/fitting/saved/dps")
        assert calls["n"] == 10   # all 5 recomputed (batch size >= 5)
        assert r.json()["pending"] == 0
    finally:
        teardown()
