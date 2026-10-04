"""Mobile R2 T1: the character overview at phone width (≤640px).

R1 covered the overview's tabs, scroll boxes and Recent Transactions; this
covers the rest:
  * Can Fly → Can fly now: link rows (D1 A), whole-row tap through a CSS
    stretched link, Show all past 10.
  * Can Fly → Missing skills: m-rows whose key 2 is the fit's total training
    time (D2 B), built from phone-only cells so the desktop card is untouched.
  * Corporation History: key 2 is tenure, the full Name and Joined under the
    row, and a sized phone-only logo lead (D3 A).
  * Both opened lists start with a full "Name" line (R2 polish A; its own
    tests are in test_mobile_r2_polish_a.py).
  * Combat Profile: the ISK row as label/value lines (D6 A), the charts grid
    minmax, and legends below the doughnut and stream charts.
  * 40px tap targets for the summary toggles and location headers.

Desktop must render identically (D21): every phone cell is m-only and every
new class has rules only in this task's site.css section. Names and ids are
invented."""
import asyncio
import json
import os
import re
import shutil
import subprocess
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
                           rule_bodies)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ACTIONS_JS = os.path.join(_ROOT, "static", "js", "actions.js")


def _classes(attrs):
    return attrs.get("class", "").split()


class _Elements(HTMLParser):
    """Every element matching `pick(tag, attrs)`: its tag, attrs, text, and
    its direct children's tag, attrs and text. Attribute order and
    whitespace don't matter. Matches don't nest in the markup read here."""

    def __init__(self, pick):
        super().__init__(convert_charrefs=True)
        self.pick, self.stack, self.found = pick, [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        parent = self.stack[-1][1] if self.stack else None
        el = None
        if self.pick(tag, a):
            el = {"tag": tag, "attrs": a, "text": "", "kids": []}
            self.found.append(el)
        elif parent is not None and parent.get("_depth") == len(self.stack):
            parent["kids"].append({"tag": tag, "attrs": a, "text": ""})
        if tag in VOID:
            return
        owner = el or parent
        if el is not None:
            el["_depth"] = len(self.stack) + 1
        self.stack.append((tag, owner))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.stack or self.stack[-1][1] is None:
            return
        el = self.stack[-1][1]
        el["text"] += data
        if len(self.stack) > el["_depth"] and el["kids"]:
            el["kids"][-1]["text"] += data


def _elements(html, pick):
    p = _Elements(pick)
    p.feed(html)
    p.close()
    for el in p.found:
        el["text"] = norm(el["text"])
        for k in el["kids"]:
            k["text"] = norm(k["text"])
    return p.found


def _with_class(cls):
    return lambda tag, a: cls in _classes(a)


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
        assert {"b-row", "m-row", "m-row--link", "cf-fly"} <= set(_classes(r["attrs"]))
        assert "cf-miss" not in _classes(r["attrs"])
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
    details = _elements(html, lambda tag, a: tag == "details")
    assert [_classes(d["attrs"]) for d in details] == [["m-clamp-wrap"], ["m-clamp-wrap"]]


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
        assert {"b-card", "m-row", "cf-miss"} <= set(_classes(r["attrs"]))
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
        # The full "ship — fit" Name line comes first (R2 polish A).
        assert list(labelled) == ["Name", "Sample Gunnery", "Sample Navigation", "Folder"]
        assert labelled["Name"]["text"] == f"Sample Cruiser — {f['name']}"
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
        assert len(phone) == 2 + 1 + 2 + 1  # two keys, Name, two skills, Folder


def test_missing_card_phone_copies_match_the_desktop_card():
    """Key 1, Name and Folder render from the same values as the desktop
    head row, so the copies can't drift apart (escaping included)."""
    fits = _missing(2) + [dict(_missing(1)[0], id=7, name="R&D <Fit>", folder="A & B")]
    html = _render_can_fly(0, missing_fits=fits)
    rows = _miss_rows(html)
    assert len(rows) == 3
    for r in rows:
        head = r["cells"][0]                     # desktop head row: fit link + folder
        key1, folder = row_keys(r)[0], row_labelled(r)["Folder"]
        assert head["kids"][0]["href"] == key1["kids"][0]["href"]
        assert head["text"] == f"{key1['text']} {folder['text']}"
        assert row_labelled(r)["Name"]["text"] == key1["text"]
    assert html.count("R&amp;D &lt;Fit&gt;") == 3 and html.count("A &amp; B") == 2


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
        assert {"m-row", "ov-corp"} <= set(_classes(r["attrs"]))
        assert r["attrs"]["data-click"] == "toggleMRow"


def test_corp_history_keys_on_name_and_tenure():
    rows = _corp_rows(_render_overview())
    keys = [[k["text"] for k in row_keys(r)] for r in rows]
    assert keys == [["Sample Corp Current", "1.3y"], ["Sample Corp Short", "12d"],
                    ["Sample Corp Unknown"], ["Sample Corp Brief", "<1d"]]


def test_corp_history_labels_the_full_name_and_the_start_date():
    rows = _corp_rows(_render_overview())
    assert len(rows) == len(_HISTORY)
    for h, r in zip(_HISTORY, rows):
        labelled = row_labelled(r)
        assert list(labelled) == ["Name", "Joined"]
        assert labelled["Name"]["text"] == h["corporation_name"]
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
    toggles = lambda tag, a: a.get("data-click") == "toggleAssetLocation"
    clones = _elements(_render_overview(), toggles)
    assets = _elements(render_page(cd, "partials/assets_partial.html", "/character/90000001/assets-partial",
                                   locations=[{"location": "Sample Station", "items": []}], docked_at=None),
                       toggles)
    assert len(clones) == 1 and len(assets) == 1
    for head in clones + assets:
        assert _classes(head["attrs"]) == ["asset-location-header"]


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
    (row,) = _elements(_render_kill_stats(), _with_class("ks-isk"))
    # The desktop inline layout is unchanged.
    assert row["attrs"]["style"] == ("display:flex;flex-wrap:wrap;text-align:center;"
                                     "border:1px solid var(--border);margin-bottom:0.75rem;")
    assert [_classes(k["attrs"]) for k in row["kids"]] == [["ks-isk-tile"]] * 3
    assert [k["text"] for k in row["kids"]] == ["2.50B ISK Destroyed", "400.0M ISK Lost", "86% Efficiency"]


def test_stream_chart_box_carries_a_class_hook():
    """Phones give the stream chart a taller box: its legend sits below the
    plot there. Desktop keeps the inline 200px."""
    (box,) = _elements(_render_kill_stats(), _with_class("ks-stream"))
    assert box["attrs"]["style"] == "position:relative;height:200px;"
    assert [(k["tag"], k["attrs"].get("data-chart-kind")) for k in box["kids"]] == [("canvas", "stream")]


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


def _phone_block(sec):
    """The body of the section's phone @media block, found by matching its
    braces, and whatever follows the block up to the end marker."""
    m = re.match(r"\s*@media \(max-width: 640px\) \{", sec)
    assert m, "the section must open with its phone @media block"
    depth, i = 1, m.end()
    while depth:
        assert i < len(sec), "the phone @media block is never closed"
        depth += (sec[i] == "{") - (sec[i] == "}")
        i += 1
    return sec[m.end():i - 1], sec[i:]


_HOOKS = (".cf-fly", ".ov-toggle", ".ov-corp", ".ks-isk", ".ks-stream")


def test_css_new_class_hooks_have_no_desktop_rules():
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    phone, after = _phone_block(_sec())
    assert after.strip() == "", f"rules after the phone block: {after.strip()[:80]!r}"
    for cls in _HOOKS:
        assert re.search(re.escape(cls) + r"\b", phone), cls
        assert len(re.findall(re.escape(cls) + r"\b", css)) == len(re.findall(re.escape(cls) + r"\b", phone)), (
            f"{cls} is styled outside the R2 T1 phone block")


def test_css_stream_chart_box_is_taller_on_phones():
    body = _flat(rule_bodies(_sec(), ".ks-stream"))
    assert re.search(r"height: (\d+)px !important", body)
    assert int(re.search(r"height: (\d+)px", body).group(1)) >= 260


def test_css_corp_logo_slot_holds_its_size_when_the_logo_fails():
    """data-on-error="hide" sets display:none on the phone logo; the lead
    cell keeps an 18px slot so key 1 stays aligned with its neighbours.
    min-width needs !important to beat `.m-row > * { min-width: 0 !important }`."""
    body = _flat(rule_bodies(_sec(), '.ov-corp.m-row > [data-m="lead"]'))
    assert "min-width: 18px !important" in body
    assert "min-height: 18px" in body


# ── actions.js: chart legends ─────────────────────────────────────────

def _combat_js():
    """The combat-chart code: from _combatChart to the next section."""
    with open(_ACTIONS_JS, encoding="utf-8") as fh:
        js = fh.read()
    start = js.index("function _combatChart(")
    return js[start:js.index("// ── Activity history panel", start)]


def test_combat_chart_legends_drop_below_the_chart_on_phones():
    block = _combat_js()
    m = re.search(r"var (\w+) = \(window\.matchMedia && window\.matchMedia\('\(max-width: 640px\)'\)"
                  r"\.matches\) \? 'bottom' : 'right';", block)
    assert m, "the legend side must come from the 640px media query"
    side = m.group(1)
    assert len(re.findall(rf"position: {side}\b", block)) == 2      # doughnut + stream
    assert not re.search(r"position:\s*'right'", block)


def test_combat_charts_redraw_when_the_breakpoint_is_crossed():
    """Text check: one guarded 640px change listener that redraws every
    combat chart on the page."""
    block = _combat_js()
    assert "if (window.matchMedia && !window._combatLegendMq)" in block
    assert "window._combatLegendMq = window.matchMedia('(max-width: 640px)');" in block
    assert "window.renderCombatCharts(document);" in block
    assert re.search(r"_combatLegendMq\.addEventListener\('change', \w+\)", block)


_LEGEND_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const state = { phone: false };
const mqls = [];
function matchMedia(q) {
  const m = { media: q, ls: [],
    get matches() { return state.phone && q === '(max-width: 640px)'; },
    addEventListener(t, fn) { if (t === 'change') this.ls.push(fn); },
    addListener(fn) { this.ls.push(fn); } };
  mqls.push(m);
  return m;
}
let made = [];
function Chart(canvas, cfg) { this.canvas = canvas; this.cfg = cfg; canvas._chart = this; made.push(this); }
Chart.getChart = c => c._chart || null;
Chart.prototype.destroy = function () { this.destroyed = true; this.canvas._chart = null; };
const canvases = [
  { dataset: { chartKind: 'autopsy', chart: JSON.stringify({ solo: 1, small_gang: 0, fleet: 0, smartbomb: 0, npc: 0 }) } },
  { dataset: { chartKind: 'stream', chart: JSON.stringify({ datasets: [{ label: 'x', data: [1] }], weeks: 1 }) } },
];
const document = {
  querySelectorAll: sel => (sel === 'canvas[data-chart-kind]' ? canvases : []),
  querySelector: () => null, getElementById: () => null,
  body: { addEventListener() {} }, addEventListener() {},
};
const sandbox = { window: { matchMedia }, document, console, Date, Chart,
  localStorage: { getItem: () => null, setItem() {} },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0 };
sandbox.window.document = document;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
vm.runInContext(src, sandbox);      // a second run must not add a second listener

const out = { atLoad: made.length };
sandbox.window.renderCombatCharts(document);      // the page's first draw (desktop)
out.first = made.map(c => [c.cfg.type, c.cfg.options.plugins.legend.position]);
const fire = phone => {
  const before = made.slice();
  state.phone = phone;
  made = [];
  mqls.forEach(m => m.ls.slice().forEach(fn => fn({ matches: m.matches, media: m.media })));
  return { charts: made.map(c => [c.cfg.type, c.cfg.options.plugins.legend.position]),
           oldDestroyed: before.every(c => c.destroyed) };
};
out.toPhone = fire(true);
out.toDesktop = fire(false);
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def legend_run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("legend") / "harness.js"
    harness.write_text(_LEGEND_HARNESS)
    run = subprocess.run(["node", str(harness), _ACTIONS_JS], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_combat_chart_legends_follow_the_breakpoint(legend_run):
    """Behaviour, with actions.js run twice against a stub DOM: nothing is
    drawn at load; the first draw on a desktop puts legends on the right;
    crossing to phone redraws each chart once (one listener, not two) with
    legends below, destroying the old instances; crossing back restores
    the right-hand legends."""
    assert legend_run["atLoad"] == 0
    assert legend_run["first"] == [["doughnut", "right"], ["line", "right"]]
    assert legend_run["toPhone"] == {"charts": [["doughnut", "bottom"], ["line", "bottom"]], "oldDestroyed": True}
    assert legend_run["toDesktop"] == {"charts": [["doughnut", "right"], ["line", "right"]], "oldDestroyed": True}
