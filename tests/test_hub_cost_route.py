"""Hub comparison helpers: benchmark cost, routes, fetch stamps."""
import asyncio
import tempfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.market.history as history
import app.market.orders as orders
from app.db.models import Base
from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial
from app.market import hub_cost, hub_route

PRODUCT, BLUEPRINT = 2001, 2002
REGION = 10000043


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def session_factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=BLUEPRINT, product_type_id=PRODUCT,
                                    product_quantity=2))
            db.add(SDEBlueprintMaterial(blueprint_type_id=BLUEPRINT, activity_id=1,
                                        material_type_id=34, quantity=100))
            db.add(SDEBlueprintMaterial(blueprint_type_id=BLUEPRINT, activity_id=1,
                                        material_type_id=35, quantity=10))
            await db.commit()

    _run(setup())
    yield factory
    _run(engine.dispose())


def _patch_cost_sources(monkeypatch, prices, invented=False):
    async def fake_prices(db):
        return prices

    async def fake_invention(db, ids):
        return {ids[0]: {}} if invented else {}

    monkeypatch.setattr(hub_cost.market_lp, "get_price_map", fake_prices)
    monkeypatch.setattr(hub_cost.sde, "get_invention_data", fake_invention)


def _benchmark(session_factory, type_id=PRODUCT):
    async def go():
        async with session_factory() as db:
            return await hub_cost.benchmark(db, type_id)
    return _run(go())


def test_benchmark_cost_at_me_10(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    # ME 10: 100 -> 90, 10 -> 9. (90*5 + 9*20) / 2 units = 315
    bench = _benchmark(session_factory)
    assert bench.cost == pytest.approx(315.0)
    assert bench.me == 10 and bench.missing is None


def test_benchmark_cost_uses_me_2_for_invented_products(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0}, invented=True)
    # ME 2: 100 -> 98, 10 -> 10 (9.8 rounds up). (98*5 + 10*20) / 2 = 345
    bench = _benchmark(session_factory)
    assert bench.cost == pytest.approx(345.0)
    assert bench.me == 2


def test_benchmark_cost_none_when_a_material_is_unpriced(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0})
    bench = _benchmark(session_factory)
    assert bench.cost is None and bench.missing == hub_cost.UNPRICED


def test_benchmark_cost_none_when_the_price_list_is_empty(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {})        # get_price_map's answer when ESI fails
    bench = _benchmark(session_factory)
    assert bench.cost is None and bench.missing == hub_cost.UNPRICED


def test_benchmark_cost_skips_a_lower_blueprint_with_no_materials(session_factory,
                                                                  monkeypatch):
    async def add_empty_blueprint():
        async with session_factory() as db:
            db.add(SDEBlueprintInfo(blueprint_type_id=BLUEPRINT - 10,
                                    product_type_id=PRODUCT, product_quantity=7))
            await db.commit()

    _run(add_empty_blueprint())
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    assert _benchmark(session_factory).cost == pytest.approx(315.0)


def test_benchmark_cost_none_without_a_blueprint(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    bench = _benchmark(session_factory, type_id=9999)
    assert bench.cost is None and bench.missing == hub_cost.NO_BLUEPRINT


# ── routes ──────────────────────────────────────────────────────────────
#  1 ── 2 ── 3 ── 4        all highsec except 3
#  └──── 5 ──── 6 ── 4     highsec detour: 1-5-6-4
GRAPH = {1: [2, 5], 2: [1, 3], 3: [2, 4], 4: [3, 6], 5: [1, 6], 6: [5, 4], 7: []}
SEC = {1: 0.9, 2: 0.8, 3: 0.3, 4: 0.7, 5: 0.6, 6: 0.5, 7: 1.0}


def test_bfs_distances():
    assert hub_route.bfs_distances(GRAPH, 1) == {1: 0, 2: 1, 5: 1, 3: 2, 6: 2, 4: 3}


def test_route_prefers_a_highsec_only_path_of_equal_length():
    routes = hub_route.routes_from(GRAPH, SEC, 1, {"a": 4, "b": 3, "c": 7})
    assert routes["a"] == {"jumps": 3, "highsec": True}      # 1-5-6-4
    assert routes["b"] == {"jumps": 2, "highsec": False}     # destination is lowsec
    assert routes["c"] is None                               # unreachable


def test_route_from_a_lowsec_origin_always_leaves_highsec():
    assert hub_route.routes_from(GRAPH, SEC, 3, {"a": 4})["a"] == {
        "jumps": 1, "highsec": False}


def test_security_is_highsec_from_0_45():
    assert hub_route.is_highsec(0.45) and not hub_route.is_highsec(0.449)
    assert not hub_route.is_highsec(None)


# ── fetch stamps ────────────────────────────────────────────────────────

@pytest.fixture
def clean_order_cache():
    """Empty the module-level order cache and remembered failures before AND
    after, so a failing assertion cannot leak cached books (or a failure
    stamp that suppresses a fetch) into later tests."""
    orders._cache.clear()
    orders._locks.clear()
    orders._failed.clear()
    yield
    orders._cache.clear()
    orders._locks.clear()
    orders._failed.clear()


def test_orders_fetched_at_is_none_until_a_fetch_succeeds(monkeypatch, clean_order_cache):
    async def boom(region_id, type_id):
        raise RuntimeError("down")

    monkeypatch.setattr(orders, "_fetch_orders_esi", boom)
    assert _run(orders.get_orders(REGION, PRODUCT)) == []
    assert orders.fetched_at(REGION, PRODUCT) is None

    async def ok(region_id, type_id):
        return []

    monkeypatch.setattr(orders, "_fetch_orders_esi", ok)
    # The failure is remembered for FAILURE_TTL; clearing it models that time
    # passing (or the retry button).
    orders.clear_failure(REGION, PRODUCT)
    _run(orders.get_orders(REGION, PRODUCT))
    assert orders.fetched_at(REGION, PRODUCT) is not None


def test_history_fetched_at_reads_the_meta_stamp(session_factory, monkeypatch):
    async def ok(region_id, type_id):
        return []

    async def go():
        async with session_factory() as db:
            before = await history.fetched_at(db, REGION, PRODUCT)
            monkeypatch.setattr(history, "_fetch_history_esi", ok)
            await history.get_history(REGION, PRODUCT, db)
            after = await history.fetched_at(db, REGION, PRODUCT)
            return before, after

    before, after = _run(go())
    assert before is None and after is not None
