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

The EVE-style tree picker (2026-07-10 plan, Task 2) keys results by
`market_group_id` (subtree semantics via `get_market_group_subtree_products`)
and adds two auth-gated htmx fragment endpoints: `/industry/build-finder/tree`
(lazy per-level expansion) and `/industry/build-finder/tree/search` (path-
labeled group search; a sub-2-char query restores the root tree).
"""
import asyncio
import base64
import json
import os
import re
import tempfile
from html.parser import HTMLParser

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User, get_db
from tests.conftest import ensure_user
from app.db.sde_models import (
    SDEType, SDEGroup, SDEMarketGroup, SDEBlueprintInfo, SDEBlueprintMaterial,
    SDEBlueprintInvention, SDEBlueprintInventionMaterial, SDEBlueprintInventionProduct,
    SDEBlueprintInventionSkill,
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
    r = _client().get("/industry/build-finder/results?market_group_id=42",
                      follow_redirects=False)
    assert r.status_code == 401


def test_build_finder_tree_401_when_unauthenticated():
    r = _client().get("/industry/build-finder/tree?parent=0",
                      follow_redirects=False)
    assert r.status_code == 401


def test_build_finder_tree_search_401_when_unauthenticated():
    r = _client().get("/industry/build-finder/tree/search?q=frig",
                      follow_redirects=False)
    assert r.status_code == 401


# ── route-level invention composition ──────────────────────────────────────────

INV_GROUP_ID = 5501
INV_MARKET_GROUP_ID = 5601
INV_PRODUCT_ID = 115501
INV_T2_BLUEPRINT_ID = 115502
INV_T1_BLUEPRINT_ID = 115503
INV_ENCRYPTION_SKILL_ID = 115504
INV_SCIENCE_A_SKILL_ID = 115505
INV_SCIENCE_B_SKILL_ID = 115506
INV_BP_GROUP_ID = 5502   # blueprint group (category 9) for invention sources


def _authed_client():
    """TestClient carrying a signed session cookie — idiom from
    tests/test_pnl_route.py (itself from tests/test_networth.py)."""
    import app.main as main

    ensure_user(1)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": 1}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _seeded_invention_db(with_invention_chain: bool, extra_seed=None):
    """Temp sqlite DB with one buildable T2-shaped product in a group.
    `with_invention_chain=True` also seeds the full invention chain
    (mirrors tests/test_invention_lookup.py's fixture); `False` leaves
    `sde_blueprint_invention` empty entirely, to exercise the
    "tables not yet imported" footnote path. `extra_seed`, if given, is an
    async callable that receives the open session before it commits."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEGroup(group_id=INV_GROUP_ID, category_id=6, group_name="Test T2 Group"))
            db.add(SDEMarketGroup(
                market_group_id=INV_MARKET_GROUP_ID, parent_group_id=None,
                market_group_name="Test T2 Market Group"))
            db.add(SDEType(type_id=INV_PRODUCT_ID, type_name="Test T2 Widget",
                           group_id=INV_GROUP_ID,
                           market_group_id=INV_MARKET_GROUP_ID, published=True))
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
                db.add(SDEBlueprintInventionProduct(
                    blueprint_type_id=INV_T1_BLUEPRINT_ID,
                    product_blueprint_type_id=INV_T2_BLUEPRINT_ID, base_runs=1,
                ))
                db.add(SDEGroup(group_id=INV_BP_GROUP_ID, category_id=9,
                                group_name="Test Blueprint Group"))
                db.add(SDEType(type_id=INV_T1_BLUEPRINT_ID, type_name="Test T1 Blueprint",
                               group_id=INV_BP_GROUP_ID))
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
            if extra_seed is not None:
                await extra_seed(db)
            db.add(User(id=1))  # the session _authed_client carries
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
                "market_group_id": INV_MARKET_GROUP_ID, "me": 0,
                "structure": "npc_station", "rig": "none", "security": "highsec",
                "character_id": 0, "encryption": 4, "science": 4,
                "decryptor": "none",
            },
        )
        assert r.status_code == 200
        body = r.text
        assert "Test T2 Widget" in body
        assert "inv)" in body
        assert 'title="Invented from Test T1 Blueprint"' in body
        assert '<span class="m-only" data-m-label="Invented from">Test T1 Blueprint</span>' in body
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
            params={"market_group_id": INV_MARKET_GROUP_ID, "me": 0},
        )
        assert r.status_code == 200
        body = r.text
        assert "not yet imported" in body
        assert "inv)" not in body
    finally:
        teardown()


def _get_results(monkeypatch, price_map, **params):
    monkeypatch.setattr(
        market_lp, "get_price_map",
        lambda db: asyncio.sleep(0, result=dict(price_map)),
    )
    r = _authed_client().get(
        "/industry/build-finder/results",
        params={"market_group_id": INV_MARKET_GROUP_ID, "me": 0, **params},
    )
    assert r.status_code == 200
    return r.text


def test_build_finder_results_empty_products_table_shows_not_imported_note(monkeypatch):
    """The note keys on the products table the lookup reads, so an activity
    table that is filled while the products table is empty still shows it."""
    async def only_activity(db):
        db.add(SDEBlueprintInvention(
            blueprint_type_id=INV_T1_BLUEPRINT_ID,
            product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
            probability=0.3, base_runs=1, time=63900))

    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=only_activity)
    try:
        body = _get_results(monkeypatch, _INV_PRICE_MAP)
        assert "not yet imported" in body
        assert "inv)" not in body
    finally:
        teardown()


# Rifter Blueprint invents both Wolf Blueprint (-> Wolf) and Jaguar Blueprint
# (-> Jaguar); the activity row keeps only the first.
RIFTER_BP, WOLF_BP, WOLF, JAGUAR_BP, JAGUAR = 691, 11372, 11371, 11401, 11400


def test_build_finder_results_second_invention_product_also_shows_overhead(monkeypatch):
    async def rifter(db):
        db.add(SDEGroup(group_id=INV_BP_GROUP_ID, category_id=9,
                        group_name="Test Blueprint Group"))
        db.add(SDEType(type_id=RIFTER_BP, type_name="Rifter Blueprint",
                       group_id=INV_BP_GROUP_ID))
        db.add(SDEBlueprintInvention(
            blueprint_type_id=RIFTER_BP, product_blueprint_type_id=WOLF_BP,
            probability=0.3, base_runs=1, time=63900))
        for bp in (WOLF_BP, JAGUAR_BP):
            db.add(SDEBlueprintInventionProduct(
                blueprint_type_id=RIFTER_BP, product_blueprint_type_id=bp, base_runs=1))
        db.add(SDEBlueprintInventionMaterial(
            blueprint_type_id=RIFTER_BP, material_type_id=20410, quantity=8))
        for sid in (INV_ENCRYPTION_SKILL_ID, INV_SCIENCE_A_SKILL_ID, INV_SCIENCE_B_SKILL_ID):
            db.add(SDEBlueprintInventionSkill(blueprint_type_id=RIFTER_BP, skill_type_id=sid))
        for pid, name, bp in ((WOLF, "Wolf", WOLF_BP), (JAGUAR, "Jaguar", JAGUAR_BP)):
            db.add(SDEType(type_id=pid, type_name=name,
                           group_id=INV_GROUP_ID,
                           market_group_id=INV_MARKET_GROUP_ID, published=True))
            db.add(SDEBlueprintInfo(blueprint_type_id=bp, product_type_id=pid,
                                    product_quantity=1, manufacturing_time=600))
            db.add(SDEBlueprintMaterial(blueprint_type_id=bp, activity_id=1,
                                        material_type_id=34, quantity=100))

    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=rifter)
    try:
        body = _get_results(monkeypatch, {
            34: 5.0, 20410: 1000.0, WOLF: 2_000_000.0, JAGUAR: 2_000_000.0,
            INV_PRODUCT_ID: 2_000_000.0,
        })
        assert body.count(" inv)") == 2
        assert body.count('title="Invented from Rifter Blueprint"') == 2
        assert body.count('data-m-label="Invented from">Rifter Blueprint<') == 2
    finally:
        teardown()


RELIC_IDS = (30187, 30752, 34412)  # Intact, Malfunctioning, Wrecked (type ids)
RELIC_NAMES = {30187: "Test Intact Relic", 30752: "Test Malfunctioning Relic",
               34412: "Test Wrecked Relic"}


def _relic_seed():
    async def seed(db):
        db.add(SDEGroup(group_id=99001, category_id=34, group_name="Test Relic Group"))
        for rid, prob, runs in zip(RELIC_IDS, (0.26, 0.21, 0.14), (20, 10, 3)):
            db.add(SDEType(type_id=rid, type_name=RELIC_NAMES[rid], group_id=99001))
            db.add(SDEBlueprintInvention(
                blueprint_type_id=rid, product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
                probability=prob, base_runs=runs, time=1))
            db.add(SDEBlueprintInventionProduct(
                blueprint_type_id=rid, product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
                base_runs=runs))
            db.add(SDEBlueprintInventionMaterial(
                blueprint_type_id=rid, material_type_id=20410, quantity=2))
            for sid in (INV_ENCRYPTION_SKILL_ID, INV_SCIENCE_A_SKILL_ID, INV_SCIENCE_B_SKILL_ID):
                db.add(SDEBlueprintInventionSkill(blueprint_type_id=rid, skill_type_id=sid))
        for sid, nm in ((INV_ENCRYPTION_SKILL_ID, "Test Encryption Methods"),
                        (INV_SCIENCE_A_SKILL_ID, "Mechanical Engineering"),
                        (INV_SCIENCE_B_SKILL_ID, "High Energy Physics")):
            db.add(SDEType(type_id=sid, type_name=nm))
    return seed


def test_build_finder_results_null_category_source_is_skipped(monkeypatch):
    """A source whose group and type both lack a category could be a relic,
    so it is never costed (not as a non-relic)."""
    async def seed(db):
        db.add(SDEType(type_id=30187, type_name="Test Unknown Source"))
        db.add(SDEBlueprintInvention(
            blueprint_type_id=30187, product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
            probability=0.26, base_runs=20, time=1))
        db.add(SDEBlueprintInventionProduct(
            blueprint_type_id=30187, product_blueprint_type_id=INV_T2_BLUEPRINT_ID,
            base_runs=20))
        db.add(SDEBlueprintInventionMaterial(
            blueprint_type_id=30187, material_type_id=20410, quantity=2))

    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=seed)
    try:
        body = _get_results(monkeypatch, _INV_PRICE_MAP)
        assert " inv)" not in body
        assert "Test Unknown Source" not in body
    finally:
        teardown()


def test_build_finder_results_relic_source_uses_the_only_priced_relic(monkeypatch):
    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=_relic_seed())
    try:
        body = _get_results(monkeypatch, {
            34: 5.0, 20410: 1000.0, INV_PRODUCT_ID: 2_000_000.0, 34412: 500_000.0,
        })
        assert " inv)" in body
        assert 'title="Invented from Test Wrecked Relic"' in body
        assert "Test Intact Relic" not in body
    finally:
        teardown()


def test_build_finder_results_relic_price_is_part_of_the_overhead(monkeypatch):
    """Intact is far more likely and yields more runs, but a pricey relic
    makes Wrecked the cheaper source per unit."""
    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=_relic_seed())
    try:
        body = _get_results(monkeypatch, {
            34: 5.0, 20410: 1000.0, INV_PRODUCT_ID: 2_000_000.0,
            30187: 900_000_000.0, 30752: 900_000_000.0, 34412: 1_000.0,
        })
        assert 'title="Invented from Test Wrecked Relic"' in body
        # By hand: attempt = 2 datacores x 1000 + relic 1000 = 3000;
        # P = 0.14 x (1 + 4/40 + 8/30) = 0.14 x 1.36667 = 0.19133;
        # 3 runs x 1 unit, so 3000 / 0.19133 / 3 = 5226.9 -> "5.23K".
        assert "(+5.23K inv)" in body
    finally:
        teardown()


def test_build_finder_results_all_relics_unpriced_leaves_the_row_unpriced(monkeypatch):
    teardown = _seeded_invention_db(with_invention_chain=False, extra_seed=_relic_seed())
    try:
        body = _get_results(monkeypatch, {
            34: 5.0, 20410: 1000.0, INV_PRODUCT_ID: 2_000_000.0,
        })
        assert " inv)" not in body
        cell = re.search(r'data-m-label="Build / unit"[^>]*><span>([^<]*)', body)
        assert cell and cell.group(1) == "—"
        assert "Invented from" not in body, "no source shown where no overhead is"
    finally:
        teardown()


# ── tree + search fragments (EVE-style picker, Task 2) ────────────────────────

TREE_ROOT_ID = 10        # Ships
TREE_FRIGATES_ID = 11    # child of Ships, has a child of its own
TREE_CRUISERS_ID = 12    # leaf child of Ships
TREE_AF_ID = 13          # Assault Frigates, child of Frigates

TREE_RIFTER_ID = 100     # buildable at Frigates
TREE_RIFTER_BP_ID = 1100
TREE_WOLF_ID = 200       # buildable at Assault Frigates
TREE_WOLF_BP_ID = 1200


def _seeded_tree_db():
    """Temp sqlite DB with a 3-level market-group tree + two buildable
    products at different depths (mirrors tests/test_market_group_subtree.py's
    fixture), wired in via the get_db override idiom."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SDEMarketGroup(market_group_id=TREE_ROOT_ID,
                                  parent_group_id=None, market_group_name="Ships"))
            db.add(SDEMarketGroup(market_group_id=TREE_FRIGATES_ID,
                                  parent_group_id=TREE_ROOT_ID,
                                  market_group_name="Frigates"))
            db.add(SDEMarketGroup(market_group_id=TREE_CRUISERS_ID,
                                  parent_group_id=TREE_ROOT_ID,
                                  market_group_name="Cruisers"))
            db.add(SDEMarketGroup(market_group_id=TREE_AF_ID,
                                  parent_group_id=TREE_FRIGATES_ID,
                                  market_group_name="Assault Frigates"))

            db.add(SDEType(type_id=TREE_RIFTER_ID, type_name="Rifter",
                           market_group_id=TREE_FRIGATES_ID, published=True))
            db.add(SDEType(type_id=TREE_WOLF_ID, type_name="Wolf",
                           market_group_id=TREE_AF_ID, published=True))

            db.add(SDEBlueprintInfo(blueprint_type_id=TREE_RIFTER_BP_ID,
                                    product_type_id=TREE_RIFTER_ID,
                                    product_quantity=1))
            db.add(SDEBlueprintInfo(blueprint_type_id=TREE_WOLF_BP_ID,
                                    product_type_id=TREE_WOLF_ID,
                                    product_quantity=1))
            db.add(SDEBlueprintMaterial(blueprint_type_id=TREE_RIFTER_BP_ID,
                                        activity_id=1, material_type_id=34,
                                        quantity=100))
            db.add(SDEBlueprintMaterial(blueprint_type_id=TREE_WOLF_BP_ID,
                                        activity_id=1, material_type_id=34,
                                        quantity=200))
            db.add(User(id=1))  # the session _authed_client carries
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


def test_build_finder_tree_root_fragment():
    """parent=0 -> root groups: Ships appears with an expand control that
    htmx-loads its children into a per-node child slot."""
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder/tree", params={"parent": 0})
        assert r.status_code == 200
        body = r.text
        assert "Ships" in body
        # expand arrow targets the next level + per-node child slot exists
        assert f"/industry/build-finder/tree?parent={TREE_ROOT_ID}" in body
        assert f'id="bft-kids-{TREE_ROOT_ID}"' in body
        # only roots at this level
        assert "Frigates" not in body
    finally:
        teardown()


def test_build_finder_tree_children_fragment():
    """parent=<id> -> that node's children, wrapped in the node's own child
    slot (outerHTML swap target). Branch children get an expand control;
    leaves don't."""
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder/tree",
                                 params={"parent": TREE_ROOT_ID})
        assert r.status_code == 200
        body = r.text
        # fragment IS the child slot for the expanded node
        assert f'id="bft-kids-{TREE_ROOT_ID}"' in body
        assert "Frigates" in body and "Cruisers" in body
        # Frigates has a child (Assault Frigates) -> expandable
        assert f"/industry/build-finder/tree?parent={TREE_FRIGATES_ID}" in body
        # Cruisers is a leaf -> no expand URL for it
        assert f"/industry/build-finder/tree?parent={TREE_CRUISERS_ID}" not in body
        # both are selectable
        assert f'data-mg-id="{TREE_FRIGATES_ID}"' in body
        assert f'data-mg-id="{TREE_CRUISERS_ID}"' in body
    finally:
        teardown()


def test_build_finder_tree_search_returns_path_labels():
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder/tree/search",
                                 params={"q": "frig"})
        assert r.status_code == 200
        body = r.text
        # path-labeled, selectable results (HTML-escaped " > " separator)
        assert "Ships &gt; Frigates" in body
        assert "Ships &gt; Frigates &gt; Assault Frigates" in body
        assert f'data-mg-id="{TREE_FRIGATES_ID}"' in body
        assert f'data-mg-id="{TREE_AF_ID}"' in body
        assert "Cruisers" not in body
    finally:
        teardown()


def test_build_finder_tree_search_short_query_restores_root():
    """Under 2 chars (incl. cleared box) the search endpoint returns the root
    tree so the htmx swap restores the browser instead of erroring."""
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder/tree/search",
                                 params={"q": "f"})
        assert r.status_code == 200
        body = r.text
        assert "Ships" in body
        assert "&gt;" not in body  # no path labels — this is the root tree
    finally:
        teardown()


def test_build_finder_results_ranks_whole_subtree(monkeypatch):
    """Selecting a branch node (Ships) ranks buildables from ALL descendant
    groups — Rifter (depth 2) and Wolf (depth 3)."""
    teardown = _seeded_tree_db()
    monkeypatch.setattr(
        market_lp, "get_price_map",
        lambda db: asyncio.sleep(0, result={
            34: 5.0, TREE_RIFTER_ID: 1000.0, TREE_WOLF_ID: 5000.0,
        }),
    )
    try:
        r = _authed_client().get(
            "/industry/build-finder/results",
            params={"market_group_id": TREE_ROOT_ID, "me": 0},
        )
        assert r.status_code == 200
        assert "Rifter" in r.text
        assert "Wolf" in r.text
    finally:
        teardown()


def test_build_finder_results_no_group_prompts_pick(monkeypatch):
    teardown = _seeded_tree_db()
    monkeypatch.setattr(
        market_lp, "get_price_map",
        lambda db: asyncio.sleep(0, result={}),
    )
    try:
        r = _authed_client().get("/industry/build-finder/results",
                                 params={"me": 0})
        assert r.status_code == 200
        assert "Pick a group" in r.text
    finally:
        teardown()


def test_build_finder_page_has_tree_picker():
    """The page carries the tree container (htmx root load), the hidden
    market_group_id input inside the results form, the selected-path label,
    and the search box — and no longer renders the flat group_id select."""
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder")
        assert r.status_code == 200
        body = r.text
        assert 'id="bf-tree"' in body
        assert 'name="market_group_id"' in body
        assert 'id="bf-selected-path"' in body
        assert 'id="bf-tree-search"' in body
        assert 'name="group_id"' not in body
    finally:
        teardown()


# ── ISS-121: the root tree loads into its own box ──────────────────────────

class _Elements(HTMLParser):
    """Every element's attrs with the attrs of its open ancestors (outermost
    first), so a test can resolve htmx's attribute inheritance."""
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.found.append((a, [x for _, x in self.stack]))
        if tag not in self._VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _hx_target_id(attrs, ancestors):
    """The id of the element htmx 1.9 swaps a request from `attrs` into
    (getTarget): the closest element, itself first, that carries hx-target,
    unless an ancestor's hx-disinherit covers it first; "this" names the
    element carrying the attribute; no hx-target at all means the element
    itself. Only `this` and `#id` values are resolved."""
    for el in [attrs] + ancestors[::-1]:
        dis = el.get("hx-disinherit") or el.get("data-hx-disinherit") or ""
        if el is not attrs and (dis == "*" or "hx-target" in dis.split()):
            return attrs.get("id")
        value = el.get("hx-target") or el.get("data-hx-target")
        if value == "this":
            return el.get("id")
        if value:
            assert value.startswith("#"), value
            return value[1:]
    return attrs.get("id")


def test_root_tree_loads_into_its_own_box():
    """ISS-121: #bf-tree sits inside #bf-form, and htmx inherits the form's
    hx-target="#bf-results". With no target of its own, the root tree's
    load swapped into the results box: #bf-tree stayed empty, and the
    picker's select and expand/collapse handlers, delegated on #bf-tree,
    never saw a node. The tree's own requests must land in #bf-tree, while
    the form (submitted by Rank builds and by picking a group) still
    renders into #bf-results."""
    teardown = _seeded_tree_db()
    try:
        r = _authed_client().get("/industry/build-finder")
        assert r.status_code == 200
        parser = _Elements()
        parser.feed(r.text)
    finally:
        teardown()
    by_id = {a["id"]: (a, anc) for a, anc in parser.found if a.get("id")}

    tree, tree_anc = by_id["bf-tree"]
    assert tree["hx-get"] == "/industry/build-finder/tree?parent=0"
    assert tree["hx-trigger"] == "load"
    assert any(a.get("id") == "bf-form" for a in tree_anc), \
        "the tree is inside the results form, so it would inherit the form's target"
    assert _hx_target_id(tree, tree_anc) == "bf-tree"

    search, search_anc = by_id["bf-tree-search"]
    assert _hx_target_id(search, search_anc) == "bf-tree"

    form, form_anc = by_id["bf-form"]
    assert form["hx-get"] == "/industry/build-finder/results"
    assert _hx_target_id(form, form_anc) == "bf-results"
