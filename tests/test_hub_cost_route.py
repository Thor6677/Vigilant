"""Hub comparison helpers: benchmark cost, routes, fetch stamps."""
import asyncio
import tempfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.market.history as history
import app.market.orders as orders
from app.db.models import Base
from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial, SDESystem, SDEType
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


def _add_blueprint_type(session_factory, market_group_id):
    async def go():
        async with session_factory() as db:
            db.add(SDEType(type_id=BLUEPRINT, type_name="Sample Blueprint", published=True,
                           market_group_id=market_group_id))
            await db.commit()
    _run(go())


def test_a_blueprint_sold_on_the_market_is_not_copy_only(session_factory, monkeypatch):
    _add_blueprint_type(session_factory, market_group_id=264)
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    assert _benchmark(session_factory).copy_only is False


def test_a_blueprint_off_the_market_is_copy_only(session_factory, monkeypatch):
    _add_blueprint_type(session_factory, market_group_id=None)
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    bench = _benchmark(session_factory)
    assert bench.copy_only is True and bench.cost is not None


def test_an_invented_product_is_not_copy_only(session_factory, monkeypatch):
    # Its blueprint is off the market too, but the note already names invention.
    _add_blueprint_type(session_factory, market_group_id=None)
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0}, invented=True)
    assert _benchmark(session_factory).copy_only is False


def test_no_blueprint_is_not_copy_only(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    assert _benchmark(session_factory, type_id=9999).copy_only is False


# ── routes ──────────────────────────────────────────────────────────────
SEC = {1: 0.9, 2: 0.8, 3: 0.3, 4: 0.7, 5: 0.6, 6: 0.47, 7: 1.0}


@pytest.fixture
def planner(session_factory, monkeypatch):
    """Stands in for EVE's route planner: `answers[(origin, destination)]` is
    a list of systems, None (no route) or an exception to raise. Calls are
    recorded in `planner.calls`."""
    async def add_systems():
        async with session_factory() as db:
            for sid, sec in SEC.items():
                db.add(SDESystem(system_id=sid, system_name=f"S{sid}", security=sec))
            await db.commit()
    _run(add_systems())

    class Planner:
        answers: dict = {}
        calls: list = []

    async def fake_fetch(origin, destination):
        Planner.calls.append((origin, destination))
        answer = Planner.answers[(origin, destination)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    Planner.answers, Planner.calls = {}, []
    hub_route._routes.clear()
    monkeypatch.setattr(hub_route, "_fetch_route", fake_fetch)
    yield Planner
    hub_route._routes.clear()


def _routes(session_factory, origin, hub_systems):
    async def go():
        async with session_factory() as db:
            return await hub_route.hub_routes(db, origin, hub_systems)
    return _run(go())


def test_routes_come_from_the_planner_with_jumps_and_a_highsec_flag(session_factory,
                                                                    planner):
    planner.answers = {(1, 4): [1, 5, 6, 4],      # 6 is 0.47: shows as 0.5, highsec
                       (1, 3): [1, 2, 3],         # ends in lowsec
                       (1, 7): None}              # the planner has no route
    routes = _routes(session_factory, 1, {"a": 4, "b": 3, "c": 7})
    assert routes["a"] == {"jumps": 3, "highsec": True}
    assert routes["b"] == {"jumps": 2, "highsec": False}
    assert routes["c"] is None


def test_route_from_a_lowsec_origin_leaves_highsec(session_factory, planner):
    planner.answers = {(3, 4): [3, 4]}
    assert _routes(session_factory, 3, {"a": 4})["a"] == {"jumps": 1, "highsec": False}


def test_a_hub_in_the_starting_system_is_zero_jumps_without_asking(session_factory,
                                                                   planner):
    assert _routes(session_factory, 1, {"a": 1})["a"] == {"jumps": 0, "highsec": True}
    assert planner.calls == []


def test_a_planner_failure_is_unavailable_for_that_hub_only_and_not_remembered(
        session_factory, planner):
    planner.answers = {(1, 4): RuntimeError("down"), (1, 2): [1, 2]}
    routes = _routes(session_factory, 1, {"a": 4, "b": 2})
    assert routes["a"] == hub_route.UNAVAILABLE
    assert routes["b"] == {"jumps": 1, "highsec": True}
    planner.answers[(1, 4)] = [1, 5, 6, 4]
    planner.calls.clear()
    assert _routes(session_factory, 1, {"a": 4, "b": 2})["a"]["jumps"] == 3
    assert planner.calls == [(1, 4)]             # b was remembered, a retried


def test_routes_and_no_route_answers_are_remembered_for_a_day(session_factory, planner,
                                                              monkeypatch):
    from datetime import datetime, timedelta, timezone
    clock = [datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(hub_route, "_now", lambda: clock[0])
    planner.answers = {(1, 4): [1, 5, 6, 4], (1, 7): None}
    _routes(session_factory, 1, {"a": 4, "c": 7})
    _routes(session_factory, 1, {"a": 4, "c": 7})
    assert sorted(planner.calls) == [(1, 4), (1, 7)]
    clock[0] += timedelta(hours=24, minutes=1)
    _routes(session_factory, 1, {"a": 4, "c": 7})
    assert len(planner.calls) == 4


def test_fetch_route_asks_for_the_secure_route_and_reads_404_as_no_route(monkeypatch):
    import httpx
    seen = []

    class FakeClient:
        def __init__(self, token, db=None):
            pass

        async def get_public(self, path, params=None):
            seen.append((path, params))
            if path == "/route/1/7/":
                request = httpx.Request("GET", "https://example.invalid" + path)
                raise httpx.HTTPStatusError("404", request=request,
                                            response=httpx.Response(404, request=request))
            if path == "/route/1/3/":
                request = httpx.Request("GET", "https://example.invalid" + path)
                raise httpx.HTTPStatusError("503", request=request,
                                            response=httpx.Response(503, request=request))
            return [1, 5, 6, 4]

    monkeypatch.setattr(hub_route, "ESIClient", FakeClient)
    assert _run(hub_route._fetch_route(1, 4)) == [1, 5, 6, 4]
    assert seen == [("/route/1/4/", {"flag": "secure"})]
    assert _run(hub_route._fetch_route(1, 7)) is None
    with pytest.raises(httpx.HTTPStatusError):
        _run(hub_route._fetch_route(1, 3))


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


def test_benchmark_with_a_given_price_map_skips_the_fetch(session_factory, monkeypatch):
    _patch_cost_sources(monkeypatch, {34: 5.0, 35: 20.0})
    fetched = _benchmark(session_factory)

    async def must_not_fetch(db):
        raise AssertionError("the price list was fetched")
    monkeypatch.setattr(hub_cost.market_lp, "get_price_map", must_not_fetch)

    async def go():
        async with session_factory() as db:
            return await hub_cost.benchmark(db, PRODUCT, price_map={34: 5.0, 35: 20.0})
    given = _run(go())
    assert given.cost == pytest.approx(fetched.cost) == pytest.approx(315.0)
