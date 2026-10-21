"""SDE lookups behind /industry/copy-packs: get_blueprint_tree,
search_manufacturable_products and blueprint_copy_data_loaded.

Temp-DB idiom from tests/test_invention_lookup.py: a real sqlite file so the
actual SQLite dialect runs, plus a private event loop per test.
"""
import asyncio
import tempfile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import (
    SDEBlueprintInfo,
    SDEBlueprintInvention,
    SDEBlueprintMaterial,
    SDEBlueprintMfgSkill,
    SDEType,
)
from app.sde import lookup as sde

HULL, PLATE, CORE, TRIT, ODD = 1, 2, 3, 34, 99


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


async def _seed(SessionLocal, copyable=True):
    async with SessionLocal() as db:
        for tid, name in ((HULL, "Widget Hull"), (PLATE, "Widget Plate"),
                          (CORE, "Widget Core"), (TRIT, "Tritanium"),
                          (ODD, "Widget Oddity")):
            db.add(SDEType(type_id=tid, type_name=name, published=True))
        db.add(_info(101, HULL, copy=1000 if copyable else None, limit=1))
        db.add(_mat(101, PLATE, 6))
        db.add(_mat(101, TRIT, 100))
        db.add(SDEBlueprintMfgSkill(blueprint_type_id=101, skill_type_id=22242, level=3))
        # Two blueprints make PLATE; the copyable one (202) must win.
        db.add(_info(203, PLATE))
        db.add(_mat(203, TRIT, 1))
        db.add(_info(202, PLATE, copy=50 if copyable else None, limit=40, qty=3))
        db.add(_mat(202, CORE, 2))
        # CORE's blueprint can't be copied and is an invention product.
        db.add(_info(303, CORE))
        db.add(_mat(303, TRIT, 5))
        db.add(SDEBlueprintInvention(blueprint_type_id=302, product_blueprint_type_id=303,
                                     probability=0.3, base_runs=10, time=100))
        # ODD has a blueprint row but no materials: not buildable.
        db.add(_info(404, ODD, copy=10))
        await db.commit()


def _with_db(scenario, copyable=True):
    engine, SessionLocal = _temp_engine()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal, copyable)
        async with SessionLocal() as db:
            return await scenario(db)

    try:
        return _run(run())
    finally:
        _run(engine.dispose())


def test_tree_walks_every_level_and_prefers_copyable_blueprints():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL))
    assert set(tree) == {HULL, PLATE, CORE}
    assert tree[HULL]["materials"] == [(PLATE, 6), (TRIT, 100)]
    assert tree[HULL]["skills"] == [(22242, 3)]
    assert tree[HULL]["max_production_limit"] == 1
    assert tree[PLATE]["blueprint_type_id"] == 202
    assert tree[PLATE]["product_quantity"] == 3
    assert tree[CORE]["copying_time"] is None and tree[CORE]["invented"] is True
    assert tree[CORE]["invented_runs"] == 10
    assert tree[HULL]["invented"] is False and tree[HULL]["invented_runs"] is None


def test_tree_stops_at_max_depth():
    tree = _with_db(lambda db: sde.get_blueprint_tree(db, HULL, max_depth=1))
    assert set(tree) == {HULL, PLATE}


def test_tree_of_an_item_without_materials_is_empty():
    assert _with_db(lambda db: sde.get_blueprint_tree(db, ODD)) == {}


def test_search_lists_buildable_products_shortest_first():
    found = _with_db(lambda db: sde.search_manufacturable_products(db, "widget"))
    assert [r["type_name"] for r in found] == ["Widget Core", "Widget Hull", "Widget Plate"]


def test_search_ignores_blank_queries():
    assert _with_db(lambda db: sde.search_manufacturable_products(db, "  ")) == []


def test_copy_data_loaded_follows_copying_time():
    assert _with_db(sde.blueprint_copy_data_loaded) is True


def test_copy_data_loaded_false_when_no_copy_times():
    # ODD's blueprint (404) keeps a copy time, so drop it for this check.
    async def scenario(db):
        from sqlalchemy import update
        await db.execute(update(SDEBlueprintInfo).values(copying_time=None))
        await db.commit()
        return await sde.blueprint_copy_data_loaded(db)
    assert _with_db(scenario, copyable=False) is False
