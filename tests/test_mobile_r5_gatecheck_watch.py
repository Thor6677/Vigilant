"""Mobile R5 T3: Gate Check, Watchlist, Trending and the WH Tracker at
phone width. Desktop renders as before (D21): everything here is a class or
data attribute desktop has no rule for, an m-only cell, or a rule in this
task's phone section of site.css.

- Gate Check waypoints (D8 A) keep their <details> kill expansion and gain
  hooks that fold each row into two lines on phones: number, security,
  system and kills or ✓ first; region and danger flags below. The Gatecamp
  Finder and War Targets summaries take the same shape.
- Gate Check's three tabs become an equal-width 44px bar (D9 A), and its
  two forms stack.
- Watchlist alerts become m-rows: kind tag · system · HH:MM (D10 A).
- Trending's most-violent rows, built in JS, become m-rows (D11 A).
- The WH Tracker's character picker and Refresh button become thumb-sized.

Contexts follow the shapes the routes build (app/intel/safety.py,
app/routes/intel_watch.py, app/routes/wh_tracker.py); every name is
invented."""
import functools
import re
from datetime import datetime
from html.parser import HTMLParser

from app.routes import gatecheck as gatecheck_mod
from app.routes import intel_watch as watch_mod
from app.routes import starmap as starmap_mod
from app.routes import wh_tracker as tracker_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           css_section, norm, phone_block, render_page, row_keys,
                           row_labelled, row_lead, rule_bodies, selectors, source)

_section = functools.partial(css_section, release="R5")


def _phone():
    body, after = phone_block(_section("T3"))
    return body, after


# ── a small parser: elements by class, with their direct children ─────

class _Kids(HTMLParser):
    """Every element carrying class `cls`: its tag, attrs and classes, plus
    each direct child's tag, attrs, classes and text (normalised later)."""

    def __init__(self, cls):
        super().__init__(convert_charrefs=True)
        self.cls = cls
        self.stack = []   # [tag, kind, ref]; kind "el" | "kid" | "in" | None
        self.out = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        cls = a.get("class", "").split()
        parent = self.stack[-1] if self.stack else None
        entry = [tag, None, None]
        if parent and parent[1] == "el":
            kid = {"tag": tag, "attrs": a, "classes": cls, "text": ""}
            parent[2]["kids"].append(kid)
            entry = [tag, "kid", kid]
        elif parent and parent[1] in ("kid", "in"):
            entry = [tag, "in", parent[2]]
        if self.cls in cls:
            el = {"tag": tag, "attrs": a, "classes": cls, "kids": []}
            self.out.append(el)
            entry = [tag, "el", el]
        if tag not in VOID:
            self.stack.append(entry)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for e in reversed(self.stack):
            if e[1] in ("kid", "in"):
                e[2]["text"] += data
                return
            if e[1] == "el":
                return


def _by_class(html, cls):
    p = _Kids(cls)
    p.feed(html)
    p.close()
    for el in p.out:
        for k in el["kids"]:
            k["text"] = norm(k["text"])
    return p.out


class _Tags(HTMLParser):
    """Every start tag as (tag, attrs), in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def _tags(html):
    p = _Tags()
    p.feed(html)
    p.close()
    return p.tags


_FLAGS = ("DICTOR", "HIC", "SMARTBOMB")


# ── Gate Check fixtures (app/intel/safety.py shapes) ──────────────────

def _kill(kid, npc=False):
    return {"killmail_id": kid, "time_str": "4m ago", "victim_ship": "Sample Hauler",
            "victim_ship_id": 648, "victim_char_id": 90000101, "attacker_count": 6,
            "attacker_ships": [{"name": "Sample Interdictor", "type_id": 22456, "count": 2}],
            "attacker_weapons": {}, "value": 1.2e8, "value_str": "120M", "is_npc": npc}


def _system(name, region, sec, kills=(), threat="safe", dictor=False, hic=False, sb=False, **extra):
    kills = list(kills)
    return {"system_id": 30090000 + len(name), "system_name": name, "security": sec,
            "sec_color": "#33aa55", "region": region,
            "kill_count": len(kills), "pvp_kills": sum(1 for k in kills if not k["is_npc"]),
            "threat": threat, "has_smartbombs": sb, "has_dictors": dictor, "has_hics": hic,
            "total_value": 2.4e8, "total_value_str": "240M", "kills": kills, **extra}


# Four waypoints: two safe, two with kills and danger flags.
_ROUTE = [
    _system("Origin Sample", "Sample Reach", 1.0, waypoint=1),
    _system("Danger Sample", "Test Expanse", 0.4, [_kill(1), _kill(2), _kill(3, npc=True)],
            threat="dangerous", dictor=True, waypoint=2),
    _system("Bomb Sample", "Test Expanse", 0.2, [_kill(4), _kill(5)],
            threat="smartbomb", hic=True, sb=True, waypoint=3),
    _system("Dest Sample", "Sample Reach", 0.9, waypoint=4),
]


def _render_route():
    return render_page(gatecheck_mod, "partials/gatecheck_route.html", "/intel/gatecheck/check",
                       systems=_ROUTE, total_jumps=4, total_kills=4, dangerous_count=2,
                       caution_count=0, origin="Origin Sample", destination="Dest Sample",
                       avoid_ids=[], starmap_pref="shortest")


def _expected_flags(s):
    return [f for f, on in zip(_FLAGS, (s["has_dictors"], s["has_hics"], s["has_smartbombs"])) if on]


def _assert_two_lines(row, s, *, kills_text):
    """A .gc-wp row: line 2 is the region then the danger flags (gc-wp-l2,
    in that order); everything else stays on line 1, with the system name
    as its gc-wp-name cell and the kill count (or ✓) present."""
    kids = row["kids"]
    l2 = [k for k in kids if "gc-wp-l2" in k["classes"]]
    l1 = [k for k in kids if "gc-wp-l2" not in k["classes"]]
    assert l2, f"{s['system_name']}: no line-2 cells"
    assert "gc-wp-region" in l2[0]["classes"] and l2[0]["text"] == s["region"], l2[0]
    assert [k["text"] for k in l2[1:]] == _expected_flags(s)
    assert all("gc-flag" in k["classes"] for k in l2[1:])
    # No flag or region is left on line 1.
    assert not [k for k in l1 if k["text"] in _FLAGS or k["text"] == s["region"]]
    names = [k for k in l1 if "gc-wp-name" in k["classes"]]
    assert len(names) == 1 and names[0]["text"] == s["system_name"]
    assert kills_text in [k["text"] for k in l1], [k["text"] for k in l1]
    # Line-2 cells come after the name in the markup (desktop order kept).
    assert kids.index(names[0]) < kids.index(l2[0])


# ── Gate Check: route waypoints (D8 A) ────────────────────────────────

def test_route_waypoints_carry_the_two_line_hooks():
    rows = _by_class(_render_route(), "gc-wp")
    assert len(rows) == 4
    for row, s in zip(rows, _ROUTE):
        assert "gc-wp--jump" in row["classes"], "route rows indent line 2 past the jump number"
        assert row["kids"][0]["text"] == str(s["waypoint"])
        kills = f"{s['pvp_kills']} kills" if s["pvp_kills"] else "✓"
        _assert_two_lines(row, s, kills_text=kills)


def test_route_kill_expansion_is_unchanged():
    html = _render_route()
    tags = _tags(html)
    details = [a for t, a in tags if t == "details"]
    assert len(details) == 2, "only waypoints with kills expand"
    assert all("open" not in a for a in details)
    rows = _by_class(html, "gc-wp")
    assert [r["tag"] for r in rows] == ["div", "summary", "summary", "div"]
    for r in rows:
        name = next(k for k in r["kids"] if "gc-wp-name" in k["classes"])
        assert name["tag"] == "a" and name["attrs"]["href"].startswith("https://zkillboard.com/system/")
        if r["tag"] == "summary":
            # The link opens zKillboard without toggling the <details>.
            assert name["attrs"].get("data-mousedown") == "noop" and "data-stop" in name["attrs"]
    # NPC kills stay filtered; every other kill still lists under its waypoint.
    kill_links = [a for t, a in tags if t == "a" and a.get("href", "").startswith("https://zkillboard.com/kill/")]
    assert len(kill_links) == 4
    # These rows are native <details>, never m-rows.
    assert "m-row" not in html and "toggleMRow" not in html


# ── Gate Check: Gatecamp Finder and War Targets ───────────────────────

_CAMPS = [
    _system("Camp Sample", "Sample Reach", 0.3, [_kill(11), _kill(12), _kill(13)],
            threat="dangerous", dictor=True, sb=True),
    _system("Quiet Sample", "Test Expanse", 0.5, [_kill(14), _kill(15), _kill(16)]),
]


def test_finder_summaries_take_the_two_line_shape():
    html = render_page(gatecheck_mod, "partials/gatecheck_finder.html", "/intel/gatecheck/finder",
                       camps=_CAMPS)
    rows = _by_class(html, "gc-wp")
    assert [r["tag"] for r in rows] == ["summary", "summary"]
    for row, s in zip(rows, _CAMPS):
        assert "gc-wp--jump" not in row["classes"]
        _assert_two_lines(row, s, kills_text=f"{s['pvp_kills']} kills")
        assert s["total_value_str"] in [k["text"] for k in row["kids"]]   # value stays on line 1
    assert len([t for t, _ in _tags(html) if t == "details"]) == 2


def test_war_target_summaries_take_the_two_line_shape():
    systems = [_system("Target Sample", "Sample Reach", -0.2, [_kill(21), _kill(22)], hic=True),
               _system("Roam Sample", "Test Expanse", 0.1, [_kill(23)])]
    html = render_page(gatecheck_mod, "partials/gatecheck_wartarget.html", "/intel/gatecheck/wartargets",
                       entity_name="Sample Raiders", entity_type="corporation",
                       total_kills=2, total_losses=1, systems=systems)
    rows = _by_class(html, "gc-wp")
    assert [r["tag"] for r in rows] == ["summary", "summary"]
    for row, s in zip(rows, systems):
        kms = f"{s['kill_count']} km{'s' if s['kill_count'] != 1 else ''}"
        _assert_two_lines(row, s, kills_text=kms)
    assert "m-row" not in html


# ── Gate Check page: tabs (D9 A) and forms ────────────────────────────

def _render_gatecheck():
    locs = [{"system_name": "Origin Sample", "character_name": "Pilot Sample"}]
    return render_page(gatecheck_mod, "gatecheck.html", "/intel/gatecheck", char_locations=locs)


def test_gatecheck_tab_bar_keeps_its_three_gctab_buttons():
    html = _render_gatecheck()
    bars = _by_class(html, "gc-tabs")
    assert len(bars) == 1
    tabs = bars[0]["kids"]
    assert [k["tag"] for k in tabs] == ["button"] * 3
    assert [k["attrs"].get("data-tab") for k in tabs] == ["checker", "finder", "wartargets"]
    assert all("gc-tab" in k["classes"] and k["attrs"].get("data-click") == "gcTab" for k in tabs)
    assert ["is-active" in k["classes"] for k in tabs] == [True, False, False]
    assert "function gcTab()" in html


def test_gatecheck_forms_stack_on_phones():
    stacks = _by_class(_render_gatecheck(), "m-stack")
    assert len(stacks) == 2
    route, wt = stacks
    src = source("gatecheck.html")
    form_row = src[src.index('id="gc-form"'):src.index("Avoid systems")]
    assert "m-stack" in form_row
    for name in ('name="origin"', 'name="destination"', 'name="flag"', 'type="submit"'):
        assert name in form_row
    wt_row = src[src.index('hx-post="/intel/gatecheck/wartargets"'):src.index("Shows where and when")]
    assert "m-stack" in wt_row
    for name in ('name="entity_name"', 'name="entity_type"', 'type="submit"'):
        assert name in wt_row
    # Both stacks are the flex rows holding the fields, so each field is a child.
    assert len(route["kids"]) == 4 and len(wt["kids"]) == 3


def test_autocomplete_rows_keep_their_class():
    src = source("gatecheck.html")
    assert "'<div class=\"gc-dd-item\" data-mousedown=\"gcPick\"'" in src


# ── Watchlist (D10 A) ─────────────────────────────────────────────────

_ALERTS = [
    {"id": 1, "kind": "system_watch", "killmail_id": 9000001, "system_id": 30090001,
     "system_name": "Watch Sample", "matched_label": "Staging",
     "matched_entity_id": None, "triggered_at": datetime(2026, 10, 4, 18, 42)},
    {"id": 2, "kind": "hunter_watch", "killmail_id": 9000002, "system_id": 30090002,
     "system_name": "Hunt Sample", "matched_label": "Sample Raiders",
     "matched_entity_id": 98000001, "triggered_at": datetime(2026, 10, 4, 7, 5)},
    {"id": 3, "kind": "system_watch", "killmail_id": 9000003, "system_id": 30090003,
     "system_name": "Plain Sample", "matched_label": None,
     "matched_entity_id": None, "triggered_at": datetime(2026, 10, 3, 23, 1)},
]


def _render_watch(alerts=_ALERTS):
    systems = [{"id": 1, "system_id": 30090001, "label": "Staging", "system_name": "Watch Sample",
                "security": 0.4},
               {"id": 2, "system_id": 30090003, "label": None, "system_name": "Plain Sample",
                "security": None}]
    hunters = [{"id": 1, "kind": "corporation", "entity_id": 98000001, "label": "Sample Raiders",
                "notes": "roams at night"},
               {"id": 2, "kind": "character", "entity_id": 90000201, "label": None, "notes": None}]
    return render_page(watch_mod, "intel_watch.html", "/intel/watch", systems=systems,
                       hunters=hunters, unwatched_asset_systems=[], asset_system_count=0,
                       alerts=alerts)


def test_watch_alerts_are_mrows():
    html = _render_watch()
    rows = assert_mrow(html, 3)
    assert len(rows) == 3, "only the alerts are m-rows"
    assert all(r["attrs"].get("data-click") == "toggleMRow" for r in rows)
    assert all("w-row" in r["attrs"]["class"].split() for r in rows)


def test_watch_alert_keys_lead_and_labels():
    rows = cells_rows(_render_watch())
    assert len(rows) == 3
    for row, a in zip(rows, _ALERTS):
        lead = row_lead(row)
        kind = "SYSTEM" if a["kind"] == "system_watch" else "HUNTER"
        assert len(lead) == 1 and lead[0]["text"] == kind
        keys = row_keys(row)
        assert [k["text"] for k in keys] == [a["system_name"], a["triggered_at"].strftime("%H:%M")]
        assert "b-muted" in keys[1]["attrs"].get("class", "").split(), "key 2 (the time) is muted"
        labelled = [c for c in row["cells"] if "data-m-label" in c["attrs"]]
        assert [c["attrs"]["data-m-label"] for c in labelled] == ["Name", "Matched", "Time", "Kill"]
        cells = row_labelled(row)
        assert cells["Name"]["text"] == a["system_name"]
        assert cells["Matched"]["text"] == (a["matched_label"] or "—")
        assert cells["Time"]["text"] == a["triggered_at"].strftime("%Y-%m-%d %H:%M") + " UTC"
        kill = cells["Kill"]["kids"][0]
        assert kill["href"] == f"https://zkillboard.com/kill/{a['killmail_id']}/"
        # A 40px link; not .b-btn, whose flex:1 would stretch it across the open row.
        assert kill["class"].split() == ["m-tap"]
        assert_single_value_child(row)
        # Every phone cell is phone-only: desktop keeps its own markup.
        for c in keys + labelled + lead:
            assert "m-only" in c["attrs"].get("class", "").split(), c


def test_watch_alerts_with_none_still_render_the_empty_note():
    html = _render_watch(alerts=[])
    assert "No alerts yet." in html and "m-row" not in html


def test_watch_lists_get_tap_sized_buttons_and_stacked_forms():
    html = _render_watch()
    tags = _tags(html)
    removes = [a for t, a in tags if t == "button" and a.get("class", "").split()[:2] == ["b-btn", "is-danger"]]
    assert len(removes) == 4, "two systems and two hunters"
    assert all("m-tap" in a["class"].split() for a in removes)
    zkb = [a for t, a in tags if t == "a" and re.match(r"https://zkillboard\.com/(corporation|character)/", a.get("href", ""))]
    assert len(zkb) == 2 and all("m-tap" in a["class"].split() for a in zkb)
    forms = [a for t, a in tags if t == "form" and "w-form" in a.get("class", "").split()]
    assert len(forms) == 2 and all("m-stack" in a["class"].split() for a in forms)


# ── Trending (D11 A) ──────────────────────────────────────────────────

def _violent_template():
    src = source("trending.html")
    start = src.index("vc.innerHTML = violentResp.systems.map(")
    tpl_start = src.index("return `", start) + len("return `")
    tpl_end = src.index("`;", tpl_start)
    return src, start, src[tpl_start:tpl_end]


def _fill(tpl):
    """Replace each top-level ${…} in a JS template literal with "v", so
    the rest parses as HTML. Brace-matched, so a nested template inside an
    interpolation goes with it."""
    out, i = "", 0
    while True:
        j = tpl.find("${", i)
        if j < 0:
            return out + tpl[i:]
        out += tpl[i:j] + "v"
        depth, k = 1, j + 2
        while depth:
            depth += (tpl[k] == "{") - (tpl[k] == "}")
            k += 1
        i = k


def test_trending_violent_rows_are_mrows():
    _, _, tpl = _violent_template()
    rows = assert_mrow(_fill(tpl), 1)
    assert len(rows) == 1
    assert rows[0]["attrs"].get("data-click") == "toggleMRow"
    assert "b-trending-row" in rows[0]["attrs"]["class"].split()


def test_trending_violent_row_cells():
    _, _, tpl = _violent_template()
    row = cells_rows(_fill(tpl))[0]
    lead = row_lead(row)
    assert len(lead) == 1 and "b-trending-rank" in lead[0]["attrs"]["class"].split()
    keys = row_keys(row)
    assert len(keys) == 2
    assert "${sysName}" in tpl
    k2 = keys[1]["attrs"]["class"].split()
    assert "neg" in k2 and "b-trending-count" in k2, "key 2 is the ship kills, in the danger colour"
    labelled = [c for c in row["cells"] if "data-m-label" in c["attrs"]]
    assert [c["attrs"]["data-m-label"] for c in labelled] == ["Name", "Region", "Pods", "Map"]
    assert_single_value_child(row)
    link = row_labelled(row)["Map"]["kids"][0]
    assert link["href"] == "/map#v" and link["class"].split() == ["m-tap"]
    for c in keys + labelled:
        assert "m-only" in c["attrs"].get("class", "").split(), c


def test_trending_violent_template_fills_each_phone_cell():
    """Each phone cell carries one value: the system twice (key 1 and
    Name), the ship kills, the region, the pods (0 rather than missing) and
    the map link."""
    _, _, tpl = _violent_template()
    flat = norm(tpl)
    assert 'data-m="key">${sysName}<' in flat
    assert 'data-m-label="Name">${sysName}<' in flat
    assert 'data-m-label="Region">${regName || \'—\'}<' in flat
    assert "${(s.pod_kills || 0).toLocaleString()}" in flat
    assert "${s.ship_kills.toLocaleString()} ships" in flat


def test_trending_inits_the_rows_after_writing_them():
    src, start, _ = _violent_template()
    write = src.index("}).join('');", start)
    init = src.index("window.mRowInit(vc)", start)
    assert write < init < src.index("})();", start)
    # Guarded: actions.js loads after the page content, and the fetches can
    # resolve first. DOMContentLoaded's own init covers that case.
    assert "if (window.mRowInit) window.mRowInit(vc);" in src


def test_trending_alliance_rows_stay_plain_rows():
    src = source("trending.html")
    alliance = src[src.index("function renderAlliance"):src.index("if (sovResp) {", src.index("function renderAlliance"))]
    assert "b-trending-row" in alliance
    assert "m-row" not in alliance and "data-m" not in alliance


def test_trending_page_renders():
    html = render_page(starmap_mod, "trending.html", "/trending")
    assert 'id="violent-list"' in html and 'id="sov-gained"' in html and 'id="sov-lost"' in html


# ── WH Tracker ────────────────────────────────────────────────────────

def test_tracker_picker_labels_and_refresh_carry_their_hooks():
    chars = [{"id": 90000001, "name": "Pilot Sample", "has_scope": True},
             {"id": 90000002, "name": "Pilot Spare", "has_scope": False}]
    html = render_page(tracker_mod, "wh_tracker.html", "/intel/tracker", chars=chars)
    tags = _tags(html)
    labels = [a for t, a in tags if t == "label" and "wht-pick" in a.get("class", "").split()]
    assert len(labels) == 2
    refresh = [a for t, a in tags if a.get("id") == "wht-refresh"]
    assert len(refresh) == 1 and "m-tap" in refresh[0]["class"].split()


# ── site.css: this task's phone section ───────────────────────────────

def test_section_is_phone_only():
    body, after = _phone()
    assert after.strip() == "", "no desktop rules: desktop renders as before"
    assert body.strip(), "the phone block has rules"


def test_tab_bar_is_an_equal_width_44px_bar():
    body, _ = _phone()
    bar = rule_bodies(body, "div.gc-tabs")
    assert "display: grid" in bar and "grid-template-columns: repeat(3, minmax(0, 1fr))" in bar
    tab = rule_bodies(body, "div.gc-tabs > .gc-tab")
    assert "min-height: 44px" in tab
    # The head's .gc-tab.is-active colours the active tab; leave colour and
    # border to it.
    assert "color" not in tab and "border" not in tab


def test_small_controls_are_40px():
    body, _ = _phone()
    for sel in ("#tab-checker .gc-route-btn", ".gc-dropdown > .gc-dd-item", "label.wht-pick"):
        assert "min-height: 40px" in rule_bodies(body, sel), sel


def test_waypoint_rows_fold_into_two_lines():
    body, _ = _phone()
    row = rule_bodies(body, ".gc-wp")
    assert "flex-wrap: wrap" in row
    brk = rule_bodies(body, ".gc-wp::after")
    assert 'content: ""' in brk and "flex-basis: 100%" in brk and "order: 1" in brk
    assert "order: 2" in rule_bodies(body, ".gc-wp > .gc-wp-l2")
    name = rule_bodies(body, ".gc-wp > .gc-wp-name")
    assert "min-width: 0" in name and "text-overflow: ellipsis" in name
    region = rule_bodies(body, ".gc-wp > .gc-wp-region")
    assert "margin-left" in region
    # A long region gives way to the flags instead of wrapping one onto a
    # third line (the route's inline min-width:80px needs !important).
    assert "flex: 1 1 0" in region and "max-width: max-content" in region
    assert "min-width: 0 !important" in region and "text-overflow: ellipsis" in region
    assert "margin-left" in rule_bodies(body, ".gc-wp--jump > .gc-wp-region")


def test_trending_alliance_rows_truncate_and_grow():
    body, _ = _phone()
    for list_id in ("#sov-gained", "#sov-lost"):
        assert "min-height: 40px" in rule_bodies(body, f"{list_id} > .b-trending-row")
        assert "min-width: 0" in rule_bodies(body, f"{list_id} .b-trending-name")


def test_tracker_system_name_wraps():
    body, _ = _phone()
    assert "overflow-wrap: anywhere" in rule_bodies(body, "#wht-panel .ws-name")


def test_shared_class_rules_are_scoped_to_these_pages():
    """.ws-* and .sa-* also style the wormhole system page (T4's), and
    .b-trending-* names three lists here: every rule naming them is scoped
    by an id on this task's pages."""
    body, _ = _phone()
    for m in re.finditer(r"([^{}]+)\{[^{}]*\}", body):
        for sel in selectors(m.group(1)):
            if re.search(r"\.(ws|sa)-", sel):
                assert sel.startswith("#wht-panel "), sel
            if ".b-trending" in sel:
                assert sel.startswith(("#sov-gained ", "#sov-lost ", "#violent-list ")), sel
