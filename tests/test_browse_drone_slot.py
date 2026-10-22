"""The fitting tool's module browser must report drones as drones (ISS-126).

get_market_group_items reads slot_type from sde_module_slots, which has no row
for a drone, so the browse response said null and the tool added the drone to
the cargo hold. Temp-DB idiom from tests/test_copy_pack_lookup.py: a real
sqlite file, a private event loop per test.
"""
import asyncio
import tempfile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import SDEGroup, SDEMarketGroup, SDEModuleSlot, SDEType
from app.sde import lookup as sde

MARKET = 500
DRONE_CAT, MODULE_CAT, CHARGE_CAT = 18, 7, 8
DRONE_GROUP, MODULE_GROUP, CHARGE_GROUP = 100, 101, 102
HORNET, SCOUT, GUN, SCRIPT, GHOST = 1001, 1002, 2001, 3001, 4001


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _browse(extra=()):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add_all([
                SDEGroup(group_id=DRONE_GROUP, category_id=DRONE_CAT, group_name="Combat Drone"),
                SDEGroup(group_id=MODULE_GROUP, category_id=MODULE_CAT, group_name="Gun"),
                SDEGroup(group_id=CHARGE_GROUP, category_id=CHARGE_CAT, group_name="Script"),
            ])
            for tid, name, grp in [(HORNET, "Hornet I", DRONE_GROUP), (SCOUT, "Scout I", DRONE_GROUP),
                                   (GUN, "Test Gun", MODULE_GROUP), (SCRIPT, "Test Script", CHARGE_GROUP),
                                   (GHOST, "No Group Item", None)]:
                db.add(SDEType(type_id=tid, type_name=name, group_id=grp, market_group_id=MARKET, published=True))
            db.add(SDEModuleSlot(type_id=GUN, slot_type="high"))
            db.add_all(extra)
            await db.commit()
            return {i["type_id"]: i for i in await sde.get_market_group_items(db, MARKET)}
    finally:
        await engine.dispose()


def test_drones_report_the_drone_slot_type():
    items = _run(_browse())
    assert items[HORNET]["slot_type"] == "drone"
    assert items[SCOUT]["slot_type"] == "drone"


def test_modules_keep_their_real_slot_type():
    assert _run(_browse())[GUN]["slot_type"] == "high"


def test_charges_and_unclassified_items_stay_null_so_they_get_no_badge():
    items = _run(_browse())
    assert items[SCRIPT]["slot_type"] is None
    assert items[GHOST]["slot_type"] is None


def test_a_module_slot_row_wins_over_the_drone_category():
    # A drone-category type that does have a slot row keeps that row.
    items = _run(_browse(extra=[SDEModuleSlot(type_id=HORNET, slot_type="rig")]))
    assert items[HORNET]["slot_type"] == "rig"
