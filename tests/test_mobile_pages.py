"""Mobile R1 pages (mobile design §6, §4.6): render each reworked page with a
minimal hand-built context and check its phone markup.

Pages are rendered in full (base.html included) through the route module's
own `templates.env`, after importing app.main so every env carries the
globals and filters the real routes register. Character pages define macros
at the top level, outside `{% block content %}`, so rendering the content
block alone (as tests/_dashboard_fixture.py does for the dashboard) would
leave them undefined. Names and ids are invented."""
import os
import re
import types
from html.parser import HTMLParser

import app.main  # noqa: F401 — populates every router's templates.env.globals
from tests._mobile import assert_mrow

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES = os.path.join(_ROOT, "app", "templates")
_NS = types.SimpleNamespace
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}


def _request(path="/"):
    return _NS(
        url=_NS(path=path),
        state=_NS(csp_nonce="test-nonce"),
        session={"user_id": 1, "is_admin": False,
                 "active_character_id": 90000001, "csrf_token": "t"},
    )


def _render(module, name, path="/", **ctx):
    """Full page through the route module's own Jinja env."""
    return module.templates.env.get_template(name).render(request=_request(path), **ctx)


def _source(name):
    with open(os.path.join(_TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


class _Cells(HTMLParser):
    """For every .m-row: its attrs, and each direct child's attrs, text and
    element children (attrs of the child's own direct children).

    Limits, all acceptable for the hand-written templates it reads:
    - No implied end tags. An unclosed <td>, <li> or <p> stays open, so the
      next sibling is read as its child rather than as another cell.
    - An end tag closes the nearest open element with that name, so a stray
      end tag can close a row early. Cells after it are dropped, which could
      hide a third key from assert_mrow.
    - `_labelled` keys cells by label, so two cells with the same label merge
      into the last one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # [tag, kind, ref]; kind: "row" | "cell" | "in" | None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        parent = self.stack[-1] if self.stack else None
        entry = [tag, None, None]
        if parent and parent[1] == "row":
            cell = {"tag": tag, "attrs": a, "text": "", "kids": []}
            self.rows[parent[2]]["cells"].append(cell)
            entry = [tag, "cell", cell]
        elif parent and parent[1] in ("cell", "in"):
            if parent[1] == "cell":
                parent[2]["kids"].append(a)
            entry = [tag, "in", parent[2]]
        if "m-row" in a.get("class", "").split():
            self.rows.append({"tag": tag, "attrs": a, "cells": []})
            entry = [tag, "row", len(self.rows) - 1]
        if tag not in _VOID:
            self.stack.append(entry)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for e in reversed(self.stack):
            if e[1] in ("cell", "in"):
                e[2]["text"] += data
                return
            if e[1] == "row":
                return


def _rows(html):
    p = _Cells()
    p.feed(html)
    p.close()
    for r in p.rows:
        for c in r["cells"]:
            c["text"] = _norm(c["text"])
    return p.rows


def _keys(row):
    return [c for c in row["cells"] if c["attrs"].get("data-m") == "key"]


def _lead(row):
    return [c for c in row["cells"] if c["attrs"].get("data-m") == "lead"]


def _labelled(row):
    return {c["attrs"]["data-m-label"]: c for c in row["cells"] if "data-m-label" in c["attrs"]}


def _assert_single_value_child(row):
    """An open row lays a labelled cell out as label · value, with the value
    pieces grouped on the right (justify-content:flex-end; flex-wrap:wrap),
    so several element children would still render. This helper is stricter
    than the CSS on purpose: one wrapper per value keeps each value laid out
    as a unit, the way its desktop cell is. m-hide children don't count."""
    for label, c in _labelled(row).items():
        shown = [k for k in c["kids"] if "m-hide" not in k.get("class", "").split()]
        assert len(shown) <= 1, f"labelled cell {label!r} has {len(shown)} element children"


# ── Task 13: Skill Plans (§6.1) ──────────────────────────────────────

from app.routes import skill_plans as skill_plans_mod  # noqa: E402

_SITE_CSS = os.path.join(_ROOT, "static", "css", "site.css")


def _plan(pid, name, visibility="personal", n_entries=3, corp=None, description=""):
    return _NS(id=pid, name=name, visibility=visibility, owner_corp_id=corp,
               owner_alliance_id=None, description=description,
               entries=[_NS() for _ in range(n_entries)])


def _render_skill_plans():
    mine = _plan(1, "Alpha Doctrine", description="Gunnery basics")
    shared = _plan(2, "Corp Logistics", visibility="corporation", n_entries=1, corp=98000001)
    return _render(
        skill_plans_mod, "skill_plans.html", "/skill-plans",
        groups={"personal": [mine], "corporation": [shared], "alliance": [], "custom": []},
        plan_stats={1: {"total_sp": 1234567, "time_str": "4d 2h"},
                    2: {"total_sp": 0, "time_str": "—"}},
        editable={1: True, 2: False}, characters=[],
        corp_names={98000001: "Sample Corp"}, alliance_names={},
        eligible_corps=[{"id": 98000001, "name": "Sample Corp"}], eligible_alliances=[])


def test_skill_plans_create_form_stacks_on_phones():
    html = _render_skill_plans()
    grid = re.search(r'<div class="m-stack"[^>]*>(.*?)</div>', html, re.S)
    assert grid, "the create-form grid needs the m-stack class"
    body = grid.group(1)
    for needle in ('id="new-plan-name"', 'id="new-plan-scope"',
                   'id="new-plan-target"', 'data-click="createPlan"'):
        assert needle in body, needle
    # onScopeChange() shows and hides the target select through its inline
    # display, so the hidden-by-default state must survive.
    assert re.search(r'id="new-plan-target"[^>]*display:none', body)


def test_skill_plans_m_stack_never_sets_child_display():
    """onScopeChange() toggles #new-plan-target with style.display; a
    display rule on `.m-stack > *` would override that toggle."""
    with open(_SITE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    rule = re.search(r"\.m-stack > \*\s*\{([^}]*)\}", css)
    assert rule, ".m-stack > * rule missing from site.css"
    assert "display" not in rule.group(1)


def test_skill_plans_rows_are_link_rows_keyed_by_name_and_time():
    html = _render_skill_plans()
    rows = assert_mrow(html, min_rows=2)
    for r in rows:
        assert r["tag"] == "a"
        assert "m-row--link" in r["attrs"]["class"].split()
        assert r["attrs"]["href"].startswith("/skill-plans/")
    mine, shared = _rows(html)[:2]
    name, time = _keys(mine)
    assert "Alpha Doctrine" in name["text"] and "Personal" in name["text"]
    assert time["text"] == "4d 2h"
    assert "Corp · Sample Corp" in _keys(shared)[0]["text"]


def test_skill_plans_phone_line_carries_skills_and_sp():
    """A link row can't expand, so Skills and SP ride in key 1 as a
    phone-only line instead of labelled cells."""
    mine, shared = _rows(_render_skill_plans())[:2]
    name = _keys(mine)[0]
    meta = [k for k in name["kids"] if "m-only" in k.get("class", "").split()]
    assert len(meta) == 1 and "display:block" in meta[0]["style"]
    assert "3 skills · 1,234,567 SP" in name["text"]
    html = _render_skill_plans()
    assert re.search(r'<span class="m-only"[^>]*>\s*3 skills · 1,234,567 SP\s*</span>', html)
    assert re.search(r'<span class="m-only"[^>]*>\s*1 skill\s*</span>', html)
    shared_name = _keys(shared)[0]["text"]
    assert "1 skill" in shared_name and " SP" not in shared_name


def test_skill_plans_badge_line_ellipsises_on_phones():
    html = _render_skill_plans()
    assert re.search(r'<span class="m-trunc"[^>]*>\s*<span[^>]*>Alpha Doctrine', html)
    from tests.test_mobile_css import _phone
    rule = re.search(r'\.m-row > \[data-m="key"\] > \.m-trunc > \*\s*\{([^}]*)\}', _phone())
    assert rule, "m-trunc rule missing from the phone CSS"
    assert "text-overflow: ellipsis" in rule.group(1) and "min-width: 0" in rule.group(1)


# ── Task 14: Planetary (§6.2) ────────────────────────────────────────

from jinja2 import Environment, FileSystemLoader  # noqa: E402

from app.routes import pi as pi_mod  # noqa: E402

_PI_PAGES = {
    "planetary.html": "colonies",
    "planetary_lookup.html": "lookup",
    "planetary_calculator.html": "calculator",
    "planetary_chain.html": "chain",
}
_PI_URLS = ("/industry/planetary", "/industry/planetary/lookup",
            "/industry/planetary/calculator", "/industry/planetary/chain")


def _render_planetary():
    pilot = _NS(character_id=90000001, character_name="Pilot Alpha")
    rows = [
        {"char": pilot, "planet": {"planet_id": 40000001, "planet_type": "gas",
                                   "system_name": "Sample System", "num_pins": 12,
                                   "expiry_warning": "critical", "expiry_time_str": "3h 10m"}},
        {"char": pilot, "planet": {"planet_id": 40000002, "planet_type": "barren",
                                   "system_name": None, "num_pins": 4,
                                   "expiry_warning": None, "expiry_time_str": None}},
    ]
    return _render(pi_mod, "planetary.html", "/industry/planetary",
                   rows=rows, missing_scope_chars=[], sort="expiry", filter_type="",
                   filter_warn="", all_types=["barren", "gas"], total_planets=2,
                   expiring_soon=1, warning_cnt=0, pin_group_names={})


def test_planetary_colony_rows_keep_their_expander():
    rows = assert_mrow(_render_planetary(), min_rows=2)
    for r in rows:
        a = r["attrs"]
        assert a["class"].split() == ["pi-row-top", "m-row"]
        assert a["data-click"] == "toggleExpanded"           # not toggleMRow (§4.3)
        assert a["data-toggle-target"] == ".pi-row"
        assert a["hx-trigger"] == "click once"
        assert a["hx-get"].startswith("/industry/planetary/planet/90000001/")


def test_planetary_colony_rows_tag_portrait_character_and_expiry():
    first, second = _rows(_render_planetary())[:2]
    assert [c["tag"] for c in _lead(first)] == ["img"]
    name, expiry = _keys(first)
    assert name["text"] == "Pilot Alpha"
    assert expiry["text"] == "3h 10m"
    assert "color:var(--danger)" in expiry["attrs"]["style"]   # urgency colour kept
    labelled = _labelled(first)
    assert list(labelled) == ["Planet", "System", "Pins"]
    assert labelled["Planet"]["text"] == "gas"
    assert labelled["System"]["text"] == "Sample System"
    assert labelled["Pins"]["text"] == "12"
    arrow = [c for c in first["cells"] if "pi-arrow" in c["attrs"].get("class", "")]
    assert len(arrow) == 1
    assert "data-m" not in arrow[0]["attrs"] and "data-m-label" not in arrow[0]["attrs"]
    assert _keys(second)[1]["text"] == "no extractor"
    assert _labelled(second)["System"]["text"] == "—"
    for r in (first, second):
        _assert_single_value_child(r)


def test_planetary_column_header_hides_on_phones():
    assert re.search(r'<div class="b-panel-head m-head"', _render_planetary())


def test_planetary_subnav_keeps_desktop_pills_and_adds_a_view_dropdown():
    html = _render_planetary()
    pills = re.search(r'<nav class="m-tabs-desktop" style="display:flex;gap:0.5rem;font-size:11px;">(.*?)</nav>', html, re.S).group(1)
    details = re.search(r'<details class="m-tabs">(.*?)</details>', html, re.S).group(1)
    assert '<span class="m-tabs-label">View</span><span class="m-tabs-current">Colonies</span>' in details
    for part in (pills, details):
        for url in _PI_URLS:
            assert f'href="{url}"' in part, url
    assert 'href="/industry/planetary" class="b-btn is-active"' in pills
    assert pills.count("is-active") == 1
    assert 'href="/industry/planetary" class="is-active" aria-current="page"' in details
    assert 'class="b-tab-strip' not in html          # no desktop tab strip on PI pages


def test_planetary_filter_wraps_each_label_and_select():
    html = _render_planetary()
    assert 'class="pi-filter"' in html
    pairs = re.findall(r'<span class="m-pair">\s*<label class="b-muted-sm">(\w+)</label>\s*<select name="(\w+)"', html)
    assert pairs == [("Sort", "sort"), ("Type", "filter_type"), ("Expiry", "filter_warn")]
    # Desktop keeps the label beside its select, as before the wrappers.
    assert re.search(r"@media \(min-width: 641px\)\s*\{\s*\.pi-filter \.m-pair\s*\{\s*display:\s*inline-flex", html)


def test_planetary_tabs_macro_marks_each_view_active():
    env = Environment(loader=FileSystemLoader(_TEMPLATES))
    tmpl = env.from_string('{% from "partials/_pi_tabs.html" import pi_tabs %}{{ pi_tabs(active) }}')
    for key, label in (("colonies", "Colonies"), ("lookup", "System Lookup"),
                       ("calculator", "Calculator"), ("chain", "Chain Explorer")):
        html = tmpl.render(active=key)
        assert f'<span class="m-tabs-current">{label}</span>' in html
        assert html.count('aria-current="page"') == 1          # dropdown only
        assert html.count('b-btn is-active') == 1              # desktop pill


def test_planetary_sibling_pages_share_the_view_tabs():
    for page, key in _PI_PAGES.items():
        src = _source(page)
        assert '{% from "partials/_pi_tabs.html" import pi_tabs %}' in src, page
        assert f"{{{{ pi_tabs('{key}') }}}}" in src, page
        assert "<nav " not in src, page


def test_planetary_expanded_row_shows_the_planet_detail():
    """The planet detail used to carry an inline display:none, which the
    page's `.pi-row.is-expanded .pi-row-detail` rule can't beat, so the
    htmx-loaded detail never showed. Hiding it from the page <style> lets
    the expanded rule win (same specificity order: base rule first)."""
    html = _render_planetary()
    details = re.findall(r'<div id="pi-detail-[^"]*" class="pi-row-detail"([^>]*)>', html)
    assert len(details) == 2
    for attrs in details:
        assert "display:none" not in attrs.replace(" ", "")
    blocks = [b for b in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S) if ".pi-row-detail" in b]
    assert len(blocks) == 1
    style = blocks[0]
    base = re.search(r"(?m)^\s*\.pi-row-detail\s*\{\s*display:\s*none;?\s*\}", style)
    shown = re.search(r"\.pi-row\.is-expanded \.pi-row-detail\s*\{\s*display:\s*block;?\s*\}", style)
    assert base and shown
    assert base.start() < shown.start()


def test_planetary_portrait_holds_its_size_while_loading():
    """On phones `.m-row > *` forces width:auto !important over the inline
    28px, so an unloaded (or failed) portrait would collapse to ~2px and the
    name would jump when it arrives. The width/height attributes give the
    img an intrinsic size that holds whether it's loaded, pending or broken."""
    for row in _rows(_render_planetary()):
        (lead,) = _lead(row)
        assert lead["attrs"].get("width") == "28"
        assert lead["attrs"].get("height") == "28"


# ── Task 15: Blueprints (§6.3) ───────────────────────────────────────

from app.routes import blueprints as blueprints_mod  # noqa: E402

_LONG_BP = "Sample Capital Construction Component With A Very Long Name Blueprint"


def _render_blueprints(extra=()):
    """A BPO (10/20) and a BPC (2/4). `extra` adds (raw row, name) pairs; the
    default render stays these two rows, which other tests index."""
    raw = [
        {"type_id": 691, "item_id": 1, "quantity": -1, "material_efficiency": 10,
         "time_efficiency": 20, "runs": -1, "location_flag": "Hangar", "location_id": 60000001},
        {"type_id": 692, "item_id": 2, "quantity": -2, "material_efficiency": 2,
         "time_efficiency": 4, "runs": 5, "location_flag": "Hangar", "location_id": 60000001},
    ]
    names = {691: "Sample Frigate Blueprint", 692: _LONG_BP}
    for row, name in extra:
        raw.append(row)
        names[row["type_id"]] = name
    bps = blueprints_mod._process_blueprints(raw, names)
    char = {"character_id": 90000001, "character_name": "Pilot Alpha",
            "corporation_id": None, "corporation_name": None}
    return _render(blueprints_mod, "blueprints.html", "/character/90000001/blueprints",
                   char=char, blueprints=bps, groups=blueprints_mod._group_blueprints(bps, "type"),
                   stats=blueprints_mod._compute_stats(bps), error=None, is_corp=False,
                   corp_id=None, filter="all", group_by="type")


def test_blueprints_rows_follow_the_mrow_contract():
    rows = assert_mrow(_render_blueprints(), min_rows=2)
    assert all(r["attrs"]["data-click"] == "toggleMRow" for r in rows)


def test_blueprints_rows_key_on_name_and_me_te():
    bpo, bpc = _rows(_render_blueprints())[:2]
    lead = _lead(bpo)
    assert [c["tag"] for c in lead] == ["img"]
    assert "m-only" in lead[0]["attrs"]["class"].split()        # phone-only copy of the icon
    # Phone CSS forces width:auto on row children, so without intrinsic size the
    # lead collapses to ~2px until (or unless) the image loads.
    assert lead[0]["attrs"]["width"] == "24" and lead[0]["attrs"]["height"] == "24"
    assert lead[0]["attrs"].get("loading") == "lazy"            # hidden on desktop: never fetched there
    name, mete = _keys(bpo)
    assert name["text"] == "Sample Frigate Blueprint"
    assert mete["text"] == "10/20"
    assert "m-only" in mete["attrs"]["class"].split()           # desktop keeps ME and TE columns
    assert "color:var(--success)" in mete["attrs"]["style"]     # fully researched
    assert _keys(bpc)[0]["text"] == _LONG_BP
    assert _keys(bpc)[1]["text"] == "2/4"
    for r in (bpo, bpc):
        assert list(_labelled(r)) == ["Type", "Runs", "Location", "Calc"]
        _assert_single_value_child(r)
    assert _labelled(bpo)["Type"]["text"] == "BPO"
    assert _labelled(bpo)["Runs"]["text"] == "∞"
    assert _labelled(bpc)["Runs"]["text"] == "5"
    assert _labelled(bpo)["Location"]["text"] == "Personal Hangar"
    assert _labelled(bpo)["Calc"]["kids"][0]["href"] == "/industry?type_id=691"
    # The separate ME and TE cells stay untagged: hidden on phones, where
    # the combined key replaces them.
    plain = [c["text"] for c in bpo["cells"] if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]
    assert plain == ["10", "20"]


def test_blueprints_me_te_key_is_green_only_when_both_are_maxed():
    """10/14: ME is maxed, TE isn't. An `or` in the template's colour test
    would turn this key green."""
    half = {"type_id": 693, "item_id": 3, "quantity": -1, "material_efficiency": 10,
            "time_efficiency": 14, "runs": -1, "location_flag": "Hangar", "location_id": 60000001}
    rows = _rows(_render_blueprints([(half, "Sample Half Researched Blueprint")]))
    row = next(r for r in rows if _keys(r)[0]["text"] == "Sample Half Researched Blueprint")
    mete = _keys(row)[1]
    assert mete["text"] == "10/14"
    assert "var(--success)" not in mete["attrs"]["style"]
    # The desktop ME cell alone is maxed, so it stays green.
    me, te = [c for c in row["cells"] if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]
    assert "var(--success)" in me["attrs"]["style"] and "var(--success)" not in te["attrs"]["style"]


def test_blueprints_header_hides_on_phones():
    html = _render_blueprints()
    assert html.count('<div class="b-table-row m-head"') == 2      # one per group


def test_blueprints_long_names_truncate_on_desktop():
    """§6.3 desktop fix: the name column may shrink below its text, so a
    long name ellipsizes instead of pushing the row (and page) wider."""
    html = _render_blueprints()
    bpc = _rows(html)[1]
    name = _keys(bpc)[0]
    assert "min-width:0" in name["attrs"]["style"]
    icon, text = name["kids"]
    assert icon["class"] == "m-hide"                               # lead replaces it on phones
    for prop in ("min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
        assert prop in text["style"], prop
    head = re.search(r'<div class="b-table-row m-head"[^>]*>\s*<span style="([^"]*)">Blueprint</span>', html)
    assert head, "Blueprint header cell not found"
    for prop in ("min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
        assert prop in head.group(1), prop


# ── Task 16: Wallet journal (§6.4) ───────────────────────────────────

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from app.routes import character_detail as overview_mod  # noqa: E402
from app.routes import journal as journal_mod  # noqa: E402

_PILOT = _NS(character_id=90000001, character_name="Pilot Alpha",
             corporation_name="Sample Corp", alliance_name=None,
             security_status=1.5, birthday=None)


def _render_journal(entries=None):
    entries = entries or [
        {"id": 1, "date": "2026-10-01T12:00:00Z", "ref_type": "bounty_prizes",
         "ref_type_label": "Bounty Prizes", "category": "pve", "amount": 1500000.0,
         "balance": 9000000000.0, "description": "Bounty prize for clearing a sample site",
         "reason": "", "first_party": "Sample Agency", "second_party": "Pilot Alpha", "tax": 0},
        {"id": 2, "date": "2026-10-01T13:00:00Z", "ref_type": "market_escrow",
         "ref_type_label": "Market Escrow", "category": "market", "amount": -2500000.0,
         "balance": None, "description": "", "reason": "", "first_party": "",
         "second_party": "", "tax": None},
    ]
    return _render(journal_mod, "journal.html", "/character/90000001/journal",
                   char=_PILOT, entries=entries, error=None, page=1, has_more=False,
                   category="all", categories=journal_mod.CATEGORY_LABELS,
                   is_corp=False, corp_id=None, division=None)


def _overview_ctx(**over):
    ctx = dict(
        char=_PILOT, killmails_enabled=False, current_wallet=1.0e9,
        journal=[
            {"amount": 1500000.0, "balance": 9000000000.0, "ref_type": "bounty_prizes",
             "description": "Bounty prize for clearing a sample site", "date": "2026-10-01T12:00:00Z"},
            {"amount": -2500000.0, "balance": None, "ref_type": "market_escrow",
             "description": "", "date": "2026-10-01T13:00:00Z"},
        ],
        journal_error=None, chart_data_json='{"labels": [], "values": []}',
        active_range="1m", ranges=["1d", "1w", "1m"],
        active_skill=None, skillqueue=[], completed_skills=[], corp_history=[],
        total_sp_in_queue=0, total_trained_sp=5000000, unallocated_sp=0,
        has_implants_scope=False, last_synced_str="5m ago", queue_remaining=0,
        zkill=[], kills=0, losses=0, has_assets_scope=True, docked_at=None,
        current_system=None, implants=[], jump_clones=[], now=datetime(2026, 10, 3),
    )
    ctx.update(over)
    return ctx


def _render_overview(**over):
    return _render(overview_mod, "character_detail.html", "/character/90000001", **_overview_ctx(**over))


def test_journal_page_rows_key_on_type_and_amount():
    html = _render_journal()
    rows = assert_mrow(html, min_rows=2)
    assert all(r["attrs"]["data-click"] == "toggleMRow" for r in rows)
    gain, loss = _rows(html)[:2]
    kind, amount = _keys(gain)
    assert kind["text"] == "Bounty Prizes"
    assert amount["text"] == "+1.50M ISK"
    assert "color:var(--success)" in amount["attrs"]["style"]
    assert _keys(loss)[1]["text"] == "-2.50M ISK"
    assert "color:var(--danger)" in _keys(loss)[1]["attrs"]["style"]
    assert list(_labelled(gain)) == ["Date", "Description", "Balance after"]
    assert _labelled(gain)["Date"]["text"] == "2026-10-01 12:00"
    assert "Sample Agency → Pilot Alpha" in _labelled(gain)["Description"]["text"]
    assert "Bounty prize for clearing a sample site" in _labelled(gain)["Description"]["text"]
    assert _labelled(gain)["Balance after"]["text"] == "9.00B"
    # Nothing to describe: the cell stays untagged (hidden on phones)
    # rather than opening onto an empty "Description" line.
    assert list(_labelled(loss)) == ["Date", "Balance after"]
    assert _labelled(loss)["Balance after"]["text"] == "—"
    for r in (gain, loss):
        _assert_single_value_child(r)


_BARE_ENTRY = {"id": 3, "date": "2026-10-01T14:00:00Z", "ref_type": "player_donation",
               "ref_type_label": "Player Donation", "category": "transfer", "amount": 100.0,
               "balance": None, "description": "", "reason": "", "first_party": "",
               "second_party": "", "tax": None}


@pytest.mark.parametrize("fields, text", [
    ({"reason": "Sample reason"}, "Sample reason"),
    ({"tax": 1500.0}, "Tax: 1.5K ISK"),
    ({"first_party": "Sample Agency"}, "Sample Agency"),
    ({"second_party": "Pilot Alpha"}, "Pilot Alpha"),
], ids=["reason-only", "tax-only", "first-party-only", "second-party-only"])
def test_journal_any_single_detail_opens_onto_a_description(fields, text):
    """has_details is an `or` of five fields: any one alone labels the cell,
    and its value sits in the single display:block wrapper."""
    row = _rows(_render_journal([{**_BARE_ENTRY, **fields}]))[0]
    assert list(_labelled(row)) == ["Date", "Description", "Balance after"]
    desc = _labelled(row)["Description"]
    assert desc["text"] == text
    assert len(desc["kids"]) == 1 and "display:block" in desc["kids"][0]["style"]
    _assert_single_value_child(row)


def test_journal_zero_tax_alone_is_not_a_detail():
    row = _rows(_render_journal([{**_BARE_ENTRY, "tax": 0}]))[0]
    assert list(_labelled(row)) == ["Date", "Balance after"]


def test_journal_details_wrapper_is_a_plain_block():
    """§6.4: the parties, description, reason and tax share one block
    wrapper: desktop lays it out as the cell did, phones see one flex item
    whose min-width:0 lets the description ellipsize."""
    gain = _rows(_render_journal())[0]
    (wrapper,) = _labelled(gain)["Description"]["kids"]
    assert wrapper["style"] == "display:block;min-width:0;"


def test_journal_page_header_hides_on_phones():
    html = _render_journal()
    assert '<div class="b-table-row m-head" style="border-bottom:2px solid var(--border);padding:0.3rem 0.75rem;">' in html


def test_journal_overview_panel_rows_key_on_type_and_amount():
    html = _render_overview()
    rows = assert_mrow(html, min_rows=2)
    assert all(r["attrs"]["data-click"] == "toggleMRow" for r in rows)
    gain, loss = _rows(html)[:2]
    kind, amount = _keys(gain)
    assert "m-only" in kind["attrs"]["class"].split()          # desktop shows type in the combined cell
    assert kind["text"] == "bounty prizes"
    assert kind["kids"][0]["class"] == "ref-type"
    assert amount["text"] == "+1.50M"
    assert "tx-amount-pos" in amount["attrs"]["class"]
    assert "tx-amount-neg" in _keys(loss)[1]["attrs"]["class"]
    assert list(_labelled(gain)) == ["Date", "Description", "Balance after"]
    assert _labelled(gain)["Description"]["text"] == "bounty prizes Bounty prize for clearing a sample site"
    assert list(_labelled(loss)) == ["Date", "Balance after"]
    for r in (gain, loss):
        _assert_single_value_child(r)
    assert '<div class="b-table-row m-head" style="border-bottom:2px solid var(--border);padding:0.3rem 0.75rem;">' in html


def test_journal_overview_description_is_a_block_so_it_ellipsizes():
    """§6.4, also desktop: an inline span ignores overflow and text-overflow,
    so long descriptions were clipped mid-word. As a block it ellipsizes."""
    gain = _rows(_render_overview())[0]
    combined = _labelled(gain)["Description"]
    ref, desc = combined["kids"]
    assert ref["class"] == "ref-type m-hide"                   # key 1 shows the type on phones
    assert "display:block" in desc["style"]
    assert "text-overflow:ellipsis" in desc["style"]
    assert "margin-left" not in desc["style"]


# ── Task 17: Asset Search (§6.5) ─────────────────────────────────────

from app.routes import assets as assets_mod  # noqa: E402


def _render_asset_results():
    pilot = _NS(character_id=90000001, character_name="Pilot Alpha", sort_order=0)
    stack = {"type_id": 34, "type_name": "Tritanium", "quantity": 125000, "is_singleton": False,
             "location_name": "Sample Station", "region": "Sample Region", "security": 0.9,
             "location_flag": "Hangar", "jump_dist": 3, "has_origin": True}
    ship = {"type_id": 587, "type_name": "Sample Frigate", "quantity": 1, "is_singleton": True,
            "location_name": None, "region": None, "security": None,
            "location_flag": "ShipHangar", "jump_dist": None, "has_origin": False}
    groups = {"Main": {90000001: {"char": pilot, "assets": [stack, ship]}}}
    tmpl = assets_mod.templates.env.get_template("partials/assets_results.html")
    return tmpl.render(groups=groups, sorted_group_names=["Main"])


def test_assets_results_rows_key_on_item_and_quantity():
    html = _render_asset_results()
    rows = assert_mrow(html, min_rows=2)
    assert all(r["attrs"]["data-click"] == "toggleMRow" for r in rows)
    stack, ship = _rows(html)[:2]
    assert [c["tag"] for c in _lead(stack)] == ["img"]
    item, qty = _keys(stack)
    assert item["text"] == "Tritanium"
    assert qty["text"] == "×125,000"
    assert _keys(ship)[1]["text"] == "—"                         # singleton
    assert list(_labelled(stack)) == ["Location", "Sec", "Flag", "Jumps"]
    assert _labelled(stack)["Location"]["text"] == "Sample Station · Sample Region"
    assert _labelled(stack)["Sec"]["text"] == "0.9"
    assert _labelled(stack)["Flag"]["text"] == "Hangar"
    assert _labelled(stack)["Jumps"]["text"] == "3j"
    assert _labelled(ship)["Location"]["text"] == "Unknown"
    assert _labelled(ship)["Flag"]["text"] == "Ship Hangar"
    for r in (stack, ship):
        _assert_single_value_child(r)


def test_assets_results_header_hides_on_phones():
    html = _render_asset_results()
    assert re.search(r'<div class="[^"]*\bb-table-row\b[^"]*\bm-head\b[^"]*"', html)


def test_assets_lead_icon_holds_its_size_while_loading():
    img = _lead(_rows(_render_asset_results())[0])[0]
    assert img["attrs"].get("width") == "32" and img["attrs"].get("height") == "32"


def test_assets_search_bar_stacks_on_phones():
    html = _render(assets_mod, "assets.html", "/assets",
                   characters=[{"character_id": 90000001, "character_name": "Pilot Alpha",
                                "system_name": "Sample System"}],
                   active_char_id=90000001)
    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    page_css = _norm(next(s for s in styles if ".asset-search-bar {" in s))
    assert "@media (max-width: 640px) {" in page_css, "assets.html needs a phone block in its own <style>"
    css = page_css.split("@media (max-width: 640px) {", 1)[1]
    assert re.search(r"\.asset-search-bar \{ flex-direction: column; align-items: stretch;", css)
    assert re.search(r"\.asset-input \{ max-width: none; \}", css)
    assert re.search(r"\.asset-controls-right \{ flex-direction: column; align-items: stretch;", css)
    assert re.search(r"\.asset-select \{ width: 100%; max-width: 100%; \}", css)
    assert re.search(r'\.m-row > \[data-m-label="Location"\] \{ white-space: normal !important; padding-left: 0 !important; \}', css)


# ── Task 18: Character overview scroll boxes (§4.6, ISS-104) ─────────

class _Clamps(HTMLParser):
    """Collects every .m-clamp (with its direct-child count), every
    .m-showall button (with whether a .m-clamp-wrap encloses it), every
    .m-unclamp element's classes, and counts .m-clamp-wraps, including any
    nested in another."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []        # [tag, classes, clamp_ref]
        self.clamps = []
        self.showall = []
        self.unclamped = []
        self.wraps = 0
        self.nested_wraps = 0
        self._btn = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        cls = a.get("class", "").split()
        if self.stack and self.stack[-1][2] is not None:
            self.stack[-1][2]["children"] += 1
        in_wrap = any("m-clamp-wrap" in e[1] for e in self.stack)
        if "m-clamp-wrap" in cls:
            self.wraps += 1
            self.nested_wraps += in_wrap
        ref = None
        if "m-clamp" in cls:
            ref = {"children": 0, "classes": cls}
            self.clamps.append(ref)
        if "m-unclamp" in cls:
            self.unclamped.append(cls)
        if "m-showall" in cls:
            self._btn = {"in_wrap": in_wrap, "attrs": a, "text": ""}
            self.showall.append(self._btn)
        if tag not in _VOID:
            self.stack.append([tag, cls, ref])

    def handle_endtag(self, tag):
        if tag == "button":
            self._btn = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._btn is not None:
            self._btn["text"] += data


def _clamps(html):
    p = _Clamps()
    p.feed(html)
    p.close()
    for b in p.showall:
        b["text"] = _norm(b["text"])
    return p


def _queue(n):
    return [{"skill_name": f"Sample Skill {i}", "finished_level": 3, "remaining_seconds": 3600 * (i + 1)}
            for i in range(n)]


def _long_overview(n):
    return _render_overview(
        active_skill=_queue(1)[0], skillqueue=_queue(n), queue_remaining=3600 * n,
        completed_skills=[{"skill_name": f"Done Skill {i}", "finished_level": 4, "completed_ago": 7200}
                          for i in range(n)],
        corp_history=[{"corporation_id": 98000000 + i, "corporation_name": f"Sample Corp {i}",
                       "start_date": "2020-01-01", "days_in": 30, "is_current": i == 0}
                      for i in range(n)],
        jump_clones=[{"location": "Sample Station", "implants": []}],
    )


def _render_assets_partial(*sizes, docked_at=None):
    locations = [{"location": f"Sample Station {n}", "items": [{"name": f"Item {i}", "quantity": i + 1} for i in range(size)]}
                 for n, size in enumerate(sizes)]
    tmpl = overview_mod.templates.env.get_template("partials/assets_partial.html")
    return tmpl.render(locations=locations, docked_at=docked_at)


_ASSETS_LOADING = '<div style="color: var(--muted); font-size: 10px; padding: 1rem 0.75rem;">Loading…</div>'


def _overview_with_assets(n, *sizes):
    """The overview with the assets partial swapped in where htmx would put it."""
    return _long_overview(n).replace(_ASSETS_LOADING, _render_assets_partial(*sizes))


def test_unclamp_overview_scroll_boxes_on_phones():
    c = _clamps(_long_overview(12))
    # queue, recently completed, corp history, the assets container, and the
    # jump clone's implant list
    assert len(c.unclamped) == 5, c.unclamped
    assert ["assets-container", "m-unclamp"] in c.unclamped
    assert ["asset-list", "m-unclamp"] in c.unclamped


def test_unclamp_long_lists_offer_show_all():
    c = _clamps(_long_overview(12))
    assert [k["children"] for k in c.clamps] == [12, 12, 12]
    assert [b["text"] for b in c.showall] == ["Show all 12"] * 3
    for b in c.showall:
        assert b["in_wrap"]
        assert b["attrs"]["data-click"] == "toggleExpanded"
        assert b["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
        assert b["attrs"]["type"] == "button"
        assert "m-only" in b["attrs"]["class"].split()


def test_unclamp_short_lists_have_no_show_all():
    c = _clamps(_long_overview(10))
    assert c.showall == []
    assert len(c.clamps) == 3          # the clamp is inert at ten rows or fewer


def test_unclamp_asset_lists_clamp_per_location():
    c = _clamps(_render_assets_partial(12, 3))
    assert [k["children"] for k in c.clamps] == [12, 3]
    assert [b["text"] for b in c.showall] == ["Show all 12"]
    assert c.showall[0]["in_wrap"]
    assert len(c.unclamped) == 2


def test_unclamp_clamp_wraps_never_nest():
    """Task 1's clamp rules use descendant selectors: a wrap inside a wrap
    would clamp the inner list from outside and hide its Show all button."""
    html = _overview_with_assets(12, 12, 12)
    assert "Sample Station 1" in html
    c = _clamps(html)
    assert c.wraps == 5                # queue, completed, corp history, 2 asset lists
    assert c.nested_wraps == 0


def test_unclamp_eleven_rows_is_the_first_to_offer_show_all():
    """Show all appears past the 10th row: n=11 is the edge (n=10 has none)."""
    c = _clamps(_long_overview(11))
    assert [k["children"] for k in c.clamps] == [11, 11, 11]
    assert [b["text"] for b in c.showall] == ["Show all 11"] * 3
    assets = _clamps(_render_assets_partial(11, 10))
    assert [b["text"] for b in assets.showall] == ["Show all 11"]


def test_unclamp_current_location_list_starts_open():
    """The docked-at location renders open; its list keeps the clamp and
    its Show all button inside the same wrap."""
    html = _render_assets_partial(12, 3, docked_at="Sample Station 0")
    wraps = re.findall(r'<div class="(asset-list [^"]*)">', html)
    assert wraps == ["asset-list m-unclamp m-clamp-wrap open", "asset-list m-unclamp m-clamp-wrap"]
    assert "▸ Sample Station 0" in html and "▸ Sample Station 1" not in html
    c = _clamps(html)
    assert [k["children"] for k in c.clamps] == [12, 3]
    assert [b["text"] for b in c.showall] == ["Show all 12"] and c.showall[0]["in_wrap"]


class _Styled(HTMLParser):
    """Every start tag's name, classes and inline style."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a.get("class", "").split(), a.get("style", "")))


def test_unclamp_every_inline_scroll_box_in_the_overview():
    """Any element of the overview whose inline style makes it a scroll box
    (max-height plus overflow-y:auto) traps touch scrolling on phones unless
    it carries m-unclamp. Scoped to <main>: base.html's own dropdowns are
    meant to scroll."""
    html = _overview_with_assets(12, 12, 12)
    main = html.split('<main class="b-main">', 1)[1].split("</main>", 1)[0]
    p = _Styled()
    p.feed(main)
    p.close()
    boxes = []
    for tag, cls, style in p.tags:
        flat = re.sub(r"\s+", "", style)
        if "max-height:" in flat and re.search(r"overflow(-y)?:(auto|scroll)", flat):
            boxes.append((tag, cls, style))
    assert len(boxes) >= 3, boxes      # queue, recently completed, corp history
    for tag, cls, style in boxes:
        assert "m-unclamp" in cls, (tag, cls, style)


def test_unclamp_show_all_button_is_styled_in_site_css():
    with open(_SITE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert re.search(r"\.m-showall\s*\{[^}]*width:\s*100%", css)


# ── Polish B ──────────────────────────────────────────────────────────



def _pb_row_html(lead):
    return f'<div class="m-row" data-click="toggleMRow">{lead}<span data-m="key">Name</span></div>'


@pytest.mark.parametrize("lead, msg", [
    ('<img data-m="lead" src="x">', "positive width"),
    ('<img data-m="lead" src="x" height="24">', "positive width"),
    ('<img data-m="lead" src="x" width="24">', "positive height"),
    ('<img data-m="lead" src="x" width="" height="24">', "positive width"),
    ('<img data-m="lead" src="x" width="0" height="24">', "positive width"),
    ('<img data-m="lead" src="x" width="24" height="24" style="width:32px;height:32px;">', "doesn't match"),
])
def test_polish_b_helper_rejects_image_leads_without_a_size(lead, msg):
    """Phone CSS forces width:auto !important on row children, so an image
    lead without width/height collapses to ~2px until (or unless) it loads."""
    with pytest.raises(AssertionError, match=msg):
        assert_mrow(_pb_row_html(lead))


@pytest.mark.parametrize("lead", [
    '<img data-m="lead" src="x" width="28" height="28" style="width:28px;height:28px;border-radius:2px;">',
    '<img data-m="lead" src="x" width="32" height="32" style="min-width:0;max-width:40px;">',
    '<td data-m="lead"><span class="b-dot" style="width:7px;height:7px;"></span></td>',
    '<span data-m="lead"><img src="x"></span>',
])
def test_polish_b_helper_accepts_sized_image_leads_and_other_leads(lead):
    """A dot or a wrapper isn't an image lead (the dashboard's online dot),
    and min-/max-width aren't the img's own inline size."""
    assert_mrow(_pb_row_html(lead))


def test_polish_b_every_r1_image_lead_carries_its_size():
    pages = (_render_planetary(), _render_blueprints(), _render_asset_results())
    for html in pages:
        rows = assert_mrow(html, min_rows=2)
        assert any(t == "img" for r in rows for t in r["child_tags"])


def _pb_render_blueprints():
    raw = [
        {"type_id": 691, "item_id": 1, "quantity": -1, "material_efficiency": 10,
         "time_efficiency": 20, "runs": -1, "location_flag": "Hangar", "location_id": 60000001},
        {"type_id": 692, "item_id": 2, "quantity": -1, "material_efficiency": 0,
         "time_efficiency": 0, "runs": -1, "location_flag": "Hangar", "location_id": 60000001},
        {"type_id": 693, "item_id": 3, "quantity": -1, "material_efficiency": 0,
         "time_efficiency": 4, "runs": -1, "location_flag": "Hangar", "location_id": 60000001},
    ]
    names = {691: "Sample Alpha Blueprint", 692: "Sample Bravo Blueprint", 693: "Sample Charlie Blueprint"}
    bps = blueprints_mod._process_blueprints(raw, names)
    char = {"character_id": 90000001, "character_name": "Pilot Alpha",
            "corporation_id": None, "corporation_name": None}
    html = _render(blueprints_mod, "blueprints.html", "/character/90000001/blueprints",
                   char=char, blueprints=bps, groups=blueprints_mod._group_blueprints(bps, "type"),
                   stats=blueprints_mod._compute_stats(bps), error=None, is_corp=False,
                   corp_id=None, filter="all", group_by="type")
    return {_keys(r)[0]["text"]: r for r in _rows(html)}


def test_polish_b_blueprint_calc_link_is_a_tap_target():
    """In an open phone row the Calc link is a 40px m-tap target; the class
    has no desktop rule, so desktop keeps the small inline link."""
    for row in _pb_render_blueprints().values():
        (link,) = _labelled(row)["Calc"]["kids"]
        assert "m-tap" in link["class"].split()
        assert "font-size:9px" in link["style"]


def test_polish_b_blueprint_me_te_key_is_labelled():
    """The ME and TE headers are hidden on phones, so the combined key
    names itself."""
    rows = _pb_render_blueprints()
    for name, want in (("Sample Alpha Blueprint", "ME 10 / TE 20"),
                       ("Sample Bravo Blueprint", "ME 0 / TE 0"),
                       ("Sample Charlie Blueprint", "ME 0 / TE 4")):
        mete = _keys(rows[name])[1]
        assert mete["attrs"]["title"] == want
        assert mete["attrs"]["aria-label"] == want


def test_polish_b_blueprint_unresearched_me_te_is_muted():
    """0/0 is muted like the desktop ME and TE cells' zeros; any research
    shows in text colour; fully researched stays green."""
    rows = _pb_render_blueprints()
    styles = {n: _keys(r)[1]["attrs"]["style"] for n, r in rows.items()}
    assert "color:var(--success)" in styles["Sample Alpha Blueprint"]
    assert "color:var(--muted)" in styles["Sample Bravo Blueprint"]
    assert "color:var(--text)" in styles["Sample Charlie Blueprint"]
    assert "color:var(--success)" not in styles["Sample Charlie Blueprint"]


def test_polish_b_journal_type_badge_is_classed_for_phones():
    """Phone CSS centres the badge and lifts its 120px cap through this
    class; the inline desktop styles stay as they were."""
    gain = _rows(_render_journal())[0]
    (badge,) = _keys(gain)[0]["kids"]
    assert "journal-type" in badge["class"].split()
    assert "max-width:120px" in badge["style"]
    assert "display:inline-block" in badge["style"]


from tests.test_mobile_css import _media_bodies as _pb_media_bodies  # noqa: E402


def test_polish_b_planet_detail_scrolls_inside_itself_on_phones():
    html = _render_planetary()
    (style,) = [b for b in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S) if ".pi-row-detail" in b]
    phone = _pb_media_bodies(re.sub(r"/\*.*?\*/", "", style, flags=re.S), "max-width: 640px")
    assert phone, "planetary.html needs a phone block in its own <style>"
    assert re.search(r"\.pi-row-detail\s*\{\s*overflow-x:\s*auto;?\s*\}", phone)


class _PBPieces(HTMLParser):
    """For each .m-row's labelled cell `label`: (classes, own text) of every
    element inside it, in document order."""

    def __init__(self, label):
        super().__init__(convert_charrefs=True)
        self.label, self.stack, self.rows = label, [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "m-row" in a.get("class", "").split():
            self.rows.append([])
        inside = any(e[1] for e in self.stack) or bool(
            self.stack and self.stack[-1][2] and a.get("data-m-label") == self.label)
        piece = None
        if inside and a.get("data-m-label") != self.label:
            piece = [set(a.get("class", "").split()), ""]
            self.rows[-1].append(piece)
        if tag not in _VOID:
            self.stack.append((tag, inside, "m-row" in a.get("class", "").split(), piece))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for e in reversed(self.stack):
            if e[3] is not None:
                e[3][1] += data
                return


def _pb_pieces(html, label):
    p = _PBPieces(label)
    p.feed(html)
    p.close()
    return [[(cls, _norm(t)) for cls, t in row] for row in p.rows]


_PB_LONG_DESC = ("Contract reward for hauling a very large container of assorted goods "
                 "from the sample hub to the far region")                     # 108 chars
_PB_LONG_REASON = "Fuel and supplies for the sample structure out in the far region"  # 64 chars


def _pb_render_journal_long():
    def entry(i, description="", reason="", fp="", sp=""):
        return {"id": i, "date": f"2026-10-01T1{i}:00:00Z", "ref_type": "player_donation",
                "ref_type_label": "Player Donation", "category": "other", "amount": 1000.0,
                "balance": 5000.0, "description": description, "reason": reason,
                "first_party": fp, "second_party": sp, "tax": None}
    entries = [entry(0, _PB_LONG_DESC, _PB_LONG_REASON, fp="Sample Agency"),
               entry(1, "A short description", "A short reason")]
    return _render(journal_mod, "journal.html", "/character/90000001/journal",
                   char=_PILOT, entries=entries, error=None, page=1, has_more=False,
                   category="all", categories=journal_mod.CATEGORY_LABELS,
                   is_corp=False, corp_id=None, division=None)


def test_polish_b_journal_cut_text_has_a_full_phone_copy():
    """Desktop keeps the cut copy (m-hide on phones); an open phone row shows
    the full text (m-only, never shown on desktop)."""
    assert len(_PB_LONG_DESC) > 80 and len(_PB_LONG_REASON) > 60
    long_row = _pb_pieces(_pb_render_journal_long(), "Description")[0]
    assert ({"m-hide"}, _PB_LONG_DESC[:80] + "…") in long_row
    assert ({"m-only"}, _PB_LONG_DESC) in long_row
    assert ({"m-hide"}, _PB_LONG_REASON[:60]) in long_row
    assert ({"m-only"}, _PB_LONG_REASON) in long_row
    # Both copies sit in the one value wrapper: the cell still has one child.
    _assert_single_value_child(_rows(_pb_render_journal_long())[0])


def test_polish_b_journal_uncut_text_renders_once():
    short_row = _pb_pieces(_pb_render_journal_long(), "Description")[1]
    assert not any(cls & {"m-hide", "m-only"} for cls, _ in short_row)
    texts = [t for _, t in short_row]
    assert "A short description" in texts and "A short reason" in texts


def test_polish_b_overview_cut_description_has_a_full_phone_copy():
    journal = [
        {"amount": 1.0, "balance": 2.0, "ref_type": "player_donation",
         "description": _PB_LONG_DESC, "date": "2026-10-01T12:00:00Z"},
        {"amount": 1.0, "balance": 2.0, "ref_type": "player_donation",
         "description": "A short description", "date": "2026-10-01T13:00:00Z"},
    ]
    html = _render_overview(journal=journal)
    long_row, short_row = _pb_pieces(html, "Description")[:2]
    assert ({"m-hide"}, _PB_LONG_DESC[:60] + "…") in long_row
    assert ({"m-only"}, _PB_LONG_DESC) in long_row
    assert [cls for cls, _ in short_row] == [{"ref-type", "m-hide"}, set()]
    assert short_row[1][1] == "A short description"
    for r in _rows(html)[:2]:
        _assert_single_value_child(r)
