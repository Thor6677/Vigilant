"""T-082: scope loading and wreck valuation. Ids and names are invented."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Killmail, KillmailItem
from app.intel import battle_loot as bl

SYS_A, SYS_B = 30009001, 30009002
T0 = datetime(2026, 11, 7, 14, 0)
EPOCH = int(T0.replace(tzinfo=timezone.utc).timestamp())
ALLY, CORP = 99000001, 98000001
TRITA, WIDGET, BPC = 34, 90001, 90002


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── loader ────────────────────────────────────────────────────────────────

@pytest.fixture
def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(init())
    yield Session
    _run(engine.dispose())


def _km(i, system, when, value=1.0):
    return Killmail(killmail_id=i, killmail_hash="h", killmail_time=when, solar_system_id=system,
                    victim_ship_type_id=587, total_value=value)


def _load(Session, scope):
    async def go():
        async with Session() as db:
            return await bl.load_scope(db, scope)
    return _run(go())


def test_two_windows_in_different_systems(session):
    async def seed():
        async with session() as db:
            db.add_all([_km(1, SYS_A, T0), _km(2, SYS_B, T0 + timedelta(minutes=30)),
                        _km(3, SYS_A + 5, T0)])          # a system that is not in the scope
            await db.commit()
    _run(seed())
    scope = bl.scope_from_related([SYS_A, SYS_B], EPOCH)
    kills, truncated = _load(session, scope)
    assert sorted(k["killmail_id"] for k in kills) == [1, 2] and not truncated


def test_overlapping_windows_dedupe(session):
    async def seed():
        async with session() as db:
            db.add(_km(1, SYS_A, T0))
            await db.commit()
    _run(seed())
    scope = bl.parse_windows([f"{SYS_A}:{EPOCH - 3600}:{EPOCH + 3600}", f"{SYS_A}:{EPOCH - 60}:{EPOCH + 7200}"])
    assert len(scope) == 2
    kills, _ = _load(session, scope)
    assert [k["killmail_id"] for k in kills] == [1]


def test_window_edges_are_inclusive_and_outsiders_excluded(session):
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=2)

    async def seed():
        async with session() as db:
            db.add_all([_km(1, SYS_A, start), _km(2, SYS_A, end),
                        _km(3, SYS_A, start - timedelta(seconds=1)),
                        _km(4, SYS_A, end + timedelta(seconds=1))])
            await db.commit()
    _run(seed())
    kills, _ = _load(session, bl.scope_from_related([SYS_A], EPOCH))
    assert sorted(k["killmail_id"] for k in kills) == [1, 2]


def test_truncation_keeps_the_most_valuable(session, monkeypatch):
    monkeypatch.setattr(bl, "MAX_KILLS", 3)

    async def seed():
        async with session() as db:
            db.add_all([_km(i, SYS_A, T0, value=float(i)) for i in range(1, 7)])
            await db.commit()
    _run(seed())
    kills, truncated = _load(session, bl.scope_from_related([SYS_A], EPOCH))
    assert truncated and [k["killmail_id"] for k in kills] == [6, 5, 4]


def test_items_load_in_chunks(session, monkeypatch):
    monkeypatch.setattr(bl, "ITEM_CHUNK", 2)

    async def seed():
        async with session() as db:
            db.add_all([_km(i, SYS_A, T0) for i in range(1, 6)])
            await db.flush()
            db.add_all([KillmailItem(killmail_id=i, item_type_id=TRITA, quantity_dropped=i, flag=5)
                        for i in range(1, 6)])
            await db.commit()

        async with session() as db:
            return await bl.load_items(db, [1, 2, 3, 4, 5, 99])
    items = _run(seed())
    assert sorted(items) == [1, 2, 3, 4, 5]
    assert items[3] == [(TRITA, 0, 3, False, 5)]


def test_every_window_query_is_hinted_to_the_system_time_index():
    scope = bl.scope_from_related([SYS_A, SYS_B], EPOCH)
    for w in scope:
        sql = str(bl.window_statement(w).compile(dialect=sqlite_dialect.dialect()))
        assert f"INDEXED BY {bl.SYSTEM_TIME_INDEX}" in sql


def test_the_hinted_index_exists_on_the_model():
    assert bl.SYSTEM_TIME_INDEX in {i.name for i in Killmail.__table__.indexes}


# ── valuation ─────────────────────────────────────────────────────────────

def _kill(i, alliance=ALLY, corp=CORP):
    return {"killmail_id": i, "time": T0, "system_id": SYS_A, "char_id": 91000000 + i, "corp_id": corp,
            "alliance_id": alliance, "ship_type_id": 587, "total_value": 0.0}


def _item(type_id, destroyed=0, dropped=0, flag=5, nested=False):
    return (type_id, destroyed, dropped, False, flag)


def test_dropped_and_destroyed_are_split_and_priced_differently():
    kills = [_kill(1)]
    items = {1: [_item(TRITA, destroyed=1000, dropped=500), _item(WIDGET, destroyed=2, dropped=3)]}
    avg = {TRITA: 4.0, WIDGET: 1000.0}
    jita = {WIDGET: ("ok", 1200.0)}
    v = bl.value_wrecks(kills, items, avg, jita, set(), min_isk=0)
    (r,) = v["rows"]
    assert r["dropped"] == 500 * 4.0 + 3 * 1200.0        # dropped at Jita where priced
    assert r["destroyed"] == 1000 * 4.0 + 2 * 1000.0     # destroyed always at the average
    assert r["approx"] is True                           # tritanium is on the average
    assert v["by_type"][WIDGET] == {"qty": 3, "value": 3600.0, "source": "jita"}


def test_price_sources():
    avg = {1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0, 6: 100.0}
    jita = {1: ("ok", 120.0), 2: ("none", None), 3: ("error", None), 4: ("ok", 501.0),
            6: ("budget", None), 7: ("ok", 50.0)}
    got = {t: bl.price_for_drop(t, avg, jita, {8}) for t in (1, 2, 3, 4, 5, 6, 7, 8, 9)}
    assert got[1] == (120.0, "jita", None)
    assert got[2] == (100.0, "avg_no_jita", None)
    assert got[3] == (100.0, "avg_fetch_failed", None)
    assert got[4] == (100.0, "avg_thin", 501.0)           # more than 5x the average: use the average
    assert got[5] == (0.0, "unpriced", None)              # no average, not looked up
    assert got[6] == (100.0, "avg_budget", None)
    assert got[7] == (50.0, "jita", None)                 # no average at all: Jita is all there is
    assert got[8] == (0.0, "blueprint", None)
    assert got[9] == (0.0, "unpriced", None)


def test_exactly_five_times_the_average_is_trusted():
    assert bl.price_for_drop(1, {1: 100.0}, {1: ("ok", 500.0)}, set())[1] == "jita"


def test_blueprints_are_valued_zero():
    kills = [_kill(1)]
    items = {1: [_item(BPC, destroyed=1, dropped=1)]}
    v = bl.value_wrecks(kills, items, {BPC: 5e9}, {}, {BPC}, 0)
    assert v["rows"][0]["dropped"] == 0 and v["rows"][0]["destroyed"] == 0
    assert v["by_type"][BPC]["source"] == "blueprint"


def test_zero_drop_and_threshold_flags():
    kills = [_kill(1), _kill(2), _kill(3)]
    items = {1: [_item(WIDGET, dropped=20)], 2: [_item(WIDGET, destroyed=9)], 3: [_item(WIDGET, dropped=1)]}
    v = bl.value_wrecks(kills, items, {WIDGET: 1_000_000.0}, {}, set(), min_isk=10_000_000)
    by_id = {r["kill"]["killmail_id"]: r for r in v["rows"]}
    assert [r["kill"]["killmail_id"] for r in v["rows"]] == [1, 3, 2]     # dropped value, descending
    assert by_id[1]["worth"] and not by_id[3]["worth"]
    assert by_id[2]["zero_drop"] and not by_id[1]["zero_drop"]


def test_kills_without_item_rows_are_unknown_counted_and_not_valued():
    v = bl.value_wrecks([_kill(1), _kill(2)], {2: [_item(WIDGET, dropped=1)]}, {WIDGET: 100.0}, {}, set(), 0)
    by_id = {r["kill"]["killmail_id"]: r for r in v["rows"]}
    assert by_id[1]["unknown"] and not by_id[1]["zero_drop"]       # so the zero-drop filter keeps it
    assert not by_id[2]["unknown"]
    assert v["no_items"] == 1 and v["wrecks"] == 1                 # counted, but not a valued wreck
    assert v["dropped"] == 100.0
    assert sum(g["wrecks"] for g in v["groups"]) == v["wrecks"]


def test_group_totals_add_up_and_use_alliance_else_corp():
    kills = [_kill(1), _kill(2), _kill(3, alliance=None, corp=CORP), _kill(4, alliance=None, corp=None)]
    items = {i: [_item(WIDGET, destroyed=1, dropped=i)] for i in (1, 2, 3, 4)}
    v = bl.value_wrecks(kills, items, {WIDGET: 100.0}, {}, set(), 0)
    assert {g["key"] for g in v["groups"]} == {f"a:{ALLY}", f"c:{CORP}", "n:0"}
    assert sum(g["dropped"] for g in v["groups"]) == v["dropped"] == 1000.0
    assert sum(g["destroyed"] for g in v["groups"]) == v["destroyed"]
    assert sum(g["wrecks"] for g in v["groups"]) == v["wrecks"] == 4
    assert [g["key"] for g in v["groups"]] == ["n:0", f"a:{ALLY}", f"c:{CORP}"]   # by dropped value, then key


def test_top_drops_are_the_three_most_valuable():
    items = {1: [_item(t, dropped=1) for t in (11, 12, 13, 14)]}
    avg = {11: 1.0, 12: 4.0, 13: 3.0, 14: 2.0}
    v = bl.value_wrecks([_kill(1)], items, avg, {}, set(), 0)
    assert [t for t, _q, _v in v["rows"][0]["top"]] == [12, 13, 14]


def test_window_query_survives_extreme_but_bounded_times():
    scope = bl.parse_windows([f"{SYS_A}:{bl.MAX_EPOCH - 3600}:{bl.MAX_EPOCH}"])
    str(bl.window_statement(scope[0]).compile(dialect=sqlite_dialect.dialect()))
