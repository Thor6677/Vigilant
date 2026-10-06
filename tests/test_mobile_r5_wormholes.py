"""Mobile R5 T4: the wormhole pages at phone width.

- The system finder's results are link rows: class badge, system, statics
  (D12 A). Its eight rows of toggle filters fold behind one "Filters ▾"
  button on phones (D13 A).
- Systems / Types / Effects get the phone "Section ▾" dropdown.
- The types matrix and the effect tables swipe sideways with their first
  column pinned (D14 A); the matrix fades on its right edge while there is
  more to scroll.
- The kill heatmap keeps its own sideways scroll (D15 A). Its partial is
  anonymous-reachable, so it carries no actions.js bindings.
- Small controls become thumb-sized. Desktop is unchanged: every hook is a
  class only the phone CSS reads, or an m-only element.

Contexts follow the shapes app/routes/wormholes.py builds; every system,
pilot, corporation and alliance name is invented."""
import functools
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from app.routes import wormholes as wh_mod
from tests._mobile import (VOID, assert_mrow, cells_rows, css_section, norm, phone_block,
                           render_page, row_keys, row_lead, row_labelled, rule_bodies,
                           source)

_section = functools.partial(css_section, release="R5")

# Every actions.js binding: dead for a logged-out visitor (base.html never
# loads actions.js for them).
_BINDING = re.compile(r"\sdata-(click|change|input|submit|keydown|mousedown|focus|on-error)=")


class _Tags(HTMLParser):
    """Every start tag as (tag, attrs, parent attrs)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a, self.stack[-1][1] if self.stack else {}))
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _tags(html):
    p = _Tags()
    p.feed(html)
    p.close()
    return p.tags


def _cls(a):
    return a.get("class", "").split()


def _with_class(html, name):
    return [(t, a, parent) for t, a, parent in _tags(html) if name in _cls(a)]


def _phone(task="T4"):
    body, after = phone_block(_section(task))
    assert not after.strip(), "R5 T4 needs no desktop rule"
    return body


def _decl(body, selector, prop):
    """The value of `prop` in the phone rules for `selector`, !important kept."""
    rules = rule_bodies(body, selector)
    assert rules, f"no phone rule for {selector!r}"
    m = re.findall(rf"(?:^|[;\s]){re.escape(prop)}\s*:\s*([^;]+)", rules)
    assert m, f"{selector!r} sets no {prop}"
    return norm(m[-1])


# ── Finder results: link rows (D12 A) ─────────────────────────────────

_SYSTEMS = [
    {"system_name": "J900101", "wh_class": 5, "effect": "wolf_rayet", "statics": ["H296", "N062"]},
    {"system_name": "J900202", "wh_class": 3, "effect": "pulsar", "statics": ["D845"]},
    {"system_name": "J900303", "wh_class": 2, "effect": None, "statics": []},
]


def _render_list(systems=_SYSTEMS, page=1, total_pages=1):
    return render_page(
        wh_mod, "partials/wormhole_system_list.html", "/wormholes/search",
        systems=systems, total=len(systems) if total_pages == 1 else 50 * total_pages,
        page=page, total_pages=total_pages,
        class_label=wh_mod._class_label, class_color=wh_mod._class_color,
        effect_label=wh_mod._effect_label, wh_data=wh_mod._wh_data,
        q="", wh_class="5", effect="", static_dest="", planets="", perfect_pi="", shattered="")


def test_finder_results_are_link_rows_class_system_statics():
    html = _render_list()
    assert len(assert_mrow(html, min_rows=3)) == 3
    rows = cells_rows(html)
    assert len(rows) == 3
    for row, sys in zip(rows, _SYSTEMS):
        assert row["tag"] == "a"
        assert row["attrs"]["href"] == f"/wormholes/system/{sys['system_name']}"
        assert "m-row--link" in _cls(row["attrs"])
        assert "data-click" not in row["attrs"]
        (lead,) = row_lead(row)
        assert lead["text"] == wh_mod._class_label(sys["wh_class"])
        assert "wh-sys-class" in _cls(lead["attrs"])
        k1, k2 = row_keys(row)
        assert k1["text"] == sys["system_name"]
        assert "wh-sys-statics" in _cls(k2["attrs"])
        assert k2["text"] == (", ".join(sys["statics"]) if sys["statics"] else "—")
        # A link row never opens, so it has nothing labelled; the effect cell
        # is untagged and so hidden on phones.
        assert row_labelled(row) == {}
        untagged = [c for c in row["cells"] if "data-m" not in c["attrs"]]
        assert [c["text"] for c in untagged] == [wh_mod._effect_label(sys["effect"]) or "—"]


def test_finder_header_row_is_hidden_on_phones_and_the_list_has_a_hook():
    html = _render_list()
    (head,) = _with_class(html, "b-panel-head")
    assert "m-head" in _cls(head[1])
    (panel,) = _with_class(html, "wh-sys-list")
    assert "b-panel" in _cls(panel[1])
    rows = [a for t, a, parent in _tags(html) if "m-row" in _cls(a)]
    assert rows and all("wh-sys-list" in _cls(p) for t, a, p in _tags(html) if "m-row" in _cls(a))


def test_finder_paging_keeps_its_buttons_and_gets_a_wrap_hook():
    html = _render_list(page=6, total_pages=20)
    (pager,) = _with_class(html, "wh-pager")
    buttons = [a for t, a, p in _tags(html) if t == "button" and "wh-pager" in _cls(p)]
    assert len(buttons) >= 8
    assert all(b["hx-get"].startswith("/wormholes/search?page=") and "wh_class=5" in b["hx-get"]
               for b in buttons)
    assert all(b["hx-target"] == "#wh-results" for b in buttons)


# ── Finder page: filter fold (D13 A) and the Section dropdown ─────────

_EFFECTS = wh_mod._wh_data.get("effects", {})


def _render_finder():
    return render_page(
        wh_mod, "wormholes.html", "/wormholes",
        effects_list=list(_EFFECTS.keys()),
        effects_labels={k: v["name"] for k, v in _EFFECTS.items()},
        effect_colors=wh_mod.EFFECT_COLORS,
        class_colors=wh_mod._wh_data.get("class_colors", {}))


def test_finder_filters_fold_behind_one_phone_only_toggle():
    html = _render_finder()
    (panel,) = _with_class(html, "wf-panel")
    assert "b-panel" in _cls(panel[1])
    (toggle,) = _with_class(html, "wf-fold-toggle")
    tag, a, parent = toggle
    assert tag == "button" and a["type"] == "button"
    assert "m-only" in _cls(a)
    assert "wf-panel" in _cls(parent), "the toggle sits directly in the filter panel"
    assert a["data-click"] == "toggleFilterFold"
    assert a["aria-expanded"] == "false"
    # The summary of active filters: none on first load.
    (current,) = _with_class(html, "wf-fold-current")
    assert current[1]["id"] == "wf-fold-current"
    assert re.search(r'id="wf-fold-current"[^>]*>None</span>', html)
    # Every toggle row folds; the search row stays out.
    rows = [(a, p) for t, a, p in _tags(html) if "wf-row" in _cls(a)]
    assert all("wf-panel" in _cls(p) for a, p in rows)
    folded = [a for a, p in rows if "wf-fold" in _cls(a)]
    assert len(rows) == 6 and len(folded) == 5
    search_row = [a for a, p in rows if "wf-fold" not in _cls(a)]
    assert len(search_row) == 1
    assert 'id="wh-search"' in html.split('class="wf-row"', 1)[1].split("</div>", 1)[0]
    # The toggle comes after the search row and before the first folded row.
    i_toggle = html.index("wf-fold-toggle")
    assert html.index('id="wh-search"') < i_toggle < html.index('id="filter-class"')


def test_finder_script_keeps_the_fold_summary_current():
    html = _render_finder()
    script = html[html.index("function toggleFilterFold"):]
    assert "classList.toggle('is-expanded')" in script.split("function", 2)[1]
    assert "aria-expanded" in script.split("function", 2)[1]
    for fn in ("toggleFilter", "setExclusive"):
        body = html.split(f"function {fn}()", 1)[1].split("\n}\n", 1)[0]
        assert "updateFilterSummary()" in body, fn
    summary = html.split("function updateFilterSummary()", 1)[1].split("\n}\n", 1)[0]
    assert "wf-fold-current" in summary and "'None'" in summary


_TABS = [("/wormholes", "Systems"), ("/wormholes/types", "Wormhole Types"),
         ("/wormholes/effects", "System Effects")]


def _assert_section_dropdown(html, active):
    (strip,) = _with_class(html, "wh-tabs")
    assert "m-tabs-desktop" in _cls(strip[1])
    desk = re.findall(r'<a href="([^"]+)" class="wh-tab[^"]*">([^<]+)</a>', html)
    assert desk == _TABS
    (dd,) = _with_class(html, "m-tabs")
    assert dd[0] == "details"
    drop = html[html.index('<details class="m-tabs">'):]
    drop = drop[:drop.index("</details>")]
    assert re.search(r'm-tabs-label">Section<', drop)
    assert re.search(rf'm-tabs-current">{re.escape(active)}<', drop)
    links = re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', drop)
    assert links == _TABS
    assert re.search(rf'class="is-active" aria-current="page">{re.escape(active)}<', drop)
    # The dropdown follows the strip it replaces on phones.
    assert html.index('class="wh-tabs') < html.index('<details class="m-tabs">')


def test_finder_has_the_section_dropdown():
    _assert_section_dropdown(_render_finder(), "Systems")


# ── Types matrix: pinned From column, fade, scroll to detail ──────────

def _render_types():
    return render_page(
        wh_mod, "wormhole_types.html", "/wormholes/types",
        matrix=wh_mod._wh_data.get("connection_matrix", {}),
        wh_meta=wh_mod._wh_data.get("wormhole_meta", {}), type_lookup={},
        class_label=wh_mod._class_label, class_color=wh_mod._class_color,
        format_mass=wh_mod._format_mass, format_time=wh_mod._format_time,
        ship_size_hint=wh_mod._ship_size_hint, wh_data=wh_mod._wh_data)


def test_types_matrix_sits_in_a_scroll_box_with_a_pinned_from_column():
    html = _render_types()
    (box,) = _with_class(html, "wm-scroll")
    assert "overflow-x:auto" in box[1]["style"]
    (table,) = [(t, a, p) for t, a, p in _tags(html) if "wm-table" in _cls(a)]
    assert "wm-scroll" in _cls(table[2]), "the table is the scroll box's own child"
    froms = _with_class(html, "wm-from")
    assert len(froms) == 11 and all(t == "td" for t, a, p in froms)
    # The code links still load the detail panel by htmx.
    codes = [a for t, a, p in _tags(html) if t == "a" and a.get("hx-target") == "#wh-type-detail"]
    assert len(codes) > 20 and all(a["hx-get"].startswith("/wormholes/types/") for a in codes)


def test_types_script_fades_while_there_is_more_and_scrolls_to_the_detail():
    html = _render_types()
    (script,) = [s for s in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
                 if "querySelector('.wm-scroll')" in s]
    assert "classList.toggle('is-more'" in script
    for hook in ("'scroll'", "'resize'", "'load'"):
        assert hook in script, hook
    assert "htmx:afterSwap" in script and "scrollIntoView" in script
    assert "(max-width: 640px)" in script, "only phones scroll to the detail"
    _assert_section_dropdown(html, "Wormhole Types")


_SCROLL_HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const out = [];
for (const [phone, still] of [[true, false], [true, true], [false, false], [false, true]]) {
  const listeners = {};
  const detail = {
    addEventListener(type, fn) { listeners[type] = fn; },
    scrollIntoView(opts) { out.push({ phone, still, opts }); },
  };
  const window = {
    matchMedia(q) {
      if (q === '(max-width: 640px)') return { matches: phone };
      if (q === '(prefers-reduced-motion: reduce)') return { matches: still };
      throw new Error('unexpected media query ' + q);
    },
    addEventListener() {},
  };
  const document = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getElementById(id) { if (id !== 'wh-type-detail') throw new Error(id); return detail; },
  };
  vm.runInNewContext(src, { window, document });
  listeners['htmx:afterSwap'].call(detail, { target: {} });       // a nested swap: ignored
  listeners['htmx:afterSwap'].call(detail, { target: detail });
}
process.stdout.write(JSON.stringify(out));
"""


def test_types_detail_scroll_honours_reduced_motion(tmp_path):
    """Phones scroll the loaded detail into view: smoothly, or with a jump
    when the visitor asks for reduced motion (as the fitting tool's
    scrollToFitStats does). Desktop never scrolls."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    html = _render_types()
    (script,) = [s for s in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
                 if "querySelector('.wm-scroll')" in s]
    (tmp_path / "page.js").write_text(script)
    (tmp_path / "harness.js").write_text(_SCROLL_HARNESS)
    res = subprocess.run(["node", str(tmp_path / "harness.js"), str(tmp_path / "page.js")],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [
        {"phone": True, "still": False, "opts": {"block": "start", "behavior": "smooth"}},
        {"phone": True, "still": True, "opts": {"block": "start", "behavior": "auto"}},
    ]


# ── Effect tables: swipe, Modifier pinned (D14 A) ─────────────────────

def _render_effects():
    return render_page(wh_mod, "wormhole_effects.html", "/wormholes/effects", effects=_EFFECTS)


def test_each_effect_table_scrolls_in_its_own_box():
    html = _render_effects()
    tables = [(t, a, p) for t, a, p in _tags(html) if "we-table" in _cls(a)]
    assert len(tables) == len(_EFFECTS) == 6
    for t, a, parent in tables:
        assert "we-scroll" in _cls(parent) and "b-panel" in _cls(parent)
        assert "overflow-x:auto" in parent["style"]
    _assert_section_dropdown(html, "System Effects")


# ── Kill activity partial: anonymous, no bindings (D15 A) ─────────────

def _kills_ctx(**over):
    heatmap = [[0] * 24 for _ in range(7)]
    heatmap[1][14], heatmap[4][20] = 2, 1
    ctx = dict(
        kill_count=3, days=30, heatmap=heatmap,
        heatmap_ids={"1,14": [111, 112], "4,20": [113]},
        heatmap_npc={"4,20": True}, heatmap_age={"1,14": 0, "4,20": 1},
        recent_kills=[{"killmail_id": 113, "ship_type_id": 587, "ship_name": "Rifter",
                       "character_name": "Pilot Alpha", "corporation_name": "Kestrel Forge Industries",
                       "alliance_name": "Northern Ledger Alliance", "is_npc": False,
                       "value": 12500000, "time_ago": "2d ago"}],
        day_names=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], max_kills=2,
        most_recent=datetime(2026, 10, 1, 20, 15, tzinfo=timezone.utc), days_ago=2.5,
        age_labels=["<10d", "10-20d", "20-30d"], system_name="J900101",
        top_corps=[{"id": 98000001, "name": "Kestrel Forge Industries", "count": 2}],
        top_alliances=[{"id": 99000001, "name": "Northern Ledger Alliance", "count": 2}])
    ctx.update(over)
    return ctx


def _render_kills(session=None, **over):
    return render_page(wh_mod, "partials/wormhole_kills.html", "/wormholes/system/J900101/kills",
                       session=session, **_kills_ctx(**over))


def test_kills_partial_rendered_anonymously_has_no_actions_bindings():
    html = _render_kills(session=None)
    assert not _BINDING.search(html), _BINDING.search(html).group(0)
    for word in ("toggleMRow", "m-showall", "toggleExpanded", "m-row"):
        assert word not in html, word
    empty = _render_kills(session=None, kill_count=0, recent_kills=[], top_corps=[], top_alliances=[])
    assert not _BINDING.search(empty)


def test_kills_partial_keeps_the_heatmap_box_and_hooks_its_small_controls():
    html = _render_kills()
    (rng,) = _with_class(html, "whk-range")
    buttons = [a for t, a, p in _tags(html) if t == "button" and "whk-range" in _cls(p)]
    assert len(buttons) == 3
    assert [b.get("hx-get") for b in buttons] == [
        None, "/wormholes/system/J900101/kills?days=60", "/wormholes/system/J900101/kills?days=90"]
    (active,) = _with_class(html, "whk-active")
    rows = [a for t, a, p in _tags(html) if t == "a" and "whk-active" in _cls(p)]
    assert len(rows) == 2 and all(a["href"].startswith("https://zkillboard.com/") for a in rows)
    # The heatmap keeps its own sideways scroll and its 15px cells; a cell
    # with kills still links the first one.
    tables = [p for t, a, p in _tags(html) if t == "table"]
    assert len(tables) == 1 and tables[0]["style"] == "overflow-x:auto;"
    root = _tags(html)[0]
    assert root[0] == "div" and _cls(root[1])[:2] == ["b-panel", "whk"]
    assert html.count("width:15px;height:15px") == 7 * 24
    assert 'href="https://zkillboard.com/kill/111/"' in html


def test_kills_partial_titles_carry_the_11px_hook():
    """"Most Active" and "Activity Heatmap" (inline 9px) gain only the
    whk-title class, as children of the .whk panel the phone rule names;
    desktop keeps the 9px."""
    html = _render_kills()
    titles = [(a, p) for t, a, p in _tags(html) if "whk-title" in _cls(a)]
    assert len(titles) == 2
    for a, parent in titles:
        assert _cls(a) == ["whk-title"]
        assert a["style"].startswith("font-size:9px;")
        assert "whk" in _cls(parent)
    assert re.findall(r'class="whk-title"[^>]*>([^<]*)<', html) == ["Most Active", "Activity Heatmap"]


# ── System page and type page: tap targets only ───────────────────────

def _render_system():
    effect = dict(_EFFECTS["wolf_rayet"], key="wolf_rayet")
    return render_page(
        wh_mod, "wormhole_system.html", "/wormholes/system/J900101",
        system={"system_id": 31009999, "system_name": "J900101", "region": "E-R00099",
                "constellation": "E-C00999"},
        celestials={"planets": [{"planet_name": "J900101 I", "planet_index": 1, "distance_au": 2.4,
                                 "type_name": "Barren", "moon_count": 0},
                                {"planet_name": "J900101 II", "planet_index": 2, "distance_au": 9.1,
                                 "type_name": "Gas", "moon_count": 4}],
                    "star": {"type_name": "Sun A0 (Blue Small)"}},
        statics=[{"code": "H296", "target_class": 5, "respawn": "static"}],
        statics_known=True,
        wandering=[{"code": "K162", "target_class": None, "from_class": "?"}],
        effect=effect, wh_class=5, class_label=wh_mod._class_label,
        class_color=wh_mod._class_color, effect_label=wh_mod._effect_label,
        wh_data=wh_mod._wh_data)


def test_system_page_hooks_its_own_small_controls():
    html = _render_system()
    (zoom,) = _with_class(html, "wsp-zoom")
    buttons = [a for t, a, p in _tags(html) if t == "button" and "wsp-zoom" in _cls(p)]
    assert [b["id"] for b in buttons] == ["zoom-full", "zoom-dscan"]
    (conns,) = _with_class(html, "wsp-conns")
    assert "ws-section" in _cls(conns[1])
    (links,) = _with_class(html, "wsp-links")
    assert "ws-links" in _cls(links[1])
    # The kill partial still lazy-loads into #wh-kills.
    assert 'id="wh-kills"' in html and 'hx-get="/wormholes/system/J900101/kills"' in html


def _render_type_page(**session):
    return render_page(
        wh_mod, "wormhole_type_page.html", "/wormholes/types/H296", **session,
        code="H296",
        wh_type={"target_class": 5, "max_stable_time": 1440, "max_jump_mass": 1000000000,
                 "max_stable_mass": 3300000000, "mass_regen": 0},
        meta={"appears_in": ["c5"], "respawn": "static"},
        class_label=wh_mod._class_label, class_color=wh_mod._class_color,
        format_mass=wh_mod._format_mass, format_time=wh_mod._format_time,
        ship_size_hint=wh_mod._ship_size_hint, wh_data=wh_mod._wh_data)


def test_type_page_back_link_is_hooked_and_the_page_has_no_bindings():
    """/wormholes/types/{code} has no session check, so the page and its
    partial must work without actions.js."""
    html = _render_type_page(session=None)
    (back,) = _with_class(html, "wh-back")
    assert back[0] == "a" and back[1]["href"] == "/wormholes/types"
    body = html[html.index('class="b-page-header"'):]
    assert not _BINDING.search(body)
    partial = render_page(
        wh_mod, "partials/wormhole_type_detail.html", "/wormholes/types/H296", session=None,
        code="H296", wh_type=None, meta={}, class_label=wh_mod._class_label,
        class_color=wh_mod._class_color, format_mass=wh_mod._format_mass,
        format_time=wh_mod._format_time, ship_size_hint=wh_mod._ship_size_hint,
        wh_data=wh_mod._wh_data)
    assert not _BINDING.search(partial)


# ── site.css: R5 T4 section ───────────────────────────────────────────

def test_css_finder_rows_badge_lead_and_muted_statics():
    body = _phone()
    assert "currentColor" in _decl(body, ".wh-sys-list > .m-row > .wh-sys-class", "border")
    assert _decl(body, ".wh-sys-list > .m-row > .wh-sys-statics", "color") == "var(--muted) !important"
    assert _decl(body, ".wh-sys-list > .m-row > .wh-sys-statics > span", "color") == "var(--muted) !important"
    assert _decl(body, ".wh-sys-list > .m-row > .wh-sys-statics", "font-size") == "12px !important"
    # One line height for both keys: the 16px name and 12px statics then
    # share a top edge, as well as a centre line.
    assert _decl(body, '.wh-sys-list > .m-row > [data-m="key"]', "line-height") == "1.5rem"


def test_css_pager_wraps_and_its_controls_are_40px_wide():
    body = _phone()
    assert _decl(body, ".wh-pager", "flex-wrap") == "wrap"
    assert _decl(body, ".wh-pager > button", "min-width") == "40px"
    assert _decl(body, ".wh-pager > span", "min-height") == "40px"


def test_css_filter_fold_and_its_40px_buttons():
    body = _phone()
    assert _decl(body, ".wf-panel:not(.is-expanded) > .wf-fold", "display") == "none !important"
    assert _decl(body, ".wf-fold-toggle", "min-height") == "44px"
    assert _decl(body, ".wf-fold-toggle", "width") == "100%"
    assert "▴" in rule_bodies(body, ".wf-panel.is-expanded > .wf-fold-toggle::after")
    assert "ellipsis" in _decl(body, ".wf-fold-toggle .wf-fold-current", "text-overflow")
    assert _decl(body, ".wf-panel .wf-btn", "min-width") == "40px"
    assert _decl(body, ".wf-panel .wf-btn", "font-size") == "12px"


def test_css_buttons_keep_the_44px_phone_floor():
    """The R1 phone layer makes every <button> at least 44px tall. The
    buttons this task touches only need width and a bigger glyph; a
    min-height here (more specific than `button`) would shrink them."""
    body = _phone()
    for sel in (".wf-panel .wf-btn", ".wh-pager > button", ".whk-range > button",
                ".wsp-zoom > button", ".wf-fold-toggle"):
        rules = rule_bodies(body, sel)
        assert rules, sel
        heights = re.findall(r"(?<![\w-])(?:min-|max-)?height\s*:\s*(\d+)px", rules)
        assert all(int(h) >= 44 for h in heights), (sel, heights)


def test_css_types_matrix_pins_the_from_column_and_fades():
    body = _phone()
    for sel in (".wm-table td.wm-from", ".wm-table th:first-child"):
        assert _decl(body, sel, "position") == "sticky", sel
        assert _decl(body, sel, "left") == "0", sel
    assert _decl(body, ".wm-table td.wm-from", "background") == "var(--bg)"
    # Collapsed borders are painted by the table and scroll away from a
    # sticky cell, letting the codes show through its edge. Separate borders
    # belong to each cell: right and bottom, plus left on the first column
    # and top on the header row, so lines stay 1px. The template's own
    # <style> comes after site.css, hence the .wm-scroll prefix.
    assert _decl(body, ".wm-scroll > .wm-table", "border-collapse") == "separate"
    assert _decl(body, ".wm-scroll > .wm-table", "border-spacing") == "0"
    assert _decl(body, ".wm-scroll > .wm-table td", "border-width") == "0 1px 1px 0"
    assert _decl(body, ".wm-scroll > .wm-table tr > :first-child", "border-left-width") == "1px"
    assert _decl(body, ".wm-scroll > .wm-table thead th", "border-top-width") == "1px"
    assert "linear-gradient" in _decl(body, ".wm-scroll.is-more", "mask-image")
    assert "linear-gradient" in _decl(body, ".wm-scroll.is-more", "-webkit-mask-image")
    assert _decl(body, ".wm-table td a", "min-height") == "40px"
    assert _decl(body, ".wm-table td a", "min-width") == "40px"
    assert _decl(body, ".wm-table td br", "display") == "none"
    assert _decl(body, "#wh-type-detail", "scroll-margin-top")


def test_css_effect_tables_pin_the_modifier_column():
    body = _phone()
    for sel in (".we-table td:first-child", ".we-table th:first-child"):
        assert _decl(body, sel, "position") == "sticky", sel
        assert _decl(body, sel, "left") == "0", sel
    assert _decl(body, ".we-table td:first-child", "background").startswith("var(--surface")
    assert _decl(body, ".we-table td", "white-space") == "nowrap"


def test_css_kill_activity_controls():
    body = _phone()
    assert _decl(body, ".whk-range > button", "min-width") == "40px"
    assert _decl(body, ".whk-range > button", "font-size") == "12px !important"
    assert _decl(body, ".whk-active > a", "min-height") == "40px"
    # The heatmap's ~540px table and the recent kills' nowrap pilot lines
    # must not widen the column the panel sits in (the system page's and the
    # WH Tracker's .ws-grid), or the page scrolls sideways and the heatmap's
    # own box never does.
    assert _decl(body, ".whk", "contain") == "inline-size"
    # Its two section titles: 11px over their inline 9px.
    assert _decl(body, ".whk > .whk-title", "font-size") == "11px !important"


def test_css_system_page_tap_targets_are_scoped_to_the_system_page():
    """The WH Tracker panel reuses .ws-conn and .ws-links; these rules must
    reach the system page's own copies only."""
    body = _phone()
    assert _decl(body, ".wsp-conns .ws-conn a", "min-height") == "40px"
    assert _decl(body, ".wsp-links a", "min-height") == "40px"
    assert _decl(body, ".wsp-zoom > button", "min-width") == "40px"
    assert _decl(body, ".wh-back", "min-height") == "40px"
    for sel in re.findall(r"([^{}]+)\{", body):
        for s in sel.split(","):
            if ".ws-" in s:
                assert ".wsp-" in s, f"unscoped system-page selector: {s.strip()}"


def test_css_system_page_small_text_is_11px():
    body = _phone()
    for sel in (".wsp-conns .ws-section-title", ".wsp-grid .ws-section-title",
                ".wsp-grid .ws-effect-table th"):
        assert _decl(body, sel, "font-size") == "11px", sel
    for sel in (".wsp-grid .ws-section-title > span", ".wsp-legend"):
        assert _decl(body, sel, "font-size") == "11px !important", sel
    src = source("wormhole_system.html")
    assert src.count('class="ws-grid wsp-grid"') == 1
    assert src.count('class="wsp-legend"') == 1


def test_css_pinned_tables_keep_every_hairline():
    """Sticky first columns drop row lines at fractional row heights: the
    matrix cells get whole-pixel padding, and the effect tables use separate
    borders like the matrix."""
    body = _phone()
    for sel in (".wm-scroll > .wm-table th", ".wm-scroll > .wm-table td"):
        assert "padding: 3px 5px" in rule_bodies(body, sel), sel
    eff = rule_bodies(body, ".we-scroll > .we-table")
    assert "border-collapse: separate" in eff and "border-spacing: 0" in eff
    # Whole-pixel row heights everywhere a sticky column sits (verified on a
    # fresh load: an injected <style> forces a repaint that hides the gaps).
    assert "line-height: 15px" in rule_bodies(body, ".wm-scroll > .wm-table thead th")
    th = rule_bodies(body, ".we-scroll > .we-table th")
    td = rule_bodies(body, ".we-scroll > .we-table td")
    assert "padding: 6px 8px" in th and "line-height: 15px" in th
    assert "padding: 6px 8px" in td and "line-height: 18px" in td


def test_full_tool_link_meets_the_link_floor():
    src = source("wormhole_system.html")
    assert 'href="/tools/structure-age" class="wsp-fulltool"' in src
    body = _phone()
    rule = rule_bodies(body, ".wsp-fulltool")
    assert "min-height: 40px" in rule and "display: inline-flex" in rule
