"""Mobile R2 T1: the character overview at phone width (≤640px).

R1 covered the overview's tabs, scroll boxes and Recent Transactions; this
covers the rest:
  * Can Fly → Can fly now: link rows (D1 A), whole-row tap through a CSS
    stretched link, Show all past 10.
  * Can Fly → Missing skills: m-rows whose key 2 is the fit's total training
    time (D2 B), built from phone-only cells so the desktop card is untouched.
  * Corporation History: key 2 is tenure, Joined under the row, and a sized
    phone-only logo lead (D3 A).
  * Combat Profile: the ISK row as label/value lines (D6 A), the charts grid
    minmax, and legends below the doughnut and stream charts.
  * 40px tap targets for the summary toggles and location headers.

Desktop must render identically (D21): every phone cell is m-only and every
new class has rules only in this task's site.css section. Names and ids are
invented."""
import asyncio
import os
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.db.models import Base
from app.routes import character_detail as cd
from tests._mobile import (SITE_CSS, VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, norm, render_page, row_keys, row_labelled, row_lead,
                           rule_bodies, source)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ACTIONS_JS = os.path.join(_ROOT, "static", "js", "actions.js")


def _classes(attrs):
    return attrs.get("class", "").split()


# ── Can Fly partial ───────────────────────────────────────────────────

def _flyable(n):
    return [{"id": i + 1, "name": f"Sample Fit {i}", "ship_type_id": 99201,
             "ship_name": "Sample Frigate", "folder": "Sample Folder" if i % 2 else None,
             "can_fly": True, "missing": []} for i in range(n)]


def _missing(n, with_str=True):
    fits = []
    for i in range(n):
        f = {"id": 100 + i, "name": f"Gap Fit {i}", "ship_type_id": 99202,
             "ship_name": "Sample Cruiser", "folder": "Sample Doctrine" if i % 2 == 0 else None,
             "can_fly": False,
             "missing": [
                 {"skill_id": 33101, "have": 0, "need": 3, "skill_name": "Sample Gunnery",
                  "time_str": "1h 2m"},
                 {"skill_id": 33102, "have": 2, "need": 4, "skill_name": "Sample Navigation",
                  "time_str": "1d 3h 4m"},
             ],
             "missing_training_minutes": 1690.0}
        if with_str:
            f["missing_training_str"] = f"1d 4h {i}m"
        fits.append(f)
    return fits


def _render_can_fly(fly=3, miss=2, missing_fits=None):
    fly_fits = _flyable(fly)
    miss_fits = _missing(miss) if missing_fits is None else missing_fits
    return render_page(cd, "partials/character_can_fly.html", "/character/90000001/can-fly",
                       error=None, no_scope=False, total=len(fly_fits) + len(miss_fits),
                       can_fly=len(fly_fits), can_fly_fits=fly_fits, missing_fits=miss_fits)


def _fly_rows(html):
    return [r for r in cells_rows(html) if "m-row--link" in _classes(r["attrs"])]


def _miss_rows(html):
    return [r for r in cells_rows(html) if "b-card" in _classes(r["attrs"])]


def test_can_fly_partial_follows_the_mrow_contract():
    rows = assert_mrow(_render_can_fly(3, 2), min_rows=5)
    assert len(rows) == 5


def test_can_fly_rows_are_link_rows_keyed_by_fit_and_folder():
    rows = _fly_rows(_render_can_fly(3, 0))
    assert len(rows) == 3
    for i, r in enumerate(rows):
        assert {"b-row", "m-row", "m-row--link"} <= set(_classes(r["attrs"]))
        k1, k2 = row_keys(r)
        assert k1["tag"] == "a"
        assert k1["attrs"]["href"] == f"/tools/fitting?load={i + 1}"
        assert k1["text"] == f"Sample Frigate — Sample Fit {i}"
        assert k2["text"] == ("Sample Folder" if i % 2 else "—")
        assert not row_labelled(r)


def test_can_fly_link_rows_carry_no_data_click():
    html = _render_can_fly(12, 0)
    rows = _fly_rows(html)
    assert len(rows) == 12
    for r in rows:
        assert "data-click" not in r["attrs"]
        for c in r["cells"]:
            assert "data-click" not in c["attrs"]
    # The whole can-fly list has no click binding besides Show all.
    assert re.findall(r'data-click="([^"]+)"', html) == ["toggleExpanded"]


@pytest.mark.parametrize("n, shown", [(10, False), (11, True)])
def test_can_fly_show_all_only_past_ten(n, shown):
    c = clamps(_render_can_fly(n, 0))
    assert [k["children"] for k in c.clamps] == [n]
    assert c.wraps == 1 and c.nested_wraps == 0
    if shown:
        assert [b["text"] for b in c.showall] == [f"Show all {n}"]
        b = c.showall[0]
        assert b["in_wrap"]
        assert b["attrs"]["type"] == "button"
        assert b["attrs"]["data-click"] == "toggleExpanded"
        assert b["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
        assert {"m-only", "m-showall"} <= set(_classes(b["attrs"]))
    else:
        assert c.showall == []


@pytest.mark.parametrize("n, shown", [(10, False), (11, True)])
def test_missing_skills_show_all_only_past_ten(n, shown):
    c = clamps(_render_can_fly(0, n))
    # The clamp holds the cards only, not the note above them.
    assert [k["children"] for k in c.clamps] == [n]
    assert c.wraps == 1 and c.nested_wraps == 0
    assert [b["text"] for b in c.showall] == ([f"Show all {n}"] if shown else [])
    assert all(b["in_wrap"] for b in c.showall)


def test_both_can_fly_lists_clamp_separately():
    html = _render_can_fly(11, 11)
    c = clamps(html)
    assert [k["children"] for k in c.clamps] == [11, 11]
    assert [b["text"] for b in c.showall] == ["Show all 11", "Show all 11"]
    assert c.wraps == 2 and c.nested_wraps == 0
    # Each <details> is its own wrap: Show all toggles only its own list.
    assert html.count('<details class="m-clamp-wrap"') == 2


class _Parents(HTMLParser):
    """For every .m-showall button: its parent's tag and classes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parents = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "m-showall" in _classes(a):
            self.parents.append(self.stack[-1])
        if tag not in VOID:
            self.stack.append((tag, _classes(a)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def test_show_all_sits_beside_the_list_not_among_its_rows():
    """.b-row:last-child drops the last row's border on desktop. A Show all
    button among the rows (even display:none) would steal :last-child and
    draw a new desktop border, so it must sit outside the rows' container:
    a direct child of its <details> wrap, after the .m-clamp list."""
    p = _Parents()
    p.feed(_render_can_fly(11, 11))
    p.close()
    assert p.parents == [("details", ["m-clamp-wrap"])] * 2


def test_missing_cards_are_mrows_keyed_by_fit_and_training_time():
    fits = _missing(3)
    rows = _miss_rows(_render_can_fly(0, missing_fits=fits))
    assert len(rows) == 3
    for f, r in zip(fits, rows):
        assert {"b-card", "m-row"} <= set(_classes(r["attrs"]))
        assert "m-row--link" not in _classes(r["attrs"])
        assert r["attrs"]["data-click"] == "toggleMRow"
        k1, k2 = row_keys(r)
        assert k1["text"] == f"Sample Cruiser — {f['name']}"
        # key 1 holds an inline link: taps on the name open the fit, taps
        # beside it open the row (toggleMRow skips links).
        assert [k.get("href") for k in k1["kids"]] == [f"/tools/fitting?load={f['id']}"]
        assert k2["text"] == f["missing_training_str"]
        assert "accent" in k2["attrs"].get("style", "")
        assert not row_lead(r)


def test_missing_cards_label_each_skill_and_the_folder():
    fits = _missing(2)
    rows = _miss_rows(_render_can_fly(0, missing_fits=fits))
    assert len(rows) == 2
    for f, r in zip(fits, rows):
        labelled = row_labelled(r)
        assert list(labelled) == ["Sample Gunnery", "Sample Navigation", "Folder"]
        assert labelled["Sample Gunnery"]["text"] == "0 → 3 · 1h 2m approx."
        assert labelled["Sample Navigation"]["text"] == "2 → 4 · 1d 3h 4m approx."
        assert labelled["Folder"]["text"] == (f["folder"] or "—")
        assert_single_value_child(r)


def test_missing_cards_keep_the_desktop_markup_untagged():
    """Desktop markup (head row + skill lines) is untouched and untagged, so
    phones hide it; every phone cell is m-only, so desktop hides those."""
    rows = _miss_rows(_render_can_fly(0, 2))
    assert len(rows) == 2
    for r in rows:
        desktop = [c for c in r["cells"] if "m-only" not in _classes(c["attrs"])]
        phone = [c for c in r["cells"] if "m-only" in _classes(c["attrs"])]
        assert len(desktop) == 2
        for c in desktop:
            assert "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]
        assert all("data-m" in c["attrs"] or "data-m-label" in c["attrs"] for c in phone)
        assert len(phone) == 2 + 2 + 1      # two keys, two skills, Folder


def test_missing_card_without_a_training_str_still_renders():
    """_enrich_missing_with_training returns early (no skill ids) without
    setting the total; the card then has one key."""
    fits = _missing(1, with_str=False)
    html = _render_can_fly(0, missing_fits=fits)
    rows = assert_mrow(html)
    (r,) = _miss_rows(html)
    assert [k["text"] for k in row_keys(r)] == ["Sample Cruiser — Gap Fit 0"]
    assert "None" not in html


def test_can_fly_summaries_are_tap_target_hooks():
    html = _render_can_fly(1, 1)
    summaries = re.findall(r"<summary([^>]*)>", html)
    assert len(summaries) == 2
    for attrs in summaries:
        assert re.search(r'class="[^"]*\bov-toggle\b', attrs), attrs
        assert "min-height" not in attrs and "line-height" not in attrs


def test_can_fly_route_existing_details_contract_holds():
    """test_can_fly_route pins exactly two collapsed <details>; the clamp
    wraps reuse them rather than adding more."""
    html = _render_can_fly(2, 2)
    assert html.count("<details") == 2
    assert "<details open" not in html


# ── _enrich_missing_with_training ─────────────────────────────────────

_SKILL_A, _SKILL_B = 33191, 33192


def _enrich(fits):
    async def run(path):
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all,
                                    tables=[sm.SDEType.__table__, sm.SDESkillInfo.__table__])
            async with Session() as db:
                db.add_all([
                    sm.SDEType(type_id=_SKILL_A, type_name="Sample Skill A"),
                    sm.SDEType(type_id=_SKILL_B, type_name="Sample Skill B"),
                    sm.SDESkillInfo(type_id=_SKILL_A, primary_attr=165, secondary_attr=166, rank=1.0),
                    sm.SDESkillInfo(type_id=_SKILL_B, primary_attr=167, secondary_attr=168, rank=2.0),
                ])
                await db.commit()
                await cd._enrich_missing_with_training(db, fits)
        finally:
            await engine.dispose()
    return run


def test_enrich_sets_each_fits_total_training_str(tmp_path):
    fits = [
        {"missing": [{"skill_id": _SKILL_A, "have": 0, "need": 1},
                     {"skill_id": _SKILL_B, "have": 2, "need": 3}]},
        {"missing": [{"skill_id": _SKILL_A, "have": 0, "need": 1}]},
    ]
    asyncio.run(_enrich(fits)(tmp_path / "sde.db"))
    for f in fits:
        assert f["missing_training_str"] == cd._format_train_duration(f["missing_training_minutes"])
    # Rank 1, 0→1 (250 SP) plus rank 2, 2→3 (13170 SP) at 25.5 SP/min.
    assert fits[0]["missing_training_str"] == "8h 46m"
    assert fits[1]["missing_training_str"] == "9m"
    assert fits[0]["missing"][1]["skill_name"] == "Sample Skill B"


def test_enrich_without_skill_ids_leaves_the_total_unset():
    """The early return (no skill ids anywhere) touches nothing, so the
    template reads the total with f.get()."""
    fits = [{"missing": []}]
    asyncio.run(cd._enrich_missing_with_training(None, fits))
    assert "missing_training_str" not in fits[0]
    assert "missing_training_minutes" not in fits[0]


# ── Overview: corporation history and toggles ─────────────────────────

_PILOT = NS(character_id=90000001, character_name="Pilot Alpha",
            corporation_name="Sample Corp", alliance_name=None,
            security_status=1.5, birthday=None)

_HISTORY = [
    {"corporation_id": 98000001, "corporation_name": "Sample Corp Current",
     "start_date": "2025-06-01", "days_in": 490, "is_current": True},
    {"corporation_id": 98000002, "corporation_name": "Sample Corp Short",
     "start_date": "2025-05-20", "days_in": 12, "is_current": False},
    {"corporation_id": 98000003, "corporation_name": "Sample Corp Unknown",
     "start_date": "2025-05-19", "days_in": None, "is_current": False},
    {"corporation_id": 98000004, "corporation_name": "Sample Corp Brief",
     "start_date": "2025-05-19", "days_in": 0, "is_current": False},
]


def _render_overview(**over):
    ctx = dict(
        char=_PILOT, killmails_enabled=False, current_wallet=1.0e9,
        journal=[], journal_error=None, chart_data_json='{"labels": [], "values": []}',
        active_range="1m", ranges=["1d", "1w", "1m"],
        active_skill={"skill_name": "Sample Skill", "finished_level": 3, "remaining_seconds": 3600},
        skillqueue=[{"skill_name": f"Sample Skill {i}", "finished_level": 3,
                     "remaining_seconds": 3600 * (i + 1)} for i in range(3)],
        completed_skills=[{"skill_name": "Done Skill", "finished_level": 4, "completed_ago": 7200}],
        corp_history=_HISTORY,
        total_sp_in_queue=0, total_trained_sp=5000000, unallocated_sp=0,
        has_implants_scope=False, last_synced_str="5m ago", queue_remaining=3600,
        zkill=[], kills=0, losses=0, has_assets_scope=True, docked_at=None,
        current_system=None, implants=[],
        jump_clones=[{"location": "Sample Station", "implants": []}],
        now=datetime(2026, 10, 3),
    )
    ctx.update(over)
    return render_page(cd, "character_detail.html", "/character/90000001", **ctx)


def _corp_rows(html):
    return [r for r in cells_rows(html)
            if row_keys(r) and row_keys(r)[0]["text"].startswith("Sample Corp ")]


def test_corp_history_rows_follow_the_mrow_contract():
    html = _render_overview()
    assert_mrow(html, min_rows=4)
    rows = _corp_rows(html)
    assert len(rows) == 4
    for r in rows:
        assert "m-row" in _classes(r["attrs"])
        assert r["attrs"]["data-click"] == "toggleMRow"


def test_corp_history_keys_on_name_and_tenure():
    rows = _corp_rows(_render_overview())
    keys = [[k["text"] for k in row_keys(r)] for r in rows]
    assert keys == [["Sample Corp Current", "1.3y"], ["Sample Corp Short", "12d"],
                    ["Sample Corp Unknown"], ["Sample Corp Brief", "<1d"]]


def test_corp_history_labels_the_start_date_joined():
    rows = _corp_rows(_render_overview())
    assert len(rows) == len(_HISTORY)
    for h, r in zip(_HISTORY, rows):
        labelled = row_labelled(r)
        assert list(labelled) == ["Joined"]
        assert labelled["Joined"]["text"] == h["start_date"]
        assert_single_value_child(r)


def test_corp_history_lead_is_a_phone_only_sized_logo():
    """The desktop img keeps data-on-error="hide", which a tagged cell would
    override (the phone rules use display !important), so it stays untagged
    and phones get their own wrapped, sized copy."""
    rows = _corp_rows(_render_overview())
    assert len(rows) == len(_HISTORY)
    for h, r in zip(_HISTORY, rows):
        (lead,) = row_lead(r)
        assert lead["tag"] == "span"
        assert "m-only" in _classes(lead["attrs"])
        (img,) = lead["kids"]
        assert img["width"] == "18" and img["height"] == "18"
        assert img["data-on-error"] == "hide"
        assert f"/corporations/{h['corporation_id']}/logo" in img["src"]
        desktop_imgs = [c for c in r["cells"] if c["tag"] == "img"]
        assert len(desktop_imgs) == 1
        assert desktop_imgs[0]["attrs"]["data-on-error"] == "hide"
        assert "data-m" not in desktop_imgs[0]["attrs"]


def test_corp_history_keeps_its_r1_clamp():
    c = clamps(_render_overview(corp_history=[dict(_HISTORY[1], corporation_name=f"Sample Corp {i}")
                                              for i in range(11)]))
    assert 11 in [k["children"] for k in c.clamps]
    assert "Show all 11" in [b["text"] for b in c.showall]
    assert c.nested_wraps == 0


def test_overview_summaries_are_tap_target_hooks():
    html = _render_overview()
    summaries = {norm(unescape(re.sub(r"<[^>]+>", "", m.group(2)))): m.group(1)
                 for m in re.finditer(r"<summary([^>]*)>(.*?)</summary>", html, flags=re.S)}
    for text in ("✓ Recently Completed (1)", "Corporation History (4)"):
        assert text in summaries, summaries.keys()
        assert re.search(r'class="[^"]*\bov-toggle\b', summaries[text])
        assert "min-height" not in summaries[text]


def test_location_headers_share_one_class_hook():
    """The jump-clone header (here) and the asset-location header (in
    partials/assets_partial.html) both carry .asset-location-header, the
    hook the 40px phone rule targets."""
    html = _render_overview()
    heads = re.findall(r'<div class="([^"]*)" data-click="toggleAssetLocation"', html)
    assert heads == ["asset-location-header"]
    assert 'class="asset-location-header" data-click="toggleAssetLocation"' in source(
        "partials/assets_partial.html")


# ── Combat Profile partial ────────────────────────────────────────────

def _render_kill_stats():
    return render_page(
        cd, "partials/character_kill_stats.html", "/character/90000001/kill-stats",
        char=_PILOT, character_id=90000001, year=2026, current_year=2026,
        summary={"kills": 5, "losses": 2, "isk_destroyed": 2.5e9, "isk_lost": 4.0e8},
        ships=[], weapons=[], systems=[],
        autopsy={"solo": 1, "small_gang": 1, "fleet": 0, "smartbomb": 0, "npc": 0}, autopsy_total=2,
        type_names={}, system_names={}, system_security={},
        streak={"current_win": 1, "longest_win": 3, "days_since_loss": 4},
        gang_split={"solo": 1, "small": 0, "medium": 0, "fleet": 0}, gang_total=1,
        cal_cells=[], cal_max=0, untouchable=[], profitability=[],
        radar_labels=["a"], radar_values=[1], radar_raw=[1],
        ts_datasets=[{"label": "Sample Frigate", "data": [1, 2]}], ts_weeks=2,
        backfill_complete=True)


def test_isk_row_and_tiles_carry_class_hooks():
    html = _render_kill_stats()
    m = re.search(r'<div class="ks-isk" style="([^"]*)">(.*?)\n    </div>\n', html, flags=re.S)
    assert m, "ISK summary row not found"
    # The desktop inline layout is unchanged.
    assert m.group(1) == ("display:flex;flex-wrap:wrap;text-align:center;"
                          "border:1px solid var(--border);margin-bottom:0.75rem;")
    tiles = re.findall(r'<div class="ks-isk-tile" style="[^"]*">', m.group(2))
    assert len(tiles) == 3
    for label in ("ISK Destroyed", "ISK Lost", "Efficiency"):
        assert label in m.group(2)


def test_charts_grid_never_overflows_a_narrow_phone():
    html = _render_kill_stats()
    assert "grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr))" in html
    assert "minmax(280px,1fr)" not in html


# ── site.css: this task's section ─────────────────────────────────────

def _sec():
    return css_section("T1")


def _flat(body):
    return re.sub(r"\s+", " ", body)


def test_css_can_fly_rows_stretch_their_link_over_the_row():
    sec = _sec()
    row = _flat(rule_bodies(sec, ".cf-fly.m-row--link"))
    assert re.search(r"position: relative", row)
    link = _flat(rule_bodies(sec, '.cf-fly.m-row--link > a[data-m="key"]::after'))
    assert re.search(r'content: ""', link)
    assert re.search(r"position: absolute", link)
    assert re.search(r"inset: 0", link)


def test_css_can_fly_folder_key_is_capped_in_vw():
    body = _flat(rule_bodies(_sec(), '.cf-fly.m-row--link > [data-m="key"] ~ [data-m="key"]'))
    assert re.search(r"max-width: \d+vw !important", body)
    assert "text-overflow: ellipsis" in body


def test_css_toggles_and_location_headers_are_40px():
    sec = _sec()
    summary = _flat(rule_bodies(sec, "summary.ov-toggle"))
    assert "min-height: 40px" in summary
    # Vertically centred while keeping display:list-item, so the native
    # disclosure marker stays.
    assert "line-height: 40px" in summary
    assert "display" not in summary
    head = _flat(rule_bodies(sec, ".asset-location-header"))
    assert "min-height: 40px" in head


def test_css_isk_row_becomes_label_value_lines():
    sec = _sec()
    row = _flat(rule_bodies(sec, ".ks-isk"))
    assert "flex-direction: column" in row
    assert "text-align: left !important" in row
    tile = _flat(rule_bodies(sec, ".ks-isk > .ks-isk-tile"))
    assert "display: flex" in tile
    assert "flex-direction: row-reverse" in tile      # label (2nd child) left, value right
    assert "justify-content: space-between" in tile
    assert "border-right: none !important" in tile
    assert "border-bottom: 1px solid var(--border)" in tile
    assert "flex: none !important" in tile and "min-width: 0 !important" in tile
    last = _flat(rule_bodies(sec, ".ks-isk > .ks-isk-tile:last-child"))
    assert "border-bottom: none" in last
    kids = _flat(rule_bodies(sec, ".ks-isk > .ks-isk-tile > *"))
    assert "margin-top: 0 !important" in kids
    # The value keeps its colour: nothing here sets one.
    assert "color" not in tile + kids


def test_css_new_class_hooks_have_no_desktop_rules():
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    sec = _sec()
    phone = sec[:sec.rindex("}") + 1]
    for cls in (".cf-fly", ".ov-toggle", ".ks-isk"):
        assert cls in phone, cls
        assert css.count(cls) == sec.count(cls), f"{cls} is styled outside the R2 T1 section"
    assert re.fullmatch(r"\s*@media \(max-width: 640px\) \{.*\}\s*", sec, flags=re.S)


# ── actions.js: chart legends ─────────────────────────────────────────

def _combat_js():
    with open(_ACTIONS_JS, encoding="utf-8") as fh:
        js = fh.read()
    start = js.index("function _combatChart(")
    end = js.index("window.renderCombatCharts", start)
    end = js.index("};", end)
    return js[start:end]


def test_combat_chart_legends_drop_below_the_chart_on_phones():
    block = _combat_js()
    m = re.search(r"var (\w+) = \(window\.matchMedia && window\.matchMedia\('\(max-width: 640px\)'\)"
                  r"\.matches\) \? 'bottom' : 'right';", block)
    assert m, "the legend side must come from the 640px media query"
    side = m.group(1)
    assert len(re.findall(rf"position: {side}\b", block)) == 2      # doughnut + stream
    assert not re.search(r"position:\s*'right'", block)
