"""Hub comparison endpoint and partial. Names are invented."""
import asyncio
import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.market.history as history
import app.market.orders as orders
from app.db.models import Base, MarketHistoryMeta, User, get_db
from app.db.sde_models import SDEStation, SDESystem, SDEType
from app.esi.market import APPRAISAL_HUBS
from app.market import hub_prefs
from app.routes import market_hubs
from tests._mobile import render_page, source
from tests.conftest import ensure_user

USER_ID = 92
PREFS_USER_ID = 93      # has saved fees: 8% sales tax, 2% broker fee
TYPE_ID = 2001
NOT_MARKET = 2999
TODAY = date(2026, 11, 10)
JITA = APPRAISAL_HUBS["jita"]
AMARR = APPRAISAL_HUBS["amarr"]
HEK = APPRAISAL_HUBS["hek"]
DODIXIE = APPRAISAL_HUBS["dodixie"]
ORIGIN_ID = 30099001    # invented system id
CSRF = "test-csrf-token-hub-comparison-0001"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _authed_client(user_id=USER_ID):
    """Signed session cookie for `user_id`, with a CSRF token in the session
    and the matching header on every request (htmx sends it on POSTs)."""
    import app.main as main
    ensure_user(user_id)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", signer.sign(data).decode())
    client.headers.update({"X-CSRF-Token": CSRF})
    return client


@pytest.fixture
def factory(tmp_path):
    """Temp DB with one market type, one non-market type, a starting system,
    the five hub stations and a user with saved fees. The file lives in
    pytest's tmp_path, so it is cleaned up with it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hubs.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add(User(id=USER_ID))
            db.add(User(id=PREFS_USER_ID))
            db.add(SDEType(type_id=TYPE_ID, type_name="Sample Hull", published=True,
                           market_group_id=5, group_id=1))
            db.add(SDEType(type_id=NOT_MARKET, type_name="Sample Relic", published=True,
                           market_group_id=None, group_id=1))
            db.add(SDESystem(system_id=ORIGIN_ID, system_name="Sample System",
                             security=0.9))
            for n, hub in enumerate(APPRAISAL_HUBS.values()):
                db.add(SDEStation(station_id=hub["station_id"],
                                  station_name=f"Sample Hub Station {n}",
                                  system_id=30099100 + n))
            await db.commit()
            await hub_prefs.save_prefs(db, PREFS_USER_ID, 8, 2, None)

    _run(setup())
    yield session_factory
    _run(engine.dispose())


@pytest.fixture
def app_db(factory, monkeypatch):
    """`factory`'s DB behind get_db; ESI fetchers stubbed. Yields `calls`,
    which records every upstream fetch. Hek's orders always fail."""
    import app.main as main

    async def override_get_db():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    _clear_market_state()
    calls = []

    async def fake_orders(region_id, type_id):
        calls.append(("orders", region_id))
        if region_id == HEK["region_id"]:
            raise RuntimeError("down")
        station = next(h["station_id"] for h in APPRAISAL_HUBS.values()
                       if h["region_id"] == region_id)
        return [
            {"is_buy_order": False, "price": 331e6, "volume_remain": 14,
             "location_id": station, "min_volume": 1},
            {"is_buy_order": True, "price": 284e6, "volume_remain": 6,
             "location_id": station, "min_volume": 1},
        ]

    async def fake_history(region_id, type_id):
        calls.append(("history", region_id))
        return [{"date": (TODAY - timedelta(days=i + 1)).isoformat(), "average": 330e6,
                 "highest": 340e6, "lowest": 320e6, "volume": 5, "order_count": 3}
                for i in range(30)]

    async def fake_benchmark(db, type_id):
        return 268e6, 10

    monkeypatch.setattr(orders, "_fetch_orders_esi", fake_orders)
    monkeypatch.setattr(history, "_fetch_history_esi", fake_history)
    monkeypatch.setattr(market_hubs.hub_cost, "benchmark", fake_benchmark)
    monkeypatch.setattr(market_hubs, "_today", lambda: TODAY)
    yield calls
    main.app.dependency_overrides.pop(get_db, None)
    _clear_market_state()


def _clear_market_state():
    """Module-level order cache and remembered fetch failures (orders and
    history) — cleared before and after each test so none leak."""
    orders._cache.clear()
    orders._locks.clear()
    orders._failed.clear()
    history._failed.clear()


def test_hubs_401_when_unauthenticated():
    import app.main as main
    assert TestClient(main.app).get(f"/market/type/{TYPE_ID}/hubs").status_code == 401


def test_non_market_type_renders_nothing_and_fetches_nothing(app_db):
    resp = _authed_client().get(f"/market/type/{NOT_MARKET}/hubs")
    assert resp.status_code == 200
    assert resp.text.strip() == ""
    assert app_db == []


def test_five_hubs_render_and_one_failed_hub_is_unavailable(app_db):
    resp = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    assert resp.status_code == 200
    body = resp.text
    for name in ("Jita", "Amarr", "Dodixie", "Hek", "Rens"):
        assert name in body
    assert body.count("Unavailable") == 2          # Hek: once in the table, once in the cards
    assert re.search(r'Hek</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable', body)
    assert body.count('<tr class="hc-s">') == 4     # a verdict row per loaded hub
    assert body.count('<div class="hc-card">') == 5
    assert "Under-supplied" in body                # 14 listed against 5 a day
    assert "+46.9M" in body                        # 331M * (1 - 0.0487) - 268M
    assert "fills 6 of 10" in body
    assert len([c for c in app_db if c[0] == "orders"]) == 5
    assert len([c for c in app_db if c[0] == "history"]) == 5


def test_a_hub_whose_history_failed_is_unavailable(app_db, monkeypatch):
    async def history_down_at_dodixie(region_id, type_id):
        if region_id == DODIXIE["region_id"]:
            raise RuntimeError("down")
        return [{"date": (TODAY - timedelta(days=i + 1)).isoformat(), "average": 330e6,
                 "highest": 340e6, "lowest": 320e6, "volume": 5, "order_count": 3}
                for i in range(30)]

    monkeypatch.setattr(history, "_fetch_history_esi", history_down_at_dodixie)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    # Dodixie (history), Hek (orders) and Rens: once Dodixie's history fetch
    # has failed, Rens (after it, with no stored history) is not attempted in
    # the same request. Each once in the table, once in the cards.
    assert body.count("Unavailable") == 6
    assert re.search(r'Dodixie</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable',
                     body)
    assert re.search(r'Rens</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable',
                     body)
    assert body.count('<tr class="hc-s">') == 2
    assert "+46.9M" in body


def test_quantity_is_clamped_and_typed_cost_is_used(app_db):
    resp = _authed_client().get(
        f"/market/type/{TYPE_ID}/hubs?qty=-4&cost_mode=typed&cost=300m")
    assert 'name="qty" value="1"' in resp.text
    assert "+14.9M" in resp.text                   # 331M * 0.9513 - 300M


@pytest.mark.parametrize("raw, shown", [
    ("5000000", "1000000"), ("abc", "1"),
    ("1e999", "1000000"), ("inf", "1000000"),
    pytest.param("9" * 400, "1000000", id="400-digits"),
    ("nan", "1"), ("", "1"), ("-inf", "1"),
])
def test_quantity_upper_clamp_and_junk(app_db, raw, shown):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs", params={"qty": raw}).text
    assert f'name="qty" value="{shown}"' in body


@pytest.mark.parametrize("raw, n", [
    ("10", 10), ("1,500", 1500), ("2.7", 2), ("0", 1), ("-4", 1), ("5000000", 1_000_000),
    ("1e999", 1_000_000), ("inf", 1_000_000), ("Infinity", 1_000_000),
    pytest.param("9" * 400, 1_000_000, id="400-digits"),
    ("nan", 1), ("-inf", 1), ("", 1), ("abc", 1), (None, 1),
])
def test_parse_qty(raw, n):
    assert market_hubs._parse_qty(raw) == n


def test_fees_fall_back_to_saved_preferences(app_db):
    client = _authed_client(PREFS_USER_ID)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert 'name="sales_tax" value="8.0"' in body
    assert 'name="broker_fee" value="2.0"' in body
    assert "+29.9M" in body                        # 331M * (1 - 0.10) - 268M
    # Fees typed into the form beat the saved ones.
    body = client.get(
        f"/market/type/{TYPE_ID}/hubs?qty=10&sales_tax=3.37&broker_fee=1.5").text
    assert "+46.9M" in body and "+29.9M" not in body


def test_no_cost_leaves_profit_blank(app_db, monkeypatch):
    async def none_benchmark(db, type_id):
        return None, 10
    monkeypatch.setattr(market_hubs.hub_cost, "benchmark", none_benchmark)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert '<div class="hc-pick-k">Fastest sale</div>' in body
    assert "best margin" not in body.lower()
    assert "+46.9M" not in body
    assert "enter a cost" in body


def test_starting_system_adds_the_route_column(app_db, monkeypatch):
    seen = {}

    async def fake_routes(db, origin, hub_systems):
        seen.update(origin=origin, hub_systems=hub_systems)
        return {key: {"jumps": 3, "highsec": True} for key in hub_systems}

    monkeypatch.setattr(market_hubs.hub_route, "hub_routes", fake_routes)
    body = _authed_client().get(
        f"/market/type/{TYPE_ID}/hubs?qty=10&origin=sample+system").text
    assert ">Route<" in body and "3 j" in body
    assert 'name="origin" value="Sample System"' in body
    assert seen["origin"] == ORIGIN_ID
    assert set(seen["hub_systems"]) == set(APPRAISAL_HUBS)


def test_unknown_starting_system_is_reported(app_db):
    body = _authed_client().get(
        f"/market/type/{TYPE_ID}/hubs?qty=10&origin=Nowhere+Sample").text
    assert "Unknown system: Nowhere Sample." in body
    assert ">Route<" not in body


def test_unroutable_hub_says_no_route_in_table_and_cards(app_db, monkeypatch):
    async def fake_routes(db, origin, hub_systems):
        return {key: (None if key == "amarr" else {"jumps": 3, "highsec": True})
                for key in hub_systems}

    monkeypatch.setattr(market_hubs.hub_route, "hub_routes", fake_routes)
    body = _authed_client().get(
        f"/market/type/{TYPE_ID}/hubs?qty=10&origin=sample+system").text
    table = body[body.index('<table class="hc-table m-hide">'):body.index("</table>")]
    cards = _element(body, "hc-results")[_element(body, "hc-results").index("hc-cards"):]
    assert table.count("no route") == 1 and cards.count("no route") == 1
    assert "no route ·" not in cards


def test_starting_system_text_is_escaped(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs",
                                params={"qty": "10", "origin": 'x"><b>bold'}).text
    assert '"><b>' not in body
    assert 'value="x&#34;&gt;&lt;b&gt;bold"' in body
    assert "Unknown system: x&#34;&gt;&lt;b&gt;bold." in body


# ── remembered failures in the endpoint ─────────────────────────────────

def test_a_failed_hub_is_not_refetched_until_retry(app_db):
    client = _authed_client()
    hek = ("orders", HEK["region_id"])
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    assert app_db.count(hek) == 1
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=11").text
    assert len(app_db) == before                   # nothing upstream at all, Hek included
    assert "Days to sell 11" in body
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=11&retry=1").text
    assert app_db.count(hek) == 2                  # the retry tries Hek once more
    assert len(app_db) == before + 1               # and nothing else: the rest are fresh
    assert body.count("Unavailable") == 2


def test_retry_clears_remembered_history_failures_too(app_db, monkeypatch):
    state = {"down": True}

    async def flaky_history(region_id, type_id):
        app_db.append(("history", region_id))
        if state["down"]:
            raise RuntimeError("down")
        return [{"date": (TODAY - timedelta(days=i + 1)).isoformat(), "average": 330e6,
                 "highest": 340e6, "lowest": 320e6, "volume": 5, "order_count": 3}
                for i in range(30)]

    monkeypatch.setattr(history, "_fetch_history_esi", flaky_history)
    client = _authed_client()
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    state["down"] = False
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    assert [c for c in app_db if c[0] == "history"] == [("history", JITA["region_id"])]
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10&retry=1").text
    assert len([c for c in app_db if c[0] == "history"]) == 6     # one more for each hub
    assert body.count("Unavailable") == 2                          # only Hek's orders


def test_with_esi_down_a_first_view_makes_one_history_attempt(app_db, monkeypatch):
    async def orders_down(region_id, type_id):
        app_db.append(("orders", region_id))
        raise RuntimeError("down")

    async def history_down(region_id, type_id):
        app_db.append(("history", region_id))
        raise RuntimeError("down")

    monkeypatch.setattr(orders, "_fetch_orders_esi", orders_down)
    monkeypatch.setattr(history, "_fetch_history_esi", history_down)
    resp = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    assert resp.status_code == 200
    assert len([c for c in app_db if c[0] == "orders"]) <= 5
    assert len([c for c in app_db if c[0] == "history"]) <= 1
    assert resp.text.count("Unavailable") == 10    # five hubs, table and cards
    assert 'class="hc-picks"' not in resp.text


def test_history_failure_mid_request_skips_only_hubs_that_need_the_network(
        app_db, factory, monkeypatch):
    client = _authed_client()
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")        # all five histories stored

    async def forget(region_ids):
        async with factory() as db:
            await db.execute(delete(MarketHistoryMeta).where(
                MarketHistoryMeta.region_id.in_(region_ids)))
            await db.commit()

    # Jita and Dodixie now need a fetch; Amarr, Hek and Rens are fresh.
    _run(forget([JITA["region_id"], DODIXIE["region_id"]]))

    async def history_down(region_id, type_id):
        app_db.append(("history", region_id))
        raise RuntimeError("down")

    monkeypatch.setattr(history, "_fetch_history_esi", history_down)
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert app_db[before:] == [("history", JITA["region_id"])]   # Dodixie not attempted
    assert history.recently_failed(DODIXIE["region_id"], TYPE_ID)
    assert not history.recently_failed(AMARR["region_id"], TYPE_ID)
    # Jita and Dodixie (no history) and Hek (orders) are unavailable; Amarr
    # and Rens render from their stored history.
    assert body.count("Unavailable") == 6
    assert body.count('<tr class="hc-s">') == 2


def test_a_stale_order_book_still_renders_with_its_age(app_db, monkeypatch):
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(orders, "_now", lambda: now)
    stale_book = [
        {"is_buy_order": False, "price": 331e6, "volume_remain": 14,
         "location_id": AMARR["station_id"], "min_volume": 1},
        {"is_buy_order": True, "price": 284e6, "volume_remain": 6,
         "location_id": AMARR["station_id"], "min_volume": 1},
    ]
    orders._cache[(AMARR["region_id"], TYPE_ID)] = (now - timedelta(hours=7), stale_book)
    working = orders._fetch_orders_esi

    async def amarr_down(region_id, type_id):
        if region_id == AMARR["region_id"]:
            app_db.append(("orders", region_id))
            raise RuntimeError("down")
        return await working(region_id, type_id)

    monkeypatch.setattr(orders, "_fetch_orders_esi", amarr_down)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert app_db.count(("orders", AMARR["region_id"])) == 1   # the refresh was tried
    assert "Orders 420 min old" in body
    assert not re.search(r'Amarr</span></td>\s*<td colspan="\d+" class="hc-unavail"', body)
    assert body.count("Unavailable") == 2                       # only Hek
    assert body.count('<tr class="hc-s">') == 4                 # Amarr loaded as well
    assert "+46.9M" in body


def test_post_hub_prefs_saves_fees_and_starting_system(app_db, factory):
    client = _authed_client()
    resp = client.post("/market/hub-prefs",
                       data={"sales_tax": "5", "broker_fee": "2.5", "origin": "sample system"})
    assert resp.status_code == 200 and resp.text == "Saved."

    async def read():
        async with factory() as db:
            return await hub_prefs.get_prefs(db, USER_ID)

    assert _run(read()) == hub_prefs.HubPrefs(5.0, 2.5, ORIGIN_ID)
    # A typo keeps the saved system rather than clearing it.
    resp = client.post("/market/hub-prefs",
                       data={"sales_tax": "4", "broker_fee": "2", "origin": "Nowhere Sample"})
    assert resp.text == "Fees saved; starting system not recognised."
    assert _run(read()) == hub_prefs.HubPrefs(4.0, 2.0, ORIGIN_ID)


def test_item_page_loads_the_section_and_passes_only_known_keys(app_db):
    client = _authed_client()
    body = client.get(f"/market/type/{TYPE_ID}").text
    assert f'hx-get="/market/type/{TYPE_ID}/hubs"' in body
    body = client.get(
        f"/market/type/{TYPE_ID}?qty=10&cost_mode=typed&cost=300m&other=x").text
    assert (f'hx-get="/market/type/{TYPE_ID}/hubs?qty=10&amp;cost_mode=typed&amp;cost=300m"'
            in body)
    assert app_db == []


# ── the partial on its own ──────────────────────────────────────────────

def _row(name, **over):
    row = {"key": name.lower(), "name": name, "unavailable": False,
           "speed": "fast", "speed_label": "Fast", "supply": "under",
           "supply_label": "Under-supplied", "verdict": "Sells about 5 a day.",
           "outlier_note": "", "lowest_sell": "331.0M", "highest_buy": "284.0M",
           "demand_30": "5", "demand_7": "6", "trend": "up", "stock": "14",
           "stock_days": "2.8 days", "lw_profit": "+46.9M", "lw_sign": "pos",
           "lw_margin": "17.5%", "lw_days": "2 – 5", "lw_batch": "+469.0M",
           "now_avg": "282.7M", "now_fill": "fills 6 of 10", "now_profit": "+5.1M",
           "now_sign": "pos", "now_batch": "+30.6M", "route": "11 j",
           "route_note": "highsec"}
    row.update(over)
    return row


def _render(**over):
    ctx = dict(type_id=TYPE_ID, qty=10, cost_mode="", cost_value="",
               cost_placeholder="benchmark 268.0M",
               notes=["Benchmark: sample note."], origin_name="Sample System",
               sales_tax_pct=3.37, broker_fee_pct=1.5, age_str="Orders 4 min old",
               show_route=True,
               picks=[{"k": "Fastest sale", "v": "Amarr — about 2 – 5 days", "d": "Fast."}],
               rows=[_row("Jita"), _row("Amarr"),
                     {"key": "hek", "name": "Hek", "unavailable": True}])
    ctx.update(over)
    return render_page(market_hubs, "partials/market_hub_comparison.html",
                       f"/market/type/{TYPE_ID}/hubs", **ctx)


def test_partial_has_a_desktop_table_and_phone_cards():
    html = _render()
    assert '<table class="hc-table m-hide">' in html
    assert '<div class="hc-cards m-only">' in html
    assert html.count('<div class="hc-card">') == 3
    assert f'hx-get="/market/type/{TYPE_ID}/hubs"' in html
    assert "Days to sell 10" in html


def test_partial_hides_the_route_column_without_a_starting_system():
    assert ">Route<" not in _render(show_route=False)
    assert ">Route<" in _render()


def test_partial_has_no_inline_event_handlers():
    html = _render()
    assert "onclick=" not in html and "onchange=" not in html
    # The same idea as the site-wide scan (tests/test_csp_inline_handlers.py),
    # on the rendered HTML: no on<event>= attribute of any kind.
    assert not re.search(r"\bon[a-z]+\s*=", html)


def test_only_the_retry_button_asks_for_a_retry():
    html = _render()
    assert """hx-vals='{"retry": "1"}'""" in _tag(html, 'class="hc-retry"')
    assert "retry" not in _tag(html, "<form")
    assert 'name="retry"' not in html


def test_table_headers_have_scopes():
    html = _render()
    headers = re.findall(r"<th\b[^>]*>", html)
    assert headers
    for th in headers:
        assert ('scope="colgroup"' in th) == ("colspan" in th), th
        assert 'scope="col"' in th or 'scope="colgroup"' in th, th


def test_trend_arrows_have_text_for_screen_readers():
    html = _render(rows=[_row("Jita", trend="up"), _row("Amarr", trend="down")])
    assert re.search(r'<span class="hc-pos"[^>]*aria-hidden="true"[^>]*>▲</span>'
                     r'<span class="visually-hidden">rising</span>', html)
    assert re.search(r'<span class="hc-neg"[^>]*aria-hidden="true"[^>]*>▼</span>'
                     r'<span class="visually-hidden">falling</span>', html)


def test_desktop_table_scrolls_inside_its_own_box():
    html = _render()
    assert re.search(r'<div class="hc-scroll">\s*<table class="hc-table m-hide">', html)
    css = source("market_type.html")
    assert re.search(r"\.hc-scroll\s*\{[^}]*overflow-x:\s*auto", css)


def test_pick_boxes_fit_a_narrow_phone():
    css = source("market_type.html")
    rule = re.search(r"\.hc-picks\s*\{([^}]*)\}", css).group(1)
    assert "minmax(min(260px,100%),1fr)" in rule.replace(" ", "")


def test_slow_tag_keeps_the_amber_warning_colour():
    css = source("market_type.html")
    assert re.search(r"\.hc-slow\s*\{\s*color:#c97a20;", css)
    # Documented next to the rule: --warn is the accent gold here, not amber.
    i = css.index(".hc-slow")
    assert "--warn" in css[css.rindex("/*", 0, i):i]


# ── inputs stay in place; results refresh on their own ─────────────────

def _element(html, elem_id):
    """The outer HTML of the <div id=elem_id>, found by counting div depth."""
    start = html.index(f'<div id="{elem_id}"')
    depth, pos = 0, start
    for m in re.finditer(r"<div\b|</div>", html[start:]):
        depth += 1 if m.group() == "<div" else -1
        if depth == 0:
            return html[start:start + m.end()]
    raise AssertionError(f"unclosed #{elem_id}")


def _tag(html, marker):
    """The whole start tag containing `marker`."""
    i = html.index(marker)
    return html[html.rindex("<", 0, i):html.index(">", i) + 1]


def _notes(html):
    return [unescape(n) for n in re.findall(r'<div class="hc-note">([^<]*)</div>', html)]


def test_partial_puts_everything_that_depends_on_the_inputs_in_hc_results():
    html = _render()
    results = _element(html, "hc-results")
    before = html[:html.index('<div id="hc-results"')]
    # The form, its inputs and the save status come first, outside the results.
    assert "</form>" in before and "<form" not in results
    assert 'id="hc-saved"' in before and 'id="hc-saved"' not in results
    for marker in ("Orders 4 min old", "Benchmark: sample note.", 'class="hc-picks"',
                   '<table class="hc-table m-hide">', '<div class="hc-cards m-only">',
                   'class="hc-foot"'):
        assert marker in results and marker not in before


def test_form_and_retry_replace_only_the_results():
    html = _render()
    form = _tag(html, "<form")
    for attr in ('hx-target="#hc-results"', 'hx-select="#hc-results"',
                 'hx-swap="outerHTML"', 'hx-trigger="change, submit"',
                 f'hx-get="/market/type/{TYPE_ID}/hubs"'):
        assert attr in form
    retry = _tag(html, 'class="hc-retry"')
    for attr in ('hx-target="#hc-results"', 'hx-select="#hc-results"',
                 'hx-swap="outerHTML"', 'hx-include="#hc-form"'):
        assert attr in retry
    # The save button sits inside the form: it must not inherit hx-select,
    # or its "Saved." reply would be filtered down to nothing.
    save = _tag(html, 'hx-post="/market/hub-prefs"')
    assert 'hx-select="unset"' in save and 'hx-target="#hc-saved"' in save


def test_item_page_first_load_swaps_the_whole_partial(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}").text
    hubs = _tag(body, 'id="mk-hubs"')
    assert 'hx-trigger="load"' in hubs and 'hx-swap="innerHTML"' in hubs
    assert "hx-select" not in hubs


def test_every_input_has_an_id_and_a_bound_label():
    html = _render()
    inputs = re.findall(r"<input\b[^>]*>", html)
    assert len(inputs) == 6
    for tag in inputs:
        found = re.search(r'\bid="([^"]+)"', tag)
        assert found, tag
        if 'type="hidden"' not in tag:
            assert f'<label class="hc-fld" for="{found.group(1)}">' in html, tag


def test_one_cost_field_with_a_benchmark_placeholder(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert "<select" not in body and "readonly" not in body
    cost = _tag(body, 'id="hc-cost"')
    assert 'name="cost" value=""' in cost and 'placeholder="benchmark 268.0M"' in cost
    assert _tag(body, 'id="hc-cost-mode"').count('value=""') == 1
    assert "+46.9M" in body                        # blank cost: the benchmark is used
    assert _notes(_element(body, "hc-results"))[0] == (
        "Cost used: 268.0M each — benchmark (materials at EVE's average reference "
        "price, ME 10, no job fees or invention cost).")


def test_without_a_benchmark_the_placeholder_asks_for_a_cost(app_db, monkeypatch):
    async def none_benchmark(db, type_id):
        return None, 10
    monkeypatch.setattr(market_hubs.hub_cost, "benchmark", none_benchmark)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert 'placeholder="enter a cost"' in _tag(body, 'id="hc-cost"')
    assert _notes(body)[0] == ("No blueprint for this item — enter a cost per unit "
                               "to see profit and margin.")


@pytest.mark.parametrize("query, note", [
    ("cost=300m", "Cost used: 300.0M each — your figure."),
    ("cost=300m&cost_mode=typed", "Cost used: 300.0M each — your figure."),
    ("cost=300m&cost_mode=benchmark", "Cost used: 300.0M each — your figure."),
    ("cost=300m&cost_mode=build", "Cost used: 300.0M each — from your build."),
])
def test_a_typed_cost_is_used_and_labelled(app_db, query, note):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10&{query}").text
    assert "+14.9M" in body                        # 331M * 0.9513 - 300M
    assert _notes(_element(body, "hc-results"))[0] == note


def test_cost_mode_build_is_echoed_and_nothing_else(app_db):
    client = _authed_client()
    for mode, echoed in (("build", "build"), ("typed", ""), ("benchmark", ""), ("x", "")):
        body = client.get(f"/market/type/{TYPE_ID}/hubs?cost=1m&cost_mode={mode}").text
        assert f'value="{echoed}"' in _tag(body, 'id="hc-cost-mode"'), mode


def test_unreadable_cost_falls_back_to_the_benchmark_and_says_so(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10&cost=lots").text
    assert "+46.9M" in body
    assert _notes(body)[:2] == [
        "Cost used: 268.0M each — benchmark (materials at EVE's average reference "
        "price, ME 10, no job fees or invention cost).",
        "Could not read that cost; using the benchmark.",
    ]


def test_unreadable_cost_without_a_benchmark_asks_for_a_number(app_db, monkeypatch):
    async def none_benchmark(db, type_id):
        return None, 10
    monkeypatch.setattr(market_hubs.hub_cost, "benchmark", none_benchmark)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10&cost=lots").text
    assert "Could not read that cost; enter a number such as 180m." in _notes(body)
    assert "+46.9M" not in body


@pytest.mark.parametrize("raw, shown", [
    ("4.35", "4.35"), ("0.4", "0.4"), ("268000000.0", "268000000"), ("180m", "180000000"),
    ("1.256", "1.26"),
])
def test_cost_echo_keeps_up_to_two_decimals(app_db, raw, shown):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?cost={raw}").text
    assert f'name="cost" value="{shown}"' in body


def _cheap_market(monkeypatch):
    """A 10 ISK item, so a cost's decimals show in the profit figures."""
    async def cheap_orders(region_id, type_id):
        station = next(h["station_id"] for h in APPRAISAL_HUBS.values()
                       if h["region_id"] == region_id)
        return [{"is_buy_order": False, "price": 10.0, "volume_remain": 50,
                 "location_id": station, "min_volume": 1},
                {"is_buy_order": True, "price": 9.0, "volume_remain": 50,
                 "location_id": station, "min_volume": 1}]

    async def cheap_history(region_id, type_id):
        return [{"date": (TODAY - timedelta(days=i + 1)).isoformat(), "average": 10.0,
                 "highest": 10.5, "lowest": 9.5, "volume": 40, "order_count": 9}
                for i in range(30)]

    monkeypatch.setattr(orders, "_fetch_orders_esi", cheap_orders)
    monkeypatch.setattr(history, "_fetch_history_esi", cheap_history)


def _figures(body):
    return re.findall(r'class="[^"]*\bhc-(?:pos|neg)">([^<]+)<', _element(body, "hc-results"))


def test_cost_echo_round_trips_to_the_same_figures(app_db, monkeypatch):
    _cheap_market(monkeypatch)
    client = _authed_client()
    first = client.get(f"/market/type/{TYPE_ID}/hubs?qty=5&cost=4.35").text
    echoed = re.search(r'name="cost" value="([^"]*)"', first).group(1)
    assert echoed == "4.35"
    again = client.get(f"/market/type/{TYPE_ID}/hubs?qty=5&cost={echoed}").text
    assert _figures(first) and _figures(first) == _figures(again)
    assert "+5.16" in _figures(first)              # 10 * (1 - 0.0487) - 4.35


def test_quantity_used_shows_in_the_table_and_every_card(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=lots").text
    results = _element(body, "hc-results")
    # Junk quantity reads as 1: the table header plus each of the four loaded cards.
    assert results.count("Days to sell 1<") == 5
