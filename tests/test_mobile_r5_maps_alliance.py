"""Mobile R5 T5: Star Map, Wormhole Map and the alliance page on phones.

Maps: `#map-root` is `height: calc(100vh - 48px)` in each template's own
<style>. On iOS `100vh` includes the collapsing URL bar, so the bottom of
the map sat under it. On phones the root (hook class `map-fill`) uses
`100dvh`, with the `100vh` declaration before it as the fallback. The
template's rule is an id rule loaded after site.css, so the phone rule
needs the id too (`#map-root.map-fill`) to win. Desktop keeps `100vh`.
No frontend/src change: the React app already handles touch.

Alliance page, Recent Changes (user decision D16 A): the rows are built
in a JS template string after fetch(), so the m-row tags live in that
string and the page calls `window.mRowInit` after writing them. Lead is
the ▲/▼ arrow, key 1 the system, key 2 "from X" / "to X" with its
alliance link; opened rows show Name, Region and When. The list is
wrapped in a JS-built clamp with "Show all N" only past 10 rows, and the
500px inner scroll box drops its scroll on phones (m-unclamp). Desktop
renders as before (D21): the four desktop cells are unchanged and every
new cell is m-only.

The page script is run under node against a stub DOM and stub fetch().
Names and ids are invented."""
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from app.routes import starmap as starmap_mod
from tests._mobile import (assert_mrow, assert_single_value_child, cells_rows, clamps,
                           css_section, phone_block, render_page, row_keys, row_labelled,
                           row_lead, rule_bodies, source)

_section = functools.partial(css_section, release="R5")

_ALLIANCE = 99000001
_OTHER = 99000002
_OTHER_NAME = "Sample Counterparty Alliance"
_REGION = "Sample Region"


# ── maps ──────────────────────────────────────────────────────────────

class _ById(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.by_id = {}

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if "id" in a:
            self.by_id[a["id"]] = a


def _ids(html):
    p = _ById()
    p.feed(html)
    p.close()
    return p.by_id


@pytest.mark.parametrize("template,path", [("map.html", "/map"),
                                           ("map_wormholes.html", "/map/wormholes")])
def test_map_root_carries_the_fill_hook(template, path):
    html = render_page(starmap_mod, template, path, entry_js=None, preload_js=[])
    root = _ids(html)["map-root"]
    assert "map-fill" in root.get("class", "").split()


def test_wormhole_map_keeps_its_space_attribute():
    html = render_page(starmap_mod, "map_wormholes.html", "/map/wormholes",
                       entry_js=None, preload_js=[])
    assert _ids(html)["map-root"].get("data-space") == "w"


@pytest.mark.parametrize("template", ["map.html", "map_wormholes.html"])
def test_map_templates_keep_100vh_for_desktop(template):
    style = re.search(r"<style[^>]*>(.*?)</style>", source(template), re.S).group(1)
    body = rule_bodies(style, "#map-root")
    assert "height: calc(100vh - 48px);" in body
    assert "dvh" not in style


def test_phone_section_has_no_desktop_rule():
    _phone, after = phone_block(_section("T5"))
    assert after.strip() == ""


def test_map_height_uses_dvh_with_a_vh_fallback():
    """The id in the selector outranks the template's own `#map-root` rule,
    which comes later in the page. `100vh` first: a browser without dvh
    drops the second declaration and keeps it."""
    phone, _ = phone_block(_section("T5"))
    body = rule_bodies(phone, "#map-root.map-fill")
    vh = body.find("height: calc(100vh - 48px)")
    dvh = body.find("height: calc(100dvh - 48px)")
    assert vh != -1 and dvh != -1, body
    assert vh < dvh


# ── alliance page: template and script text ───────────────────────────

def _alliance_html():
    return render_page(starmap_mod, "alliance_detail.html", f"/alliance/{_ALLIANCE}",
                       alliance_id=_ALLIANCE)


def _alliance_script():
    html = _alliance_html()
    scripts = [s for s in re.findall(r"<script nonce=\"test-nonce\">(.*?)</script>", html, re.S)
               if "changes-list" in s]
    assert len(scripts) == 1
    return scripts[0]


def test_changes_list_drops_its_inner_scroll_on_phones():
    box = _ids(_alliance_html())["changes-list"]
    assert "m-unclamp" in box.get("class", "").split()
    # Desktop keeps its 500px scroll box.
    assert "max-height:500px" in box.get("style", "")
    assert "overflow-y:auto" in box.get("style", "")


def test_script_row_template_carries_the_mrow_tags():
    """Always runs, unlike the node test below."""
    js = _alliance_script()
    assert 'class="b-change-row m-row" data-click="toggleMRow"' in js
    assert 'data-m="lead"' in js
    assert js.count('data-m="key"') == 2
    for label in ("Name", "Region", "When"):
        assert f'data-m-label="{label}"' in js


def test_script_calls_mrowinit_after_writing_the_rows():
    js = _alliance_script()
    write = js.rfind("cl.innerHTML =")
    init = js.find("window.mRowInit(cl)")
    assert write != -1 and init > write


def test_script_builds_the_clamp_with_show_all_past_10():
    js = _alliance_script()
    for cls in ('class="m-clamp-wrap"', 'class="m-clamp"',
                'class="m-only m-showall" data-click="toggleExpanded" data-toggle-target=".m-clamp-wrap"'):
        assert cls in js
    assert re.search(r"> 10 \? `<button type=\"button\" class=\"m-only m-showall\"", js)


# ── alliance page: the script run under node ──────────────────────────

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [srcPath, fxPath] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));

const els = {};
const el = id => (els[id] = els[id] || { id, textContent: '', innerHTML: '', src: '' });
const calls = [];
const aria = [];
const seq = [];
const window = {
  mRowInit(root) { seq.push('mRowInit'); calls.push({ id: root && root.id, html: root && root.innerHTML }); },
  initToggleExpandedAria(root) { seq.push('aria'); aria.push({ id: root && root.id, html: root && root.innerHTML }); },
};
const ok = body => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
function fetch(url) {
  if (url.startsWith('/api/map/alliance/')) return ok(fx.detail);
  if (url === '/map/data/systems.json') return ok(fx.systems);
  if (url.startsWith('/api/map/alliances?ids=')) return ok(fx.names);
  return Promise.resolve({ ok: false, json: () => Promise.resolve(null) });
}
const sandbox = { window, document: { getElementById: el }, fetch, console };
vm.createContext(sandbox);
Promise.resolve(vm.runInContext(src, sandbox)).then(() => {
  process.stdout.write(JSON.stringify({ html: el('changes-list').innerHTML, calls, aria, seq }));
}, err => { console.error(err); process.exit(1); });
"""


def _changes(n):
    """n changes: the first a gain from the counterparty, the second a loss
    to nobody (Unclaimed), the third in a system missing from systems.json
    (no region); the rest alternate gain/loss against the counterparty."""
    out = []
    for i in range(n):
        gain = i % 2 == 0
        other = None if i == 1 else _OTHER
        out.append({
            "system_id": 30000099 if i == 2 else 30000001 + i,
            "changed_at": f"2026-09-{(i % 28) + 1:02d}T{i % 24:02d}:15:00+00:00",
            "old_alliance_id": other if gain else _ALLIANCE,
            "new_alliance_id": _ALLIANCE if gain else other,
            "direction": "gain" if gain else "loss",
        })
    return out


@pytest.fixture(scope="module")
def run_page(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("alliance")
    (d / "harness.js").write_text(_HARNESS)
    (d / "page.js").write_text(_alliance_script())
    cache = {}

    def run(n):
        if n not in cache:
            fx = {
                "detail": {"name": "Sample Alliance", "ticker": "SMPL", "sov_system_count": 3,
                           "sov_gained_7d": 2, "sov_lost_7d": 1, "date_founded": None,
                           "recent_changes": _changes(n)},
                "systems": [{"id": 30000001 + i, "name": f"SMP-{i:02d}", "regName": _REGION}
                            for i in range(max(n, 1)) if i != 2],
                "names": {str(_OTHER): _OTHER_NAME},
            }
            (d / f"fx{n}.json").write_text(json.dumps(fx))
            res = subprocess.run(["node", str(d / "harness.js"), str(d / "page.js"),
                                  str(d / f"fx{n}.json")],
                                 capture_output=True, text=True, timeout=30)
            assert res.returncode == 0, res.stderr
            cache[n] = json.loads(res.stdout)
        return cache[n]
    return run


def test_rows_meet_the_contract(run_page):
    html = run_page(11)["html"]
    assert_mrow(html, 11)
    for row in cells_rows(html):
        assert_single_value_child(row)


def test_mrowinit_runs_once_on_the_list_after_the_write(run_page):
    out = run_page(11)
    assert len(out["calls"]) == 1
    assert out["calls"][0]["id"] == "changes-list"
    assert out["calls"][0]["html"] == out["html"]
    assert "m-row" in out["calls"][0]["html"]


def test_lead_is_the_coloured_arrow(run_page):
    rows = cells_rows(run_page(11)["html"])
    gain, loss = rows[0], rows[1]
    for row, cls, arrow in ((gain, "gain", "▲"), (loss, "loss", "▼")):
        lead = row_lead(row)
        assert len(lead) == 1
        assert lead[0]["attrs"]["class"].split() == ["b-change-dir", cls]
        assert lead[0]["text"] == arrow


def test_keys_are_system_then_counterparty(run_page):
    rows = cells_rows(run_page(11)["html"])
    k1, k2 = row_keys(rows[0])
    assert "m-only" in k1["attrs"]["class"].split()
    assert k1["text"] == "SMP-00"
    assert k2["text"] == f"from {_OTHER_NAME}"
    assert {"class": "b-text", "href": f"/alliance/{_OTHER}"} in k2["kids"]
    # A loss to nobody: "to Unclaimed", no link.
    k1, k2 = row_keys(rows[1])
    assert k1["text"] == "SMP-01"
    assert k2["text"] == "to Unclaimed"
    assert k2["kids"] == []


def test_labels_are_name_region_when_in_order(run_page):
    rows = cells_rows(run_page(11)["html"])
    labelled = [c["attrs"]["data-m-label"] for c in rows[0]["cells"] if "data-m-label" in c["attrs"]]
    assert labelled == ["Name", "Region", "When"]
    cells = row_labelled(rows[0])
    assert cells["Name"]["text"] == "SMP-00"
    assert cells["Region"]["text"] == _REGION
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", cells["When"]["text"])
    for label in ("Name", "Region"):
        assert "m-only" in cells[label]["attrs"]["class"].split()


def test_unknown_system_has_no_empty_region_line(run_page):
    row = cells_rows(run_page(11)["html"])[2]
    assert list(row_labelled(row)) == ["Name", "When"]
    assert row_keys(row)[0]["text"] == "System 30000099"


def test_desktop_cells_are_unchanged(run_page):
    """Above 640px every m-only cell is display:none, so the grid still
    holds the four cells it had before, in order and with the same styles."""
    rows = cells_rows(run_page(11)["html"])
    assert len(rows) == 11
    for row in rows:
        shown = [c for c in row["cells"] if "m-only" not in c["attrs"].get("class", "").split()]
        assert [c["tag"] for c in shown] == ["span"] * 4
        arrow, system, side, when = shown
        assert arrow["attrs"]["class"].split()[0] == "b-change-dir"
        assert "style" not in arrow["attrs"]
        assert system["attrs"] == {}
        assert [k.get("style") for k in system["kids"]] == [
            "color:var(--accent);", "color:var(--muted);font-size:9px;margin-left:0.5rem;"]
        assert side["attrs"]["style"] == "color:var(--muted);font-size:10px;"
        assert when["attrs"]["style"] == "color:var(--muted);font-size:9px;"


@pytest.mark.parametrize("n", [10, 11])
def test_show_all_gets_aria_expanded_once_the_list_is_written(run_page, n):
    """The list is written by this script, not htmx, so actions.js's
    DOMContentLoaded / htmx:afterSettle pass never reaches its Show all:
    without this call the button has no aria-expanded until its first tap.
    It runs once, on the list, after the rows are written (and after
    mRowInit), so it sees the button."""
    out = run_page(n)
    assert out["aria"] == [{"id": "changes-list", "html": out["html"]}]
    assert out["seq"] == ["mRowInit", "aria"]


def test_empty_list_needs_no_aria_pass(run_page):
    out = run_page(0)
    assert out["aria"] == [] and out["seq"] == []


@pytest.mark.parametrize("n,button", [(10, None), (11, "Show all 11")])
def test_show_all_only_past_10(run_page, n, button):
    c = clamps(run_page(n)["html"])
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.clamps) == 1
    assert c.clamps[0]["children"] == n          # the button is outside .m-clamp
    if button is None:
        assert c.showall == []
    else:
        assert len(c.showall) == 1
        b = c.showall[0]
        assert b["in_wrap"] and b["text"] == button
        assert b["attrs"]["type"] == "button"
        assert b["attrs"]["data-click"] == "toggleExpanded"
        assert b["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
        assert "m-only" in b["attrs"]["class"].split()


def test_no_changes_keeps_the_empty_message(run_page):
    out = run_page(0)
    assert "No sovereignty changes in the last 7 days." in out["html"]
    assert "m-row" not in out["html"]


# ── alliance page: phone CSS ──────────────────────────────────────────

def test_counterparty_column_is_capped_and_ellipsised():
    """A 50-character alliance name in key 2 would otherwise squeeze the
    system to nothing and push the row sideways."""
    phone, _ = phone_block(_section("T5"))
    grid = rule_bodies(phone, "#changes-list .m-row")
    assert re.search(r"grid-template-columns:\s*auto minmax\(0, 1fr\) fit-content\(\d+%\) 14px !important", grid)
    key2 = rule_bodies(phone, '#changes-list .m-row > [data-m="key"] ~ [data-m="key"]')
    assert "overflow: hidden" in key2
    assert "text-overflow: ellipsis" in key2


def test_open_row_shows_the_whole_counterparty():
    """The labels are Name, Region and When (D16 A), so the only place a
    phone can read a long alliance name in full is key 2 of an open row:
    there it wraps instead of ellipsising."""
    phone, _ = phone_block(_section("T5"))
    body = rule_bodies(phone, '#changes-list .m-row.is-open:not(.m-row--link) > [data-m="key"] ~ [data-m="key"]')
    assert "white-space: normal" in body
    assert "text-overflow: clip" in body
    # Arrow, system and chevron stay on the wrapped name's first line.
    assert "align-items: start !important" in rule_bodies(phone, "#changes-list .m-row.is-open:not(.m-row--link)")


def test_alliance_keys_reach_12px_and_the_open_link_gets_a_tap_area():
    phone, _ = phone_block(_section("T5"))
    keys = rule_bodies(phone, '#changes-list .m-row > [data-m="key"]')
    assert "font-size: 12px !important" in keys, "key 2 carries an inline 10px"
    open_k2 = '#changes-list .m-row.is-open:not(.m-row--link) > [data-m="key"] ~ [data-m="key"]'
    assert "overflow: visible" in rule_bodies(phone, open_k2), "a clipped key 2 would cut the hit area"
    link = rule_bodies(phone, open_k2 + " > a")
    # 10px up, 18px down: the text sits ~11px below the row top, so a larger
    # top pad would reach into the row above and steal its tap.
    assert "padding: 10px 0 18px" in link and "margin: -10px 0 -18px" in link
    assert "min-height: 40px" in rule_bodies(phone, "#changes-list .m-row"), "the borderless last row"
