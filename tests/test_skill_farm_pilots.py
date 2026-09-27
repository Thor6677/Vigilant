"""app/skillfarm/pilots.py — settings + pilot CRUD, validation, and the IDOR
boundary (T-073's main stated risk).

Service-level tests against a real temp-file sqlite DB (the
test_networth.py / test_sync_field_sessions.py idiom) — these functions take
an AsyncSession directly, so no TestClient/route wiring is needed to exercise
the IDOR checks themselves; tests/test_skill_farm_route.py covers the same
boundary through the actual HTTP endpoints.
"""
import asyncio
import tempfile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Character, SkillFarmPilot, SkillFarmSettings
from app.skillfarm import pilots as farm_pilots
from app.skillfarm.constants import SKILL_FLOOR_SP

USER_A = 1
USER_B = 2
CHAR_A = 90000001   # belongs to USER_A
CHAR_B = 90000002   # belongs to USER_B


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _temp_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _char(cid, user_id, name="Farm Pilot"):
    from datetime import datetime
    return Character(
        character_id=cid, character_name=name, user_id=user_id,
        access_token="a", refresh_token="r", token_expiry=datetime(2099, 1, 1),
        scopes="",
    )


@pytest.fixture
def db():
    engine, SessionLocal = _temp_engine()

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            session.add(_char(CHAR_A, USER_A, "Alpha Pilot"))
            session.add(_char(CHAR_B, USER_B, "Bravo Pilot"))
            await session.commit()

    _run(seed())

    async def _go(fn):
        async with SessionLocal() as session:
            return await fn(session)

    def go(fn):
        return _run(_go(fn))

    class DB:
        pass

    d = DB()
    d.go = go
    yield d
    _run(engine.dispose())


# ── Settings ──────────────────────────────────────────────────────────────

def test_get_settings_creates_a_default_row(db):
    row = db.go(lambda s: farm_pilots.get_settings(s, USER_A))
    assert row.user_id == USER_A
    assert row.plex_per_month == 500
    assert row.price_source == "sell"

    # Second read finds the same row, doesn't duplicate it.
    again = db.go(lambda s: farm_pilots.get_settings(s, USER_A))
    assert again.sales_tax_pct == row.sales_tax_pct


def test_save_settings_persists_valid_values(db):
    row = db.go(lambda s: farm_pilots.save_settings(s, USER_A, 4.0, 250, "buy"))
    assert row.sales_tax_pct == 4.0
    assert row.plex_per_month == 250
    assert row.price_source == "buy"


@pytest.mark.parametrize("tax", [-0.1, 100.1, float("nan"), float("inf")])
def test_save_settings_rejects_bad_tax(db, tax):
    with pytest.raises(farm_pilots.ValidationError):
        db.go(lambda s: farm_pilots.save_settings(s, USER_A, tax, 500, "sell"))


@pytest.mark.parametrize("plex", [-1, 10_001])
def test_save_settings_rejects_bad_plex_per_month(db, plex):
    with pytest.raises(farm_pilots.ValidationError):
        db.go(lambda s: farm_pilots.save_settings(s, USER_A, 8.0, plex, "sell"))


def test_save_settings_rejects_bad_price_source(db):
    with pytest.raises(farm_pilots.ValidationError):
        db.go(lambda s: farm_pilots.save_settings(s, USER_A, 8.0, 500, "median"))


def test_tax_boundaries_are_inclusive(db):
    db.go(lambda s: farm_pilots.save_settings(s, USER_A, 0.0, 0, "sell"))
    db.go(lambda s: farm_pilots.save_settings(s, USER_A, 100.0, 10_000, "buy"))


# ── Pilots: add / IDOR ──────────────────────────────────────────────────────

def test_add_pilot_for_own_character_succeeds(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    assert pilot.user_id == USER_A
    assert pilot.character_id == CHAR_A
    assert pilot.base_sp == SKILL_FLOOR_SP  # default


def test_add_pilot_refuses_someone_elses_character():
    """The IDOR the ticket calls out: USER_A must not be able to add
    USER_B's character as their own farm pilot."""
    engine, SessionLocal = _temp_engine()

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            session.add(_char(CHAR_A, USER_A))
            session.add(_char(CHAR_B, USER_B))
            await session.commit()

    _run(seed())

    async def attempt():
        async with SessionLocal() as session:
            with pytest.raises(farm_pilots.NotOwned):
                await farm_pilots.add_pilot(session, USER_A, CHAR_B)
            rows = (await session.execute(select(SkillFarmPilot))).scalars().all()
            assert rows == []

    _run(attempt())
    _run(engine.dispose())


def test_add_pilot_rejects_a_nonexistent_character(db):
    """Indistinguishable from "belongs to someone else" on purpose — a
    crafted id can't be used to probe which character ids exist."""
    with pytest.raises(farm_pilots.NotOwned):
        db.go(lambda s: farm_pilots.add_pilot(s, USER_A, 999999999))


@pytest.mark.parametrize("base_sp", [-1, 1_000_000_001])
def test_add_pilot_rejects_base_sp_out_of_range(db, base_sp):
    with pytest.raises(farm_pilots.ValidationError):
        db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A, base_sp))


def test_base_sp_boundaries_are_inclusive(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A, 0))
    assert pilot.base_sp == 0


def test_adding_the_same_character_twice_is_idempotent(db):
    first = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A, 6_000_000))
    second = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A, 7_000_000))
    assert first.id == second.id
    # The second call's base_sp is ignored — use update_base_sp to change it.
    assert second.base_sp == 6_000_000

    async def _count_rows(session):
        rows = (await session.execute(select(SkillFarmPilot))).scalars().all()
        return len(rows)
    assert db.go(_count_rows) == 1


def test_eligible_characters_excludes_already_added_pilots(db):
    before = db.go(lambda s: farm_pilots.eligible_characters(s, USER_A))
    assert [c.character_id for c in before] == [CHAR_A]

    db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))

    after = db.go(lambda s: farm_pilots.eligible_characters(s, USER_A))
    assert after == []


def test_eligible_characters_never_includes_another_users_characters(db):
    eligible = db.go(lambda s: farm_pilots.eligible_characters(s, USER_A))
    assert CHAR_B not in [c.character_id for c in eligible]


# ── Pilots: update / remove IDOR ────────────────────────────────────────────

def test_update_base_sp_on_own_pilot(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    updated = db.go(lambda s: farm_pilots.update_base_sp(s, USER_A, pilot.id, 8_000_000))
    assert updated.base_sp == 8_000_000


def test_update_base_sp_refuses_someone_elses_pilot_row(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    with pytest.raises(farm_pilots.NotOwned):
        db.go(lambda s: farm_pilots.update_base_sp(s, USER_B, pilot.id, 8_000_000))
    # Untouched.
    async def reread(session):
        row = (await session.execute(
            select(SkillFarmPilot).where(SkillFarmPilot.id == pilot.id)
        )).scalar_one()
        return row.base_sp
    assert db.go(reread) == SKILL_FLOOR_SP


@pytest.mark.parametrize("base_sp", [-1, 1_000_000_001])
def test_update_base_sp_rejects_out_of_range(db, base_sp):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    with pytest.raises(farm_pilots.ValidationError):
        db.go(lambda s: farm_pilots.update_base_sp(s, USER_A, pilot.id, base_sp))


def test_remove_pilot_deletes_own_row(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    assert db.go(lambda s: farm_pilots.remove_pilot(s, USER_A, pilot.id)) is True
    assert db.go(lambda s: farm_pilots.list_pilots(s, USER_A)) == []


def test_remove_pilot_refuses_someone_elses_row(db):
    pilot = db.go(lambda s: farm_pilots.add_pilot(s, USER_A, CHAR_A))
    assert db.go(lambda s: farm_pilots.remove_pilot(s, USER_B, pilot.id)) is False
    # Still there.
    assert len(db.go(lambda s: farm_pilots.list_pilots(s, USER_A))) == 1


def test_remove_pilot_missing_id_is_a_no_op(db):
    assert db.go(lambda s: farm_pilots.remove_pilot(s, USER_A, 999999)) is False
