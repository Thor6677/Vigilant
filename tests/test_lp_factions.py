"""Tests for the LP store faction-grouped corp tree (Phase 4 Task 3,
EVE-style pickers plan).

Mirrors `tests/test_lp_roi.py` / `tests/test_market_orders.py` disciplines:

1. **Grouping math** — `get_corps_by_faction(db)` against a monkeypatched
   roster + faction fetch (no network) and a real temp-sqlite `sde_npc_corps`
   table (temp-DB idiom from `tests/test_invention_lookup.py`). Majors first
   alphabetically, then remaining named factions alphabetically, "Other"
   last; a corp absent from `sde_npc_corps` (or present with a NULL
   `faction_id`) falls to "Other"; an empty `sde_npc_corps` table (pending
   SDE reimport) degrades the whole roster to "Other" with `degraded: True`
   and a `note`; a hard `/universe/factions/` fetch failure also degrades to
   "Other"/`degraded: True` but without a `note`. Neither degraded path
   populates the cache (next call retries) — same fetch-once/
   failure-not-cached discipline as `get_npc_corps` in the same module.
2. **Route auth gating** — TestClient smoke test, same pattern as the
   existing `/market/lp/*` route tests.
3. **Fragment shape** — the placeholder `partials/lp_corp_tree.html`
   rendered standalone through a Jinja Environment (matches
   test_market_orders.py's template-render idiom). Only asserts the
   Task3∩Task4-stable contract (`data-corp-id`, corp/faction names, the
   degraded note) since Task 4 restyles this fragment.
"""
import asyncio
import os
import tempfile

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import SDENpcCorp
from app.market import lp

_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "app", "templates")


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


async def _seed_npc_corps(SessionLocal, faction_map: dict[int, int | None]):
    """`faction_map`: {corp_id: faction_id_or_None} — one `SDENpcCorp` row
    per entry. A corp entirely absent from `sde_npc_corps` also falls to
    "Other" (same as a NULL `faction_id` row) — tests exercise both."""
    async with SessionLocal() as db:
        for cid, fid in faction_map.items():
            db.add(SDENpcCorp(corporation_id=cid, faction_id=fid))
        await db.commit()


def _reset_caches(monkeypatch):
    """Module-global caches persist across tests — reset all four to their
    initial (unpopulated) state before every test in this file."""
    monkeypatch.setattr(lp, "_npc_corp_ids", None)
    monkeypatch.setattr(lp, "_npc_corp_names", {})
    monkeypatch.setattr(lp, "_corp_faction_map", None)
    monkeypatch.setattr(lp, "_faction_names", {})


def _seed_roster(monkeypatch, corps: dict[int, str]):
    """corps: {corp_id: name} — monkeypatch the NPC-roster fetch helpers so
    `get_npc_corps()` (called internally by `get_corps_by_faction()`)
    resolves without touching the network."""
    async def fake_ids():
        return list(corps.keys())

    async def fake_names(ids):
        return {cid: corps[cid] for cid in ids}

    monkeypatch.setattr(lp, "_fetch_npc_corp_ids_esi", fake_ids)
    monkeypatch.setattr(lp, "_fetch_names_esi", fake_names)


FACTIONS = [
    {"faction_id": 500001, "name": "Caldari State"},
    {"faction_id": 500002, "name": "Minmatar Republic"},
    {"faction_id": 500003, "name": "Amarr Empire"},
    {"faction_id": 500004, "name": "Gallente Federation"},
    {"faction_id": 500011, "name": "Guristas Pirates"},
]


# ── grouping math ────────────────────────────────────────────────────────────

def test_grouping_majors_first_then_others_then_other_last(monkeypatch):
    _reset_caches(monkeypatch)
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        corps = {
            1: "Caldari Navy",
            2: "Republic Fleet",
            3: "Amarr Navy",
            4: "Federation Navy",
            5: "Guristas",
            6: "Unaffiliated Trading Co",  # no sde_npc_corps row -> Other
        }
        _seed_roster(monkeypatch, corps)
        await _seed_npc_corps(SessionLocal, {
            1: 500001, 2: 500002, 3: 500003, 4: 500004, 5: 500011,
        })

        async def fake_factions():
            return FACTIONS

        monkeypatch.setattr(lp, "_fetch_factions_esi", fake_factions)

        async with SessionLocal() as db:
            grouped = await lp.get_corps_by_faction(db)

        names = [g["faction_name"] for g in grouped]
        assert names == [
            "Amarr Empire",
            "Caldari State",
            "Gallente Federation",
            "Minmatar Republic",
            "Guristas Pirates",
            "Other",
        ]
        other = next(g for g in grouped if g["faction_name"] == "Other")
        assert [c["corporation_id"] for c in other["corps"]] == [6]
        assert not other.get("degraded")

    _run(scenario())


def test_corp_without_faction_id_falls_to_other(monkeypatch):
    _reset_caches(monkeypatch)
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        _seed_roster(monkeypatch, {1: "Some Corp"})
        # Row present in sde_npc_corps but with a NULL faction_id (most NPC
        # corps) -> still falls to Other, same as no row at all.
        await _seed_npc_corps(SessionLocal, {1: None})

        async def fake_factions():
            return FACTIONS

        monkeypatch.setattr(lp, "_fetch_factions_esi", fake_factions)

        async with SessionLocal() as db:
            grouped = await lp.get_corps_by_faction(db)

        assert len(grouped) == 1
        assert grouped[0]["faction_name"] == "Other"
        assert grouped[0]["corps"][0]["name"] == "Some Corp"

    _run(scenario())


def test_faction_fetch_failure_degrades_and_does_not_cache(monkeypatch):
    _reset_caches(monkeypatch)
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        _seed_roster(monkeypatch, {1: "Some Corp", 2: "Other Corp"})
        # sde_npc_corps IS populated here — the failure under test is the
        # ESI factions-name fetch, not a pending SDE reimport.
        await _seed_npc_corps(SessionLocal, {1: 500001, 2: None})

        async def failing_factions():
            raise RuntimeError("esi down")

        monkeypatch.setattr(lp, "_fetch_factions_esi", failing_factions)

        async with SessionLocal() as db:
            grouped = await lp.get_corps_by_faction(db)

        assert len(grouped) == 1
        assert grouped[0]["faction_name"] == "Other"
        assert grouped[0]["degraded"] is True
        assert grouped[0].get("note") is None  # no reimport note for this cause
        assert {c["corporation_id"] for c in grouped[0]["corps"]} == {1, 2}

        # failure NOT cached — the cache stays unpopulated
        assert lp._corp_faction_map is None

        # a later successful call retries and populates normally
        async def fake_factions():
            return FACTIONS

        monkeypatch.setattr(lp, "_fetch_factions_esi", fake_factions)

        async with SessionLocal() as db:
            grouped2 = await lp.get_corps_by_faction(db)

        names = [g["faction_name"] for g in grouped2]
        assert "Caldari State" in names
        assert lp._corp_faction_map is not None  # now cached

    _run(scenario())


def test_empty_sde_table_degrades_with_reimport_note_and_does_not_cache(monkeypatch):
    _reset_caches(monkeypatch)
    engine, SessionLocal = _temp_engine()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        _seed_roster(monkeypatch, {1: "Some Corp", 2: "Other Corp"})
        # sde_npc_corps table exists but is EMPTY — a pre-reimport deploy.

        async def fake_factions():
            return FACTIONS

        monkeypatch.setattr(lp, "_fetch_factions_esi", fake_factions)

        async with SessionLocal() as db:
            grouped = await lp.get_corps_by_faction(db)

        assert len(grouped) == 1
        assert grouped[0]["faction_name"] == "Other"
        assert grouped[0]["degraded"] is True
        assert "SDE reimport" in grouped[0]["note"]
        assert {c["corporation_id"] for c in grouped[0]["corps"]} == {1, 2}

        # failure NOT cached — the cache stays unpopulated
        assert lp._corp_faction_map is None

        # once the table is populated (reimport finishes), a later call
        # retries and populates the cache normally
        await _seed_npc_corps(SessionLocal, {1: 500001, 2: None})

        async with SessionLocal() as db:
            grouped2 = await lp.get_corps_by_faction(db)

        names = [g["faction_name"] for g in grouped2]
        assert "Caldari State" in names
        assert lp._corp_faction_map is not None  # now cached

    _run(scenario())


# ── route: auth gating ───────────────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_corps_tree_route_401_when_unauthenticated():
    client = _client()
    r = client.get("/market/lp/corps-tree")
    assert r.status_code == 401


# ── fragment shape (Task3∩Task4-stable contract) ────────────────────────────

def _render_tree(**ctx):
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    tmpl = env.get_template("partials/lp_corp_tree.html")
    return tmpl.render(**ctx)


def test_tree_fragment_renders_corp_ids_and_faction_names():
    factions = [
        {"faction_name": "Caldari State", "corps": [
            {"corporation_id": 1000125, "name": "Caldari Navy"},
        ]},
        {"faction_name": "Other", "corps": [
            {"corporation_id": 1000180, "name": "Unaffiliated Trading Co"},
        ]},
    ]
    html = _render_tree(factions=factions, degraded=False)
    assert 'data-corp-id="1000125"' in html
    assert "Caldari Navy" in html
    assert "Caldari State" in html
    assert 'data-corp-id="1000180"' in html
    assert "Unaffiliated Trading Co" in html
    assert "Faction data unavailable" not in html


def test_tree_fragment_degraded_shows_retry_note():
    factions = [
        {"faction_name": "Other", "corps": [
            {"corporation_id": 1000125, "name": "Caldari Navy"},
        ], "degraded": True},
    ]
    html = _render_tree(factions=factions, degraded=True)
    assert "Faction data unavailable" in html
    assert 'data-corp-id="1000125"' in html


def test_tree_fragment_degraded_shows_sde_pending_note_when_given():
    factions = [
        {"faction_name": "Other", "corps": [
            {"corporation_id": 1000125, "name": "Caldari Navy"},
        ], "degraded": True, "note": "NPC corp faction data pending SDE reimport."},
    ]
    html = _render_tree(factions=factions, degraded=True, note=factions[0]["note"])
    assert "pending SDE reimport" in html
    assert 'data-corp-id="1000125"' in html
