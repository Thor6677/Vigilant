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
    element children (attrs of the child's own direct children)."""

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
    """An open row lays a labelled cell out as a flex row (label · value).
    More than one visible element child would spread across that row, so
    multi-part values need exactly one wrapper. m-hide children don't count."""
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
