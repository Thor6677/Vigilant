"""Tests for Task 3 of the invention expected-cost plan:
`sde.get_invention_data` (bulk inventability chain lookup) and
`_resolve_invention_skills` (character/manual skill-level resolution).

Temp-DB idiom from tests/test_networth.py: a real sqlite file (via
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
    SDEGroup,
    SDEBlueprintInfo,
    SDEBlueprintInvention,
    SDEBlueprintInventionProduct,
    SDEBlueprintInventionMaterial,
    SDEBlueprintInventionSkill,
)
from app.sde import lookup as sde
from app.routes.industry import _resolve_invention_skills


# Rifter Blueprint invents two things: Wolf Blueprint (-> Wolf) and Jaguar
# Blueprint (-> Jaguar). The activity row keeps only the first product.
T1_BLUEPRINT_ID = 691
T2_BLUEPRINT_ID = 11372
PRODUCT_ID = 11371
JAGUAR_BLUEPRINT_ID = 11401
JAGUAR_ID = 11400

BLUEPRINT_CATEGORY_ID = 9
RELIC_CATEGORY_ID = 34

ENCRYPTION_SKILL_ID = 21791   # "Minmatar Encryption Methods"
SCIENCE_A_SKILL_ID = 3402     # "Mechanical Engineering"
SCIENCE_B_SKILL_ID = 11433    # "High Energy Physics"


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
        db.add(SDEBlueprintInfo(
            blueprint_type_id=T2_BLUEPRINT_ID,
            product_type_id=PRODUCT_ID,
            product_quantity=1,
            manufacturing_time=600,
        ))
        db.add(SDEBlueprintInfo(
            blueprint_type_id=JAGUAR_BLUEPRINT_ID,
            product_type_id=JAGUAR_ID,
            product_quantity=1,
            manufacturing_time=600,
        ))
        db.add(SDEBlueprintInvention(
            blueprint_type_id=T1_BLUEPRINT_ID,
            product_blueprint_type_id=T2_BLUEPRINT_ID,
            probability=0.3,
            base_runs=1,
            time=63900,
        ))
        for product_bp in (T2_BLUEPRINT_ID, JAGUAR_BLUEPRINT_ID):
            db.add(SDEBlueprintInventionProduct(
                blueprint_type_id=T1_BLUEPRINT_ID,
                product_blueprint_type_id=product_bp, base_runs=1))
        db.add(SDEGroup(group_id=105, category_id=BLUEPRINT_CATEGORY_ID,
                        group_name="Frigate Blueprint"))
        db.add(SDEType(type_id=T1_BLUEPRINT_ID, type_name="Rifter Blueprint",
                       group_id=105))
        db.add(SDEBlueprintInventionMaterial(
            blueprint_type_id=T1_BLUEPRINT_ID, material_type_id=20410, quantity=8))
        db.add(SDEBlueprintInventionMaterial(
            blueprint_type_id=T1_BLUEPRINT_ID, material_type_id=20424, quantity=8))
        db.add(SDEBlueprintInventionSkill(
            blueprint_type_id=T1_BLUEPRINT_ID, skill_type_id=SCIENCE_A_SKILL_ID))
        db.add(SDEBlueprintInventionSkill(
            blueprint_type_id=T1_BLUEPRINT_ID, skill_type_id=SCIENCE_B_SKILL_ID))
        db.add(SDEBlueprintInventionSkill(
            blueprint_type_id=T1_BLUEPRINT_ID, skill_type_id=ENCRYPTION_SKILL_ID))
        db.add(SDEType(type_id=ENCRYPTION_SKILL_ID, type_name="Minmatar Encryption Methods"))
        db.add(SDEType(type_id=SCIENCE_A_SKILL_ID, type_name="Mechanical Engineering"))
        db.add(SDEType(type_id=SCIENCE_B_SKILL_ID, type_name="High Energy Physics"))
        await db.commit()


# ── get_invention_data ───────────────────────────────────────────────────

def test_get_invention_data_resolves_full_chain():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [PRODUCT_ID])

        assert PRODUCT_ID in data
        row = data[PRODUCT_ID]
        assert row["t2_blueprint_type_id"] == T2_BLUEPRINT_ID
        assert row["per_run_output_qty"] == 1
        assert len(row["sources"]) == 1
        src = row["sources"][0]
        assert src["source_type_id"] == T1_BLUEPRINT_ID
        assert src["source_name"] == "Rifter Blueprint"
        assert src["source_is_relic"] is False
        assert src["probability"] == 0.3
        assert src["base_runs"] == 1
        assert {(d["material_type_id"], d["quantity"]) for d in src["datacores"]} == {
            (20410, 8), (20424, 8),
        }
        assert set(src["skill_ids"]) == {
            SCIENCE_A_SKILL_ID, SCIENCE_B_SKILL_ID, ENCRYPTION_SKILL_ID,
        }

    _run(scenario())


def test_get_invention_data_non_inventable_product_absent():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)
        # Product with no SDEBlueprintInfo row at all -> no chain.
        no_chain_product = 999999

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [PRODUCT_ID, no_chain_product])

        assert PRODUCT_ID in data
        assert no_chain_product not in data

    _run(scenario())


def test_get_invention_data_second_product_of_a_source_resolves():
    """Rifter Blueprint invents Wolf Blueprint and Jaguar Blueprint, but the
    activity row keeps only the first. Both products resolve to source 691
    with the activity's probability."""
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(SessionLocal)

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [PRODUCT_ID, JAGUAR_ID])

        assert set(data) == {PRODUCT_ID, JAGUAR_ID}
        jaguar = data[JAGUAR_ID]
        assert jaguar["t2_blueprint_type_id"] == JAGUAR_BLUEPRINT_ID
        assert [s["source_type_id"] for s in jaguar["sources"]] == [T1_BLUEPRINT_ID]
        assert jaguar["sources"][0]["probability"] == 0.3
        assert {d["material_type_id"] for d in jaguar["sources"][0]["datacores"]} == {
            20410, 20424,
        }

    _run(scenario())


# Three relic tiers feeding one invented blueprint (ids of real relic types;
# the names and the blueprint/product ids here are invented).
RELIC_INTACT, RELIC_MALFUNCTIONING, RELIC_WRECKED = 30187, 30752, 34412
RELIC_GROUP_ID = 99001
T3_BLUEPRINT_ID = 99002
T3_PRODUCT_ID = 99003


async def _seed_relics(SessionLocal, wrecked_has_type=True, wrecked_has_activity=True):
    async with SessionLocal() as db:
        db.add(SDEGroup(group_id=RELIC_GROUP_ID, category_id=RELIC_CATEGORY_ID,
                        group_name="Test Relic Group"))
        db.add(SDEBlueprintInfo(
            blueprint_type_id=T3_BLUEPRINT_ID, product_type_id=T3_PRODUCT_ID,
            product_quantity=1, manufacturing_time=600))
        tiers = [
            (RELIC_INTACT, "Test Intact Relic", 0.26, 20),
            (RELIC_MALFUNCTIONING, "Test Malfunctioning Relic", 0.21, 10),
            (RELIC_WRECKED, "Test Wrecked Relic", 0.14, 3),
        ]
        # Added out of id order on purpose: the lookup must sort them.
        for rid, name, prob, runs in reversed(tiers):
            if rid != RELIC_WRECKED or wrecked_has_type:
                db.add(SDEType(type_id=rid, type_name=name, group_id=RELIC_GROUP_ID))
            if rid != RELIC_WRECKED or wrecked_has_activity:
                db.add(SDEBlueprintInvention(
                    blueprint_type_id=rid, product_blueprint_type_id=T3_BLUEPRINT_ID,
                    probability=prob, base_runs=runs, time=1))
            db.add(SDEBlueprintInventionProduct(
                blueprint_type_id=rid, product_blueprint_type_id=T3_BLUEPRINT_ID,
                base_runs=runs))
            db.add(SDEBlueprintInventionMaterial(
                blueprint_type_id=rid, material_type_id=20410, quantity=2))
        await db.commit()


def test_get_invention_data_multi_source_ordered_by_id():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_relics(SessionLocal)

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [T3_PRODUCT_ID])

        sources = data[T3_PRODUCT_ID]["sources"]
        assert [s["source_type_id"] for s in sources] == [
            RELIC_INTACT, RELIC_MALFUNCTIONING, RELIC_WRECKED]
        assert [(s["probability"], s["base_runs"]) for s in sources] == [
            (0.26, 20), (0.21, 10), (0.14, 3)]
        assert all(s["source_is_relic"] for s in sources)
        assert all(s["datacores"] == [{"material_type_id": 20410, "quantity": 2}]
                   for s in sources)

    _run(scenario())


def test_get_invention_data_source_without_a_type_row_is_skipped():
    """sde_types holds only published types. A source with no row there (the
    unpublished legacy Electronics relic) is skipped, never returned as a
    non-relic that would be costed without the relic price."""
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_relics(SessionLocal, wrecked_has_type=False)

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [T3_PRODUCT_ID])

        assert [s["source_type_id"] for s in data[T3_PRODUCT_ID]["sources"]] == [
            RELIC_INTACT, RELIC_MALFUNCTIONING]

    _run(scenario())


def test_get_invention_data_source_with_null_category_is_skipped():
    """A type row whose group is missing and whose own category is NULL could
    be a relic; it is skipped, never returned as a non-relic."""
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_relics(SessionLocal)
        async with SessionLocal() as db:
            wrecked = await db.get(SDEType, RELIC_WRECKED)
            wrecked.group_id = None
            await db.commit()

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [T3_PRODUCT_ID])

        assert [s["source_type_id"] for s in data[T3_PRODUCT_ID]["sources"]] == [
            RELIC_INTACT, RELIC_MALFUNCTIONING]

    _run(scenario())


def test_get_invention_data_products_row_without_activity_row_is_skipped():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_relics(SessionLocal, wrecked_has_activity=False)

        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [T3_PRODUCT_ID])

        assert [s["source_type_id"] for s in data[T3_PRODUCT_ID]["sources"]] == [
            RELIC_INTACT, RELIC_MALFUNCTIONING]

    _run(scenario())


def test_get_invention_data_empty_input():
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            data = await sde.get_invention_data(db, [])
        assert data == {}

    _run(scenario())


# ── _resolve_invention_skills ────────────────────────────────────────────

SKILL_NAMES = {
    ENCRYPTION_SKILL_ID: "Minmatar Encryption Methods",
    SCIENCE_A_SKILL_ID: "Mechanical Engineering",
    SCIENCE_B_SKILL_ID: "High Energy Physics",
}


def test_resolve_character_mode_missing_science_flags_and_zeros():
    # Encryption trained to 4, one science trained to 5, the other untrained
    # (absent from char_skills entirely).
    char_skills = {
        ENCRYPTION_SKILL_ID: 4,
        SCIENCE_A_SKILL_ID: 5,
        # SCIENCE_B_SKILL_ID intentionally absent -> untrained -> level 0
    }
    # Skill id order deliberately NOT encryption-first, to prove name-based
    # (not positional) identification.
    skill_ids = [SCIENCE_A_SKILL_ID, SCIENCE_B_SKILL_ID, ENCRYPTION_SKILL_ID]

    e, s1, s2, missing = _resolve_invention_skills(
        char_skills, skill_ids, SKILL_NAMES, encryption_manual=4, science_manual=4,
    )

    assert e == 4  # encryption correctly identified by name, not first-in-list
    assert {s1, s2} == {5, 0}
    assert missing is True


def test_resolve_character_mode_all_trained_no_flag():
    char_skills = {
        ENCRYPTION_SKILL_ID: 4,
        SCIENCE_A_SKILL_ID: 5,
        SCIENCE_B_SKILL_ID: 3,
    }
    skill_ids = [ENCRYPTION_SKILL_ID, SCIENCE_A_SKILL_ID, SCIENCE_B_SKILL_ID]

    e, s1, s2, missing = _resolve_invention_skills(
        char_skills, skill_ids, SKILL_NAMES, encryption_manual=4, science_manual=4,
    )

    assert e == 4
    assert {s1, s2} == {5, 3}
    assert missing is False


def test_resolve_character_mode_degenerate_skill_list_falls_back_to_manual():
    char_skills = {ENCRYPTION_SKILL_ID: 4}
    # Only two skill ids -> can't safely split E vs S -> manual fallback.
    skill_ids = [ENCRYPTION_SKILL_ID, SCIENCE_A_SKILL_ID]

    result = _resolve_invention_skills(
        char_skills, skill_ids, SKILL_NAMES, encryption_manual=2, science_manual=3,
    )

    assert result == (2, 3, 3, False)


def test_resolve_manual_mode_returns_manual_values_no_flag():
    result = _resolve_invention_skills(
        None, [ENCRYPTION_SKILL_ID, SCIENCE_A_SKILL_ID, SCIENCE_B_SKILL_ID],
        SKILL_NAMES, encryption_manual=4, science_manual=5,
    )
    assert result == (4, 5, 5, False)
