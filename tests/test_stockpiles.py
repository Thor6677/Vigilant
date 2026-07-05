"""Tests for stockpile watchlists (Phase 5 Task 3).

Service tests use the manual-event-loop + temp-file DB idiom (mirrors
tests/test_networth.py): a real sqlite file so the CRUD + `IN` queries execute
against the actual SQLite dialect. No ESI is ever hit — holdings come from
fixture asset JSON seeded into CharacterAssetCache.

Pure-math tests (sum_holdings / compute_deficit / build_rows) and the alert
checker (check_user_targets) run with plain fixtures + injected suppression
dict / emit callable, so they never touch the DB or the clock.

Route smoke tests use TestClient to prove the endpoints are auth-gated.
"""
import asyncio
import json
import tempfile
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    Character,
    CharacterAssetCache,
    StockpileTarget,
)
from app.stockpiles.alerts import check_user_targets
from app.stockpiles.holdings import (
    add_target,
    build_rows,
    compute_deficit,
    delete_target,
    holdings_for_user,
    list_targets,
    sum_holdings,
)

USER_ID = 7
CHAR_A = 90000001
CHAR_B = 90000002


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _temp_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _make_char(cid, user_id=USER_ID, is_active=True) -> Character:
    return Character(
        character_id=cid,
        character_name=f"Pilot {cid}",
        access_token="dummy-access",
        refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes="",
        user_id=user_id,
        is_active=is_active,
    )


# ── holdings math ───────────────────────────────────────────────────────────

def test_sum_holdings_sums_qty_across_lists_by_type():
    a = [{"type_id": 34, "quantity": 1000}, {"type_id": 35, "quantity": 5}]
    b = [{"type_id": 34, "quantity": 250}, {"type_id": 36, "quantity": 1}]
    totals = sum_holdings([a, b])
    assert totals == {34: 1250, 35: 5, 36: 1}


def test_sum_holdings_ignores_missing_type_and_bad_qty():
    assets = [
        {"type_id": 34, "quantity": 100},
        {"type_id": None, "quantity": 9},     # no type -> ignored
        {"type_id": 35},                      # missing qty -> 0
        {"type_id": 35, "quantity": "junk"},  # non-numeric -> 0
    ]
    assert sum_holdings([assets]) == {34: 100, 35: 0}


def test_sum_holdings_empty():
    assert sum_holdings([]) == {}
    assert sum_holdings([[], None]) == {}


def test_compute_deficit_floors_at_zero():
    assert compute_deficit(30, 100) == 70   # short
    assert compute_deficit(100, 100) == 0   # exactly met
    assert compute_deficit(120, 100) == 0   # surplus is not a deficit


def test_build_rows_joins_and_flags_under():
    targets = [
        StockpileTarget(id=1, user_id=USER_ID, type_id=34, target_qty=1000, note="ammo"),
        StockpileTarget(id=2, user_id=USER_ID, type_id=35, target_qty=10, note=None),
    ]
    holdings = {34: 250}  # 35 absent -> current 0
    names = {34: "Tritanium", 35: "Pyerite"}
    rows = build_rows(targets, holdings, names)

    r34 = next(r for r in rows if r["type_id"] == 34)
    assert r34["current"] == 250 and r34["target_qty"] == 1000
    assert r34["deficit"] == 750 and r34["under"] is True
    assert r34["type_name"] == "Tritanium" and r34["note"] == "ammo"

    r35 = next(r for r in rows if r["type_id"] == 35)
    assert r35["current"] == 0 and r35["deficit"] == 10 and r35["under"] is True


def test_build_rows_name_fallback_when_unknown():
    targets = [StockpileTarget(id=1, user_id=USER_ID, type_id=99999, target_qty=5)]
    rows = build_rows(targets, {99999: 10}, names={})
    assert rows[0]["type_name"] == "Type 99999"
    assert rows[0]["under"] is False and rows[0]["deficit"] == 0


# ── holdings_for_user: active-character filter + account-wide sum ────────────

def test_holdings_for_user_sums_active_chars_only():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char(CHAR_A, is_active=True))
            db.add(_make_char(CHAR_B, is_active=False))  # inactive -> excluded
            db.add(CharacterAssetCache(
                character_id=CHAR_A,
                assets_json=json.dumps([{"type_id": 34, "quantity": 500}])))
            db.add(CharacterAssetCache(
                character_id=CHAR_B,
                assets_json=json.dumps([{"type_id": 34, "quantity": 9999}])))
            await db.commit()

        async with SessionLocal() as db:
            holdings = await holdings_for_user(db, USER_ID)
        # Only the active character's 500 counts; the inactive alt is skipped.
        assert holdings == {34: 500}

    _run(scenario())


def test_holdings_for_user_empty_when_no_chars():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            assert await holdings_for_user(db, 424242) == {}

    _run(scenario())


# ── target CRUD ─────────────────────────────────────────────────────────────

def test_add_list_delete_target_roundtrip():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with SessionLocal() as db:
            t = await add_target(db, USER_ID, type_id=34, target_qty=1000, note=" ammo ")
            tid = t.id
        assert t.note == "ammo"  # trimmed

        async with SessionLocal() as db:
            targets = await list_targets(db, USER_ID)
        assert len(targets) == 1 and targets[0].type_id == 34

        async with SessionLocal() as db:
            removed = await delete_target(db, USER_ID, tid)
        assert removed is True

        async with SessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(StockpileTarget))).scalar()
        assert count == 0

    _run(scenario())


def test_delete_target_scoped_to_user():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            t = await add_target(db, USER_ID, type_id=34, target_qty=10)
            tid = t.id

        # A different user cannot delete USER_ID's target.
        async with SessionLocal() as db:
            removed = await delete_target(db, user_id=999, target_id=tid)
        assert removed is False

        async with SessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(StockpileTarget))).scalar()
        assert count == 1

    _run(scenario())


# ── alert checker: below emits once, suppressed, above doesn't ───────────────

def _rows(type_id, current, target):
    """One build_rows-shaped row for the checker."""
    return [{
        "id": 1, "type_id": type_id, "type_name": f"Type {type_id}",
        "target_qty": target, "current": current,
        "deficit": max(0, target - current), "under": current < target,
    }]


def test_alert_emits_once_then_suppressed():
    calls = []
    suppress = {}

    def fake_emit(user_id, event):
        calls.append((user_id, event))

    rows = _rows(34, current=30, target=100)  # under-stocked

    # First check at t=0 -> emits.
    n1 = check_user_targets(USER_ID, rows, now=0.0, suppress=suppress, emit=fake_emit)
    # Second check 1h later, still under -> suppressed (within 24h window).
    n2 = check_user_targets(USER_ID, rows, now=3600.0, suppress=suppress, emit=fake_emit)

    assert n1 == 1 and n2 == 0
    assert len(calls) == 1
    uid, event = calls[0]
    assert uid == USER_ID
    assert event["type"] == "stockpile_low"
    assert "Type 34" in event["body"]


def test_alert_reemits_after_suppression_window():
    calls = []
    suppress = {}

    def fake_emit(user_id, event):
        calls.append(event)

    rows = _rows(34, current=30, target=100)
    check_user_targets(USER_ID, rows, now=0.0, suppress=suppress, emit=fake_emit)
    # >24h later -> window expired -> emits again.
    n = check_user_targets(USER_ID, rows, now=24 * 3600 + 1, suppress=suppress, emit=fake_emit)
    assert n == 1 and len(calls) == 2


def test_alert_above_target_does_not_emit_and_clears_suppression():
    calls = []
    suppress = {(USER_ID, 34): 0.0}  # pre-existing suppression from a prior drop

    def fake_emit(user_id, event):
        calls.append(event)

    rows = _rows(34, current=150, target=100)  # now over target
    n = check_user_targets(USER_ID, rows, now=1000.0, suppress=suppress, emit=fake_emit)

    assert n == 0 and calls == []
    # Recovery clears the key so a fresh drop alerts immediately (not muted).
    assert (USER_ID, 34) not in suppress


def test_alert_recovered_then_dropped_emits_immediately():
    calls = []
    suppress = {}

    def fake_emit(user_id, event):
        calls.append(event)

    # Drop -> emit.
    check_user_targets(USER_ID, _rows(34, 30, 100), now=0.0, suppress=suppress, emit=fake_emit)
    # Recover shortly after -> clears suppression, no emit.
    check_user_targets(USER_ID, _rows(34, 200, 100), now=100.0, suppress=suppress, emit=fake_emit)
    # Drop again well within 24h of the FIRST alert -> emits (crossing-below).
    n = check_user_targets(USER_ID, _rows(34, 10, 100), now=200.0, suppress=suppress, emit=fake_emit)
    assert n == 1 and len(calls) == 2


# ── monkeypatched _emit_notification default path ───────────────────────────

def test_check_user_targets_default_emit_hits_choke_point(monkeypatch):
    captured = []
    from app.routes import dashboard
    monkeypatch.setattr(dashboard, "_emit_notification",
                        lambda uid, ev: captured.append((uid, ev)))

    # No suppress/emit passed -> uses module dict + _default_emit, which must
    # route through dashboard._emit_notification (late-bound via module import).
    from app.stockpiles import alerts
    monkeypatch.setattr(alerts, "_last_alert", {})  # isolate the shared dict
    n = check_user_targets(USER_ID, _rows(34, 0, 50), now=0.0)
    assert n == 1
    assert captured and captured[0][0] == USER_ID
    assert captured[0][1]["type"] == "stockpile_low"


# ── route smoke: auth gating ────────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_stockpiles_page_redirects_when_unauthenticated():
    r = _client().get("/tools/stockpiles", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def test_stockpiles_search_401_when_unauthenticated():
    r = _client().get("/tools/stockpiles/search?q=trit")
    assert r.status_code == 401


def test_stockpiles_add_rejected_when_unauthenticated():
    # CSRF middleware (403, no session token) or the handler's user_id gate
    # (401) — either way the state-mutating endpoint is unreachable.
    r = _client().post("/tools/stockpiles", data={"type_id": 34, "target_qty": 10})
    assert r.status_code in (401, 403)


def test_stockpiles_delete_rejected_when_unauthenticated():
    r = _client().delete("/tools/stockpiles/1")
    assert r.status_code in (401, 403)
