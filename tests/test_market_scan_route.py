"""Hub Scan page and routes. Names and figures are invented.

TestClient cannot run the real background job reliably, so `scan_runner.start`
is replaced by a fake that records its arguments and registers a running job;
results are put in the database through `scan_results.save`."""
import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from html import unescape

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketWatchlist, User, get_db
from app.db.sde_models import SDEMarketGroup, SDEStation, SDESystem, SDEType
from app.esi.market import APPRAISAL_HUBS
from app.market import hub_prefs, hub_view, scan_results, scan_runner, watchlists
from app.market.scan_results import Settings
from tests._mobile import source
from tests.conftest import ensure_user

USER_ID = 91
OTHER_USER_ID = 92
GROUP_ID = 10
TYPE_ID = 2001
ORIGIN_ID = 30099001
CSRF = "test-csrf-token-hub-scan-0001"
SCANNED = datetime(2026, 11, 10, 14, 5, tzinfo=timezone.utc)
SETTINGS = Settings(budget_isk=5e8, share_pct=20.0, sales_tax_pct=3.37, broker_fee_pct=1.5)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _client(session: dict) -> TestClient:
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps(session).encode())
    client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set("vigilant_session", signer.sign(data).decode())
    client.headers.update({"X-CSRF-Token": CSRF})
    return client


def _authed_client(user_id=USER_ID):
    """Signed session cookie for `user_id`, with a CSRF token in the session
    and the matching header on every request (htmx sends it on POSTs)."""
    ensure_user(user_id)
    return _client({"user_id": user_id, "csrf_token": CSRF})


def _anon_client():
    """No user, but a valid CSRF token: a POST reaches the route itself."""
    return _client({"csrf_token": CSRF})


@pytest.fixture
def factory(tmp_path):
    """Temp DB: two users, one market group holding one item, the five hub
    stations and a starting system."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add_all([User(id=USER_ID), User(id=OTHER_USER_ID)])
            db.add(SDEMarketGroup(market_group_id=GROUP_ID, parent_group_id=None,
                                  market_group_name="Sample Ships"))
            db.add(SDEType(type_id=TYPE_ID, type_name="Sample Hull", published=True,
                           market_group_id=GROUP_ID, group_id=1))
            db.add(SDESystem(system_id=ORIGIN_ID, system_name="Sample System", security=0.9))
            for n, hub in enumerate(APPRAISAL_HUBS.values()):
                db.add(SDEStation(station_id=hub["station_id"],
                                  station_name=f"Sample Hub Station {n}",
                                  system_id=30099100 + n))
            await db.commit()

    _run(setup())
    yield session_factory
    _run(engine.dispose())


class _Started(list):
    """The fake start's calls; `.started` reads like the plan's fixture."""
    @property
    def started(self):
        return self


@pytest.fixture
def app_db(factory, monkeypatch):
    import app.main as main

    async def override_get_db():
        async with factory() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    scan_runner.reset()
    calls = _Started()

    def fake_start(user_id, kind, ref, settings):
        calls.append({"user_id": user_id, "kind": kind, "ref": ref, "settings": settings})
        current = scan_runner._jobs.get(user_id)
        if current is not None and current.state in scan_runner.ACTIVE:
            return current          # one job per user, as the real runner
        job = scan_runner.Job(user_id, kind, ref, settings, state="running",
                              done=1, total=4, label="Sample Ships")
        scan_runner._jobs[user_id] = job
        return job

    monkeypatch.setattr(scan_runner, "start", fake_start)
    yield calls
    main.app.dependency_overrides.pop(get_db, None)
    scan_runner.reset()


def _ok(name, type_id, **over):
    base = {"type_id": type_id, "name": name, "status": "ok", "hub": "rens",
            "source": "haul", "cost": 100.0, "cost_basis": "jita", "copy_only": False,
            "price": 160.0, "price_flag": None, "profit_each": 52.0, "margin": 0.52,
            "qty": 7, "qty_limit": "hub", "profit_per_day": 52.0, "demand_30": 5.0,
            "stock": 5, "days_of_stock": 1.0, "speed": "fast", "supply": "under",
            "other": {"source": "build", "profit_each": 22.0}}
    base.update(over)
    return base


# Two ok rows (one loses money), one with no recent sales.
ROWS = [
    _ok("Sample Hull", TYPE_ID),
    _ok("Losing Hull", 2002, hub="amarr", profit_each=-4.0, profit_per_day=-8.0, margin=-0.04),
    {"type_id": 2003, "name": "Quiet Hull", "status": "no_sales"},
]


async def save_result(factory, user_id, rows, *, kind="category", ref=GROUP_ID,
                      label="Sample Ships", status="complete", item_total=None,
                      item_scanned=None, routes=None, notes=None):
    async with factory() as db:
        await scan_results.save(
            db, user_id, kind, ref, label=label, status=status,
            item_total=len(rows) if item_total is None else item_total,
            item_scanned=len(rows) if item_scanned is None else item_scanned,
            settings=SETTINGS, notes=notes or [], routes=routes or {}, rows=rows,
            now=SCANNED)


async def _save_scan_prefs(factory, user_id, budget, share):
    async with factory() as db:
        await hub_prefs.save_scan_prefs(db, user_id, budget, share)


async def _get_prefs(factory, user_id):
    async with factory() as db:
        return await hub_prefs.get_prefs(db, user_id)


async def _add_watchlist(factory, user_id, name):
    async with factory() as db:
        wl = MarketWatchlist(user_id=user_id, name=name,
                             created_at=datetime(2026, 11, 1))
        db.add(wl)
        await db.commit()
        return wl.id


def _tag(html, marker):
    """The whole start tag containing `marker`."""
    i = html.index(marker)
    return html[html.rindex("<", 0, i):html.index(">", i) + 1]


def _result(client, query="kind=category&ref=10"):
    return client.get(f"/market/hub-scan/result?{query}").text


# ── access ─────────────────────────────────────────────────────────────────

def test_anonymous_is_turned_away(app_db):
    import app.main as main
    anon = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    page = anon.get("/market/hub-scan")
    assert page.status_code in (302, 307) and page.headers["location"] == "/"
    for path in ("/market/hub-scan/count", "/market/hub-scan/status",
                 "/market/hub-scan/result?kind=category&ref=10"):
        assert anon.get(path).status_code == 401, path
    posting = _anon_client()
    for path in ("/market/hub-scan/start", "/market/hub-scan/cancel"):
        assert posting.post(path, data={"kind": "category", "ref": "10"}).status_code == 401
    assert app_db.started == []


def test_post_without_csrf_is_refused(app_db):
    c = _authed_client()
    c.headers.pop("X-CSRF-Token")
    assert c.post("/market/hub-scan/start", data={"kind": "blueprints"}).status_code == 403
    assert c.post("/market/hub-scan/cancel").status_code == 403
    assert app_db.started == []


def test_result_is_per_user(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS))
    assert _result(_authed_client(OTHER_USER_ID)) == ""
    assert "Sample Hull" in _result(_authed_client(USER_ID))


def test_status_is_per_user(app_db):
    _authed_client(USER_ID).post("/market/hub-scan/start",
                                 data={"kind": "category", "ref": "10"})
    other = _authed_client(OTHER_USER_ID).get("/market/hub-scan/status?kind=category&ref=10").text
    assert "every 2s" not in other and "Scanning" not in other


# ── page ───────────────────────────────────────────────────────────────────

def test_page_shows_saved_inputs(app_db, factory):
    _run(_save_scan_prefs(factory, USER_ID, 2e9, 35))
    resp = _authed_client().get("/market/hub-scan")
    assert resp.status_code == 200
    html = resp.text
    assert 'value="2B"' in _tag(html, 'name="budget"')
    assert 'value="35"' in _tag(html, 'name="share"')
    assert 'id="hs-status"' in html and 'id="hs-result"' in html
    assert 'href="/market/hub-scan"' in html                     # the nav item


def test_page_defaults_and_numeric_inputs(app_db):
    html = _authed_client().get("/market/hub-scan").text
    assert 'value="500M"' in _tag(html, 'name="budget"')
    assert 'inputmode="decimal"' not in _tag(html, 'name="budget"')   # takes "1.5B"
    for name in ("share", "sales_tax", "broker_fee"):
        assert 'inputmode="decimal"' in _tag(html, f'name="{name}"')
    assert "checked" in _tag(html, 'id="hs-kind-category"')
    assert "Pick a list to scan." in html                       # nothing picked yet
    assert 'hx-get="/industry/build-finder/tree?parent=0"' in html


def test_page_with_a_list_in_the_address_loads_its_count_and_result(app_db):
    html = _authed_client().get("/market/hub-scan?kind=category&ref=10").text
    assert 'value="10"' in _tag(html, 'id="hs-ref"')
    assert "Sample Ships" in html                               # the chosen path
    assert "/market/hub-scan/count?kind=category&amp;ref=10" in html
    assert "/market/hub-scan/result?kind=category&amp;ref=10" in html


def test_page_with_an_active_job_shows_its_list_and_polls(app_db):
    _authed_client().post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    html = _authed_client().get("/market/hub-scan?kind=blueprints").text
    assert "checked" in _tag(html, 'id="hs-kind-category"')
    assert "checked" not in _tag(html, 'id="hs-kind-blueprints"')
    assert 'value="10"' in _tag(html, 'id="hs-ref"')
    assert "Sample Ships" in html
    status = _tag(html, 'id="hs-status"')
    assert 'hx-trigger="every 2s"' in status and 'data-htmx-no-error="1"' in status
    assert "kind=category&amp;ref=10&amp;was=1" in status
    assert "1 of 4 items" in html


def test_page_with_an_active_watchlist_job_selects_that_watchlist(app_db, factory):
    first = _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    second = _run(_add_watchlist(factory, USER_ID, "Sample List"))
    _authed_client().post("/market/hub-scan/start", data={"kind": "watchlist", "ref": str(second)})
    html = _authed_client().get("/market/hub-scan").text
    assert "checked" in _tag(html, 'id="hs-kind-watchlist"')
    assert "selected" in _tag(html, f'<option value="{second}"')
    assert "selected" not in _tag(html, f'<option value="{first}"')
    assert 'hidden' not in _tag(html, 'id="hs-pick-watchlist"')
    assert 'hidden' in _tag(html, 'id="hs-pick-category"')


def test_page_lists_only_the_users_watchlists(app_db, factory):
    _run(_add_watchlist(factory, USER_ID, "Sample List"))
    _run(_add_watchlist(factory, OTHER_USER_ID, "Other List"))
    html = _authed_client().get("/market/hub-scan").text
    assert "Sample List" in html and "Other List" not in html


def test_page_has_no_inline_event_handlers_and_scripts_carry_the_nonce(app_db):
    html = _authed_client().get("/market/hub-scan").text
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html)
    page = source("market_hub_scan.html")
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts and all('nonce="{{ request.state.csp_nonce }}"' in s for s in scripts)
    assert page.index("{% block content %}") < page.index("<script") < page.index("{% endblock %}", page.index("<script"))
    for partial in ("partials/hub_scan_result.html", "partials/hub_scan_status.html",
                    "partials/hub_scan_watchlist.html", "partials/hub_scan_wl_pick.html",
                    "partials/hub_scan_wl_found.html"):
        assert "<script" not in source(partial)


# ── count and status ───────────────────────────────────────────────────────

def test_count_messages(app_db, factory):
    c = _authed_client()
    assert c.get("/market/hub-scan/count?kind=category&ref=10").text == "1 item."
    assert "Pick a list" in c.get("/market/hub-scan/count?kind=category&ref=0").text
    assert "counted when the scan starts" in c.get("/market/hub-scan/count?kind=blueprints").text
    assert "no longer exists" in c.get("/market/hub-scan/count?kind=watchlist&ref=77").text
    assert "no longer exists" in c.get("/market/hub-scan/count?kind=category&ref=99").text
    theirs = _run(_add_watchlist(factory, OTHER_USER_ID, "Other List"))
    assert "no longer exists" in c.get(f"/market/hub-scan/count?kind=watchlist&ref={theirs}").text


def test_idle_status_loads_count_and_result(app_db):
    html = _authed_client().get("/market/hub-scan/status?kind=category&ref=10").text
    assert "every 2s" not in html and "data-htmx-no-error" not in _tag(html, 'id="hs-status"')
    assert "/market/hub-scan/count?kind=category&amp;ref=10" in html
    assert "/market/hub-scan/result?kind=category&amp;ref=10" in html


def test_status_without_a_list_says_pick_one(app_db):
    html = _authed_client().get("/market/hub-scan/status?kind=category&ref=0").text
    assert "Pick a list to scan." in html
    assert "/market/hub-scan/count" not in html


def test_lost_job_says_interrupted(app_db):
    html = _authed_client().get("/market/hub-scan/status?kind=category&ref=10&was=1").text
    assert "interrupted" in html
    assert "interrupted" not in _authed_client().get(
        "/market/hub-scan/status?kind=category&ref=10").text


def test_active_status_polls_every_2s_and_opts_out_of_the_error_pill(app_db):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    html = c.get("/market/hub-scan/status?kind=category&ref=10").text
    status = _tag(html, 'id="hs-status"')
    for attr in ('hx-get="/market/hub-scan/status?kind=category&amp;ref=10&amp;was=1"',
                 'hx-trigger="every 2s"', 'hx-swap="outerHTML"', 'data-htmx-no-error="1"'):
        assert attr in status
    assert "1 of 4 items" in html and "Scanning Sample Ships" in html
    cancel = _tag(html, 'hx-post="/market/hub-scan/cancel"')
    assert 'data-htmx-no-error="1"' in cancel and 'hx-target="#hs-status"' in cancel
    assert "/market/hub-scan/result" not in html                 # loads when it ends


def test_active_status_belongs_to_the_running_job(app_db):
    """Picking another list mid-scan still shows (and polls) the running one."""
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    html = c.get("/market/hub-scan/status?kind=blueprints&ref=0").text
    assert "kind=category&amp;ref=10&amp;was=1" in html


def test_finished_job_stops_polling_and_loads_the_result(app_db):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    scan_runner._jobs[USER_ID].state = "done"
    html = c.get("/market/hub-scan/status?kind=category&ref=10&was=1").text
    assert "every 2s" not in html and "interrupted" not in html
    loader = _tag(html, "/market/hub-scan/result?kind=category&amp;ref=10")
    assert 'hx-trigger="load"' in loader and 'hx-target="#hs-result"' in loader


def test_failed_or_cancelled_job_is_mentioned_only_for_its_own_list(app_db):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    job = scan_runner._jobs[USER_ID]
    job.state, job.message = "failed", "The scan failed. Scan again."
    assert "The scan failed" in c.get("/market/hub-scan/status?kind=category&ref=10").text
    assert "The scan failed" not in c.get("/market/hub-scan/status?kind=blueprints").text
    job.state = "cancelled"
    assert "Scan cancelled." in c.get("/market/hub-scan/status?kind=category&ref=10").text
    assert "Scan cancelled." not in c.get("/market/hub-scan/status?kind=category&ref=11").text


def test_cancel_returns_the_status_line(app_db):
    c = _authed_client()
    r = c.post("/market/hub-scan/cancel")
    assert r.status_code == 200 and 'id="hs-status"' in r.text
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    assert 'id="hs-status"' in c.post("/market/hub-scan/cancel").text


# ── start ──────────────────────────────────────────────────────────────────

def test_start_saves_preferences_and_returns_polling_status(app_db, factory):
    c = _authed_client()
    r = c.post("/market/hub-scan/start", data={
        "kind": "category", "ref": "10", "budget": "1.5B", "share": "30",
        "sales_tax": "4", "broker_fee": "1", "origin": "Sample System"})
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' in r.text and "1 of 4 items" in r.text
    started = app_db.started[-1]
    assert (started["user_id"], started["kind"], started["ref"]) == (USER_ID, "category", 10)
    s = started["settings"]
    assert (s.budget_isk, s.share_pct, s.sales_tax_pct, s.broker_fee_pct) == (1.5e9, 30.0, 4.0, 1.0)
    assert (s.origin_system_id, s.origin_name) == (ORIGIN_ID, "Sample System")
    prefs = _run(_get_prefs(factory, USER_ID))
    assert (prefs.scan_budget_isk, prefs.market_share_pct, prefs.sales_tax_pct,
            prefs.broker_fee_pct, prefs.origin_system_id) == (1.5e9, 30.0, 4.0, 1.0, ORIGIN_ID)


def test_start_clamps_and_falls_back(app_db, factory):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={
        "kind": "blueprints", "ref": "55", "budget": "junk", "share": "500",
        "sales_tax": "", "broker_fee": "", "origin": ""})
    s = app_db.started[-1]["settings"]
    assert app_db.started[-1]["ref"] == 0                       # blueprints are ref 0
    assert s.budget_isk == hub_prefs.DEFAULT_SCAN_BUDGET_ISK and s.share_pct == 100.0
    assert s.sales_tax_pct == hub_prefs.DEFAULT_SALES_TAX_PCT
    assert s.origin_system_id is None


def test_start_with_an_unknown_system_keeps_the_saved_one(app_db, factory):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10",
                                           "origin": "Sample System"})
    scan_runner._jobs[USER_ID].state = "done"
    r = c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10",
                                               "origin": "Nowhere Sample"})
    assert "Unknown system: Nowhere Sample." in r.text
    s = app_db.started[-1]["settings"]
    assert (s.origin_system_id, s.origin_name) == (ORIGIN_ID, "Sample System")
    assert _run(_get_prefs(factory, USER_ID)).origin_system_id == ORIGIN_ID


def test_start_without_a_list_does_not_start(app_db):
    r = _authed_client().post("/market/hub-scan/start", data={"kind": "category", "ref": "0"})
    assert r.status_code == 200
    assert "Pick a list" in r.text and 'id="hs-status"' in r.text
    assert app_db.started == []


def test_start_while_another_scan_runs_says_so(app_db):
    c = _authed_client()
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": "10"})
    r = c.post("/market/hub-scan/start", data={"kind": "blueprints"})
    assert "Your other scan is still running" in r.text
    assert "kind=category&amp;ref=10&amp;was=1" in r.text


# ── result ─────────────────────────────────────────────────────────────────

def test_result_renders_filters_and_counts(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS))
    c = _authed_client()
    html = _result(c)
    assert "Sample Hull" in html and "Losing Hull" not in html
    assert "Showing 1 of 2" in html
    assert "1 hidden: lose money or no sales this week" in html
    assert "1 skipped: no recent sales" in html
    assert "no cost" not in html and "unavailable" not in html
    assert 'href="/market/type/2001?qty=7&amp;cost=100.00#mk-hubs"' in html
    assert "Sample Ships</b> — scanned 2026-11-10 14:05 EVE time · 3 items" in html
    assert "Hide items with no profit per day" in html
    assert "checked" in _tag(html, 'id="hs-losers"')
    assert "disabled" in _tag(html, 'id="hs-losers-off"')
    shown = _result(c, "kind=category&ref=10&losers=show")
    assert "Losing Hull" in shown and "hidden:" not in shown
    assert "Showing 2 of 2" in shown
    assert "checked" not in _tag(shown, 'id="hs-losers"')
    assert "disabled" not in _tag(shown, 'id="hs-losers-off"')


def test_result_filters_by_hub_source_and_supply(app_db, factory):
    rows = [_ok("Sample Hull", TYPE_ID),
            _ok("Build Hull", 2004, hub="amarr", source="build", cost_basis="benchmark",
                supply=None, profit_per_day=90.0)]
    _run(save_result(factory, USER_ID, rows))
    c = _authed_client()
    amarr = _result(c, "kind=category&ref=10&hub=amarr")
    assert "Build Hull" in amarr and "Sample Hull" not in amarr
    assert "selected" in _tag(amarr, '<option value="amarr"')
    haul = _result(c, "kind=category&ref=10&source=haul")
    assert "Sample Hull" in haul and "Build Hull" not in haul
    under = _result(c, "kind=category&ref=10&under=1")
    assert "Sample Hull" in under and "Build Hull" not in under
    assert "checked" in _tag(under, 'name="under"')
    # The build row's link carries no cost: the item page works out the benchmark.
    assert 'href="/market/type/2004?qty=7#mk-hubs"' in _result(c)


def test_result_sorts_by_the_sort_parameter(app_db, factory):
    rows = [_ok("Zeta Hull", 2005, profit_per_day=5.0),
            _ok("Alpha Hull", 2006, profit_per_day=9.0, margin=0.1)]
    _run(save_result(factory, USER_ID, rows))
    c = _authed_client()

    def order(query):
        html = _result(c, query)
        table = html[html.index('<table class="hs-table">'):html.index("</table>")]
        return re.findall(r'class="hs-item"[^>]*>([^<]+)<', table)

    assert order("kind=category&ref=10") == ["Alpha Hull", "Zeta Hull"]
    assert order("kind=category&ref=10&sort=profit_day&dir=asc") == ["Zeta Hull", "Alpha Hull"]
    assert order("kind=category&ref=10&sort=margin") == ["Zeta Hull", "Alpha Hull"]
    assert order("kind=category&ref=10&sort=item") == ["Alpha Hull", "Zeta Hull"]
    html = _result(c, "kind=category&ref=10&sort=margin")
    assert 'value="margin"' in _tag(html, 'name="sort"')
    assert 'value="desc"' in _tag(html, 'name="dir"')
    assert "is-on" in _tag(html, 'data-sort="margin"')
    assert 'aria-sort="descending"' in html


def test_result_columns_match_the_spec(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS, routes={"rens": {"jumps": 20, "highsec": True},
                                                     "amarr": None}))
    html = _result(_authed_client())
    head = html[html.index("<thead>"):html.index("</thead>")]
    labels = [re.sub(r"<[^>]+>|[▼▲]", "", h).strip()
              for h in re.findall(r"<th\b[^>]*>(.*?)</th>", head, re.S)]
    assert labels == ["Item", "Best hub", "Source", "Cost each", "Sells for", "Profit each",
                      "Margin", "Sold / day", "Stock · days", "Quantity", "Profit / day",
                      "Route"]
    assert "20 j" in html and "highsec" in html
    for th in re.findall(r"<th\b[^>]*>", head):
        assert 'scope="col"' in th


def test_route_column_only_with_a_starting_system(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS))
    html = _result(_authed_client())
    assert ">Route<" not in html and "<span>Route</span>" not in html


def test_routes_unavailable_note(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS, routes={"rens": {"unavailable": True},
                                                     "amarr": {"jumps": 5, "highsec": True}}))
    html = _result(_authed_client())
    assert hub_view.ROUTES_UNAVAILABLE_NOTE in unescape(html)
    assert ">—<" in html                                       # the route cell


def test_incomplete_result_says_so(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS, status="incomplete", item_scanned=1, item_total=3))
    html = _result(_authed_client())
    assert "incomplete: 1 of 3 scanned" in html
    _run(save_result(factory, USER_ID, ROWS, status="incomplete",
                     item_scanned=120, item_total=412,
                     notes=["300 of 412 scanned; pick a narrower list to cover the rest."]))
    html = _result(_authed_client())
    assert "incomplete: 120 of 300 scanned" in html
    assert "300 of 412 scanned; pick a narrower list" in html


def test_unavailable_items_offer_a_retry(app_db, factory):
    rows = ROWS + [{"type_id": 2007, "name": "Lost Hull", "status": "unavailable"},
                   {"type_id": 2008, "name": "Costless Hull", "status": "no_cost"}]
    _run(save_result(factory, USER_ID, rows))
    html = _result(_authed_client())
    assert "1 skipped: no cost" in html and "1 unavailable (" in html
    retry = _tag(html, ">retry these<")
    for attr in ('hx-post="/market/hub-scan/start"', 'hx-include="#hs-form"',
                 'hx-target="#hs-status"', 'hx-swap="outerHTML"', 'data-htmx-no-error="1"'):
        assert attr in retry
    assert '"kind": "category"' in retry and '"ref": 10' in retry
    # Outside the filter form: a POST would send that form too.
    filters = html[html.index('<form id="hs-filters"'):html.index("</form>")]
    assert "retry these" not in filters and "Rescan" not in filters


def test_result_names_are_escaped(app_db, factory):
    rows = [_ok("<script>alert(1)</script>", TYPE_ID)]
    _run(save_result(factory, USER_ID, rows, label="<b>Sample</b>",
                     notes=["<img src=x>"]))
    html = _result(_authed_client())
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>Sample</b>" not in html and "&lt;b&gt;Sample&lt;/b&gt;" in html
    assert "<img src=x>" not in html


def test_page_escapes_watchlist_names(app_db, factory):
    _run(_add_watchlist(factory, USER_ID, "<script>alert(2)</script>"))
    html = _authed_client().get("/market/hub-scan?kind=watchlist").text
    assert "<script>alert(2)" not in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


def test_missing_result_is_empty(app_db):
    c = _authed_client()
    assert _result(c) == ""
    assert _result(c, "kind=category&ref=0") == ""


# ── phones ─────────────────────────────────────────────────────────────────

def test_table_for_desktop_cards_for_phones(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS, routes={"rens": {"jumps": 20, "highsec": True}}))
    html = _result(_authed_client(), "kind=category&ref=10&losers=show")
    assert re.search(r'<div class="hs-scroll m-hide">\s*<table class="hs-table">', html)
    assert '<div class="hs-cards m-only">' in html
    cards = html[html.index('<div class="hs-cards m-only">'):]
    assert cards.count('<div class="hs-card">') == 2
    assert "+52.00 / day" in cards and "Build instead: +22.00 each" in cards
    assert "<span>Route</span>20 j highsec" in cards


def _phone_block(css):
    """The body of the page's @media (max-width: 640px) block, brace-matched."""
    start = css.index("@media (max-width: 640px)")
    i = css.index("{", start) + 1
    depth = 1
    for j in range(i, len(css)):
        depth += {"{": 1, "}": -1}.get(css[j], 0)
        if depth == 0:
            return css[i:j]
    raise AssertionError("unclosed phone block")


def test_phone_css_sizes_inputs_and_nothing_overflows_320px():
    css = source("market_hub_scan.html")
    phone = _phone_block(css)
    rule = re.search(r"\.hs-controls \.b-input, \.hs-filters \.b-input\s*\{([^}]*)\}", phone)
    assert rule, "phone rule for the inputs"
    body = rule.group(1).replace(" ", "")
    assert "min-height:44px" in body and "font-size:16px" in body and "width:100%" in body
    assert re.search(r"\.hs-scroll\s*\{[^}]*overflow-x:\s*auto", css)
    assert re.search(r"\.hs-table\s*\{[^}]*min-width:\s*1080px", css)
    # Apart from the table (which scrolls inside .hs-scroll and is hidden on
    # phones), nothing is given a fixed width a 320px phone cannot hold.
    for selector, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if ".hs-table" in selector:
            continue
        for px in re.findall(r"(?:^|;|\s)(?:min-)?width:\s*(\d+)px", decls):
            assert int(px) <= 280, (selector.strip(), px)


def test_rendered_markup_has_no_fixed_inline_widths(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS))
    c = _authed_client()
    for html in (c.get("/market/hub-scan").text, _result(c)):
        for style in re.findall(r'style="([^"]*)"', html):
            for px in re.findall(r"width:\s*(\d+)px", style):
                assert int(px) <= 280, style


# ── watchlists ─────────────────────────────────────────────────────────────

async def _first_watchlist_id(factory, user_id):
    async with factory() as db:
        return (await watchlists.all_for(db, user_id))[0].id


async def _watchlist_ids(factory, user_id):
    async with factory() as db:
        return {w.name: w.id for w in await watchlists.all_for(db, user_id)}


async def _item_ids(factory, wid):
    async with factory() as db:
        return sorted(await watchlists.item_ids(db, wid))


async def _add_types(factory, *types):
    """(type_id, name, market_group_id) rows; a group outside GROUP_ID's
    subtree, so category 10 still counts one item."""
    async with factory() as db:
        db.add_all([SDEType(type_id=t, type_name=n, published=True, market_group_id=g,
                            group_id=1) for t, n, g in types])
        await db.commit()


async def _load_result(factory, user_id, kind, ref):
    async with factory() as db:
        return await scan_results.load(db, user_id, kind, ref)


def _current(html):
    return int(re.search(r'data-current="(\d+)"', _tag(html, 'id="hs-watchlists"')).group(1))


def test_watchlist_lifecycle_through_the_routes(app_db, factory):
    c = _authed_client(USER_ID)
    html = c.post("/market/watchlists", data={"name": "  Frigates  "}).text
    assert "Frigates" in html and "List created." in html
    wid = _run(_first_watchlist_id(factory, USER_ID))
    assert _current(html) == wid
    assert "Added." in c.post(f"/market/watchlists/{wid}/items", data={"type_id": "2001"}).text
    assert "Sample Hull" in c.get(f"/market/watchlists?id={wid}").text
    assert c.get(f"/market/hub-scan/count?kind=watchlist&ref={wid}").text == "1 item."
    html = c.post(f"/market/watchlists/{wid}/rename", data={"name": "Hulls"}).text
    assert "Renamed." in html and 'value="Hulls"' in _tag(html, 'id="hs-wl-rename"')
    html = c.post(f"/market/watchlists/{wid}/items/2001/delete").text
    assert "Removed." in html and "No items yet." in html
    assert c.get(f"/market/hub-scan/count?kind=watchlist&ref={wid}").text == "0 items."
    html = c.post(f"/market/watchlists/{wid}/delete").text
    assert "List deleted." in html and _current(html) == 0
    assert "no longer exists" in c.get(f"/market/hub-scan/count?kind=watchlist&ref={wid}").text


def test_manager_without_an_id_shows_the_first_list(app_db, factory):
    c = _authed_client()
    assert _current(c.get("/market/watchlists").text) == 0
    _run(_add_watchlist(factory, USER_ID, "Beta List"))
    alpha = _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    for query in ("", "?id=", "?id=0"):
        assert _current(c.get(f"/market/watchlists{query}").text) == alpha


def test_manager_reply_brings_the_picker_out_of_band(app_db, factory):
    c = _authed_client()
    first = c.post("/market/watchlists", data={"name": "Alpha List"}).text
    html = c.post("/market/watchlists", data={"name": "Beta List"}).text
    ids = _run(_watchlist_ids(factory, USER_ID))
    pick = html[html.index('<div id="hs-wl-pick"'):]
    assert 'hx-swap-oob="true"' in _tag(pick, 'id="hs-wl-pick"')
    assert "selected" in _tag(pick, f'<option value="{ids["Beta List"]}"')
    assert "selected" not in _tag(pick, f'<option value="{ids["Alpha List"]}"')
    # After the manager in the reply, never inside it.
    assert html.index('id="hs-wl-pick"') > html.index('id="hs-watchlists"')
    assert 'id="hs-wl-pick"' in first
    # The last list deleted: the picker says there are none.
    for wid in ids.values():
        html = c.post(f"/market/watchlists/{wid}/delete").text
    assert "You have no watchlists yet." in html and _current(html) == 0


def test_duplicate_name_shows_the_message_and_keeps_the_current_list(app_db, factory):
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Same"})
    c.post("/market/watchlists", data={"name": "Zulu"})
    ids = _run(_watchlist_ids(factory, USER_ID))
    html = c.post("/market/watchlists", data={"name": "same", "current": str(ids["Zulu"])}).text
    assert "already have a list" in html and _current(html) == ids["Zulu"]
    assert "Give the list a name." in c.post("/market/watchlists", data={"name": "  "}).text
    html = c.post(f"/market/watchlists/{ids['Zulu']}/rename", data={"name": "SAME"}).text
    assert "already have a list" in html and _current(html) == ids["Zulu"]
    assert set(_run(_watchlist_ids(factory, USER_ID))) == {"Same", "Zulu"}


def test_list_limit_is_the_modules(app_db, factory, monkeypatch):
    monkeypatch.setattr(watchlists, "MAX_LISTS", 1)
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Only"})
    assert "up to 1 watchlists" in c.post("/market/watchlists", data={"name": "More"}).text
    assert set(_run(_watchlist_ids(factory, USER_ID))) == {"Only"}


def test_add_item_messages(app_db, factory, monkeypatch):
    _run(_add_types(factory, (2002, "Spare Hull", 11)))
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Hulls"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    add = f"/market/watchlists/{wid}/items"
    assert "Added." in c.post(add, data={"type_id": "2001"}).text
    assert "Already on the list." in c.post(add, data={"type_id": "2001"}).text
    for junk in ("424242", "abc", "", "-1", "99999999999999999999"):
        r = c.post(add, data={"type_id": junk})
        assert r.status_code == 200 and "That is not a market item." in r.text, junk
    monkeypatch.setattr(watchlists, "MAX_ITEMS", 1)
    assert "The list is full (1 items)." in c.post(add, data={"type_id": "2002"}).text
    assert _run(_item_ids(factory, wid)) == [TYPE_ID]


def test_other_users_watchlist_is_404_everywhere(app_db, factory):
    _authed_client(USER_ID).post("/market/watchlists", data={"name": "Mine"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    _authed_client(USER_ID).post(f"/market/watchlists/{wid}/items", data={"type_id": "2001"})
    _run(save_result(factory, USER_ID, ROWS, kind="watchlist", ref=wid, label="Mine"))
    other = _authed_client(OTHER_USER_ID)
    other.post("/market/watchlists", data={"name": "Theirs"})
    for path, data in ((f"/market/watchlists/{wid}/rename", {"name": "x"}),
                       (f"/market/watchlists/{wid}/delete", {}),
                       (f"/market/watchlists/{wid}/items", {"type_id": "2001"}),
                       (f"/market/watchlists/{wid}/items/2001/delete", {}),
                       (f"/market/watchlists/{wid}/add-result", {"kind": "category", "ref": "10"}),
                       ("/market/watchlists/add-item", {"watchlist_id": str(wid), "type_id": "2001"})):
        assert other.post(path, data=data).status_code == 404, path
    for path in (f"/market/watchlists?id={wid}", f"/market/watchlists/search?wid={wid}&q=Sample"):
        assert other.get(path).status_code == 404, path
    # Nothing of the owner's changed.
    assert _run(_watchlist_ids(factory, USER_ID)) == {"Mine": wid}
    assert _run(_item_ids(factory, wid)) == [TYPE_ID]
    assert _run(_load_result(factory, USER_ID, "watchlist", wid)) is not None


def test_junk_and_out_of_range_ids_are_404_not_500(app_db, factory):
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Mine"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    for bad in ("abc", "0", "-1", "1.5", "99999999999999999999", str(2 ** 31)):
        for path, data in ((f"/market/watchlists/{bad}/rename", {"name": "x"}),
                           (f"/market/watchlists/{bad}/delete", {}),
                           (f"/market/watchlists/{bad}/items", {"type_id": "2001"}),
                           (f"/market/watchlists/{bad}/items/2001/delete", {}),
                           (f"/market/watchlists/{wid}/items/{bad}/delete", {}),
                           (f"/market/watchlists/{bad}/add-result", {"kind": "category", "ref": "10"}),
                           ("/market/watchlists/add-item", {"watchlist_id": bad, "type_id": "2001"})):
            assert c.post(path, data=data).status_code == 404, (path, data)
        assert c.get(f"/market/watchlists/search?wid={bad}&q=Sample").status_code == 404
        if bad != "0":
            assert c.get(f"/market/watchlists?id={bad}").status_code == 404, bad
    # The page's own list parameters stay out of SQLite's range too.
    huge = "99999999999999999999"
    assert c.get(f"/market/hub-scan/count?kind=watchlist&ref={huge}").text == "Pick a list to scan."
    assert c.get(f"/market/hub-scan/result?kind=category&ref={huge}").text == ""
    assert c.get(f"/market/hub-scan?kind=watchlist&ref={huge}").status_code == 200
    r = c.post(f"/market/watchlists/{wid}/add-result", data={"kind": "category", "ref": huge})
    assert r.status_code == 200 and r.text == "There is no saved result to add."


def test_watchlist_routes_need_a_session(app_db, factory):
    import app.main as main
    _authed_client().post("/market/watchlists", data={"name": "Mine"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    anon = TestClient(main.app, base_url="https://testserver")
    for path in ("/market/watchlists", f"/market/watchlists?id={wid}",
                 f"/market/watchlists/search?wid={wid}&q=Sample"):
        assert anon.get(path).status_code == 401, path
    posting = _anon_client()
    for path, data in (("/market/watchlists", {"name": "Sneaky"}),
                       (f"/market/watchlists/{wid}/rename", {"name": "x"}),
                       (f"/market/watchlists/{wid}/delete", {}),
                       (f"/market/watchlists/{wid}/items", {"type_id": "2001"}),
                       (f"/market/watchlists/{wid}/items/2001/delete", {}),
                       (f"/market/watchlists/{wid}/add-result", {"kind": "category", "ref": "10"}),
                       ("/market/watchlists/add-item", {"watchlist_id": str(wid), "type_id": "2001"})):
        assert posting.post(path, data=data).status_code == 401, path
    assert _run(_watchlist_ids(factory, USER_ID)) == {"Mine": wid}


def test_watchlist_posts_without_csrf_are_refused(app_db, factory):
    c = _authed_client()
    c.headers.pop("X-CSRF-Token")
    assert c.post("/market/watchlists", data={"name": "Mine"}).status_code == 403
    assert _run(_watchlist_ids(factory, USER_ID)) == {}


def test_add_all_from_a_result(app_db, factory, monkeypatch):
    _run(_add_types(factory, (2002, "Losing Hull", 11)))
    _run(save_result(factory, USER_ID, ROWS))
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "From scan"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    add = f"/market/watchlists/{wid}/add-result"
    # Both priced rows (the losing one too); the row with no sales is not added.
    assert c.post(add, data={"kind": "category", "ref": "10"}).text == "Added 2."
    assert _run(_item_ids(factory, wid)) == [TYPE_ID, 2002]
    assert c.post(add, data={"kind": "category", "ref": "10"}).text.startswith("Nothing new to add")
    c.post("/market/watchlists", data={"name": "Small"})
    small = _run(_watchlist_ids(factory, USER_ID))["Small"]
    monkeypatch.setattr(watchlists, "MAX_ITEMS", 1)
    text = c.post(f"/market/watchlists/{small}/add-result",
                  data={"kind": "category", "ref": "10"}).text
    assert text == "Added 1; 1 did not fit (1 items per list)."
    # Only the user's own saved result is read.
    _run(save_result(factory, OTHER_USER_ID, ROWS, ref=11))
    assert c.post(add, data={"kind": "category", "ref": "11"}).text == \
        "There is no saved result to add."


def test_result_offers_add_all_outside_the_filters(app_db, factory):
    _run(save_result(factory, USER_ID, ROWS))
    c = _authed_client()
    assert "hs-add-all" not in _result(c)                   # no lists yet
    first = _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    second = _run(_add_watchlist(factory, USER_ID, "Beta List"))
    html = _result(c)
    button = _tag(html, 'class="b-btn hs-add-all"')
    for attr in (f'hx-post="/market/watchlists/{first}/add-result"', 'hx-target="#hs-add-msg"',
                 'hx-swap="innerHTML"', 'data-htmx-no-error="1"'):
        assert attr in button
    assert '"kind": "category"' in button and '"ref": 10' in button
    assert "Add all 2 items to watchlist" in html
    assert f'<option value="{second}">Beta List</option>' in html
    assert 'name=' not in _tag(html, 'id="hs-add-wid"')
    filters = html[html.index('<form id="hs-filters"'):html.index("</form>")]
    assert "hs-add-all" not in filters and "hs-add-wid" not in filters
    # Nothing priced: nothing to add.
    _run(save_result(factory, USER_ID, [ROWS[2]]))
    assert "hs-add-all" not in _result(c)


def test_page_script_sets_the_add_all_path_from_the_select():
    page = source("market_hub_scan.html")
    i = page.index("htmx:configRequest")
    block = page[i:page.index("});", i)]
    assert "hs-add-all" in block and "hs-add-wid" in block and "e.detail.path" in block


def test_delete_cancels_only_a_matching_scan(app_db, factory, monkeypatch):
    cancelled = []
    monkeypatch.setattr(scan_runner, "cancel", lambda user_id: cancelled.append(user_id) or True)
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Alpha List"})
    c.post("/market/watchlists", data={"name": "Beta List"})
    c.post("/market/watchlists", data={"name": "Gamma List"})
    ids = _run(_watchlist_ids(factory, USER_ID))
    c.post("/market/hub-scan/start", data={"kind": "watchlist", "ref": str(ids["Alpha List"])})
    c.post(f"/market/watchlists/{ids['Beta List']}/delete")
    assert cancelled == []                                     # another list's scan runs on
    c.post(f"/market/watchlists/{ids['Alpha List']}/delete")
    assert cancelled == [USER_ID]
    # A category scan whose ref happens to equal a list id is not that list.
    scan_runner._jobs[USER_ID].state = "done"
    c.post("/market/hub-scan/start", data={"kind": "category", "ref": str(ids["Gamma List"])})
    c.post(f"/market/watchlists/{ids['Gamma List']}/delete")
    assert cancelled == [USER_ID]
    # A finished scan of the list is not cancelled either.
    c.post("/market/watchlists", data={"name": "Delta List"})
    delta = _run(_watchlist_ids(factory, USER_ID))["Delta List"]
    scan_runner._jobs[USER_ID].state = "done"
    c.post("/market/hub-scan/start", data={"kind": "watchlist", "ref": str(delta)})
    scan_runner._jobs[USER_ID].state = "done"
    c.post(f"/market/watchlists/{delta}/delete")
    assert cancelled == [USER_ID]


def test_delete_removes_items_and_saved_result(app_db, factory):
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Doomed"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    c.post(f"/market/watchlists/{wid}/items", data={"type_id": "2001"})
    _run(save_result(factory, USER_ID, ROWS, kind="watchlist", ref=wid, label="Doomed"))
    _run(save_result(factory, USER_ID, ROWS))
    c.post(f"/market/watchlists/{wid}/delete")
    assert _run(_item_ids(factory, wid)) == []
    assert _run(_load_result(factory, USER_ID, "watchlist", wid)) is None
    assert _run(_load_result(factory, USER_ID, "category", GROUP_ID)) is not None


def test_search_offers_market_items_and_escapes_wildcards(app_db, factory):
    _run(_add_types(factory, (2002, "Sample Rig", 11), (2003, "Sample Relic", None)))
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Mine"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    html = c.get(f"/market/watchlists/search?wid={wid}&q=sample").text
    assert "Sample Hull" in html and "Sample Rig" in html and "Sample Relic" not in html
    add = _tag(html, 'aria-label="Add Sample Hull"')
    for attr in (f'hx-post="/market/watchlists/{wid}/items"', 'hx-target="#hs-watchlists"',
                 'hx-swap="outerHTML"', 'hx-include="#hs-wl-q"', 'data-htmx-no-error="1"'):
        assert attr in add
    assert '"type_id": 2001' in add
    for q in ("%%", "__", "%_%"):
        found = c.get(f"/market/watchlists/search?wid={wid}&q={q}").text
        assert "<button" not in found and "No market item matches" in found, q
    assert "at least 2 letters" in c.get(f"/market/watchlists/search?wid={wid}&q=s").text
    assert c.get(f"/market/watchlists/search?wid={wid}&q=").text.strip() == ""
    assert c.get(f"/market/watchlists/search?wid={wid}&q={'x' * 5000}").status_code == 200
    # Adding from the search keeps it open, now marking the item as on the list.
    html = c.post(f"/market/watchlists/{wid}/items", data={"type_id": "2001", "q": "sample"}).text
    assert 'value="sample"' in _tag(html, 'id="hs-wl-q"')
    found = html[html.index('id="hs-wl-found"'):]
    assert "Sample Rig" in found and 'aria-label="Add Sample Hull"' not in found
    assert "on the list" in found


def test_manager_markup(app_db, factory):
    c = _authed_client()
    c.post("/market/watchlists", data={"name": "Mine"})
    wid = _run(_first_watchlist_id(factory, USER_ID))
    c.post(f"/market/watchlists/{wid}/items", data={"type_id": "2001"})
    html = c.get(f"/market/watchlists?id={wid}").text
    assert "<form" not in html                            # nothing to submit natively
    root = _tag(html, 'id="hs-watchlists"')
    assert 'data-htmx-no-error="1"' in root
    for tag in re.findall(r"<(?:button|input)\b[^>]*hx-(?:get|post)=[^>]*>", html):
        assert 'data-htmx-no-error="1"' in tag, tag
        assert 'hx-target="#hs-' in tag, tag
    for tag in re.findall(r"<button\b[^>]*>", html):
        assert 'type="button"' in tag
    delete = _tag(html, f'hx-post="/market/watchlists/{wid}/delete"')
    assert 'hx-confirm="Delete Mine and its saved scan?"' in delete
    remove = _tag(html, f'hx-post="/market/watchlists/{wid}/items/2001/delete"')
    assert 'aria-label="Remove Sample Hull"' in remove and 'hx-include="#hs-wl-q"' in remove
    search = _tag(html, 'id="hs-wl-q"')
    assert f'hx-get="/market/watchlists/search?wid={wid}"' in search
    assert 'hx-target="#hs-wl-found"' in search
    assert "1 of 300 items" in html and "1 of 20 lists" in html
    assert 'role="status"' in _tag(html, 'id="hs-wl-msg"')
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html)
    # Every field has a bound label.
    for tag in re.findall(r'<input\b[^>]*type="text"[^>]*>', html):
        field = re.search(r'\bid="([^"]+)"', tag).group(1)
        assert f'for="{field}"' in html, field


def test_page_in_watchlist_mode_shows_the_manager_for_the_picked_list(app_db, factory):
    alpha = _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    beta = _run(_add_watchlist(factory, USER_ID, "Beta List"))
    c = _authed_client()
    html = c.get(f"/market/hub-scan?kind=watchlist&ref={beta}").text
    assert "hidden" not in _tag(html, 'id="hs-wl-box"')
    assert _current(html) == beta and 'value="Beta List"' in _tag(html, 'id="hs-wl-rename"')
    assert "selected" in _tag(html, f'<option value="{beta}"')
    # Drawn into the page: no out-of-band copy of the picker, one picker only.
    assert html.count('id="hs-wl-pick"') == 1 and "hx-swap-oob" not in html
    # The manager is outside the scan form.
    start = html.index('<form id="hs-form"')
    form = html[start:html.index("</form>", start)]
    assert 'id="hs-watchlists"' not in form and 'id="hs-wl-pick"' in form
    # A list that is not the user's (or none): the first one, as the picker shows.
    for ref in ("", "0", "424242"):
        html = c.get(f"/market/hub-scan?kind=watchlist&ref={ref}").text
        assert 'value="%d"' % alpha in _tag(html, 'id="hs-ref"'), ref
        assert _current(html) == alpha
    # Another kind: the manager is there but hidden.
    html = c.get("/market/hub-scan?kind=category&ref=10").text
    assert "hidden" in _tag(html, 'id="hs-wl-box"')


def test_page_script_keeps_ref_in_step_with_the_manager():
    page = source("market_hub_scan.html")
    i = page.index("document.body.addEventListener('htmx:afterSwap'")
    block = page[i:page.index("});", i)]
    for needle in ("hs-watchlists", "data-current", "ref.value", "showStatus('watchlist'"):
        assert needle in block, needle
    assert "loadManager(t.value)" in page and "'/market/watchlists?id='" in page


def test_watchlist_names_are_escaped_everywhere(app_db, factory):
    nasty = ("<script>alert(1)</script>", '"><img src=x>')
    for name in nasty:
        _run(_add_watchlist(factory, USER_ID, name))
    _run(_add_types(factory, (2002, '<b>Bold</b> "Hull"', 11)))
    ids = _run(_watchlist_ids(factory, USER_ID))
    c = _authed_client()
    _run(save_result(factory, USER_ID, ROWS))
    pages = {}
    for name, wid in ids.items():
        c.post(f"/market/watchlists/{wid}/items", data={"type_id": "2002"})
        pages[f"manager {wid}"] = c.get(f"/market/watchlists?id={wid}").text
        pages[f"page {wid}"] = c.get(f"/market/hub-scan?kind=watchlist&ref={wid}").text
    pages["search"] = c.get(f"/market/watchlists/search?wid={wid}&q=bold").text
    pages["result"] = _result(c)
    pages["item page"] = c.get(f"/market/type/{TYPE_ID}").text
    pages["created"] = c.post("/market/watchlists", data={"name": "<i>x</i>"}).text
    for where, html in pages.items():
        for raw in ("<script>alert(1)", '"><img src=x>', "<img src=x>", "<b>Bold</b>", "<i>x</i>"):
            assert raw not in html, (where, raw)
    for key in ("manager", "page"):
        html = next(h for w, h in pages.items() if w.startswith(key))
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&#34;&gt;&lt;img src=x&gt;" in pages["result"]                    # an <option>
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages["item page"]
    assert "&lt;b&gt;Bold&lt;/b&gt;" in pages["search"]
    assert "&lt;i&gt;x&lt;/i&gt;" in pages["created"]
    # Attributes: the rename field, the delete confirmation, item aria-labels.
    wid = ids['"><img src=x>']
    html = pages[f"manager {wid}"]
    assert 'value="&#34;&gt;&lt;img src=x&gt;"' in _tag(html, 'id="hs-wl-rename"')
    assert 'hx-confirm="Delete &#34;&gt;&lt;img src=x&gt; and its saved scan?"' in html
    assert 'aria-label="Remove &lt;b&gt;Bold&lt;/b&gt; &#34;Hull&#34;"' in html


# ── the item page's control ────────────────────────────────────────────────

def test_item_page_offers_a_link_when_the_user_has_no_lists(app_db):
    html = _authed_client().get(f"/market/type/{TYPE_ID}").text
    box = html[html.index('id="mk-wl"'):html.index('id="mk-hubs"')]
    assert 'href="/market/hub-scan?kind=watchlist"' in box and "Create a watchlist" in box
    assert "<select" not in box and "hx-post" not in box


def test_item_page_control_adds_to_the_users_list(app_db, factory):
    first = _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    second = _run(_add_watchlist(factory, USER_ID, "Beta List"))
    _run(_add_watchlist(factory, OTHER_USER_ID, "Other List"))
    c = _authed_client()
    html = c.get(f"/market/type/{TYPE_ID}").text
    assert html.index('id="mk-wl"') < html.index('id="mk-hubs"')
    box = html[html.index('id="mk-wl"'):html.index('id="mk-hubs"')]
    assert f'<option value="{first}">Alpha List</option>' in box
    assert f'<option value="{second}">Beta List</option>' in box and "Other List" not in box
    assert 'name="watchlist_id"' in _tag(box, 'id="mk-wl-pick"')
    assert f'value="{TYPE_ID}"' in _tag(box, 'id="mk-wl-type"')
    button = _tag(box, 'id="mk-wl-add"')
    for attr in ('hx-post="/market/watchlists/add-item"', 'hx-include="#mk-wl-pick, #mk-wl-type"',
                 'hx-target="#mk-wl-msg"', 'data-htmx-no-error="1"', 'type="button"'):
        assert attr in button
    assert 'for="mk-wl-pick"' in box and 'role="status"' in _tag(box, 'id="mk-wl-msg"')
    add = "/market/watchlists/add-item"
    assert c.post(add, data={"watchlist_id": str(second), "type_id": str(TYPE_ID)}).text == "Added."
    assert c.post(add, data={"watchlist_id": str(second),
                             "type_id": str(TYPE_ID)}).text == "Already on that list."
    assert c.post(add, data={"watchlist_id": str(second),
                             "type_id": "424242"}).text == "That is not a market item."
    assert _run(_item_ids(factory, second)) == [TYPE_ID] and _run(_item_ids(factory, first)) == []


def test_item_page_has_no_control_for_a_non_market_type(app_db, factory):
    _run(_add_types(factory, (2003, "Sample Relic", None)))
    _run(_add_watchlist(factory, USER_ID, "Alpha List"))
    html = _authed_client().get("/market/type/2003").text
    assert 'id="mk-wl"' not in html and 'id="mk-hubs"' in html


def test_item_page_control_fits_a_phone():
    css = source("market_type.html")
    phone = _phone_block(css)
    assert re.search(r"\.mk-wl-link\s*\{[^}]*min-height:44px", phone)
    assert re.search(r"\.mk-wl\s*\{[^}]*flex-wrap:wrap", css)
    # The select and button take site.css's phone floor (44px, 16px text).
    page = source("market_type.html")
    script = page[page.index("Hub Scan's \"Add to watchlist\" (#mk-wl-add)"):]
    script = script[:script.index("</script>")]
    assert "el.id !== 'mk-wl-add'" in script and "getElementById('mk-wl-msg')" in script
    assert "Not added; try again." in script


def test_manager_and_add_all_fit_a_phone():
    phone = _phone_block(source("market_hub_scan.html"))
    rule = re.search(r"\.hs-wl-box \.b-input, \.hs-addall \.b-input\s*\{([^}]*)\}", phone)
    assert rule, "phone rule for the manager's inputs"
    body = rule.group(1).replace(" ", "")
    assert "min-height:44px" in body and "font-size:16px" in body and "width:100%" in body
    assert re.search(r"\.hs-wl-li\s*\{[^}]*min-height:44px", phone)
