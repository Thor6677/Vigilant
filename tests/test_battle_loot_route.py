"""T-082: the battle loot routes. Everything is invented: systems, entities,
types and kills live in a throwaway SQLite file; names and prices are stubbed."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.battle_loot as route
from app.db.models import Base, Killmail, KillmailItem, User, get_db
from app.db.sde_models import SDESystem, SDEType
from app.intel import battle_loot_prices as prices
from app.intel import evetools_br
from tests.test_route_auth_gating import _client

USER = 8101
SYS_A, SYS_B, SYS_C = 30009001, 30009002, 30009003
T0 = datetime(2026, 11, 7, 14, 0)
STAMP = "202611071400"
EPOCH = int(T0.replace(tzinfo=timezone.utc).timestamp())
ALLY_A, ALLY_B = 99000001, 99000002
CORP_A, CORP_B = 98000001, 98000002
SHIP, WIDGET, GADGET, BPC = 80001, 90001, 90002, 90003
BR_ID = "0123456789abcdef01234567"
W = f"{SYS_A}:{EPOCH - 3600}:{EPOCH + 7200}"

NAMES = {ALLY_A: "Alpha Alliance", ALLY_B: "Bravo Alliance", CORP_A: "Alpha Corp", CORP_B: "Bravo Corp",
         91000001: "Pilot One", 91000002: "Pilot Two", 91000003: "Pilot Three"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(User(id=USER))
            db.add_all([SDESystem(system_id=s, system_name=n) for s, n in
                        ((SYS_A, "Testwell"), (SYS_B, "Samplar"), (SYS_C, "Elsewhere"))])
            db.add_all([SDEType(type_id=SHIP, type_name="Test Frigate", category_id=6),
                        SDEType(type_id=WIDGET, type_name="Test Widget", category_id=7),
                        SDEType(type_id=GADGET, type_name="Test Gadget", category_id=7),
                        SDEType(type_id=BPC, type_name="Test Widget Blueprint", category_id=9)])
            kills = [  # id, system, minutes after T0, char, corp, alliance
                (1, SYS_A, 0, 91000001, CORP_A, ALLY_A), (2, SYS_A, 10, 91000002, CORP_B, ALLY_B),
                (3, SYS_A, 20, 91000003, CORP_B, ALLY_B), (4, SYS_B, 30, None, CORP_B, None),
                (5, SYS_A, 400, 91000001, CORP_A, ALLY_A),         # long after the window
                (6, SYS_C, 0, 91000001, CORP_A, ALLY_A)]           # another system
            for i, sysid, mins, ch, co, al in kills:
                db.add(Killmail(killmail_id=i, killmail_hash="h", killmail_time=T0 + timedelta(minutes=mins),
                                solar_system_id=sysid, victim_character_id=ch, victim_corporation_id=co,
                                victim_alliance_id=al, victim_ship_type_id=SHIP, total_value=float(i)))
            await db.flush()
            db.add_all([
                KillmailItem(killmail_id=1, item_type_id=WIDGET, quantity_dropped=30, quantity_destroyed=2, flag=5),
                KillmailItem(killmail_id=1, item_type_id=BPC, quantity_dropped=1, flag=5),
                KillmailItem(killmail_id=2, item_type_id=GADGET, quantity_dropped=5, flag=5),
                KillmailItem(killmail_id=3, item_type_id=WIDGET, quantity_destroyed=9, flag=27),   # nothing dropped
                KillmailItem(killmail_id=5, item_type_id=WIDGET, quantity_dropped=99, flag=5),
            ])
            await db.commit()
    _run(seed())

    async def override_get_db():
        async with Session() as s:
            yield s

    async def names(ids):
        return {i: NAMES[i] for i in ids if i in NAMES}

    async def avg(db):
        return {WIDGET: 1_000_000.0, GADGET: 4_000_000.0, BPC: 9e9}

    monkeypatch.setattr(route, "resolve_entity_names", names)
    monkeypatch.setattr(route, "get_price_map", avg)
    monkeypatch.setattr(route.prices, "ensure_job", lambda key, types: None)
    route._ctx_cache.clear()
    prices._cache.clear()
    prices._jobs.clear()
    main.app.dependency_overrides[get_db] = override_get_db
    yield
    main.app.dependency_overrides.pop(get_db, None)
    route._ctx_cache.clear()
    prices._cache.clear()
    _run(engine.dispose())


def _get(path, **kw):
    return _client(user_id=USER).get(path, **kw)


def _loc(r):
    return r.headers["location"]


# ── routing and auth ─────────────────────────────────────────────────────

def test_loot_is_registered_before_the_dscan_catch_all():
    """/intel/{scan_id} is public and matches in registration order."""
    paths = [r.path for r in main.app.routes if isinstance(r, APIRoute)]
    assert paths.index("/intel/loot") < paths.index("/intel/{scan_id}")


def test_intel_loot_renders_the_form_not_dscans_not_found(env):
    r = _get("/intel/loot")
    assert r.status_code == 200
    assert "Battle Link" in r.text and "Intel not found" not in r.text


@pytest.mark.parametrize("path", [
    "/intel/loot", f"/intel/loot/related/{SYS_A}/{STAMP}", f"/intel/loot/windows?w={W}",
])
def test_pages_redirect_without_a_session(env, path):
    r = _client().get(path)
    assert r.status_code == 307 or r.status_code == 302 or r.status_code == 303
    assert _loc(r) == "/"


@pytest.mark.parametrize("path", [
    f"/intel/loot/results?w={W}", f"/intel/loot/prices/status?w={W}", f"/intel/loot/wreck/1?w={W}",
])
def test_partials_are_401_without_a_session(env, path):
    r = _client().get(path)
    assert r.status_code == 401 and r.text == ""


def test_the_form_page_makes_no_outbound_call(env, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("evetools must not be reached")
    monkeypatch.setattr(evetools_br, "resolve_br", boom)
    assert _get(f"/intel/loot/related/{SYS_A}/{STAMP}").status_code == 200
    assert _get(f"/intel/loot/windows?w={W}").status_code == 200


# ── paste flow ────────────────────────────────────────────────────────────

def test_related_paste_redirects_to_the_canonical_url(env):
    r = _get(f"/intel/loot?br={quote('https://zkillboard.com/related/%d/%s/' % (SYS_A, STAMP), safe='')}")
    assert r.status_code == 303
    assert _loc(r) == f"/intel/loot/related/{SYS_A}/{STAMP}"


def test_zkill_options_survive_in_the_canonical_url(env):
    opts = quote('{"systems":[%d,%d],"hoursBefore":3,"hoursAfter":"4"}' % (SYS_A, SYS_B), safe="")
    link = f"https://zkillboard.com/related/{SYS_A}/202611071437/o/{opts}/"
    r = _get(f"/intel/loot?br={quote(link, safe='')}")
    assert r.status_code == 303
    parts = urlsplit(_loc(r))
    assert parts.path == f"/intel/loot/related/{SYS_A}/{STAMP}"
    q = parse_qs(parts.query)
    assert q == {"systems": [f"{SYS_A},{SYS_B}"], "hb": ["3"], "ha": ["4"]}
    page = _get(_loc(r))
    assert page.status_code == 200 and "Testwell" in page.text and "(+1 system)" in page.text
    # the link out to zKillboard carries the same selection
    out = re.search(r'href="(https://zkillboard\.com/related/[^"]+)"', page.text).group(1)
    assert out.startswith(f"https://zkillboard.com/related/{SYS_A}/2026110714" "00/o/")
    doc = json.loads(unquote(out.split("/o/")[1].rstrip("/")))
    assert doc == {"systems": [SYS_A, SYS_B], "hoursBefore": 3, "hoursAfter": 4}


def test_a_listed_set_replaces_the_path_system_in_the_canonical_url(env):
    opts = quote('{"systems":[%d]}' % SYS_B, safe="")
    r = _get(f"/intel/loot?br={quote(f'https://zkillboard.com/related/{SYS_A}/{STAMP}/o/{opts}/', safe='')}")
    assert _loc(r) == f"/intel/loot/related/{SYS_B}/{STAMP}"


def test_plain_related_pages_link_to_the_plain_zkill_page(env):
    out = _get(f"/intel/loot/related/{SYS_A}/{STAMP}").text
    assert f'href="https://zkillboard.com/related/{SYS_A}/{STAMP[:10]}00/"' in out


def test_unknown_system_is_refused(env):
    link = f"https://zkillboard.com/related/39999999/{STAMP}/"
    r = _get(f"/intel/loot?br={quote(link, safe='')}")
    assert r.status_code == 200 and "system this instance" in r.text


def test_bad_paste_shows_the_reason_and_never_echoes_markup(env):
    r = _get("/intel/loot?br=" + quote("https://evil.example/<script>alert(1)</script>", safe=""))
    assert r.status_code == 200
    assert "supported" in r.text and "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text


def test_evetools_paste_resolves_once_and_redirects_to_windows(env, monkeypatch):
    seen = []

    async def fake_resolve(br_id, user_id, system_exists):
        seen.append((br_id, user_id, await system_exists([SYS_A, SYS_C])))
        scope = route.bl.parse_windows([W, f"{SYS_B}:{EPOCH}:{EPOCH + 3600}"])
        return evetools_br.EvetoolsScope(scope, 7)
    monkeypatch.setattr(evetools_br, "resolve_br", fake_resolve)
    r = _get(f"/intel/loot?br={quote('https://br.evetools.org/br/' + BR_ID, safe='')}")
    assert r.status_code == 303
    assert seen == [(BR_ID, USER, {SYS_A, SYS_C})]
    parts = urlsplit(_loc(r))
    assert parts.path == "/intel/loot/windows"
    q = parse_qs(parts.query)
    assert q["src"] == [f"evetools:{BR_ID}"] and len(q["w"]) == 2


def test_resolver_failure_rerenders_the_form_with_the_fallback(env, monkeypatch):
    async def fail(*a, **k):
        return evetools_br.EvetoolsError(route.bl.SORRY_PASTE_RELATED)
    monkeypatch.setattr(evetools_br, "resolve_br", fail)
    r = _get(f"/intel/loot?br={quote('https://br.evetools.org/br/' + BR_ID, safe='')}")
    assert r.status_code == 200 and "paste each system" in r.text


def test_coverage_line_mentions_evetools_when_the_resolution_is_cached(env, monkeypatch):
    scope = route.bl.parse_windows([W])
    monkeypatch.setitem(evetools_br._cache, BR_ID, (evetools_br.time.monotonic(),
                                                    evetools_br.EvetoolsScope(scope, 31)))
    r = _get(f"/intel/loot/windows?w={quote(W, safe='')}&src=evetools:{BR_ID}")
    assert "evetools lists 31" in r.text
    assert "evetools lists" not in _get(f"/intel/loot/windows?w={quote(W, safe='')}").text


# ── bad scopes ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("qs", [
    "w=abc", "", f"w={SYS_A}:{EPOCH}:{EPOCH + 90000}", f"w={SYS_A}:{EPOCH + 5}:{EPOCH}",
    "&".join(f"w={30009100 + i}:{EPOCH}:{EPOCH + 60}" for i in range(11)),
])
def test_bad_windows_are_400(env, qs):
    assert _get(f"/intel/loot/windows?{qs}").status_code == 400
    assert _get(f"/intel/loot/results?{qs}").status_code == 400


def test_a_window_on_an_unknown_system_is_404(env):
    assert _get(f"/intel/loot/windows?w=39999999:{EPOCH}:{EPOCH + 60}").status_code == 404


def test_a_bad_related_stamp_is_refused(env):
    assert _get(f"/intel/loot/related/{SYS_A}/2026110714").status_code == 400
    assert _get(f"/intel/loot/related/{SYS_A}/202613071400").status_code == 400


# ── the report ────────────────────────────────────────────────────────────

def _page(**qs):
    extra = "".join(f"&{k}={v}" for k, v in qs.items())
    return _get(f"/intel/loot/results?w={quote(W, safe='')}{extra}")


def test_the_window_selects_the_kills(env):
    r = _get(f"/intel/loot/related/{SYS_A}/{STAMP}")
    assert r.status_code == 200
    assert "Test Frigate" in r.text and "Pilot One" in r.text
    assert "Vigilant has 3 kills" in r.text     # kills 1-3: not 4 (other system), 5 (late), 6 (other system)
    assert "Kills in Testwell 2026-11-07 13:00–16:00 EVE" in r.text
    assert "https://zkillboard.com/related/%d/%s00/" % (SYS_A, STAMP[:10]) in r.text


def test_wreck_row_has_ship_and_victim_pilot_and_sorts_by_dropped_value(env):
    r = _page()
    # wreck 1: 30 x 1M widgets (the blueprint is worth 0) = 30M; wreck 2: 5 x 4M gadgets = 20M
    assert r.text.index("Pilot One") < r.text.index("Pilot Two")
    assert "30.00M" in r.text and "20.00M" in r.text
    assert "Test Widget (30.00M)" in r.text


def test_zero_drop_wrecks_are_hidden_with_a_toggle(env):
    hidden = _page().text
    assert "Pilot Three" not in hidden and "(1)" in hidden        # the toggle names the count
    assert "Pilot Three" in _page(empty=1).text


def test_threshold_flags_worth_looting(env):
    tag = 'bl-tag bl-worth">loot'
    assert _page().text.count(tag) == 2                           # default 10M: wrecks of 30M and 20M
    assert _page(min=25).text.count(tag) == 1
    assert _page(min=100).text.count(tag) == 0


def test_group_totals_filter_and_add_up(env):
    r = _page()
    assert "Alpha Alliance" in r.text and "Bravo Alliance" in r.text
    flt = _page(group=f"a:{ALLY_B}").text
    assert "Pilot Two" in flt and "Pilot One" not in flt
    assert "Bravo Alliance ✕" in flt                              # the active group offers to clear


def test_bad_filter_values_fall_back(env):
    assert _page(min="nan").status_code == 200
    assert _page(min="abc", group="x:1").status_code == 200


def test_first_render_is_provisional_with_a_polling_status(env, monkeypatch):
    job = prices.JobState(state="running", total=150, done=37)
    monkeypatch.setattr(route.prices, "ensure_job", lambda key, types: job)
    r = _get(f"/intel/loot/related/{SYS_A}/{STAMP}")
    assert "provisional" in r.text.lower()
    assert "Fetching Jita prices 37/150" in r.text
    html = r.text
    poll = html[html.index('id="bl-price-status"'):]
    assert 'hx-trigger="every 2s"' in poll[:300] and 'data-htmx-no-error="1"' in poll[:300]
    results = html[html.index('id="bl-results"'):]
    assert 'loot-prices-ready from:body' in results[:300] and 'data-htmx-no-error="1"' in results[:300]


def test_warm_cache_renders_jita_prices_without_a_poll(env, monkeypatch):
    now = prices._now()
    prices._cache[WIDGET] = (now, "ok", 1_200_000.0)
    prices._cache[GADGET] = (now, "ok", 3_000_000.0)
    r = _get(f"/intel/loot/related/{SYS_A}/{STAMP}")
    assert 'hx-trigger="every 2s"' not in r.text
    assert "Jita 4-4 lowest sell for 2 of 2 types" in r.text
    assert "36.00M" in r.text                                     # 30 x 1.2M
    assert "provisional" not in r.text.lower()


def test_thin_jita_ask_falls_back_and_is_counted(env):
    now = prices._now()
    prices._cache[WIDGET] = (now, "ok", 9_000_000.0)              # > 5x the 1M average
    prices._cache[GADGET] = (now, "none", None)
    r = _page().text
    assert "thin Jita ask" in r and "no Jita sell" in r
    assert "30.00M" in r                                          # the average was used


def test_destroyed_is_valued_at_the_average_even_when_jita_is_known(env):
    prices._cache[WIDGET] = (prices._now(), "ok", 2_000_000.0)
    assert "2.00M" in _page().text                                # wreck 1: 2 destroyed x 1M average


# ── status poll ───────────────────────────────────────────────────────────

def test_status_keeps_polling_while_the_job_runs(env, monkeypatch):
    job = prices.JobState(state="running", total=10, done=4)
    monkeypatch.setattr(route.prices, "ensure_job", lambda key, types: job)
    r = _get(f"/intel/loot/prices/status?w={quote(W, safe='')}")
    assert 'hx-trigger="every 2s"' in r.text and 'data-htmx-no-error="1"' in r.text
    assert "4/10" in r.text and "hx-trigger" not in r.headers and "HX-Trigger" not in r.headers


def test_status_signals_when_pricing_is_done(env):
    r = _get(f"/intel/loot/prices/status?w={quote(W, safe='')}")
    assert r.headers["HX-Trigger"] == "loot-prices-ready"
    assert "every 2s" not in r.text and "Jita prices loaded" in r.text


# ── wreck expand ──────────────────────────────────────────────────────────

def test_wreck_items(env):
    prices._cache[WIDGET] = (prices._now(), "ok", 1_200_000.0)
    r = _get(f"/intel/loot/wreck/1?w={quote(W, safe='')}")
    assert r.status_code == 200 and "private" in r.headers["cache-control"]
    assert "30 × Test Widget" in r.text and "Jita" in r.text
    assert "blueprint" in r.text                                  # valued 0, tagged
    assert "2 × Test Widget" in r.text                            # destroyed


def test_wreck_outside_every_window_is_404(env):
    for kid in (5, 6, 4, 999):                                    # late, other system, other system, missing
        assert _get(f"/intel/loot/wreck/{kid}?w={quote(W, safe='')}").status_code == 404


def test_wreck_in_a_second_window_is_found(env):
    w2 = f"{SYS_B}:{EPOCH - 3600}:{EPOCH + 7200}"
    r = _get(f"/intel/loot/wreck/4?w={quote(W, safe='')}&w={quote(w2, safe='')}")
    assert r.status_code == 200 and "No item data" in r.text


# ── evetools /battle/<n> ──────────────────────────────────────────────────

def test_battle_paste_resolves_and_redirects_with_a_battle_src(env, monkeypatch):
    seen = []

    async def fake(battle_id, user_id, system_exists):
        seen.append((battle_id, user_id))
        return evetools_br.EvetoolsScope(route.bl.parse_windows([W]), 12)
    monkeypatch.setattr(evetools_br, "resolve_battle", fake)
    r = _get(f"/intel/loot?br={quote('https://br.evetools.org/battle/4242', safe='')}")
    assert r.status_code == 303 and seen == [(4242, USER)]
    q = parse_qs(urlsplit(_loc(r)).query)
    assert q["src"] == ["evetools-battle:4242"] and q["w"] == [W]
    scope = route.bl.parse_windows([W])
    monkeypatch.setitem(evetools_br._cache, "battle:4242",
                        (evetools_br.time.monotonic(), evetools_br.EvetoolsScope(scope, 12)))
    assert "evetools lists 12" in _get(_loc(r)).text


def test_battle_failure_shows_the_fallback(env, monkeypatch):
    async def fail(*a, **k):
        return evetools_br.EvetoolsError(route.bl.SORRY_PASTE_RELATED)
    monkeypatch.setattr(evetools_br, "resolve_battle", fail)
    r = _get(f"/intel/loot?br={quote('https://br.evetools.org/battle/4242', safe='')}")
    assert r.status_code == 200 and "paste each system" in r.text


# ── provisional label vs. a job that is not running ───────────────────────

class Recorder:
    def __init__(self, job=None):
        self.job, self.calls = job, []

    def __call__(self, key, types):
        self.calls.append((key, tuple(types)))
        return self.job


def test_results_start_pricing_for_prices_that_aged_out(env, monkeypatch):
    rec = Recorder(prices.JobState(state="running", total=2, done=0))
    monkeypatch.setattr(route.prices, "ensure_job", rec)
    old = prices._now() - 35 * 60                                 # past the 30 minute life
    prices._cache[WIDGET] = (old, "ok", 1_200_000.0)
    prices._cache[GADGET] = (old, "ok", 3_000_000.0)
    r = _page()
    assert rec.calls and set(rec.calls[0][1]) == {WIDGET, GADGET}
    # the poll comes back inside the re-fetched results, so prices refill without a reload
    assert 'id="bl-price-status"' in r.text and 'hx-trigger="every 2s"' in r.text
    assert 'data-htmx-no-error="1"' in r.text[r.text.index('id="bl-price-status"'):][:300]
    assert "Provisional" in r.text


def test_failed_lookups_retry_after_a_minute(env, monkeypatch):
    rec = Recorder(prices.JobState(state="running", total=1, done=0))
    monkeypatch.setattr(route.prices, "ensure_job", rec)
    prices._cache[WIDGET] = (prices._now() - 61, "error", None)
    prices._cache[GADGET] = (prices._now(), "ok", 3_000_000.0)
    r = _page()
    assert rec.calls and "every 2s" in r.text


def test_no_provisional_label_when_no_job_will_run(env):
    """ensure_job is stubbed to None (a backoff, say) while prices are missing."""
    r = _page().text
    assert "Provisional" not in r and "provisional" not in r.lower()
    assert "Jita prices are missing for 2 types" in r
    assert 'hx-trigger="every 2s"' not in r


def test_page_render_has_one_poll_element_inside_the_results(env, monkeypatch):
    job = prices.JobState(state="running", total=150, done=37)
    monkeypatch.setattr(route.prices, "ensure_job", lambda key, types: job)
    html = _get(f"/intel/loot/related/{SYS_A}/{STAMP}").text
    assert html.count('id="bl-price-status"') == 1
    assert html.index('id="bl-results"') < html.index('id="bl-price-status"')


def test_crashed_job_status_is_not_called_a_rate_limit(env, monkeypatch):
    job = prices.JobState(state="done", total=3, done=3, crashed=True)
    monkeypatch.setattr(route.prices, "get_job", lambda key: job)
    r = _get(f"/intel/loot/prices/status?w={quote(W, safe='')}").text
    assert "Jita pricing failed" in r and "rate limit" not in r


# ── an empty average-price list ───────────────────────────────────────────

def test_a_context_built_on_no_average_prices_is_not_pinned(env, monkeypatch):
    async def none(db):
        return {}
    monkeypatch.setattr(route, "get_price_map", none)
    r = _page()
    assert "EVE average prices could not be loaded" in r.text
    (until, ctx), = route._ctx_cache.values()
    assert until - route._now() <= route.TTL_NO_AVERAGES + 1 and not ctx["avg_ok"]


def test_expired_contexts_are_purged_and_the_cache_is_small(env, monkeypatch):
    assert route.CONTEXT_CACHE_ENTRIES == 8
    _page()
    (key,) = route._ctx_cache
    route._ctx_cache[key] = (route._now() - 1, route._ctx_cache[key][1])
    _get(f"/intel/loot/results?w={quote(f'{SYS_B}:{EPOCH - 3600}:{EPOCH + 7200}', safe='')}")
    assert key not in route._ctx_cache and len(route._ctx_cache) == 1


# ── kills with no item data ───────────────────────────────────────────────

def test_kills_without_item_rows_say_items_unknown_and_are_not_hidden(env):
    w = f"{SYS_B}:{EPOCH - 3600}:{EPOCH + 7200}"                 # kill 4 has no item rows
    r = _get(f"/intel/loot/results?w={quote(w, safe='')}").text
    assert "items unknown" in r and "https://zkillboard.com/kill/4/" in r
    assert "nothing dropped</span>" not in r
    assert "1 kill have no item data" in r
    assert 'class="b-empty"' not in r                             # not hidden by the zero-drop filter


def test_unknown_kills_do_not_count_in_totals(env):
    w = f"{SYS_B}:{EPOCH - 3600}:{EPOCH + 7200}"
    r = _get(f"/intel/loot/results?w={quote(w, safe='')}").text
    assert '<b>0</b><span>Wrecks</span>' in r


# ── columns ───────────────────────────────────────────────────────────────

def test_header_and_rows_have_the_same_columns(env):
    html = _page().text
    head = html[html.index('class="b-table-row m-head"'):]
    head = head[:head.index("</div>")]
    row = html[html.index('class="b-table-row m-row" data-click="toggleMRow"'):]
    row = row[:row.index('<div id="bl-d-')]
    assert head.count("<span") == 6
    assert "System · time" in head
    assert len(re.findall(r'\n    <span ', row)) == 6


# ── crafted input never 500s ──────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    f"/intel/loot/related/{2 ** 70}/{STAMP}",
    f"/intel/loot/related/{SYS_A}/{STAMP}?systems={2 ** 70}",
    f"/intel/loot/related/{SYS_A}/{STAMP}?systems=" + ",".join(str(10 ** 12 + i) for i in range(30)),
    f"/intel/loot/related/{SYS_A}/{STAMP}?systems=%EF%BC%91",
    f"/intel/loot/related/{SYS_A}/999912311400",
    f"/intel/loot/related/{SYS_A}/{STAMP}?hb=%EF%BC%91&ha=x",
    f"/intel/loot/related/{SYS_A}/{STAMP}?hb=99999999999999999999",
    f"/intel/loot/windows?w={2 ** 40}:{EPOCH}:{EPOCH + 60}",
    f"/intel/loot/windows?w={SYS_A}:99999999999:99999999998",
    f"/intel/loot/results?w={SYS_A}:{EPOCH}:99999999999",
    f"/intel/loot/wreck/{2 ** 70}?w={W}",
    f"/intel/loot/wreck/-5?w={W}",
    f"/intel/loot/results?w={W}&min=1e999&group=a:{2 ** 70}",
])
def test_crafted_input_never_500s(env, path):
    r = _get(path)
    assert r.status_code < 500, path


def test_crafted_paste_never_500s(env):
    for link in (f"https://zkillboard.com/related/{2 ** 70}/{STAMP}/",
                 "https://zkillboard.com/related/30009001/999912311400/",
                 "https://zkillboard.com/related/30009001/%s/o/%s/" % (STAMP, quote('{"systems":[%d]}' % 2 ** 70))):
        assert _get(f"/intel/loot?br={quote(link, safe='')}").status_code == 200


def test_a_listed_set_in_the_query_is_cut_to_ten(env):
    ids = ",".join(str(SYS_A + i) for i in range(30))
    r = _get(f"/intel/loot/related/{SYS_A}/{STAMP}?systems={ids}")
    assert r.status_code == 404                                   # unknown systems, cleanly
