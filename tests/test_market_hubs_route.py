"""Hub comparison endpoint and partial. Names are invented."""
import asyncio
import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.market.history as history
import app.market.orders as orders
from app.db.models import Base, MarketHistory, MarketHistoryMeta, User, get_db
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
# history.py's clock, pinned to the same day as the endpoint's: the coverage
# rule reads the meta stamp's date, so a real clock would make every figure
# depend on the day the tests run.
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
JITA = APPRAISAL_HUBS["jita"]
AMARR = APPRAISAL_HUBS["amarr"]
HEK = APPRAISAL_HUBS["hek"]
DODIXIE = APPRAISAL_HUBS["dodixie"]
RENS = APPRAISAL_HUBS["rens"]
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
    monkeypatch.setattr(history, "_now", lambda: NOW)
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
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")   # Jita fails; the rest are skipped
    state["down"] = False
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    # Jita's failure is remembered, so it is not attempted and stays
    # unavailable; the hubs skipped last time were not, and load now.
    assert [c for c in app_db if c[0] == "history"] == (
        [("history", JITA["region_id"])] + [("history", h["region_id"])
                                            for h in (AMARR, DODIXIE, HEK, RENS)])
    assert re.search(r'Jita</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable',
                     body)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10&retry=1").text
    # The retry forgets Jita's failure and fetches it; the rest are fresh.
    assert [c for c in app_db if c[0] == "history"][5:] == [("history", JITA["region_id"])]
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
    # Skipped for this request only: Dodixie is not remembered as failed.
    assert not history.recently_failed(DODIXIE["region_id"], TYPE_ID)
    assert not history.recently_failed(AMARR["region_id"], TYPE_ID)
    # Jita and Dodixie (no history) and Hek (orders) are unavailable; Amarr
    # and Rens render from their stored history.
    assert body.count("Unavailable") == 6
    assert body.count('<tr class="hc-s">') == 2
    # The next request leaves Jita alone (inside its failure window) and
    # makes its one attempt at Dodixie instead.
    before = len(app_db)
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=11")
    assert app_db[before:] == [("history", DODIXIE["region_id"])]


def _history_rows():
    return [{"date": (TODAY - timedelta(days=i + 1)).isoformat(), "average": 330e6,
             "highest": 340e6, "lowest": 320e6, "volume": 5, "order_count": 3}
            for i in range(30)]


def _jita_history_always_down(calls, monkeypatch):
    """Jita's history fetch fails every time; every other fetch works (Hek's
    orders too, so Jita's failure is the only one in play)."""
    async def all_orders(region_id, type_id):
        calls.append(("orders", region_id))
        station = next(h["station_id"] for h in APPRAISAL_HUBS.values()
                       if h["region_id"] == region_id)
        return [
            {"is_buy_order": False, "price": 331e6, "volume_remain": 14,
             "location_id": station, "min_volume": 1},
            {"is_buy_order": True, "price": 284e6, "volume_remain": 6,
             "location_id": station, "min_volume": 1},
        ]

    async def jita_down(region_id, type_id):
        calls.append(("history", region_id))
        if region_id == JITA["region_id"]:
            raise RuntimeError("down")
        return _history_rows()

    monkeypatch.setattr(orders, "_fetch_orders_esi", all_orders)
    monkeypatch.setattr(history, "_fetch_history_esi", jita_down)


LATER = [("history", h["region_id"]) for h in (AMARR, DODIXIE, HEK, RENS)]
JITA_UNAVAILABLE = r'Jita</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable'


def test_a_hub_that_keeps_failing_does_not_keep_later_hubs_from_loading(
        app_db, monkeypatch):
    _jita_history_always_down(app_db, monkeypatch)
    client = _authed_client()
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    # Request 1: five order books, then Jita's history fails and the four
    # later hubs (no stored history) are skipped — one history attempt.
    assert sorted(app_db) == sorted([("orders", h["region_id"]) for h in APPRAISAL_HUBS.values()]
                                    + [("history", JITA["region_id"])])
    assert body.count("Unavailable") == 10         # all five, table and cards
    assert history.recently_failed(JITA["region_id"], TYPE_ID)
    for _kind, region in LATER:                    # skipped, not stamped
        assert not history.recently_failed(region, TYPE_ID)
    # Request 2, straight after, an ordinary input change: Jita is inside
    # its failure window and not attempted; the four later hubs load.
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=11").text
    assert app_db[before:] == LATER
    assert body.count("Unavailable") == 2          # Jita only
    assert re.search(JITA_UNAVAILABLE, body)
    assert body.count('<tr class="hc-s">') == 4
    assert "Days to sell 11" in body and "+46.9M" in body


def test_a_retry_tries_the_failing_hub_again_and_the_next_change_loads_the_rest(
        app_db, monkeypatch):
    _jita_history_always_down(app_db, monkeypatch)
    client = _authed_client()
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")
    # Request 2 is a retry: Jita's failure is forgotten, Jita is tried and
    # fails again, so the later hubs are skipped for this request again.
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=11&retry=1").text
    assert app_db[before:] == [("history", JITA["region_id"])]
    assert body.count("Unavailable") == 10
    for _kind, region in LATER:
        assert not history.recently_failed(region, TYPE_ID)
    # Request 3, no retry: they load.
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=12").text
    assert app_db[before:] == LATER
    assert body.count("Unavailable") == 2
    assert re.search(JITA_UNAVAILABLE, body)
    assert body.count('<tr class="hc-s">') == 4


def test_a_skipped_hub_still_shows_its_stale_stored_history(app_db, factory, monkeypatch):
    client = _authed_client()
    client.get(f"/market/type/{TYPE_ID}/hubs?qty=10")        # all five histories stored

    async def age(region_ids):
        two_days_ago = NOW.replace(tzinfo=None) - timedelta(days=2)
        async with factory() as db:
            # What a fetch two days ago would have stored: rows only up to
            # three days ago (the game publishes a day's row the next day).
            await db.execute(delete(MarketHistory)
                             .where(MarketHistory.region_id.in_(region_ids))
                             .where(MarketHistory.date > TODAY - timedelta(days=3)))
            await db.execute(update(MarketHistoryMeta)
                             .where(MarketHistoryMeta.region_id.in_(region_ids))
                             .values(fetched_at=two_days_ago))
            await db.commit()

    # Jita and Dodixie are mildly stale: stamp two days old, history to
    # 2026-11-07, two days short of yesterday.
    _run(age([JITA["region_id"], DODIXIE["region_id"]]))

    async def history_down(region_id, type_id):
        app_db.append(("history", region_id))
        raise RuntimeError("down")

    monkeypatch.setattr(history, "_fetch_history_esi", history_down)
    before = len(app_db)
    body = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert app_db[before:] == [("history", JITA["region_id"])]   # Dodixie not attempted
    assert not history.recently_failed(DODIXIE["region_id"], TYPE_ID)
    # Both still render from their stored rows, each with a "history to"
    # note in the table and the card; only Hek (orders) is unavailable.
    assert body.count("Unavailable") == 2
    assert body.count('<tr class="hc-s">') == 4
    assert body.count(HISTORY_NOTE.format("2026-11-07")) == 4
    assert re.search(r"Jita</span>.*?" + re.escape(HISTORY_NOTE.format("2026-11-07")),
                     body, re.S)


# ── the demand window and stale history ─────────────────────────────────

HISTORY_NOTE = '<span class="hc-mut">(history to {})</span>'


def _seed_history(factory, region_ids, last_day, fetched_at, days=40, volume=5):
    """Store `days` daily rows ending on `last_day` and a meta stamp of
    `fetched_at` for each region, replacing whatever was there."""
    async def go():
        async with factory() as db:
            await db.execute(delete(MarketHistory).where(
                MarketHistory.region_id.in_(region_ids), MarketHistory.type_id == TYPE_ID))
            await db.execute(delete(MarketHistoryMeta).where(
                MarketHistoryMeta.region_id.in_(region_ids),
                MarketHistoryMeta.type_id == TYPE_ID))
            for region in region_ids:
                for i in range(days):
                    db.add(MarketHistory(region_id=region, type_id=TYPE_ID,
                                         date=last_day - timedelta(days=i), average=330e6,
                                         highest=340e6, lowest=320e6, volume=volume,
                                         order_count=3))
                db.add(MarketHistoryMeta(region_id=region, type_id=TYPE_ID,
                                         fetched_at=fetched_at.replace(tzinfo=None)))
            await db.commit()
    _run(go())


def _spy_on_analysis(monkeypatch):
    """Record every analyse_hub result the endpoint computes."""
    real, seen = market_hubs.analyse_hub, []

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(market_hubs, "analyse_hub", spy)
    return seen


@pytest.mark.parametrize("last_row_ago, fetched", [
    # Rows to two days ago, fetched today: yesterday's row is not out yet.
    (2, NOW),
    # Rows to three days ago, fetched yesterday afternoon and still fresh.
    # The old window (ending yesterday) read this as 93 a day, 71 over 7
    # days, trend down.
    (3, datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)),
])
def test_demand_window_ends_where_the_stored_history_does(app_db, monkeypatch,
                                                          last_row_ago, fetched):
    async def steady(region_id, type_id):
        app_db.append(("history", region_id))
        return [{"date": (TODAY - timedelta(days=last_row_ago + i)).isoformat(),
                 "average": 330e6, "highest": 340e6, "lowest": 320e6, "volume": 100,
                 "order_count": 3} for i in range(40)]

    monkeypatch.setattr(history, "_fetch_history_esi", steady)
    monkeypatch.setattr(history, "_now", lambda: fetched)
    seen = _spy_on_analysis(monkeypatch)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert len(seen) == 4                          # Hek's orders fail
    for result in seen:
        assert result["demand_30"] == pytest.approx(100.0)
        assert result["demand_7"] == pytest.approx(100.0)
        assert result["trend"] == "flat"
    assert body.count("7d: 100<") == 4


def test_history_far_behind_with_the_fetch_failing_is_unavailable(app_db, factory,
                                                                  monkeypatch):
    regions = [h["region_id"] for h in APPRAISAL_HUBS.values()]
    _seed_history(factory, regions, TODAY - timedelta(days=62), NOW - timedelta(days=60))

    async def history_down(region_id, type_id):
        app_db.append(("history", region_id))
        raise RuntimeError("down")

    monkeypatch.setattr(history, "_fetch_history_esi", history_down)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    # Sixty-day-old rows are not "no sales in 30 days": all five unavailable.
    assert body.count("Unavailable") == 10         # table and cards
    assert body.count('class="hc-retry"') == 10
    assert '<tr class="hc-s">' not in body
    assert "No sales here" not in body
    assert 'class="hc-picks"' not in body


def test_one_hub_far_behind_is_unavailable_and_the_age_line_uses_the_oldest_shown(
        app_db, factory, monkeypatch):
    yesterday_pm = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
    _seed_history(factory, [JITA["region_id"], RENS["region_id"]],
                  TODAY - timedelta(days=1), NOW)                       # current
    _seed_history(factory, [AMARR["region_id"]],
                  TODAY - timedelta(days=27), NOW - timedelta(days=25))  # 25 days stale
    _seed_history(factory, [DODIXIE["region_id"]],
                  TODAY - timedelta(days=3), yesterday_pm)              # two days behind

    async def history_down(region_id, type_id):
        app_db.append(("history", region_id))
        raise RuntimeError("down")

    monkeypatch.setattr(history, "_fetch_history_esi", history_down)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert [c for c in app_db if c[0] == "history"] == [("history", AMARR["region_id"])]
    assert re.search(r'Amarr</span></td>\s*<td colspan="\d+" class="hc-unavail">Unavailable',
                     body)
    assert body.count("Unavailable") == 4          # Amarr (history) and Hek (orders)
    assert body.count('<tr class="hc-s">') == 3    # Jita, Dodixie, Rens
    age = re.search(r'<div class="hc-age hc-mut">([^<]*)</div>', body).group(1)
    assert age.endswith("history to 2026-11-07")   # Dodixie's, the oldest shown


def test_a_hub_two_days_behind_renders_with_a_history_note(app_db, factory, monkeypatch):
    yesterday_pm = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
    _seed_history(factory, [DODIXIE["region_id"]], TODAY - timedelta(days=3), yesterday_pm)
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10").text
    assert body.count("Unavailable") == 2          # Hek's orders only
    assert body.count('<tr class="hc-s">') == 4
    note = HISTORY_NOTE.format("2026-11-07")
    assert body.count(note) == 2                   # Dodixie: table and card
    table = body[body.index('<table class="hc-table m-hide">'):body.index("</table>")]
    dodixie = table[table.index("Dodixie</span>"):]
    assert note in dodixie[:dodixie.index('<tr class="hc-r">')]   # its own verdict line
    cards = body[body.index('<div class="hc-cards m-only">'):]
    card = cards[cards.index("Dodixie</span>"):]
    assert note in card[:card.index('<div class="hc-mkt">')]


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
    # A build-cost entry link, with a key the section does not know.
    body = client.get(
        f"/market/type/{TYPE_ID}?cost_mode=build&cost=250000000&qty=20&junk=1").text
    assert (f'hx-get="/market/type/{TYPE_ID}/hubs?qty=20&amp;cost_mode=build'
            f'&amp;cost=250000000"') in body
    assert "junk" not in body.split('id="mk-hubs"')[1].split(">")[0]


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
    ctx = dict(type_id=TYPE_ID, qty=10, cost_mode="", cost_value="", build_cost="",
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
    assert "retry" not in _tag(html, '<div id="hc-results"')    # the ordinary refresh
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


def test_results_own_the_refresh_and_the_form_makes_no_request():
    """#hc-results makes the request on a change in the form, so the site's
    error handler (base.html, ISS-007), which writes into the element that
    made the request, lands in the results and leaves the inputs alone."""
    html = _render()
    results = _tag(html, '<div id="hc-results"')
    for attr in (f'hx-get="/market/type/{TYPE_ID}/hubs"', 'hx-trigger="change from:#hc-form"',
                 'hx-include="#hc-form"', 'hx-target="this"', 'hx-select="#hc-results"',
                 'hx-swap="outerHTML"', 'hx-sync="this:replace"'):
        assert attr in results
    assert "load" not in results                   # no request of its own when inserted
    assert "data-htmx-no-error" not in results
    form = _tag(html, "<form")
    assert "hx-" not in form
    retry = _tag(html, 'class="hc-retry"')
    for attr in (f'hx-get="/market/type/{TYPE_ID}/hubs"', 'hx-include="#hc-form"',
                 'hx-target="#hc-results"', 'hx-select="#hc-results"',
                 'hx-swap="outerHTML"', 'hx-sync="#hc-results:replace"'):
        assert attr in retry
    save = _tag(html, 'hx-post="/market/hub-prefs"')
    for attr in ('hx-include="#hc-form"', 'hx-target="#hc-saved"', 'hx-swap="innerHTML"'):
        assert attr in save
    assert "hx-select" not in save


class _Ancestry(HTMLParser):
    """The attributes of every element enclosing the first start tag that
    `match(tag, attrs)` accepts, outermost first."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}

    def __init__(self, match):
        super().__init__()
        self.match, self.stack, self.found = match, [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.found is None and self.match(tag, attrs):
            self.found = [a for _t, a in self.stack]
        if tag not in self.VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _ancestors(html, match):
    parser = _Ancestry(match)
    parser.feed(html)
    assert parser.found is not None, "no element matched"
    return parser.found


def _hx(attrs):
    return {k for k in attrs if k.startswith(("hx-", "data-hx-"))}


def test_the_save_button_inherits_nothing_that_filters_or_redirects_its_reply(app_db):
    # Within the partial, nothing above the save button carries an htmx
    # attribute (hx-select there would filter "Saved." down to nothing).
    above = _ancestors(_render(), lambda tag, a: a.get("hx-post") == "/market/hub-prefs")
    assert "hc-form" in [a.get("id") for a in above]
    assert all(not _hx(a) for a in above), above
    # On the item page the partial sits in #mk-hubs, which only loads it,
    # and nothing above #mk-hubs carries an htmx attribute.
    page = _authed_client().get(f"/market/type/{TYPE_ID}").text
    above = _ancestors(page, lambda tag, a: a.get("id") == "mk-hubs")
    assert above and all(not _hx(a) for a in above), above
    mk = _tag(page, 'id="mk-hubs"')
    assert set(re.findall(r"\b(hx-[a-z-]+)=", mk)) == {"hx-get", "hx-trigger", "hx-swap"}
    # The error handler checks its opt-out on the requesting element only,
    # never its ancestors; neither element carries it.
    assert "data-htmx-no-error" not in mk


def test_enter_in_a_field_cannot_submit_the_form_natively():
    """No submit button and several text fields: under the HTML spec's
    implicit-submission rules, Enter in a field submits nothing, so the page
    never navigates. (The form makes no request of its own; a change in a
    field is what refreshes the results.)"""
    html = _render()
    form = html[html.index("<form"):html.index("</form>")]
    buttons = re.findall(r"<button\b[^>]*>", form)
    assert buttons and all('type="button"' in b for b in buttons)
    assert 'type="submit"' not in html and 'type="image"' not in html
    assert len(re.findall(r'<input\b[^>]*type="text"', form)) > 1


def test_item_page_first_load_swaps_the_whole_partial(app_db):
    body = _authed_client().get(f"/market/type/{TYPE_ID}").text
    hubs = _tag(body, 'id="mk-hubs"')
    assert 'hx-trigger="load"' in hubs and 'hx-swap="innerHTML"' in hubs
    assert "hx-select" not in hubs


def test_every_input_has_an_id_and_a_bound_label():
    html = _render()
    inputs = re.findall(r"<input\b[^>]*>", html)
    assert len(inputs) == 7                        # five fields, cost_mode, build_cost
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


@pytest.mark.parametrize("query, label", [
    # First load from an entry link (no build_cost yet): its cost is the build.
    ("cost=300m&cost_mode=build", "from your build"),
    # Later requests carry the entry link's cost as build_cost.
    ("cost=300000000&cost_mode=build&build_cost=300000000", "from your build"),
    ("cost=300m&cost_mode=build&build_cost=300000000", "from your build"),
    ("cost=300000000.004&cost_mode=build&build_cost=300000000", "from your build"),
    ("cost=250m&cost_mode=build&build_cost=300000000", "your figure"),
    ("cost=300000000.01&cost_mode=build&build_cost=300000000", "your figure"),
    ("cost=300m&cost_mode=build&build_cost=", "your figure"),
    ("cost=300m&cost_mode=build&build_cost=lots", "your figure"),
    ("cost=300m&build_cost=300000000", "your figure"),
    ("cost=300m&cost_mode=typed&build_cost=300000000", "your figure"),
])
def test_build_label_only_while_the_cost_is_the_entry_links(app_db, query, label):
    body = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?qty=10&{query}").text
    assert _notes(_element(body, "hc-results"))[0].endswith(f" each — {label}.")


def test_build_cost_is_echoed_only_for_an_entry_links_build_cost(app_db):
    client = _authed_client()
    body = client.get(f"/market/type/{TYPE_ID}/hubs?cost=300m&cost_mode=build").text
    assert 'name="build_cost" value="300000000"' in _tag(body, 'id="hc-build-cost"')
    assert 'type="hidden"' in _tag(body, 'id="hc-build-cost"')
    for query in ("cost=300m", "cost=300m&cost_mode=typed", "cost_mode=build",
                  "cost=lots&cost_mode=build"):
        body = client.get(f"/market/type/{TYPE_ID}/hubs?{query}").text
        assert 'value=""' in _tag(body, 'id="hc-build-cost"'), query


def test_a_cost_typed_after_a_build_link_is_the_users_figure(app_db):
    """Round trip as the page does it: the first load's form values are sent
    back with the cost changed, then with it restored."""
    client = _authed_client()
    first = client.get(f"/market/type/{TYPE_ID}/hubs?qty=10&cost=300m&cost_mode=build").text
    form = dict(re.findall(r'<input\b[^>]*name="([^"]+)" value="([^"]*)"', first))
    assert (form["cost"], form["cost_mode"], form["build_cost"]) == (
        "300000000", "build", "300000000")

    def label(body):
        return _notes(_element(body, "hc-results"))[0]

    assert label(first) == "Cost used: 300.0M each — from your build."
    changed = client.get(f"/market/type/{TYPE_ID}/hubs", params={**form, "cost": "250m"}).text
    assert label(changed) == "Cost used: 250.0M each — your figure."
    restored = client.get(f"/market/type/{TYPE_ID}/hubs", params=form).text
    assert label(restored) == "Cost used: 300.0M each — from your build."


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


# ── entry links ─────────────────────────────────────────────────────────

def _build_finder_html(*rows):
    from app.routes import industry as industry_mod
    return render_page(industry_mod, "partials/build_finder_results.html",
                       "/industry/build-finder/results", rows=list(rows),
                       total_n=len(rows), shown=len(rows), capped=False, me=10,
                       compute_ms=1.0, invention_active=False, tables_empty=False,
                       selected_character_id=0, encryption=4, science=4,
                       decryptor="none")


def _finder_row(**over):
    row = {"product_type_id": TYPE_ID, "product_name": "Sample Hull", "cost_str": "268.00M",
           "sell_str": "331.00M", "margin_isk_str": "63.00M", "margin_pct_str": "23.5%",
           "margin_positive": True, "priced": True, "inv_str": None, "skill_missing": False,
           "inv_source": None, "hub_query": "?cost_mode=build&cost=268000000"}
    row.update(over)
    return row


def test_build_finder_rows_link_with_their_build_cost():
    unpriced = _finder_row(product_type_id=TYPE_ID + 1, priced=False, hub_query="")
    html = _build_finder_html(_finder_row(), unpriced)
    assert (f'href="/market/type/{TYPE_ID}?cost_mode=build&amp;cost=268000000"'
            in html)
    assert f'href="/market/type/{TYPE_ID + 1}"' in html
    # Both the desktop name link and the phone "Open market page" link.
    assert html.count(f'?cost_mode=build&amp;cost=268000000"') == 2


def test_build_finder_row_without_hub_query_key_keeps_the_plain_link():
    row = _finder_row()
    del row["hub_query"]
    assert f'href="/market/type/{TYPE_ID}"' in _build_finder_html(row)


@pytest.mark.parametrize("cost,param", [
    (268_000_000.0, "268000000"), (4.35, "4.35"), (0.4, "0.4"), (1234.5678, "1234.57")])
def test_hub_query_formats_the_cost_the_endpoint_echoes(cost, param):
    from app.routes import industry as industry_mod
    assert industry_mod._hub_query(cost) == f"?cost_mode=build&cost={param}"


@pytest.mark.parametrize("cost", [None, 0, 0.004, -1.0])
def test_hub_query_is_empty_without_a_usable_cost(cost):
    from app.routes import industry as industry_mod
    assert industry_mod._hub_query(cost) == ""


def test_a_cheap_build_cost_survives_from_build_finder_to_the_hub_request(app_db):
    """4.35 ISK each: Build Finder link -> item page -> hub request URL."""
    from html import unescape
    from app.routes import industry as industry_mod
    html = _build_finder_html(_finder_row(hub_query=industry_mod._hub_query(4.35)))
    link = unescape(re.search(r'href="(/market/type/[^"]*)"', html).group(1))
    assert link == f"/market/type/{TYPE_ID}?cost_mode=build&cost=4.35"
    body = _authed_client().get(link).text
    assert (f'hx-get="/market/type/{TYPE_ID}/hubs?cost_mode=build&amp;cost=4.35"'
            in body)
    # ... and the hub endpoint reads it back as that cost, from the build.
    part = _authed_client().get(f"/market/type/{TYPE_ID}/hubs?cost_mode=build&cost=4.35").text
    assert 'value="4.35"' in part
    assert "from your build" in part


def _calc(monkeypatch, *, runs, product_qty, prices, product_id=TYPE_ID, info=True):
    """industry_calculate's HTML for an invented blueprint: material A x10 and
    material B x3 per run. Nothing is bought; SDE and prices are stubbed."""
    from app.routes import industry as industry_mod
    from tests._mobile import request

    async def materials(db, bp):
        return [{"type_id": 501, "name": "Sample Ore", "quantity": 10},
                {"type_id": 502, "name": "Sample Dust", "quantity": 3}]

    async def name(db, tid):
        return "Sample Hull Blueprint"

    async def none(db, *a):
        return None

    async def bp_info(db, bp):
        if not info:
            return None
        return {"blueprint_type_id": bp, "product_type_id": product_id,
                "manufacturing_time": 600, "product_quantity": product_qty}

    async def price_map(db, ids):
        return prices

    for attr, fn in (("get_blueprint_materials", materials), ("type_id_to_name", name),
                     ("find_blueprint_for_product", none), ("get_blueprint_time", none),
                     ("get_blueprint_info", bp_info)):
        monkeypatch.setattr(industry_mod.sde, attr, fn)
    monkeypatch.setattr(industry_mod, "_get_price_map", price_map)
    resp = _run(industry_mod.industry_calculate(
        request("/industry/calculate"), type_id=9001, me=0, te=0, runs=runs,
        structure="npc_station", rig="none", security="highsec", db=None))
    return resp.body.decode()


def test_calculator_links_with_per_unit_cost_and_total_quantity(monkeypatch):
    # 3 runs: 30 Ore at 100 + 9 Dust at 2.5 = 3022.5 ISK; 3 x 5 = 15 units.
    html = _calc(monkeypatch, runs=3, product_qty=5, prices={501: 100.0, 502: 2.5})
    assert (f'href="/market/type/{TYPE_ID}?cost_mode=build&amp;cost=201.5&amp;qty=15"'
            in html)
    assert "Compare hubs" in html
    # The link row sits directly after the total-cost row.
    assert html.index("Total Material Cost (Buy All)") < html.index("Compare hubs")


def test_calculator_single_run_single_unit(monkeypatch):
    html = _calc(monkeypatch, runs=1, product_qty=1, prices={501: 100.0, 502: 2.5})
    assert f'?cost_mode=build&amp;cost=1007.5&amp;qty=1"' in html


def test_calculator_has_no_link_when_a_material_is_unpriced(monkeypatch):
    html = _calc(monkeypatch, runs=3, product_qty=5, prices={501: 100.0})
    assert "Compare hubs" not in html
    assert "cost_mode=build" not in html


def test_calculator_has_no_link_without_a_product(monkeypatch):
    html = _calc(monkeypatch, runs=3, product_qty=5, prices={501: 100.0, 502: 2.5},
                 info=False)
    assert "Compare hubs" not in html
    html = _calc(monkeypatch, runs=3, product_qty=5, prices={501: 100.0, 502: 2.5},
                 product_id=None)
    assert "Compare hubs" not in html


def test_calc_results_tolerates_a_missing_hub_link():
    from tests.test_mobile_r3_manufacturing import _render_calc
    assert "Compare hubs" not in _render_calc()
