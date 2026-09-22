# tests/test_activity_heatmap.py
"""Hour-of-week kill grids behind /tools/activity's "When is EVE busiest?".

Fixture shape follows tests/test_activity_history.py: a temp SQLite engine
with only the tables under test, one hand-managed event loop (there is no
pytest-asyncio in this repo).

Every time assertion derives its expected row from `datetime.weekday()`
rather than a literal index. The query groups by SQLite's `strftime('%w')`
(Sun=0) and the grid renders Mon-first, so a hardcoded number would keep
passing if that rotation were dropped — which is exactly the bug that would
draw the kills series one day away from the pilots series.
"""
import asyncio
from datetime import datetime

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Killmail
from app.db.sde_models import SDERegion, SDESystem
from app.intel.activity_heatmap import (
    MIN_KILLS_FOR_GRID,
    SCOPES,
    TZ_BANDS,
    empty_grid,
    grid_max,
    grid_row_index,
    grid_total,
    per_capita_grid,
    region_hour_of_week_kills,
    region_options,
    zone_hour_of_week_kills,
)

# Fixed clock so day-of-week assertions never depend on when the suite runs.
NOW = datetime(2026, 9, 20, 12, 0)

FORGE = 10000002          # k-space region holding the high-sec system
PLACID = 10000048         # k-space region holding the low-sec + null-sec pair
JSPACE_REGION = 11000031  # wormhole region

HIGH_SYS = 30000142
LOW_SYS = 30002510
NULL_SYS = 30000001
WH_SYS = 31000005         # inside the 31000000+ wormhole id range

# (killmail_time, system, how many kills at that bucket)
SEED = [
    (datetime(2026, 9, 14, 19, 30), HIGH_SYS, 3),
    (datetime(2026, 9, 15, 3, 5), LOW_SYS, 2),
    (datetime(2026, 9, 16, 4, 45), NULL_SYS, 5),
    (datetime(2026, 9, 17, 12, 10), WH_SYS, 1),
]


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Killmail.__table__.create(c))
            await conn.run_sync(lambda c: SDESystem.__table__.create(c))
            await conn.run_sync(lambda c: SDERegion.__table__.create(c))

    loop.run_until_complete(_init())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with factory() as s:
            s.add_all([
                # security is what the classifier rounds to 1dp: 0.45 rounds
                # to 0.5 and would be high-sec, so the low-sec system sits at
                # 0.4 deliberately.
                SDESystem(system_id=HIGH_SYS, system_name="HS-1", security=0.9, region_id=FORGE),
                SDESystem(system_id=LOW_SYS, system_name="LS-1", security=0.4, region_id=PLACID),
                SDESystem(system_id=NULL_SYS, system_name="NS-1", security=-0.5, region_id=PLACID),
                # A J-system's security is deeply negative; only the id range
                # keeps it out of the null-sec bucket.
                SDESystem(system_id=WH_SYS, system_name="J-1", security=-0.99, region_id=JSPACE_REGION),
                SDERegion(region_id=FORGE, region_name="Alpha Region"),
                SDERegion(region_id=PLACID, region_name="Beta Region"),
                SDERegion(region_id=JSPACE_REGION, region_name="A-R00001"),
            ])
            kill_id = 1
            for when, system_id, count in SEED:
                for _ in range(count):
                    s.add(Killmail(
                        killmail_id=kill_id, killmail_hash=f"h{kill_id}",
                        killmail_time=when, solar_system_id=system_id,
                        victim_ship_type_id=587, total_value=1.0e6, is_npc=False,
                    ))
                    kill_id += 1
            await s.commit()

    loop.run_until_complete(_seed())
    yield factory
    loop.close()


def _run(coro_factory):
    async def run():
        return await coro_factory()
    return asyncio.get_event_loop().run_until_complete(run())


def _grids(session_factory):
    async def call():
        async with session_factory() as s:
            return await zone_hour_of_week_kills(s, now=NOW)
    return _run(call)


def _cell(when):
    """(row, col) the grid should put `when` in."""
    return when.weekday(), when.hour


# ── zone classification + the hour-of-week aggregate ────────────────────────

def test_row_index_matches_the_rendered_grid_rotation():
    """SQLite %w is Sun=0; the grid is Mon-first. Sunday must land on row 6."""
    for dow_sqlite, expected in ((0, 6), (1, 0), (2, 1), (6, 5)):
        assert grid_row_index(dow_sqlite) == expected


def test_each_zone_lands_in_its_own_scope_grid(session_factory):
    grids = _grids(session_factory)
    assert set(grids) == set(SCOPES)
    for scope, (when, _system, count) in zip(("hs", "ls", "ns", "j"), SEED):
        row, col = _cell(when)
        assert grids[scope][row][col] == count, scope
        # And nowhere else in that scope's grid.
        assert grid_total(grids[scope]) == count, scope


def test_jspace_id_range_beats_negative_security(session_factory):
    """The J-system sits at -0.99 security. Classified by security alone it
    would be indistinguishable from null-sec."""
    grids = _grids(session_factory)
    row, col = _cell(SEED[3][0])
    assert grids["j"][row][col] == 1
    assert grids["ns"][row][col] == 0


def test_all_scope_is_the_sum_of_every_zone(session_factory):
    grids = _grids(session_factory)
    assert grid_total(grids["all"]) == sum(count for _t, _s, count in SEED)
    for when, _system, count in SEED:
        row, col = _cell(when)
        assert grids["all"][row][col] == count


def test_kills_outside_the_window_are_excluded(session_factory):
    """Same query, a clock far enough forward that the trailing 90 days no
    longer reach the seeded kills."""
    async def call():
        async with session_factory() as s:
            return await zone_hour_of_week_kills(s, now=datetime(2027, 6, 1))
    assert grid_total(_grids(session_factory)["all"]) > 0
    assert grid_total(_run(call)["all"]) == 0


def test_region_grid_is_scoped_to_that_regions_systems(session_factory):
    async def beta():
        async with session_factory() as s:
            return await region_hour_of_week_kills(s, PLACID, now=NOW)

    async def alpha():
        async with session_factory() as s:
            return await region_hour_of_week_kills(s, FORGE, now=NOW)

    beta_grid = _run(beta)
    # Beta holds the low-sec and null-sec systems, and only those.
    assert grid_total(beta_grid) == 7
    low_row, low_col = _cell(SEED[1][0])
    null_row, null_col = _cell(SEED[2][0])
    assert beta_grid[low_row][low_col] == 2
    assert beta_grid[null_row][null_col] == 5

    alpha_grid = _run(alpha)
    assert grid_total(alpha_grid) == 3
    hs_row, hs_col = _cell(SEED[0][0])
    assert alpha_grid[hs_row][hs_col] == 3


def test_region_options_sorted_by_name_and_flag_jspace(session_factory):
    async def call():
        async with session_factory() as s:
            return await region_options(s)
    options = _run(call)
    assert [o["region_name"] for o in options] == [
        "A-R00001", "Alpha Region", "Beta Region",
    ]
    by_id = {o["region_id"]: o for o in options}
    assert by_id[JSPACE_REGION]["jspace"] is True
    assert by_id[FORGE]["jspace"] is False
    assert by_id[PLACID]["jspace"] is False


# ── thresholds, normalisation, per-capita math ──────────────────────────────

def test_not_enough_data_threshold_is_the_window_total(session_factory):
    """The seeded slice is 11 kills — well under the floor, so the panel
    says "not enough data" rather than drawing 168 near-empty cells."""
    grids = _grids(session_factory)
    assert grid_total(grids["all"]) < MIN_KILLS_FOR_GRID

    dense = empty_grid()
    dense[2][19] = MIN_KILLS_FOR_GRID
    assert grid_total(dense) >= MIN_KILLS_FOR_GRID
    dense[2][19] = MIN_KILLS_FOR_GRID - 1
    assert grid_total(dense) < MIN_KILLS_FOR_GRID


def test_grid_max_is_per_grid_not_global(session_factory):
    """Colour scales against the selected grid's own maximum, so a quiet
    region still shows its internal shape instead of rendering flat."""
    grids = _grids(session_factory)
    assert grid_max(grids["ns"]) == 5
    assert grid_max(grids["j"]) == 1
    assert grid_max(grids["all"]) == 5
    assert grid_max(empty_grid(None)) == 0


def test_per_capita_is_kills_per_thousand_pilots():
    kills = empty_grid()
    pcu = empty_grid(None)
    kills[1][19] = 1240
    pcu[1][19] = 31000
    kills[2][4] = 500
    pcu[2][4] = 20000
    out = per_capita_grid(kills, pcu)
    assert out[1][19] == pytest.approx(1240 * 1000 / 31000, rel=1e-6)
    assert out[2][4] == pytest.approx(25.0)


def test_per_capita_leaves_unknown_presence_as_none():
    """Asymmetric on purpose: no kills means zero kills, but no pilots
    sample means unknown — dividing by a guessed zero would invent a cell."""
    kills = empty_grid()
    pcu = empty_grid(None)
    kills[0][0] = 100
    kills[0][1] = 0
    pcu[0][1] = 10000
    pcu[0][2] = 0
    kills[0][2] = 50
    out = per_capita_grid(kills, pcu)
    assert out[0][0] is None       # kills but no presence sample
    assert out[0][1] == 0.0        # no kills, presence known
    assert out[0][2] is None       # presence of zero is not a divisor


# ── cache keying + the startup pre-warm ─────────────────────────────────────

def _fresh_cache():
    from app.routes import player_stats
    player_stats._heatmap_cache.clear()
    return player_stats


def test_cache_key_includes_mode_scope_and_region(session_factory):
    player_stats = _fresh_cache()

    async def call():
        async with session_factory() as s:
            ns = await player_stats._kills_hour_of_week_grid(s, "ns", None)
            region = await player_stats._kills_hour_of_week_grid(s, "all", PLACID)
            return ns, region

    ns_grid, region_grid = _run(call)
    keys = set(player_stats._heatmap_cache)
    assert ("kills", "ns", None) in keys
    assert ("kills", "all", PLACID) in keys
    # A region selection must not be served from, or poison, the zone entry.
    assert grid_total(ns_grid) == 5
    assert grid_total(region_grid) == 7

    pcu = empty_grid(None)
    pcu[0][0] = 1000
    player_stats._heatmap_store(("percap", "ns", None), NOW, per_capita_grid(ns_grid, pcu), True)
    assert ("percap", "ns", None) in player_stats._heatmap_cache
    # Same scope, different mode — distinct entries, never one overwriting
    # the other.
    assert player_stats._heatmap_cache[("percap", "ns", None)][1] is not ns_grid


def test_warm_precomputes_all_five_zone_grids(session_factory, monkeypatch):
    """warm_activity_cache() opens with a 30s sleep, so the zone pre-warm is
    split into its own callable — this is the thing under test."""
    player_stats = _fresh_cache()
    monkeypatch.setattr(player_stats, "AsyncSessionLocal", session_factory)

    _run(player_stats.warm_heatmap_zone_grids)

    for scope in SCOPES:
        assert ("kills", scope, None) in player_stats._heatmap_cache, scope
    # One query filled all five: nothing region-keyed was computed.
    assert not [k for k in player_stats._heatmap_cache if k[2] is not None]


def test_warm_is_wired_into_the_startup_prewarm():
    import inspect

    from app.routes import player_stats
    source = inspect.getsource(player_stats.warm_activity_cache)
    assert "warm_heatmap_zone_grids()" in source


# ── timezone bands ──────────────────────────────────────────────────────────

def test_every_band_hour_is_a_real_utc_hour():
    assert TZ_BANDS
    for name, start, end in TZ_BANDS:
        assert name
        assert 0 <= start <= 23, name
        assert 0 <= end <= 23, name
        assert start != end, name


def test_wrap_around_band_splits_into_two_spans():
    """A band crossing midnight (US East, 23→03) cannot be one grid-column
    span. The template's painter splits it; this asserts the arithmetic that
    split relies on."""
    wrapped = [(n, s, e) for n, s, e in TZ_BANDS if s > e]
    assert wrapped, "expected at least one band crossing midnight UTC"
    for _name, start, end in wrapped:
        spans = [(start, 24), (0, end)]
        assert sum(b - a for a, b in spans) == (24 - start) + end
        assert all(b > a for a, b in spans)

    straight = [(n, s, e) for n, s, e in TZ_BANDS if s < e]
    assert straight
    for _name, start, end in straight:
        assert end - start > 0


# ── template contract ───────────────────────────────────────────────────────

_TEMPLATES = "app/templates"


class _FakeState:
    csp_nonce = "test-nonce"


class _FakeRequest:
    state = _FakeState()


def _render(**overrides):
    """Render tools_activity.html's own blocks against a stub base.

    base.html needs the whole app context (nav, session, user flags); the
    contract under test is entirely inside this template's content block, so
    the stub swaps the parent out and leaves everything else real.
    """
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block head %}{% endblock %}"
                                     "{% block breadcrumbs %}{% endblock %}"
                                     "{% block title %}{% endblock %}"
                                     "{% block content %}{% endblock %}"}),
            FileSystemLoader(_TEMPLATES),
        ]),
        autoescape=True,
    )
    kills = empty_grid()
    kills[1][19] = 1240
    pcu = empty_grid(None)
    pcu[1][19] = 31500
    context = dict(
        request=_FakeRequest(),
        window="30d", window_label="Last 30 days",
        window_options=[("30d", "Last 30 days")],
        live_mode=False,
        peak_pcu=0, mean_pcu=0, total_kills=0, total_isk=0,
        source_counts={}, labels=[], isk_values=[], pcu_values=[],
        kills_values=[], daily_kills_labels=[], daily_kills_counts=[],
        breakdowns_available=False, has_breakdown_data=False,
        zone_available=False, has_zone_data=False,
        solo_fleet_series={}, npc_player_series={},
        zone_series={}, zone_isk_series={},
        has_heatmap_data=True,
        pcu_heatmap=pcu,
        heatmap_grid=kills,
        heatmap_kills=kills,
        heatmap_mode="kills", heatmap_scope="ns", heatmap_region=None,
        heatmap_region_name=None,
        heatmap_max=1240, heatmap_min=0,
        heatmap_kills_total=1240, heatmap_sparse=False,
        heatmap_zone_available=True,
        heatmap_regions=[
            {"region_id": 10000002, "region_name": "Alpha Region", "jspace": False},
            {"region_id": 11000031, "region_name": "A-R00001", "jspace": True},
        ],
        heatmap_modes=[("kills", "Kills"), ("pilots", "Pilots online"),
                       ("percap", "Per capita")],
        heatmap_scopes=[("all", "All"), ("hs", "High-sec"), ("ls", "Low-sec"),
                        ("ns", "Null-sec"), ("j", "J-space")],
        heatmap_days=90, heatmap_min_kills=MIN_KILLS_FOR_GRID,
        tz_bands=TZ_BANDS,
        presence_strip=[None] * 24,
    )
    context.update(overrides)
    return env.get_template("tools_activity.html").render(**context)


def test_template_renders_five_scope_controls_and_a_mode_control():
    html = _render()
    assert html.count('class="b-btn ta-hm-scope') == 5
    for label in ("All", "High-sec", "Low-sec", "Null-sec", "J-space"):
        assert label in html
    assert html.count('class="b-btn ta-hm-mode') == 3
    for label in ("Kills", "Pilots online", "Per capita"):
        assert label in html


def test_template_renders_the_region_select_with_a_filter_box():
    html = _render()
    assert 'id="ta-region-select"' in html
    assert 'name="region"' in html
    assert 'id="ta-region-filter"' in html
    assert "Alpha Region" in html and "A-R00001" in html
    # J-space regions are tagged so the client can keep them under the
    # J-space scope only.
    assert 'data-jspace="1"' in html


def test_template_carries_scope_and_mode_through_the_window_buttons():
    """A window click must not reset the heatmap selection, and a scope
    click must not reset the window."""
    html = _render(heatmap_region=10000002, heatmap_scope="ns", heatmap_mode="percap")
    assert "window=30d&amp;mode=percap&amp;scope=ns&amp;region=10000002" in html
    # Scope buttons keep the window and drop the region (region implies zone).
    assert "window=30d&amp;mode=percap&amp;scope=hs\"" in html


def test_template_renders_the_band_legend_and_local_time_toggle():
    html = _render()
    for name, _start, _end in TZ_BANDS:
        assert name in html
    assert "approximate · DST not applied" in html
    assert 'id="ta-tz-toggle"' in html
    assert 'data-click="taToggleLocalTime"' in html
    assert 'id="ta-hm-bands"' in html


def test_template_says_not_enough_data_instead_of_drawing_a_blank_grid():
    html = _render(heatmap_sparse=True, heatmap_kills_total=11)
    assert "Not enough data" in html
    assert 'id="pcu-heatmap"' not in html
    # The controls survive, or there is no way back to a populated view.
    assert 'class="b-btn ta-hm-scope' in html


def test_per_capita_without_a_pilots_archive_says_so(session_factory):
    """Kills present, pilots-online archive still empty: per capita divides
    by nothing, so the grid is all None. Reachable on a fresh install, and
    it must not render as 168 colourless cells with no explanation."""
    player_stats = _fresh_cache()

    async def call():
        async with session_factory() as s:
            return await player_stats._build_heatmap_context(
                s, empty_grid(None), False,
                mode="percap", scope="all", region_id=None,
            )

    ctx = _run(call)
    assert ctx["heatmap_max"] == 0
    assert ctx["heatmap_sparse"] is True

    html = _render(heatmap_sparse=True, heatmap_kills_total=5000,
                   heatmap_mode="percap",
                   heatmap_grid=[[None] * 24 for _ in range(7)], heatmap_max=0)
    assert "pilots-online samples for the trailing" in html
    assert 'id="pcu-heatmap"' not in html


def test_template_hides_the_controls_when_there_are_no_kills():
    html = _render(heatmap_zone_available=False, heatmap_mode="pilots",
                   heatmap_grid=[[None] * 24 for _ in range(7)],
                   heatmap_max=0)
    assert "ta-hm-scope" not in html
    assert "ta-hm-mode" not in html
    # …but the pilots-only grid this panel shipped with still renders.
    assert 'id="pcu-heatmap"' in html


def test_template_tooltip_quotes_both_series():
    html = _render()
    assert "Tue 19:00–20:00 UTC · 1,240 kills · 31,500 pilots online avg" in html


def test_template_adds_no_inline_handlers():
    """Mirrors tests/test_csp_inline_handlers.py for the rendered output —
    the policy is enforcing, so an on*= here would be silently dead."""
    import re
    html = _render()
    handlers = [h for h in re.findall(r'\bon([a-z]+)\s*=\s*["\']', html)
                if h not in ("delete", "update")]
    assert not handlers, handlers
