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
    shared_name = _keys(shared)[0]["text"]
    assert "1 skill" in shared_name and " SP" not in shared_name
