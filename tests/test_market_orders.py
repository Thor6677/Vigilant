"""Tests for the hub order-book service + route + partial (Phase 4 Task 2).

Four disciplines, mirroring tests/test_market_history.py and
tests/test_entity_links.py:

1. **Spread math** — pure `build_order_book()` unit tests against the
   documented formula (spread_pct = spread / best_sell * 100).
2. **Cache TTL** — module-dict cache behavior with a monkeypatched ESI fetch
   and `_now` clock seam (same idiom as test_market_history.py). The
   module-level `_cache`/`_locks` dicts are cleared before/after every test so
   state never leaks between tests.
3. **Route auth gating** — TestClient smoke test proving the partial 401s
   when unauthenticated (no session), same pattern as the existing
   `/market/*` route tests.
4. **Template render correctness** — the partial is rendered standalone
   through a Jinja Environment (FileSystemLoader on app/templates, matching
   test_entity_links.py) with a fixture context shaped exactly like what the
   route builds from `build_order_book()` + `location_name()`. This is the
   only test that would catch a Jinja typo or a shape mismatch — the auth-gate
   test returns before ever reaching TemplateResponse.
"""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

import app.market.orders as orders

TYPE_ID = 34  # Tritanium
REGION = orders.DEFAULT_REGION_ID

_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "app", "templates")


@pytest.fixture(autouse=True)
def _clear_order_cache():
    """The module-level order-book cache persists across tests (it's a plain
    dict, not request-scoped) — clear it before and after every test so a
    populated key from one test can't make another test's "was this
    refetched?" assertion pass for the wrong reason."""
    orders._cache.clear()
    orders._locks.clear()
    yield
    orders._cache.clear()
    orders._locks.clear()


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _sample_orders():
    return [
        {"order_id": 1, "is_buy_order": False, "price": 105.0, "volume_remain": 500, "location_id": 60003760},
        {"order_id": 2, "is_buy_order": False, "price": 100.0, "volume_remain": 1000, "location_id": 60003760},
        {"order_id": 3, "is_buy_order": False, "price": 110.0, "volume_remain": 200, "location_id": 1029382919283},
        {"order_id": 4, "is_buy_order": True, "price": 90.0, "volume_remain": 300, "location_id": 60003760},
        {"order_id": 5, "is_buy_order": True, "price": 85.0, "volume_remain": 700, "location_id": 60003760},
        {"order_id": 6, "is_buy_order": True, "price": 80.0, "volume_remain": 50, "location_id": 1029382919283},
    ]


# ── spread math ─────────────────────────────────────────────────────────────

def test_spread_math_best_sell_100_best_buy_90():
    # Documented formula: spread = sell - buy; spread_pct = spread/sell*100.
    orders_ = [
        {"is_buy_order": False, "price": 100.0, "volume_remain": 10, "location_id": 1},
        {"is_buy_order": True, "price": 90.0, "volume_remain": 10, "location_id": 1},
    ]
    book = orders.build_order_book(orders_)
    assert book["best_sell"] == 100.0
    assert book["best_buy"] == 90.0
    assert book["spread"] == 10.0
    assert book["spread_pct"] == pytest.approx(10.0)  # NOT 11.1% ("of buy")


def test_build_order_book_sorts_and_caps_depth():
    book = orders.build_order_book(_sample_orders())
    # Sell orders ascending by price, cheapest first.
    assert [o["price"] for o in book["sell_orders"]] == [100.0, 105.0, 110.0]
    # Buy orders descending by price, highest first.
    assert [o["price"] for o in book["buy_orders"]] == [90.0, 85.0, 80.0]
    assert book["best_sell"] == 100.0
    assert book["best_buy"] == 90.0
    assert book["spread"] == 10.0
    assert book["spread_pct"] == pytest.approx(10.0)


def test_build_order_book_depth_cap_enforced(monkeypatch):
    monkeypatch.setattr(orders, "SELL_DEPTH", 2)
    monkeypatch.setattr(orders, "BUY_DEPTH", 2)
    book = orders.build_order_book(_sample_orders())
    assert len(book["sell_orders"]) == 2
    assert len(book["buy_orders"]) == 2


def test_build_order_book_empty_orders_no_crash():
    book = orders.build_order_book([])
    assert book["sell_orders"] == []
    assert book["buy_orders"] == []
    assert book["best_sell"] is None
    assert book["best_buy"] is None
    assert book["spread"] is None
    assert book["spread_pct"] is None


def test_location_name_npc_station_vs_player_structure():
    names = {60003760: "Jita IV - Moon 4 - Caldari Navy Assembly Plant"}
    assert orders.location_name(60003760, names) == "Jita IV - Moon 4 - Caldari Navy Assembly Plant"
    assert orders.location_name(60003761, names) == "Station 60003761"  # unresolved NPC id
    assert "Player structure" in orders.location_name(1029382919283, names)
    assert "1029382919283" in orders.location_name(1029382919283, names)


# ── cache TTL ────────────────────────────────────────────────────────────────

def test_orders_fetch_once_then_served_from_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        assert region_id == REGION and type_id == TYPE_ID
        return _sample_orders()

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_fetch)

    async def scenario():
        rows1 = await orders.get_orders(REGION, TYPE_ID)
        assert calls["n"] == 1
        assert len(rows1) == 6

        rows2 = await orders.get_orders(REGION, TYPE_ID)
        assert calls["n"] == 1  # still within TTL — no refetch
        assert rows2 == rows1

    _run(scenario())


def test_orders_stale_cache_triggers_refetch(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        return _sample_orders()

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_fetch)

    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    monkeypatch.setattr(orders, "_now", lambda: clock["t"])

    async def scenario():
        await orders.get_orders(REGION, TYPE_ID)
        assert calls["n"] == 1

        clock["t"] = t0 + orders.ORDER_BOOK_TTL - timedelta(minutes=1)
        await orders.get_orders(REGION, TYPE_ID)
        assert calls["n"] == 1  # within TTL

        clock["t"] = t0 + orders.ORDER_BOOK_TTL + timedelta(minutes=1)
        await orders.get_orders(REGION, TYPE_ID)
        assert calls["n"] == 2  # past TTL — refetched

    _run(scenario())


def test_orders_fetch_error_returns_stale_cache(monkeypatch):
    state = {"fail": False}

    async def fake_fetch(region_id, type_id):
        if state["fail"]:
            raise RuntimeError("ESI down")
        return _sample_orders()

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_fetch)
    monkeypatch.setattr(orders, "ORDER_BOOK_TTL", __import__("datetime").timedelta(seconds=0))

    async def scenario():
        rows = await orders.get_orders(REGION, TYPE_ID)
        assert len(rows) == 6

        state["fail"] = True
        rows2 = await orders.get_orders(REGION, TYPE_ID)
        assert len(rows2) == 6  # stale-on-error, not an empty list

    _run(scenario())


def test_orders_fetch_error_with_no_cache_returns_empty(monkeypatch):
    async def fake_fetch(region_id, type_id):
        raise RuntimeError("ESI down")

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_fetch)

    async def scenario():
        rows = await orders.get_orders(REGION, TYPE_ID)
        assert rows == []

    _run(scenario())


def test_orders_cache_keyed_per_type_independently(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(region_id, type_id):
        calls["n"] += 1
        return _sample_orders()

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_fetch)

    async def scenario():
        await orders.get_orders(REGION, 34)
        await orders.get_orders(REGION, 35)
        assert calls["n"] == 2  # different type_id → independent cache entries
        await orders.get_orders(REGION, 34)
        await orders.get_orders(REGION, 35)
        assert calls["n"] == 2  # both still fresh

    _run(scenario())


# ── route: auth gating ──────────────────────────────────────────────────────

def _client():
    import app.main as main
    return TestClient(main.app)


def test_order_book_route_401_when_unauthenticated():
    client = _client()
    r = client.get(f"/market/type/{TYPE_ID}/orders")
    assert r.status_code == 401


# ── partial template render correctness ─────────────────────────────────────

def _render_partial(**ctx):
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    tmpl = env.get_template("partials/market_order_book.html")
    return tmpl.render(**ctx)


def test_order_book_partial_renders_populated_book():
    book = orders.build_order_book(_sample_orders())
    names = {60003760: "Jita IV - Moon 4 - Caldari Navy Assembly Plant"}

    def _display_row(row):
        return {
            "price_str": f"{row['price']:.2f}",
            "volume_str": f"{row['volume_remain']:,}",
            "location_name": orders.location_name(row["location_id"], names),
        }

    html = _render_partial(
        type_id=TYPE_ID,
        sell_orders=[_display_row(r) for r in book["sell_orders"]],
        buy_orders=[_display_row(r) for r in book["buy_orders"]],
        best_sell_str=f"{book['best_sell']:.2f}",
        best_buy_str=f"{book['best_buy']:.2f}",
        spread_str=f"{book['spread']:.2f}",
        spread_pct_str=f"{book['spread_pct']:.1f}%",
    )

    assert "Jita IV - Moon 4 - Caldari Navy Assembly Plant" in html
    assert "Player structure" in html
    assert "100.00" in html  # best sell
    assert "90.00" in html  # best buy
    assert "10.0%" in html  # spread pct
    assert "Sell orders (lowest 3)" in html
    assert "Buy orders (highest 3)" in html


def test_order_book_partial_renders_empty_book_without_error():
    html = _render_partial(
        type_id=TYPE_ID,
        sell_orders=[],
        buy_orders=[],
        best_sell_str="—",
        best_buy_str="—",
        spread_str="—",
        spread_pct_str="—",
    )
    assert "No sell orders in The Forge." in html
    assert "No buy orders in The Forge." in html
