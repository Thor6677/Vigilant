"""SDE lookups behind /industry/copy-packs: get_blueprint_tree,
search_manufacturable_products and blueprint_copy_data_loaded.

Temp-DB idiom from tests/test_invention_lookup.py: a real sqlite file so the
actual SQLite dialect runs, plus a private event loop per test. sde_types
holds only published types (the loader skips the rest), so a blueprint with
no SDEType row here is an unpublished legacy one.
"""
import asyncio
import tempfile

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import (
    SDEBlueprintInfo,
    SDEBlueprintInventionProduct,
    SDEBlueprintMaterial,
    SDEBlueprintMfgSkill,
    SDEType,
)
from app.sde import lookup as sde

HULL, PLATE, CORE, GEAR, TRIT, ODD, LOOP_A, LOOP_B = 1, 2, 3, 5, 34, 99, 7, 8


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


def _info(bp, product, copy=None, limit=None, qty=1, time=100):
    return SDEBlueprintInfo(blueprint_type_id=bp, product_type_id=product,
                            product_quantity=qty, manufacturing_time=time,
                            max_production_limit=limit, copying_time=copy)


def _mat(bp, material, qty):
    return SDEBlueprintMaterial(blueprint_type_id=bp, activity_id=1,
                                material_type_id=material, quantity=qty)


async def _seed(SessionLocal):
    async with SessionLocal() as db:
        published = {
            HULL: "Widget Hull", PLATE: "Widget Plate", CORE: "Widget Core",
            GEAR: "Widget Gear", TRIT: "Tritanium", ODD: "Widget Oddity",
            LOOP_A: "Loop A", LOOP_B: "Loop B",
            101: "Widget Hull Blueprint", 202: "Widget Plate Blueprint A",
            203: "Widget Plate Blueprint B", 303: "Widget Core Blueprint",
            404: "Widget Oddity Blueprint", 501: "Widget Gear Blueprint A",
            502: "Widget Gear Blueprint B", 701: "Loop A Blueprint",
            801: "Loop B Blueprint",
        }   # 301 is deliberately absent: an unpublished legacy blueprint
        for tid, name in published.items():
            db.add(SDEType(type_id=tid, type_name=name, published=True))

        # HULL's materials are stored out of type-id order on purpose.
        db.add(_info(101, HULL, copy=1000, limit=1))
        db.add(_mat(101, TRIT, 100))
        db.add(_mat(101, PLATE, 6))
        db.add(SDEBlueprintMfgSkill(blueprint_type_id=101, skill_type_id=22242, level=3))

        # Two published blueprints make PLATE; the copyable one has the
        # HIGHER id, so only the copyable rule can pick it.
        db.add(_info(202, PLATE))
        db.add(_mat(202, TRIT, 1))
        db.add(_info(203, PLATE, copy=50, limit=40, qty=3))
        db.add(_mat(203, CORE, 2))

        # CORE: an unpublished, copyable, lower-id blueprint (301) loses to a
        # published one (303) that is an invention product with two sources.
        db.add(_info(301, CORE, copy=10))
        db.add(_mat(301, TRIT, 1))
        db.add(_info(303, CORE))
        db.add(_mat(303, TRIT, 5))
        db.add(SDEBlueprintInventionProduct(blueprint_type_id=300,
                                            product_blueprint_type_id=303, base_runs=3))
        db.add(SDEBlueprintInventionProduct(blueprint_type_id=302,
                                            product_blueprint_type_id=303, base_runs=20))

        # GEAR: the preferred blueprint (501, lower id) has no materials, so
        # the other one (502) must be chosen rather than dropping GEAR.
        db.add(_info(501, GEAR, copy=10))
        db.add(_info(502, GEAR, copy=10))
        db.add(_mat(502, TRIT, 7))

        # ODD has a blueprint row but no materials anywhere: not buildable.
        db.add(_info(404, ODD, copy=10))

        # A manufacturing cycle must still terminate.
        db.add(_info(701, LOOP_A, copy=10))
        db.add(_mat(701, LOOP_B, 1))
        db.add(_info(801, LOOP_B, copy=10))
        db.add(_mat(801, LOOP_A, 1))
        await db.commit()


def _with_db(scenario):
    engine, SessionLocal = _temp_engine()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            return await scenario(db)

    try:
        return _run(run())
    finally:
        _run(engine.dispose())


def test_tree_walks_every_level_in_material_order():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL))
    assert set(tree) == {HULL, PLATE, CORE}
    assert tree[HULL]["materials"] == [(TRIT, 100), (PLATE, 6)]
    assert tree[HULL]["skills"] == [(22242, 3)]
    assert tree[HULL]["max_production_limit"] == 1
    assert tree[HULL]["invented"] is False and tree[HULL]["invented_runs"] is None


def test_copyable_blueprint_wins_over_a_lower_id():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL))
    assert tree[PLATE]["blueprint_type_id"] == 203
    assert tree[PLATE]["product_quantity"] == 3


def test_published_blueprint_wins_and_invented_runs_is_the_most():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL))
    assert tree[CORE]["blueprint_type_id"] == 303
    assert tree[CORE]["copying_time"] is None
    assert tree[CORE]["invented"] is True and tree[CORE]["invented_runs"] == 20


def test_blueprint_without_materials_is_passed_over_not_fatal():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, GEAR))
    assert tree[GEAR]["blueprint_type_id"] == 502


def test_tree_stops_at_max_depth():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL, max_depth=1))
    assert set(tree) == {HULL, PLATE}


def test_tree_of_an_item_without_materials_is_empty():
    assert _with_db(lambda db: sde.get_blueprint_tree(db, ODD)) == {}


def test_tree_terminates_on_a_manufacturing_cycle():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, LOOP_A))
    assert set(tree) == {LOOP_A, LOOP_B}


def test_search_lists_buildable_products_shortest_first():
    found = _with_db(lambda db: sde.search_manufacturable_products(db, "widget"))
    assert [r["type_name"] for r in found] == [
        "Widget Core", "Widget Gear", "Widget Hull", "Widget Plate"]


def test_search_ignores_blank_queries():
    assert _with_db(lambda db: sde.search_manufacturable_products(db, "  ")) == []


def test_copy_data_loaded_follows_copying_time():
    assert _with_db(sde.blueprint_copy_data_loaded) is True


def test_copy_data_loaded_false_when_no_copy_times():
    async def scenario(db):
        await db.execute(update(SDEBlueprintInfo).values(copying_time=None))
        await db.commit()
        return await sde.blueprint_copy_data_loaded(db)
    assert _with_db(scenario) is False
