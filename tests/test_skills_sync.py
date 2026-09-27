"""T-073's "skills" sync field: fetch_skills_data, the no-scope path, the
purge mapping, the ensure_dashboard_cache_columns ALTER helper, and its own
row in PER_CHARACTER_USER_TABLES.
"""
import asyncio
import json
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routes.dashboard as dash
from app.auth import purge
from app.auth import scopes as perms
from app.db.models import (
    AsyncSessionLocal,
    Base,
    Character,
    CharacterDashboardCache,
    ensure_dashboard_cache_columns,
)

CHAR_ID = 90000123


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char(scopes: str) -> Character:
    return Character(
        character_id=CHAR_ID, character_name="Farm Pilot",
        access_token="a", refresh_token="r", token_expiry=datetime(2099, 1, 1),
        scopes=scopes, user_id=None,
    )


# ── fetch_skills_data: payload parsing ──────────────────────────────────────

def test_fetch_skills_data_parses_the_esi_payload(monkeypatch):
    async def fake_client_for(char):
        return object(), None

    async def fake_get_skills(client, character_id):
        assert character_id == CHAR_ID
        return {
            "total_sp": 12_345_678,
            "unallocated_sp": 500_000,
            "skills": [
                {"skill_id": 3300, "active_skill_level": 5, "trained_skill_level": 5,
                 "skillpoints_in_skill": 256000},
                {"skill_id": 3301, "active_skill_level": 3, "trained_skill_level": 4,
                 "skillpoints_in_skill": 16000},
            ],
        }

    monkeypatch.setattr(dash, "_client_for", fake_client_for)
    monkeypatch.setattr(dash.esi_char, "get_skills", fake_get_skills)

    char = _char(perms.SKILLS)
    result = _run(dash.fetch_skills_data([char], db=None))
    val, warn = result[CHAR_ID]

    assert warn is None
    assert val == {
        "total_sp": 12_345_678,
        "unallocated_sp": 500_000,
        "levels": {"3300": 5, "3301": 3},
    }


def test_fetch_skills_data_defaults_missing_unallocated_sp_to_zero(monkeypatch):
    """ESI omits unallocated_sp entirely for a character with none."""
    async def fake_client_for(char):
        return object(), None

    async def fake_get_skills(client, character_id):
        return {"total_sp": 1_000_000, "skills": []}

    monkeypatch.setattr(dash, "_client_for", fake_client_for)
    monkeypatch.setattr(dash.esi_char, "get_skills", fake_get_skills)

    val, warn = _run(dash.fetch_skills_data([_char(perms.SKILLS)], db=None))[CHAR_ID]
    assert warn is None
    assert val == {"total_sp": 1_000_000, "unallocated_sp": 0, "levels": {}}


def test_fetch_skills_data_no_scope_path(monkeypatch):
    """A character that never shared esi-skills.read_skills.v1 is skipped
    before any client/ESI call is made."""
    called = []

    async def fake_client_for(char):
        called.append(char.character_id)
        return object(), None

    monkeypatch.setattr(dash, "_client_for", fake_client_for)

    val, warn = _run(dash.fetch_skills_data([_char("")], db=None))[CHAR_ID]
    assert val is None
    assert warn == "missing_scope"
    assert called == [], "must not fetch a client for a character lacking the scope"


def test_fetch_skills_data_esi_error_is_reported_not_raised(monkeypatch):
    async def fake_client_for(char):
        return object(), None

    async def fake_get_skills(client, character_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(dash, "_client_for", fake_client_for)
    monkeypatch.setattr(dash.esi_char, "get_skills", fake_get_skills)

    val, warn = _run(dash.fetch_skills_data([_char(perms.SKILLS)], db=None))[CHAR_ID]
    assert val is None
    assert warn == "esi_error: RuntimeError"


# ── Wiring: the four sync dicts (T-073 spec) ────────────────────────────────

def test_skills_field_is_registered_everywhere_it_needs_to_be():
    assert dash.FIELD_CACHE_SECONDS["skills"] == 3600
    assert dash.FIELD_SCOPES["skills"] == perms.SKILLS
    assert dash._FIELD_DB_COLUMN["skills"] == "skills_json"
    assert dash._FIELD_FETCHERS["skills"] is dash.fetch_skills_data


def test_sync_fields_writes_skills_json(monkeypatch):
    """End-to-end through _sync_fields (test_sync_field_sessions.py's own
    pattern): the generic dict-valued-field branch must round-trip the
    skills payload into cache.skills_json exactly."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def fake_client_for(char):
        return object(), None

    async def fake_get_skills(client, character_id):
        return {"total_sp": 42, "unallocated_sp": 0, "skills": []}

    monkeypatch.setattr(dash, "AsyncSessionLocal", SessionLocal)
    monkeypatch.setattr(dash, "FIELD_CACHE_SECONDS", {"skills": 3600})
    monkeypatch.setattr(dash, "FIELD_SCOPES", {"skills": perms.SKILLS})
    monkeypatch.setattr(dash, "_FIELD_DB_COLUMN", {"skills": "skills_json"})
    monkeypatch.setattr(dash, "_FIELD_FETCHERS", {"skills": dash.fetch_skills_data})
    monkeypatch.setattr(dash, "_client_for", fake_client_for)
    monkeypatch.setattr(dash.esi_char, "get_skills", fake_get_skills)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as seed_db:
            seed_db.add(_char(perms.SKILLS))
            await seed_db.commit()

        outer_db = SessionLocal()
        char = (await outer_db.execute(
            select(Character).where(Character.character_id == CHAR_ID)
        )).scalar_one()
        cache = CharacterDashboardCache(character_id=CHAR_ID)
        from app.db.models import CharacterAssetCache
        asset_cache = CharacterAssetCache(character_id=CHAR_ID)
        outer_db.add(cache)
        outer_db.add(asset_cache)
        await outer_db.commit()
        try:
            await dash._sync_fields(CHAR_ID, char, cache, asset_cache, outer_db)
        finally:
            await outer_db.close()

        async with SessionLocal() as check_db:
            row = (await check_db.execute(
                select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == CHAR_ID)
            )).scalar_one()
            return row.skills_json

    skills_json = _run(scenario())
    _run(engine.dispose())

    assert json.loads(skills_json) == {"total_sp": 42, "unallocated_sp": 0, "levels": {}}


# ── Purge mapping ────────────────────────────────────────────────────────────

def test_purge_cache_columns_and_sync_fields_include_skills():
    assert "skillqueue_json" in purge._CACHE_COLUMNS["skills"]
    assert "skills_json" in purge._CACHE_COLUMNS["skills"]
    assert "skillqueue" in purge._SYNC_FIELDS["skills"]
    assert "skills" in purge._SYNC_FIELDS["skills"]


def test_withdrawing_the_skills_permission_clears_skills_json():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(CharacterDashboardCache(
                character_id=CHAR_ID,
                skills_json=json.dumps({"total_sp": 1, "unallocated_sp": 0, "levels": {}}),
                skillqueue_json=json.dumps([{"skill_id": 1}]),
                field_synced_json=json.dumps({"skills": "2026-01-01T00:00:00", "skillqueue": "2026-01-01T00:00:00"}),
            ))
            await db.commit()

            from app.db.cache import cache_get  # noqa: F401 — sanity import, unused directly
            counts = await purge.clear_live_state(db, CHAR_ID, {"skills"})
            await db.commit()

            row = (await db.execute(
                select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == CHAR_ID)
            )).scalar_one()
            return counts, row.skills_json, row.skillqueue_json, json.loads(row.field_synced_json or "{}")

    counts, skills_json, skillqueue_json, field_synced = _run(scenario())
    _run(engine.dispose())

    assert skills_json is None
    assert skillqueue_json is None
    assert "skills" not in field_synced
    assert "skillqueue" not in field_synced
    assert counts["dashboard_cache_columns"] == 2


# ── ensure_dashboard_cache_columns: idempotent ALTER ────────────────────────

def test_ensure_dashboard_cache_columns_is_idempotent():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        # Build an OLD-shape table (every real column except skills_json).
        async with SessionLocal() as db:
            await db.execute(text(
                "CREATE TABLE character_dashboard_cache ("
                "character_id INTEGER PRIMARY KEY, wallet FLOAT, "
                "location_json TEXT, industry_json TEXT, clones_json TEXT, "
                "orders_json TEXT, mail_json TEXT, notifications_json TEXT, "
                "contracts_json TEXT, pi_json TEXT, skillqueue_json TEXT, "
                "zkill_json TEXT, last_synced DATETIME, "
                "sync_status VARCHAR(16) NOT NULL DEFAULT 'idle', "
                "sync_error TEXT, sync_warnings_json TEXT, field_synced_json TEXT)"
            ))
            await db.commit()

        async with SessionLocal() as db:
            await ensure_dashboard_cache_columns(db)
        # Second call against an already-migrated table: must not raise.
        async with SessionLocal() as db:
            await ensure_dashboard_cache_columns(db)

        async with SessionLocal() as db:
            db.add(CharacterDashboardCache(character_id=CHAR_ID, skills_json="{}"))
            await db.commit()
            row = (await db.execute(
                select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == CHAR_ID)
            )).scalar_one()
            return row.skills_json

    result = _run(scenario())
    _run(engine.dispose())
    assert result == "{}"


# ── PER_CHARACTER_USER_TABLES: skill_farm_pilots is purged on character removal ──

def test_removing_a_character_purges_its_skill_farm_pilot_row():
    assert "skill_farm_pilots" in purge.PER_CHARACTER_USER_TABLES

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    from app.db.models import SkillFarmPilot

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SkillFarmPilot(user_id=1, character_id=CHAR_ID, base_sp=5_000_000))
            db.add(SkillFarmPilot(user_id=1, character_id=CHAR_ID + 1, base_sp=5_000_000))
            await db.commit()

            deleted = await purge.purge_character_user_rows(db, CHAR_ID)
            await db.commit()

            remaining = (await db.execute(select(SkillFarmPilot))).scalars().all()
            return deleted, [p.character_id for p in remaining]

    deleted, remaining = _run(scenario())
    _run(engine.dispose())
    assert deleted == 1
    assert remaining == [CHAR_ID + 1]
