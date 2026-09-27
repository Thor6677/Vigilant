"""app/dashboard/prefs.py: validation, caps, unknown-key drops, bad-JSON
tolerance, and load_prefs/save_prefs patch semantics against a real (throwaway
sqlite) UserDashboardPrefs row.
"""
import asyncio
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard import prefs as prefs_mod
from app.dashboard.prefs import (
    DEFAULT_PREFS, DEFAULT_TABLE_COLUMNS, GAME_WIDE_SECTIONS, MODES,
    SECTION_KEYS, TABLE_COLUMNS,
    apply_patch, load_prefs, sanitize, save_prefs,
)
from app.db.models import Base, User, UserDashboardPrefs


# ── Static schema sanity ─────────────────────────────────────────────────────

def test_game_wide_sections_are_a_subset_of_section_keys():
    assert set(GAME_WIDE_SECTIONS) <= set(SECTION_KEYS)


def test_default_prefs_are_already_valid():
    assert sanitize(DEFAULT_PREFS) == DEFAULT_PREFS


def test_default_table_columns_are_a_readable_subset_of_the_catalog():
    # T-076: the default is a curated subset, not the whole 18-column
    # catalog — see DEFAULT_TABLE_COLUMNS's own comment in prefs.py.
    assert DEFAULT_PREFS["table_columns"] == list(DEFAULT_TABLE_COLUMNS)
    assert set(DEFAULT_TABLE_COLUMNS) <= set(TABLE_COLUMNS)


# ── sanitize(): tolerance and validation ─────────────────────────────────────

@pytest.mark.parametrize("bad", [None, [], "nope", 5, True])
def test_sanitize_tolerates_non_dict_input(bad):
    assert sanitize(bad) == DEFAULT_PREFS


def test_sanitize_drops_unknown_keys():
    out = sanitize({"mode": "cards", "totally_unknown": "x"})
    assert "totally_unknown" not in out
    assert out["mode"] == "cards"


def test_sanitize_rejects_invalid_mode():
    assert sanitize({"mode": "bogus"})["mode"] == DEFAULT_PREFS["mode"]


@pytest.mark.parametrize("mode", MODES)
def test_sanitize_accepts_every_declared_mode(mode):
    # Phase 1 only *renders* compact/cards, but the stored schema accepts all
    # four so phase 2 doesn't need a migration.
    assert sanitize({"mode": mode})["mode"] == mode


def test_sanitize_caps_collapsed_groups_length():
    names = [f"g{i}" for i in range(200)]
    out = sanitize({"collapsed_groups": names})
    assert len(out["collapsed_groups"]) == 64
    assert out["collapsed_groups"] == names[:64]


def test_sanitize_truncates_overlong_group_names():
    out = sanitize({"collapsed_groups": ["x" * 500]})
    assert len(out["collapsed_groups"][0]) == 100


def test_sanitize_drops_non_string_items_from_collapsed_groups():
    out = sanitize({"collapsed_groups": ["ok", 5, None, ["nested"], "also-ok"]})
    assert out["collapsed_groups"] == ["ok", "also-ok"]


def test_sanitize_restricts_sections_to_the_known_set():
    out = sanitize({"collapsed_sections": ["wealth", "not-a-real-section"], "hidden_sections": ["activity"]})
    assert out["collapsed_sections"] == ["wealth"]
    assert out["hidden_sections"] == ["activity"]


def test_sanitize_dedupes_sections_preserving_order():
    out = sanitize({"hidden_sections": ["activity", "wealth", "activity"]})
    assert out["hidden_sections"] == ["activity", "wealth"]


def test_sanitize_restricts_table_columns_to_the_catalog_and_order():
    out = sanitize({"table_columns": ["ship", "bogus", "pilot", "ship"]})
    assert out["table_columns"] == ["ship", "pilot"]


def test_sanitize_rejects_malformed_table_sort():
    assert sanitize({"table_sort": {"key": "pilot"}})["table_sort"] == DEFAULT_PREFS["table_sort"]
    assert sanitize({"table_sort": {"key": "bogus", "dir": "asc"}})["table_sort"] == DEFAULT_PREFS["table_sort"]
    assert sanitize({"table_sort": {"key": "wallet", "dir": "sideways"}})["table_sort"] == DEFAULT_PREFS["table_sort"]
    assert sanitize({"table_sort": "wallet"})["table_sort"] == DEFAULT_PREFS["table_sort"]


def test_sanitize_accepts_valid_table_sort():
    out = sanitize({"table_sort": {"key": "wallet", "dir": "desc"}})
    assert out["table_sort"] == {"key": "wallet", "dir": "desc"}


def test_sanitize_caps_group_order_length():
    names = [f"g{i}" for i in range(200)]
    out = sanitize({"group_order": names})
    assert len(out["group_order"]) == 64
    assert out["group_order"] == names[:64]


def test_sanitize_truncates_overlong_group_order_names():
    out = sanitize({"group_order": ["x" * 500]})
    assert len(out["group_order"][0]) == 100


def test_sanitize_caps_tag_filter():
    tags = [f"t{i}" for i in range(40)]
    out = sanitize({"tag_filter": tags})
    assert len(out["tag_filter"]) == 16
    out2 = sanitize({"tag_filter": ["x" * 100]})
    assert len(out2["tag_filter"][0]) == 24


# ── apply_patch(): patch semantics ───────────────────────────────────────────

def test_apply_patch_only_touches_patched_keys():
    base = sanitize({"mode": "compact", "hidden_sections": ["wealth"]})
    out = apply_patch(base, {"mode": "cards"})
    assert out["mode"] == "cards"
    assert out["hidden_sections"] == ["wealth"]  # untouched


def test_apply_patch_drops_unknown_keys():
    base = sanitize({})
    out = apply_patch(base, {"mode": "compact", "made_up_field": 1})
    assert "made_up_field" not in out
    assert out["mode"] == "compact"


def test_apply_patch_ignores_invalid_value_and_keeps_previous():
    base = sanitize({"mode": "compact"})
    out = apply_patch(base, {"mode": "not-a-real-mode"})
    assert out["mode"] == "compact"  # invalid patch value did not reset it


def test_apply_patch_tolerates_non_dict_patch():
    base = sanitize({"mode": "compact"})
    assert apply_patch(base, None) == base
    assert apply_patch(base, ["not", "a", "dict"]) == base


# ── load_prefs / save_prefs against a real (throwaway) DB ───────────────────

USER_ID = 501


@pytest.fixture
def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add(User(id=USER_ID, role="user"))
            db.add(User(id=USER_ID + 1, role="user"))
            await db.commit()

    asyncio.run(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def test_load_prefs_defaults_when_no_row_exists(db_factory):
    async def _run():
        async with db_factory() as db:
            return await load_prefs(db, USER_ID)
    assert asyncio.run(_run()) == DEFAULT_PREFS


def test_save_then_load_round_trips(db_factory):
    async def _run():
        async with db_factory() as db:
            saved = await save_prefs(db, USER_ID, {"mode": "compact", "hidden_sections": ["activity"]})
        async with db_factory() as db:
            loaded = await load_prefs(db, USER_ID)
        return saved, loaded
    saved, loaded = asyncio.run(_run())
    assert saved["mode"] == "compact"
    assert saved["hidden_sections"] == ["activity"]
    assert loaded == saved


def test_save_prefs_patch_semantics_across_two_saves(db_factory):
    async def _run():
        async with db_factory() as db:
            await save_prefs(db, USER_ID, {"mode": "compact"})
        async with db_factory() as db:
            return await save_prefs(db, USER_ID, {"hidden_sections": ["battles"]})
    out = asyncio.run(_run())
    assert out["mode"] == "compact"              # carried over from the first save
    assert out["hidden_sections"] == ["battles"]  # applied by the second


def test_save_prefs_does_not_touch_another_users_row(db_factory):
    async def _run():
        async with db_factory() as db:
            await save_prefs(db, USER_ID, {"mode": "compact"})
        async with db_factory() as db:
            await save_prefs(db, USER_ID + 1, {"mode": "table"})
        async with db_factory() as db:
            a = await load_prefs(db, USER_ID)
            b = await load_prefs(db, USER_ID + 1)
        return a, b
    a, b = asyncio.run(_run())
    assert a["mode"] == "compact"
    assert b["mode"] == "table"


def test_load_prefs_tolerates_malformed_stored_json(db_factory):
    async def _run():
        async with db_factory() as db:
            db.add(UserDashboardPrefs(user_id=USER_ID, prefs_json="{not valid json"))
            await db.commit()
        async with db_factory() as db:
            return await load_prefs(db, USER_ID)
    assert asyncio.run(_run()) == DEFAULT_PREFS


def test_load_prefs_tolerates_valid_json_of_the_wrong_shape(db_factory):
    async def _run():
        async with db_factory() as db:
            db.add(UserDashboardPrefs(user_id=USER_ID, prefs_json=json.dumps(["not", "a", "dict"])))
            await db.commit()
        async with db_factory() as db:
            return await load_prefs(db, USER_ID)
    assert asyncio.run(_run()) == DEFAULT_PREFS
