"""SDE helpers, classification, browse flags and the engine guard for fighters (ISS-136).

Fighters (category 87) have no sde_module_slots row. They classify as slot
"fighter", the browser reports their class and squadron size and greys out the
ones a ship can never launch, and the engine keeps them out of every module
calculation so a fighter's own ability effects cannot leak onto the ship.

A temp SQLite file with seeded sde_* rows; real attribute and effect IDs,
fixture-local type IDs and neutral names.
"""
import asyncio
import tempfile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.db.models import Base
from app.db.sde_models import (
    SDEEffect, SDEModuleSlot, SDEGroup, SDEModifier, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import ATTR_CPU, ATTR_POWER, ATTR_SHIELD_HP, NON_RACK_SLOTS
from app.fitting.engine import calculate_fitting_stats
from app.routes import fitting as fitting_mod
from app.sde import lookup as sde
from tests.test_fitting_engine import OP_POST_PERCENT, SHIP, _Slice, _attrs, _seed_rows

MARKET = 600
FIGHTER_CAT, DRONE_CAT, CHARGE_CAT = 87, 18, 8
G_LIGHT, G_SUPPORT, G_HEAVY, G_SL, G_SS, G_SH, G_DRONE, G_CHARGE = range(700, 708)
LIGHT, SUPPORT, HEAVY, S_LIGHT, S_SUPPORT, S_HEAVY, DRONE, CHARGE = range(7001, 7009)
SUPERIORITY, NOCLASS = 7009, 7010
CARRIER, SUPER, TRISTAN = 7101, 7102, 7103

ATTACK, MISSILES = 6465, 6431


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _rows():
    rows = [SDEGroup(group_id=g, category_id=FIGHTER_CAT, group_name=f"Test Fighter Group {g}")
            for g in (G_LIGHT, G_SUPPORT, G_HEAVY, G_SL, G_SS, G_SH)]
    rows += [SDEGroup(group_id=G_DRONE, category_id=DRONE_CAT, group_name="Test Drone Group"),
             SDEGroup(group_id=G_CHARGE, category_id=CHARGE_CAT, group_name="Test Charge Group")]
    spec = [
        (LIGHT, "Test Light Fighter", G_LIGHT, 1000.0, {2212: 1, 2215: 6}, [ATTACK, MISSILES]),
        (SUPPORT, "Test Support Fighter", G_SUPPORT, 3000.0, {2213: 1, 2215: 3}, []),
        (HEAVY, "Test Heavy Fighter", G_HEAVY, 1800.0, {2214: 1, 2215: 6}, [ATTACK]),
        (S_LIGHT, "Test Standup Light", G_SL, 1600.0, {2740: 1, 2215: 9}, [ATTACK]),
        (S_SUPPORT, "Test Standup Support", G_SS, 4000.0, {2741: 1, 2215: 3}, []),
        (S_HEAVY, "Test Standup Heavy", G_SH, 6000.0, {2742: 1, 2215: 6}, [ATTACK]),
        (SUPERIORITY, "Test Superiority", G_LIGHT, 800.0, {2212: 1, 2215: 12}, [MISSILES]),
        (NOCLASS, "Test Classless Fighter", G_LIGHT, 1000.0, {2215: 6}, [ATTACK]),
        (DRONE, "Test Drone", G_DRONE, 5.0, {}, []),
        (CHARGE, "Test Charge", G_CHARGE, 0.1, {}, []),
    ]
    for tid, name, grp, vol, attrs, effects in spec:
        rows.append(SDEType(type_id=tid, type_name=name, group_id=grp,
                            market_group_id=MARKET, published=True, volume=vol))
        rows += _attrs(tid, attrs)
        rows += [SDETypeEffect(type_id=tid, effect_id=e) for e in effects]
    # Carrier-like (4 tubes, 3 light, 2 support, no heavy attr at all),
    # supercarrier-like (5 tubes, 3 light, 0 support, 4 heavy), a hull with none.
    rows += _attrs(CARRIER, {2216: 4, 2217: 3, 2218: 2, 2055: 65000.0})
    rows += _attrs(SUPER, {2216: 5, 2217: 3, 2218: 0, 2219: 4, 2055: 110000.0})
    rows += _attrs(TRISTAN, {283: 40.0})
    return rows


async def _with_db(fn):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all(_rows())
            await db.commit()
            return await fn(db)
    finally:
        await engine.dispose()


# ── classification ──────────────────────────────────────────────────────

def test_every_fighter_group_classifies_as_fighter_and_others_are_unchanged():
    ids = [LIGHT, SUPPORT, HEAVY, S_LIGHT, S_SUPPORT, S_HEAVY, SUPERIORITY, DRONE, CHARGE]
    got = _run(_with_db(lambda db: sde.get_module_slot_types_bulk(db, ids)))
    for t in ids[:7]:
        assert got[t] == "fighter"
    assert got[DRONE] == "drone"
    assert got[CHARGE] == "cargo"


# ── fighter info ────────────────────────────────────────────────────────

def test_fighter_info_carries_class_size_volume_and_primary():
    ids = [LIGHT, SUPPORT, HEAVY, S_LIGHT, S_SUPPORT, S_HEAVY, SUPERIORITY]
    info = _run(_with_db(lambda db: sde.get_fighter_info_bulk(db, ids)))
    assert info[LIGHT] == {"class": "light", "max_size": 6, "volume": 1000.0, "primary": "attack",
                       "has_attack": True, "has_missiles": True}
    assert info[SUPPORT] == {"class": "support", "max_size": 3, "volume": 3000.0, "primary": None,
                         "has_attack": False, "has_missiles": False}
    assert info[HEAVY]["class"] == "heavy" and info[HEAVY]["primary"] == "attack"
    assert [info[t]["class"] for t in (S_LIGHT, S_SUPPORT, S_HEAVY)] == ["standup"] * 3
    assert info[S_LIGHT]["max_size"] == 9
    # Superiority fighters have only the missile ability, which is their weapon.
    assert info[SUPERIORITY]["primary"] == "missiles" and info[SUPERIORITY]["max_size"] == 12
    assert (info[SUPERIORITY]["has_attack"], info[SUPERIORITY]["has_missiles"]) == (False, True)


def test_fighter_info_empty_input_and_unknown_attrs():
    assert _run(_with_db(lambda db: sde.get_fighter_info_bulk(db, []))) == {}
    info = _run(_with_db(lambda db: sde.get_fighter_info_bulk(db, [DRONE])))
    assert info[DRONE] == {"class": None, "max_size": 0, "volume": 5.0, "primary": None,
                        "has_attack": False, "has_missiles": False}


def test_fighter_info_is_three_queries():
    async def go(db):
        n = []
        from sqlalchemy import event
        sync = db.bind.sync_engine
        event.listen(sync, "before_cursor_execute", lambda *a: n.append(1))
        await sde.get_fighter_info_bulk(db, [LIGHT, SUPPORT, HEAVY, SUPERIORITY])
        return len(n)
    assert _run(_with_db(go)) <= 3


# ── ship limits ─────────────────────────────────────────────────────────

def test_ship_limits_default_missing_attrs_to_zero():
    lim = _run(_with_db(lambda db: sde.get_fighter_ship_limits(db, [CARRIER, SUPER, TRISTAN])))
    assert lim[CARRIER] == {"tubes": 4, "light": 3, "support": 2, "heavy": 0, "hangar_base": 65000.0}
    assert lim[SUPER] == {"tubes": 5, "light": 3, "support": 0, "heavy": 4, "hangar_base": 110000.0}
    assert lim[TRISTAN] == {"tubes": 0, "light": 0, "support": 0, "heavy": 0, "hangar_base": 0.0}


# ── browse ──────────────────────────────────────────────────────────────

def test_browse_rows_carry_fighter_class_and_squadron_max():
    items = {i["type_id"]: i for i in _run(_with_db(lambda db: sde.get_market_group_items(db, MARKET)))}
    assert items[LIGHT]["slot_type"] == "fighter"
    assert (items[LIGHT]["fighter_class"], items[LIGHT]["squadron_max"]) == ("light", 6)
    assert (items[S_HEAVY]["fighter_class"], items[S_HEAVY]["squadron_max"]) == ("standup", 6)
    assert items[DRONE]["slot_type"] == "drone" and "fighter_class" not in items[DRONE]
    assert items[CHARGE]["slot_type"] is None


class _Req:
    session = {"user_id": 1}


def _can_fit(ship):
    async def go(db):
        rows = await fitting_mod.browse_items(_Req(), MARKET, ship_type_id=ship, db=db)
        return {r["type_id"]: r["can_fit"] for r in rows}
    return _run(_with_db(go))


def test_can_fit_false_for_class_the_hull_lacks_and_for_standup_fighters():
    cf = _can_fit(CARRIER)
    assert cf[LIGHT] is True and cf[SUPPORT] is True
    assert cf[HEAVY] is False            # 2219 missing on the carrier-like hull
    assert not any(cf[t] for t in (S_LIGHT, S_SUPPORT, S_HEAVY))
    assert cf[NOCLASS] is False          # no class flag: can never launch
    cf = _can_fit(SUPER)
    assert cf[HEAVY] is True and cf[SUPPORT] is False


def test_can_fit_false_for_every_fighter_on_a_no_tube_hull():
    cf = _can_fit(TRISTAN)
    assert not any(cf[t] for t in (LIGHT, SUPPORT, HEAVY, S_LIGHT, SUPERIORITY))
    assert cf[DRONE] is True and cf[CHARGE] is True


def test_can_fit_true_without_a_ship():
    async def go(db):
        rows = await fitting_mod.browse_items(_Req(), MARKET, ship_type_id=None, db=db)
        return all(r["can_fit"] for r in rows)
    assert _run(_with_db(go)) is True


# ── engine guard ────────────────────────────────────────────────────────

FIGHTER_T = 9801
GROUP_FIGHTER_T = 9802
EFF_FIGHTER_SHIP = 9803
ATTR_FIGHTER_BONUS = 9804


def _engine_rows():
    rows = _seed_rows()
    rows += [
        SDEType(type_id=FIGHTER_T, type_name="Test Engine Fighter",
                group_id=GROUP_FIGHTER_T, category_id=FIGHTER_CAT),
        SDEEffect(effect_id=EFF_FIGHTER_SHIP, effect_name="testFighterShipMod", effect_category=0),
        SDETypeEffect(type_id=FIGHTER_T, effect_id=EFF_FIGHTER_SHIP),
        SDEModifier(effect_id=EFF_FIGHTER_SHIP, func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_SHIELD_HP, modifying_attribute_id=ATTR_FIGHTER_BONUS,
                    operator=OP_POST_PERCENT),
    ]
    rows += _attrs(FIGHTER_T, {ATTR_FIGHTER_BONUS: 50.0, ATTR_CPU: 99.0, ATTR_POWER: 99.0, 2215: 6})
    # Even a stray high-slot turret row must not make a fighter count.
    rows.append(SDEModuleSlot(type_id=FIGHTER_T, slot_type="high", is_turret=True))
    return rows


def test_fighter_items_leave_ship_attributes_and_resources_untouched():
    def stats(items):
        async def go(db):
            return await calculate_fitting_stats(db, SHIP, items)
        return _Slice().run(go, rows=_engine_rows())

    base = stats([])
    fighters = stats([{"type_id": FIGHTER_T, "type_name": "Test Engine Fighter",
                       "slot": "fighter", "quantity": 6, "online": True}] * 2)
    assert fighters["shield_hp"] == base["shield_hp"]
    for key in ("cpu_used", "pg_used", "turrets_used", "launchers_used", "total_dps"):
        assert fighters[key] == base[key]
    assert fighters["cpu_used"] == 0


def test_non_rack_slots_names_all_three():
    assert set(NON_RACK_SLOTS) == {"drone", "cargo", "fighter"}
