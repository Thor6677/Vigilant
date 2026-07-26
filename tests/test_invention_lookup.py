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
    SDEBlueprintInfo,
    SDEBlueprintInvention,
    SDEBlueprintInventionMaterial,
    SDEBlueprintInventionSkill,
)
from app.sde import lookup as sde
from app.routes.industry import _resolve_invention_skills


# Rifter (T1 blueprint 687) -> Rifter Blueprint invented into
# Wolf-class-ish T2 blueprint 11373 -> product 11371.
T1_BLUEPRINT_ID = 687
T2_BLUEPRINT_ID = 11373
PRODUCT_ID = 11371

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
        db.add(SDEBlueprintInvention(
            blueprint_type_id=T1_BLUEPRINT_ID,
            product_blueprint_type_id=T2_BLUEPRINT_ID,
            probability=0.3,
            base_runs=1,
            time=63900,
        ))
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
        assert row["t1_blueprint_type_id"] == T1_BLUEPRINT_ID
        assert row["t2_blueprint_type_id"] == T2_BLUEPRINT_ID
        assert row["probability"] == 0.3
        assert row["base_runs"] == 1
        assert row["per_run_output_qty"] == 1
        assert {(d["material_type_id"], d["quantity"]) for d in row["datacores"]} == {
            (20410, 8), (20424, 8),
        }
        assert set(row["skill_ids"]) == {
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
