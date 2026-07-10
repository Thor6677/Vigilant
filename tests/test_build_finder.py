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

Route-level invention composition (Task 4) reuses the authed-session +
`get_db`-override idiom from `tests/test_pnl_route.py`: a real temp SQLite DB
seeded with a full invention chain (mirrors `tests/test_invention_lookup.py`'s
fixture), `market_lp.get_price_map` monkeypatched to a fixture map (no
network), hit through the actual `/industry/build-finder/results` endpoint.
"""
import asyncio
import base64
import json
import os
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, get_db
from app.db.sde_models import (
    SDEType, SDEGroup, SDEBlueprintInfo, SDEBlueprintMaterial,
    SDEBlueprintInvention, SDEBlueprintInventionMaterial, SDEBlueprintInventionSkill,
)
from app.industry import build_finder
from app.industry.manufacturing import STRUCTURES, RIGS, SEC_STATUS
from app.market import lp as market_lp
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


# ── invention overhead (Task 4) ────────────────────────────────────────────────

def test_rank_builds_inventable_row_costs_at_invented_me_plus_overhead():
    """Product A (materials [100x type 34], product_quantity 1) is inventable
    at invented_me=2 (T2 base). At ME2/NPC-station/no-rig, calc_material(100,
    1, 2, 1.0, 0.0, 1.0) = ceil(100*0.98) = 98 -> build cost 98*5.0 = 490.0.
    Overhead is precomputed by the route (25.0 here) and simply added."""
    price_map = {34: 5.0, 1000: 1000.0}
    invention = {1000: {"overhead_per_unit": 25.0, "invented_me": 2, "skill_missing": False}}

    ranked = build_finder.rank_builds(
        [_products()[0]], me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map, invention=invention,
    )
    row = ranked[0]

    # Sanity: build-only cost at invented ME differs from the page ME (0).
    build_only = build_finder.build_cost_per_unit(
        [{"type_id": 34, "quantity": 100}], 1, me=2,
        struct_mat=_NPC, rig_mat_base=_NORIG, sec_mult=_HIGH, price_map=price_map,
    )
    assert build_only == 490.0

    assert row["cost_per_unit"] == 490.0 + 25.0
    assert row["invention_overhead"] == 25.0
    assert row["invented_me"] == 2
    assert row["skill_missing"] is False
    assert row["priced"] is True
    assert row["margin_isk"] == 1000.0 - 515.0


def test_rank_builds_none_overhead_row_unpriced_and_sorts_last():
    """A None overhead (unpriced decryptor, or un-inventable P<=0) forces the
    row unpriced even though the raw build cost is computable — must not be
    silently zero-costed."""
    price_map = {34: 5.0, 1000: 1000.0, 2000: 6.0}
    invention = {1000: {"overhead_per_unit": None, "invented_me": 2, "skill_missing": False}}

    ranked = build_finder.rank_builds(
        _products()[:2], me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map, invention=invention,
    )
    by_id = {r["product_type_id"]: r for r in ranked}

    assert by_id[1000]["priced"] is False
    assert by_id[1000]["cost_per_unit"] is None
    assert by_id[1000]["margin_isk"] is None
    assert by_id[1000]["margin_pct"] is None
    assert by_id[1000]["invention_overhead"] is None
    # The un-inventable row sorts after the still-priced Bravo row.
    ids = [r["product_type_id"] for r in ranked]
    assert ids == [2000, 1000]


def test_rank_builds_product_absent_from_invention_dict_is_byte_identical():
    """A product NOT present in the invention dict must behave exactly as a
    plain (no-invention) call — same cost/sell/margin, and identical extra
    keys (None/None/False) as a call with invention=None entirely."""
    price_map = {34: 5.0, 1000: 1000.0, 2000: 6.0}
    products = _products()[:2]  # Alpha (id 1000), Bravo (id 2000)

    plain = build_finder.rank_builds(
        products, me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map,
    )
    # invention dict only covers a product NOT in this products list.
    with_unrelated_invention = build_finder.rank_builds(
        products, me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map,
        invention={9999: {"overhead_per_unit": 1.0, "invented_me": 2, "skill_missing": True}},
    )

    assert plain == with_unrelated_invention
    for r in plain:
        assert r["invention_overhead"] is None
        assert r["invented_me"] is None
        assert r["skill_missing"] is False


def test_rank_builds_skill_missing_propagates():
    price_map = {34: 5.0, 1000: 1000.0}
    invention = {1000: {"overhead_per_unit": 10.0, "invented_me": 2, "skill_missing": True}}
    ranked = build_finder.rank_builds(
        [_products()[0]], me=0, struct_mat=_NPC, rig_mat_base=_NORIG,
        sec_mult=_HIGH, price_map=price_map, invention=invention,
    )
    assert ranked[0]["skill_missing"] is True


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


# ── route-level invention composition ──────────────────────────────────────────

INV_GROUP_ID = 5501
INV_PRODUCT_ID = 115501
INV_T2_BLUEPRINT_ID = 115502
INV_T1_BLUEPRINT_ID = 115503
INV_ENCRYPTION_SKILL_ID = 115504
INV_SCIENCE_A_SKILL_ID = 115505
INV_SCIENCE_B_SKILL_ID = 115506


def _authed_client():
    """TestClient carrying a signed session cookie — idiom from
    tests/test_pnl_route.py (itself from tests/test_networth.py)."""
    import app.main as main

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": 1}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _seeded_invention_db(with_invention_chain: bool):
    """Temp sqlite DB with one buildable T2-shaped product in a group.
    `with_invention_chain=True` also seeds the full invention chain
    (mirrors tests/test_invention_lookup.py's fixture); `False` leaves
    `sde_blueprint_invention` empty entirely, to exercise the
    "tables not yet imported" footnote path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEGroup(group_id=INV_GROUP_ID, category_id=6, group_name="Test T2 Group"))
            db.add(SDEType(type_id=INV_PRODUCT_ID, type_name="Test T2 Widget",
                           group_id=INV_GROUP_ID, published=True))
            db.add(SDEBlueprintInfo(
                blueprint_type_id=INV_T2_BLUEPRINT_ID, product_type_id=INV_PRODUCT_ID,
                product_quantity=1, manufacturing_time=600,
            ))
            db.add(SDEBlueprintMaterial(
                blueprint_type_id=INV_T2_BLUEPRINT_ID, activity_id=1,
                material_type_id=34, quantity=100,
            ))
            if with_invention_chain:
                db.add(SDEBlueprintInvention(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID,
                    product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
                    probability=0.3, base_runs=1, time=63900,
                ))
                db.add(SDEBlueprintInventionMaterial(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID, material_type_id=20410, quantity=8))
                db.add(SDEBlueprintInventionMaterial(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID, material_type_id=20424, quantity=8))
                db.add(SDEBlueprintInventionSkill(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID, skill_type_id=INV_ENCRYPTION_SKILL_ID))
                db.add(SDEBlueprintInventionSkill(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID, skill_type_id=INV_SCIENCE_A_SKILL_ID))
                db.add(SDEBlueprintInventionSkill(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID, skill_type_id=INV_SCIENCE_B_SKILL_ID))
                db.add(SDEType(type_id=INV_ENCRYPTION_SKILL_ID, type_name="Test Encryption Methods"))
                db.add(SDEType(type_id=INV_SCIENCE_A_SKILL_ID, type_name="Mechanical Engineering"))
                db.add(SDEType(type_id=INV_SCIENCE_B_SKILL_ID, type_name="High Energy Physics"))
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)
        _run(engine.dispose())
        os.unlink(tmp.name)

    return teardown


_INV_PRICE_MAP = {
    34: 5.0,                    # T2 blueprint's own manufacturing material
    INV_PRODUCT_ID: 2_000_000.0,  # sell price — swamps the small overhead
    20410: 1000.0,               # datacore A
    20424: 500.0,                # datacore B
}


def test_build_finder_results_inventable_row_shows_overhead_suffix(monkeypatch):
    """End-to-end: manual skill levels (no character), no decryptor. The
    product must show a "(+N inv)" suffix and no ⚠ (manual mode never flags
    skill_missing), and the probability-formula footnote must be present."""
    teardown = _seeded_invention_db(with_invention_chain=True)
    monkeypatch.setattr(
        market_lp, "get_price_map",
        lambda db: asyncio.sleep(0, result=dict(_INV_PRICE_MAP)),
    )
    try:
        r = _authed_client().get(
            "/industry/build-finder/results",
            params={
                "group_id": INV_GROUP_ID, "me": 0, "structure": "npc_station",
                "rig": "none", "security": "highsec",
                "character_id": 0, "encryption": 4, "science": 4,
                "decryptor": "none",
            },
        )
        assert r.status_code == 200
        body = r.text
        assert "Test T2 Widget" in body
        assert "inv)" in body
        assert "character missing an invention skill" not in body
        assert "P = base_prob" in body
    finally:
        teardown()


def test_build_finder_results_tables_empty_shows_not_imported_note(monkeypatch):
    """No invention chain seeded anywhere in the DB -> the group's product is
    not inventable, and the footnote must say invention data isn't imported
    yet rather than silently saying nothing (which would look identical to
    "this item just isn't inventable")."""
    teardown = _seeded_invention_db(with_invention_chain=False)
    monkeypatch.setattr(
        market_lp, "get_price_map",
        lambda db: asyncio.sleep(0, result=dict(_INV_PRICE_MAP)),
    )
    try:
        r = _authed_client().get(
            "/industry/build-finder/results",
            params={"group_id": INV_GROUP_ID, "me": 0},
        )
        assert r.status_code == 200
        body = r.text
        assert "not yet imported" in body
        assert "inv)" not in body
    finally:
        teardown()
