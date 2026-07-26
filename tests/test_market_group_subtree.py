"""Tests for Task 1 of the EVE-style pickers plan:
`sde.get_market_group_descendants`, `sde.get_market_group_subtree_products`,
and `sde.search_market_groups`.

Temp-DB idiom from tests/test_invention_lookup.py: a real sqlite file (via
create_async_engine + async_sessionmaker) so the actual SQLite dialect runs,
plus the manual private-event-loop pattern used across the repo's async
service tests.
"""
import asyncio
import tempfile

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import (
    SDEType,
    SDEMarketGroup,
    SDEBlueprintInfo,
    SDEBlueprintMaterial,
)
from app.sde import lookup as sde


# 3-level fixture tree:
#   Ships (10)
#     ├─ Frigates (11)
#     │    └─ Assault Frigates (13)   -> buildable product WOLF at this depth
#     └─ Cruisers (12)                -> only a non-buildable type here
#   buildable product RIFTER lives at Frigates (11)
ROOT_ID = 10
FRIGATES_ID = 11
CRUISERS_ID = 12
ASSAULT_FRIG_ID = 13

RIFTER_ID = 100          # buildable, market_group_id = FRIGATES_ID
RIFTER_BP_ID = 1100
WOLF_ID = 200            # buildable, market_group_id = ASSAULT_FRIG_ID
WOLF_BP_ID = 1200
ORE_THING_ID = 300       # NON-buildable (no blueprint), market_group_id = CRUISERS_ID


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


async def _seed(SessionLocal):
    async with SessionLocal() as db:
        # market-group tree
        db.add(SDEMarketGroup(
            market_group_id=ROOT_ID, parent_group_id=None, market_group_name="Ships"))
        db.add(SDEMarketGroup(
            market_group_id=FRIGATES_ID, parent_group_id=ROOT_ID,
            market_group_name="Frigates"))
        db.add(SDEMarketGroup(
            market_group_id=CRUISERS_ID, parent_group_id=ROOT_ID,
            market_group_name="Cruisers"))
        db.add(SDEMarketGroup(
            market_group_id=ASSAULT_FRIG_ID, parent_group_id=FRIGATES_ID,
            market_group_name="Assault Frigates"))

        # types
        db.add(SDEType(type_id=RIFTER_ID, type_name="Rifter",
                       market_group_id=FRIGATES_ID, published=True))
        db.add(SDEType(type_id=WOLF_ID, type_name="Wolf",
                       market_group_id=ASSAULT_FRIG_ID, published=True))
        db.add(SDEType(type_id=ORE_THING_ID, type_name="Ore Thing",
                       market_group_id=CRUISERS_ID, published=True))

        # blueprints (only the two buildable products)
        db.add(SDEBlueprintInfo(
            blueprint_type_id=RIFTER_BP_ID, product_type_id=RIFTER_ID,
            product_quantity=1))
        db.add(SDEBlueprintInfo(
            blueprint_type_id=WOLF_BP_ID, product_type_id=WOLF_ID,
            product_quantity=1))

        # manufacturing materials (activity 1)
        db.add(SDEBlueprintMaterial(
            blueprint_type_id=RIFTER_BP_ID, activity_id=1,
            material_type_id=34, quantity=1000))
        db.add(SDEBlueprintMaterial(
            blueprint_type_id=RIFTER_BP_ID, activity_id=1,
            material_type_id=35, quantity=500))
        db.add(SDEBlueprintMaterial(
            blueprint_type_id=WOLF_BP_ID, activity_id=1,
            material_type_id=36, quantity=42))
        # a non-manufacturing (activity 8 = invention) row that must NOT leak in
        db.add(SDEBlueprintMaterial(
            blueprint_type_id=RIFTER_BP_ID, activity_id=8,
            material_type_id=99, quantity=1))
        await db.commit()


# ── get_market_group_descendants ─────────────────────────────────────────

def test_descendants_full_subtree():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            desc = await sde.get_market_group_descendants(db, ROOT_ID)
        assert desc == {ROOT_ID, FRIGATES_ID, CRUISERS_ID, ASSAULT_FRIG_ID}

    _run(scenario())


def test_descendants_midtree_includes_grandchild():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            desc = await sde.get_market_group_descendants(db, FRIGATES_ID)
        assert desc == {FRIGATES_ID, ASSAULT_FRIG_ID}

    _run(scenario())


def test_descendants_leaf_is_self_only():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            desc = await sde.get_market_group_descendants(db, CRUISERS_ID)
        assert desc == {CRUISERS_ID}

    _run(scenario())


# ── get_market_group_subtree_products ────────────────────────────────────

def test_subtree_products_root_finds_both():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            total, products = await sde.get_market_group_subtree_products(db, ROOT_ID)

        assert total == 2
        by_id = {p["product_type_id"]: p for p in products}
        assert set(by_id) == {RIFTER_ID, WOLF_ID}

        rifter = by_id[RIFTER_ID]
        assert set(rifter.keys()) == {
            "product_type_id", "product_name", "blueprint_type_id",
            "product_quantity", "materials",
        }
        assert rifter["product_name"] == "Rifter"
        assert rifter["blueprint_type_id"] == RIFTER_BP_ID
        assert rifter["product_quantity"] == 1
        # activity-1 materials only (invention row for mat 99 excluded)
        assert {(m["type_id"], m["quantity"]) for m in rifter["materials"]} == {
            (34, 1000), (35, 500),
        }

    _run(scenario())


def test_subtree_products_midtree_finds_both():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        # Frigates subtree pulls in the Assault Frigates grandchild -> both.
        async with SessionLocal() as db:
            total, products = await sde.get_market_group_subtree_products(db, FRIGATES_ID)
        assert total == 2
        assert {p["product_type_id"] for p in products} == {RIFTER_ID, WOLF_ID}

    _run(scenario())


def test_subtree_products_empty_subtree():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        # Cruisers holds only a non-buildable type (no blueprint).
        async with SessionLocal() as db:
            total, products = await sde.get_market_group_subtree_products(db, CRUISERS_ID)
        assert total == 0
        assert products == []

    _run(scenario())


def test_subtree_products_cap_reports_full_total():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            total, products = await sde.get_market_group_subtree_products(
                db, ROOT_ID, cap=1)
        assert total == 2
        assert len(products) == 1

    _run(scenario())


# ── search_market_groups ─────────────────────────────────────────────────

def test_search_case_insensitive_with_path():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            rows = await sde.search_market_groups(db, "frig")

        by_id = {r["market_group_id"]: r for r in rows}
        # "frig" matches both "Frigates" and "Assault Frigates" case-insensitively
        assert FRIGATES_ID in by_id
        assert ASSAULT_FRIG_ID in by_id

        # each carries a " > "-joined path from root
        assert by_id[FRIGATES_ID]["path"] == "Ships > Frigates"
        assert by_id[ASSAULT_FRIG_ID]["path"] == "Ships > Frigates > Assault Frigates"
        assert set(by_id[FRIGATES_ID].keys()) == {
            "market_group_id", "market_group_name", "path",
        }

    _run(scenario())


def test_search_respects_limit():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        async with SessionLocal() as db:
            rows = await sde.search_market_groups(db, "frig", limit=1)
        assert len(rows) == 1

    _run(scenario())
