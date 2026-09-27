"""Tests for the T-072 can-fly engine (app/fitting/canfly.py).

Coverage:
1. evaluate_fit — pure function: can_fly true/false, missing sorted by
   biggest gap first (ties broken by need, then skill_id).
2. fit_skill_requirements — includes charges and drones, takes the max
   required level across the ship/modules/charges/drones, and skips cargo.
3. skill_reqs_cache_key changes when items change or the SDE stamp changes.
4. A cache hit (skill_reqs_key already matches) issues no SDETypeSkillReq
   query at all — can_fly_summary must serve the cached requirement set.
5. The user_fittings ALTER helper (ensure_user_fittings_skill_reqs_columns)
   adds both columns to an old-shape table and is idempotent.
"""
import asyncio
import json
import tempfile

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
from app.db.models import (
    Base, User, UserFitting, UserFittingFolder,
    ensure_user_fittings_skill_reqs_columns,
)
from app.fitting.canfly import (
    evaluate_fit, fit_skill_requirements, skill_reqs_cache_key, can_fly_summary,
)

USER_A = 8901
SHIP_TYPE_ID = 99101
MODULE_TYPE_ID = 99102
CHARGE_TYPE_ID = 99103
DRONE_TYPE_ID = 99104
CARGO_TYPE_ID = 99105

SKILL_SHIP = 33001     # required by the ship itself
SKILL_MODULE = 33002   # required by the module
SKILL_CHARGE = 33003   # required by the charge
SKILL_DRONE = 33004    # required by the drone
SKILL_SHARED = 33005   # required by both ship (lvl 2) and module (lvl 4) — max should win


def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _items_json():
    return json.dumps([
        {"type_id": MODULE_TYPE_ID, "slot": "high", "quantity": 1, "charge_type_id": CHARGE_TYPE_ID},
        {"type_id": DRONE_TYPE_ID, "slot": "drone", "quantity": 5},
        {"type_id": CARGO_TYPE_ID, "slot": "cargo", "quantity": 100},
    ])


def _seeded_db(sde_stamp: str = "2026-01-01T00:00:00+00:00"):
    """Temp-file sqlite DB with one saved fit (ship + module w/ charge +
    drone + cargo), the SDE skill-requirement rows backing it, and an
    sde_meta last_updated row. Returns (teardown, SessionLocal, fit_id, engine)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    holder: dict = {}

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A))
            db.add(sm.SDEMeta(key="last_updated", value=sde_stamp))
            for type_id, skill_id, level in [
                (SHIP_TYPE_ID, SKILL_SHIP, 3),
                (SHIP_TYPE_ID, SKILL_SHARED, 2),
                (MODULE_TYPE_ID, SKILL_MODULE, 4),
                (MODULE_TYPE_ID, SKILL_SHARED, 4),
                (CHARGE_TYPE_ID, SKILL_CHARGE, 1),
                (DRONE_TYPE_ID, SKILL_DRONE, 5),
                # Cargo's own skill requirement must never surface — if it
                # did, this would show up as a missing skill nobody trained.
                (CARGO_TYPE_ID, 33099, 5),
            ]:
                db.add(sm.SDETypeSkillReq(type_id=type_id, skill_type_id=skill_id, required_level=level))
            fit = UserFitting(
                user_id=USER_A, folder_id=None, name="Test Fit",
                ship_type_id=SHIP_TYPE_ID, items_json=_items_json(),
                implants_json="{}", boosters_json="{}",
            )
            db.add(fit)
            await db.commit()
            await db.refresh(fit)
            holder["fit_id"] = fit.id

    _run_async(_setup)

    def teardown():
        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)

    return teardown, SessionLocal, holder["fit_id"], engine


def _run(SessionLocal, fn):
    async def _wrapped():
        async with SessionLocal() as db:
            return await fn(db)
    return _run_async(lambda: _wrapped())


# ── 1. evaluate_fit ──────────────────────────────────────────────────────────

def test_evaluate_fit_can_fly_true_when_everything_trained():
    result = evaluate_fit({1: 5, 2: 3}, {1: 5, 2: 2})
    assert result == {"can_fly": True, "missing": []}


def test_evaluate_fit_reports_missing_sorted_by_biggest_gap_first():
    levels = {1: 0, 2: 3, 3: 1}
    reqs = {1: 2, 2: 5, 3: 5}
    result = evaluate_fit(levels, reqs)
    assert result["can_fly"] is False
    # gaps: skill1=2, skill2=2, skill3=4 -> skill3 first; skill1/skill2 tie
    # on gap, broken by need (2 vs 5) -> skill2 before skill1.
    gaps = [(m["skill_id"], m["have"], m["need"]) for m in result["missing"]]
    assert gaps == [(3, 1, 5), (2, 3, 5), (1, 0, 2)]


def test_evaluate_fit_untrained_skill_defaults_to_zero():
    result = evaluate_fit({}, {42: 1})
    assert result == {"can_fly": False, "missing": [{"skill_id": 42, "have": 0, "need": 1}]}


# ── 2. fit_skill_requirements ────────────────────────────────────────────────

def test_requirements_include_charges_and_drones_take_max_and_skip_cargo():
    teardown, SessionLocal, fit_id, engine = _seeded_db()
    try:
        async def _get(db):
            from sqlalchemy import select
            fit = (await db.execute(select(UserFitting).where(UserFitting.id == fit_id))).scalar_one()
            return await fit_skill_requirements(db, fit)
        reqs = _run(SessionLocal, _get)

        assert reqs[SKILL_SHIP] == 3
        assert reqs[SKILL_MODULE] == 4
        assert reqs[SKILL_CHARGE] == 1
        assert reqs[SKILL_DRONE] == 5
        assert reqs[SKILL_SHARED] == 4          # max(2, 4), not the ship's 2
        assert 33099 not in reqs                # cargo's own skill req never surfaces
    finally:
        teardown()


# ── 3. skill_reqs_cache_key ──────────────────────────────────────────────────

def test_cache_key_changes_with_items_and_with_sde_stamp():
    base = skill_reqs_cache_key(SHIP_TYPE_ID, "[]", "stamp-1")
    same = skill_reqs_cache_key(SHIP_TYPE_ID, "[]", "stamp-1")
    diff_items = skill_reqs_cache_key(SHIP_TYPE_ID, "[{\"type_id\": 1}]", "stamp-1")
    diff_stamp = skill_reqs_cache_key(SHIP_TYPE_ID, "[]", "stamp-2")

    assert base == same
    assert base != diff_items
    assert base != diff_stamp


# ── 4. Cache hit issues no SDE query ─────────────────────────────────────────

def test_cache_hit_issues_no_sde_type_skill_req_query():
    teardown, SessionLocal, fit_id, engine = _seeded_db()
    try:
        # First call populates the cache.
        async def _first(db):
            return await can_fly_summary(db, USER_A, {})
        _run(SessionLocal, _first)

        statements: list[str] = []

        def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
        try:
            async def _second(db):
                return await can_fly_summary(db, USER_A, {})
            summary = _run(SessionLocal, _second)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)

        assert summary["total"] == 1
        assert not any("sde_type_skill_reqs" in s.lower() for s in statements)
    finally:
        teardown()


def test_stale_cache_recomputes_and_a_second_call_hits_cache():
    teardown, SessionLocal, fit_id, engine = _seeded_db()
    try:
        async def _run_summary(db):
            return await can_fly_summary(db, USER_A, {SKILL_SHIP: 3, SKILL_MODULE: 4,
                                                       SKILL_CHARGE: 1, SKILL_DRONE: 5,
                                                       SKILL_SHARED: 4})
        summary1 = _run(SessionLocal, _run_summary)
        assert summary1["total"] == 1
        assert summary1["can_fly"] == 1
        assert summary1["fits"][0]["can_fly"] is True

        summary2 = _run(SessionLocal, _run_summary)
        assert summary2 == summary1
    finally:
        teardown()


# ── 5. Migration idempotency ─────────────────────────────────────────────────

def test_skill_reqs_columns_migration_is_idempotent():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _make_old_shape():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE user_fittings ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, folder_id INTEGER, "
                "name TEXT NOT NULL, description TEXT, ship_type_id INTEGER NOT NULL, "
                "items_json TEXT NOT NULL DEFAULT '[]', implants_json TEXT NOT NULL DEFAULT '{}', "
                "boosters_json TEXT NOT NULL DEFAULT '{}', "
                "created_at DATETIME, updated_at DATETIME)"
            ))

    async def _migrate_twice():
        async with SessionLocal() as db:
            await ensure_user_fittings_skill_reqs_columns(db)
        async with SessionLocal() as db:
            await ensure_user_fittings_skill_reqs_columns(db)  # must be a no-op

    async def _inspect():
        async with SessionLocal() as db:
            cols = {r[1] for r in (await db.execute(text("PRAGMA table_info(user_fittings)"))).fetchall()}
            return cols

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_make_old_shape())
        loop.run_until_complete(_migrate_twice())
        cols = loop.run_until_complete(_inspect())
        assert {"skill_reqs_json", "skill_reqs_key"} <= cols
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)
