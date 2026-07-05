"""Tests for the build-profitability finder (Phase 4 Task 4).

Pure ranking math (`rank_builds` / `build_cost_per_unit`) is tested directly
against fixture products + prices — including the product_quantity>1 case
(ammo yields many units per run) and the unpriced-material/product exclusion.
A regression test pins one known calculation through the extracted cost engine
(`app.industry.manufacturing.calc_material`) so the refactor stays honest.

Cap enforcement seeds a temp SQLite DB with >200 buildable products in one
group and asserts `get_group_buildables` reports the full count but returns
only the cap. Route gating is a TestClient smoke. Manual-event-loop idiom per
repo convention (tests/test_market_history.py).
"""
import asyncio
import os
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import (
    SDEType, SDEGroup, SDEBlueprintInfo, SDEBlueprintMaterial,
)
from app.industry import build_finder
from app.industry.manufacturing import STRUCTURES, RIGS, SEC_STATUS
from app.sde import lookup as sde


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
    return engine, async_sessionmaker(engine, expire_on_commit=False), tmp.name


# Modifier args for the "no bonuses" baseline: ME 0, NPC station, no rig, highsec.
_NPC = STRUCTURES["npc_station"]["mat"]      # 1.0
_NORIG = RIGS["none"]["mat"]                  # 0.0
_HIGH = SEC_STATUS["highsec"]["mult"]         # 1.0


# ── pure cost engine (regression pin) ─────────────────────────────────────────

def test_build_cost_per_unit_pins_known_calc():
    """100 units of type 34 at 5 ISK, ME 10, no structure/rig bonus:
    adjusted qty = ceil(100 * 0.9) = 90 → 90 * 5 = 450 ISK for one run,
    product_quantity 1 → 450 ISK/unit. Pins the extracted calc_material path."""
    materials = [{"type_id": 34, "quantity": 100}]
    cost = build_finder.build_cost_per_unit(
        materials, product_quantity=1, me=10,
        struct_mat=_NPC, rig_mat_base=_NORIG, sec_mult=_HIGH,
        price_map={34: 5.0},
    )
    assert cost == 450.0


def test_build_cost_per_unit_divides_by_product_quantity():
    """Ammo-style: one run consumes 100 tritanium (5 ISK) but yields 100 units,
    so cost/unit = 500 / 100 = 5.0 — NOT 500."""
    cost = build_finder.build_cost_per_unit(
        [{"type_id": 34, "quantity": 100}], product_quantity=100, me=0,
        struct_mat=_NPC, rig_mat_base=_NORIG, sec_mult=_HIGH,
        price_map={34: 5.0},
    )
    assert cost == 5.0


def test_build_cost_per_unit_unpriced_material_returns_none():
    cost = build_finder.build_cost_per_unit(
        [{"type_id": 999, "quantity": 1}], product_quantity=1, me=0,
        struct_mat=_NPC, rig_mat_base=_NORIG, sec_mult=_HIGH,
        price_map={34: 5.0},
    )
    assert cost is None


# ── ranking ───────────────────────────────────────────────────────────────────

def _products():
    return [
        # A: sell 1000, cost 500 → +500 ISK, +100%
        {"product_type_id": 1000, "product_name": "Alpha",
         "blueprint_type_id": 1001, "product_quantity": 1,
         "materials": [{"type_id": 34, "quantity": 100}]},
        # B (ammo): sell 6, cost 5 (product_quantity 100) → +1 ISK, +20%
        {"product_type_id": 2000, "product_name": "Bravo",
         "blueprint_type_id": 2001, "product_quantity": 100,
         "materials": [{"type_id": 34, "quantity": 100}]},
        # C: product unpriced → excluded from ranking, sorts last
        {"product_type_id": 3000, "product_name": "Charlie",
         "blueprint_type_id": 3001, "product_quantity": 1,
         "materials": [{"type_id": 34, "quantity": 100}]},
        # D: material unpriced → excluded from ranking, sorts last
        {"product_type_id": 4000, "product_name": "Delta",
         "blueprint_type_id": 4001, "product_quantity": 1,
         "materials": [{"type_id": 999, "quantity": 1}]},
    ]


def test_rank_builds_orders_by_margin_pct_desc_unpriced_last():
    price_map = {34: 5.0, 1000: 1000.0, 2000: 6.0}  # 3000 & 999 unpriced
    ranked = build_finder.rank_builds(
        _products(), me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map,
    )
    ids = [r["product_type_id"] for r in ranked]
    # A (100%) before B (20%); C and D (unpriced) after both priced rows.
    assert ids[0] == 1000
    assert ids[1] == 2000
    assert set(ids[2:]) == {3000, 4000}

    a, b = ranked[0], ranked[1]
    assert a["priced"] is True and b["priced"] is True
    assert a["margin_isk"] == 500.0 and a["margin_pct"] == 100.0
    assert b["cost_per_unit"] == 5.0 and b["margin_isk"] == 1.0 and b["margin_pct"] == 20.0

    for r in ranked[2:]:
        assert r["priced"] is False
        assert r["margin_pct"] is None


# ── cap enforcement (DB) ──────────────────────────────────────────────────────

def test_get_group_buildables_enforces_cap():
    engine, SessionLocal, _path = _temp_engine()
    GROUP = 42
    N = 205

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEGroup(group_id=GROUP, category_id=6, group_name="Widgets"))
            for i in range(N):
                tid = 500_000 + i
                bp = 900_000 + i
                db.add(SDEType(type_id=tid, type_name=f"Widget {i:04d}",
                               group_id=GROUP, published=True))
                db.add(SDEBlueprintInfo(blueprint_type_id=bp, product_type_id=tid,
                                        manufacturing_time=100, product_quantity=1))
                db.add(SDEBlueprintMaterial(blueprint_type_id=bp, activity_id=1,
                                            material_type_id=34, quantity=10))
            await db.commit()

            total_n, products = await sde.get_group_buildables(db, GROUP, cap=200)
            assert total_n == N
            assert len(products) == 200
            # First-200-by-name: names sort lexicographically, so Widget 0000..0199.
            assert products[0]["product_name"] == "Widget 0000"
            assert products[-1]["product_name"] == "Widget 0199"
            assert products[0]["materials"] == [{"type_id": 34, "quantity": 10}]

    try:
        _run(scenario())
    finally:
        _run(engine.dispose())
        os.unlink(_path)


# ── route gating ──────────────────────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_build_finder_page_redirects_when_unauthenticated():
    r = _client().get("/industry/build-finder", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_build_finder_results_401_when_unauthenticated():
    r = _client().get("/industry/build-finder/results?group_id=42",
                      follow_redirects=False)
    assert r.status_code == 401
