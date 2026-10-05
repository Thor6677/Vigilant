"""Mobile R4 T3: the Trading & Industry P&L and Net Worth pages at phone width.

User decisions (R4 picks):
  D7 A  P&L items are m-rows: key 1 the item name (plain text), key 2 the
        realized ISK in its profit/loss colour. Opened, they show Name,
        Units, Avg margin, Split (only where the item has one) and Market.
  D8 A  The desktop name link stays as it is (untagged, so phones hide it);
        a phone-only Market line inside the opened row carries the same href.
  D9 A  The Net Worth legend sits below the chart on phones, with fewer
        date labels.
Also: the stat cards on both pages become label · value lines (R2 T2's
Queue Summary pattern); the P&L chart shows fewer month labels on phones;
the Net Worth range buttons and Snapshot now are 40px; the Snapshot status
wraps on its own line; the two long notes are 12px. The picks page told the
user the P&L list shows "Show all past 10 on phones", so it is clamped.

Desktop (>640px) renders as before: every cell added for phones is m-only,
and each chart's desktop options are unchanged (checked by running the
page script in node against a stub Chart).

Pages are rendered through their route modules' templates.env with contexts
shaped like pnl_page() and networth_page() build them. Names are invented."""
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from app.routes import networth as nw_mod
from app.routes import pnl as pnl_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, norm, phone_block, render_page, row_keys,
                           row_labelled, rule_bodies)

_section = functools.partial(css_section, release="R4")
PHONE_MQ = "matchMedia('(max-width: 640px)')"


# ── fixtures ──────────────────────────────────────────────────────────

def _row(type_id, name, isk, isk_str, qty_str, margin_str, split=None):
    """One display row as pnl_page() builds it."""
    return {"type_id": type_id, "type_name": name, "realized_isk": isk,
            "realized_isk_str": isk_str, "qty_flipped": 1, "qty_flipped_str": qty_str,
            "margin_pct": None, "margin_pct_str": margin_str,
            "profit_positive": isk >= 0, "split_note": split}


_ROWS = [
    _row(2456, "Sample Combat Drone II", 412_600_000, "412.60M", "4,820", "12.4%",
         split="T: 380.10M · B: 32.50M"),
    _row(28668, "Sample Repair Paste", 96_100_000, "96.10M", "12,400", "8.2%"),
    _row(2048, "Sample Damage Control II", 41_000_000, "41.00M", "320", "18.0%"),
    _row(99001, "Sample Faction Module With A Long Name", -22_300_000, "-22.30M", "4", "—"),
]


def _render_pnl(rows=None, monthly=None):
    rows = _ROWS if rows is None else rows
    if monthly is None:
        monthly = {"labels": ["2026-07", "2026-08", "2026-09"],
                   "realized": [10.0, -5.0, 20.0], "trade": [8.0, -5.0, 15.0],
                   "build": [2.0, 0.0, 5.0]}
    return render_page(
        pnl_mod, "pnl.html", "/market/pnl",
        char_options=[{"character_id": 90000001, "name": "Sample Pilot"},
                      {"character_id": 90000002, "name": "Sample Alt"}],
        selected_character_id=0, has_rows=True, rows=rows, monthly=monthly,
        totals={"realized_isk_str": "527.40M", "realized_positive": True,
                "trade_isk_str": "494.90M", "trade_positive": True,
                "build_isk_str": "-32.50M", "build_positive": False,
                "qty_flipped_str": "17,544", "types_traded": 4},
        unmatched_total=0, assumptions=pnl_mod._assumptions(0))


def _render_nw():
    return render_page(nw_mod, "networth.html", "/tools/networth")


def _pnl_rows():
    """The rendered P&L rows beside their fixture rows (one m-row each)."""
    rows = cells_rows(_render_pnl())
    assert len(rows) == len(_ROWS)
    return zip(rows, _ROWS)


def _many_rows(n):
    return [_row(30000 + i, f"Sample Item {i}", 1000.0 * (n - i), f"{n - i}.0K", "1", "1.0%")
            for i in range(n)]


# ── a minimal element tree, for structure checks ──────────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def all(self):
        for c in self.children:
            yield c
            yield from c.all()

    def find(self, cls):
        return [n for n in self.all() if cls in n.classes]

    def by_id(self, ident):
        return [n for n in self.all() if n.attrs.get("id") == ident]

    def full_text(self):
        return norm(self.text + " " + " ".join(c.full_text() for c in self.children))


class _Tree(HTMLParser):
    """Element tree. No implied end tags: fine for these hand-written pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


def _content(html):
    """The page body from the breadcrumbs down: skips base.html's nav and menu."""
    return html[html.index('<div class="b-breadcrumbs">'):]


# ── P&L items (D7 A, D8 A) ────────────────────────────────────────────

def test_pnl_table_is_an_m_table_with_one_m_row_per_item():
    html = _render_pnl()
    rows = assert_mrow(html, 4)
    assert len(rows) == 4
    for r in rows:
        assert r["tag"] == "tr"
        assert r["attrs"]["class"].split() == ["m-row"]
        assert r["attrs"]["data-click"] == "toggleMRow"
    root = _tree(html)
    table, = root.find("pnl-table")
    assert table.tag == "table" and table.classes == ["pnl-table", "m-table"]
    thead = [c for c in table.children if c.tag == "thead"]
    assert len(thead) == 1 and "m-head" in thead[0].classes
    assert [th.full_text() for th in thead[0].all() if th.tag == "th"] == [
        "Item", "Realized ISK", "Units flipped", "Avg margin"]


def test_pnl_key_one_is_the_plain_name_and_key_two_the_coloured_realized_isk():
    for r, src in _pnl_rows():
        k1, k2 = row_keys(r)
        assert k1["text"] == src["type_name"]
        assert "m-only" in k1["attrs"]["class"].split()
        assert k1["kids"] == []                         # plain text, no link
        assert k2["text"] == src["realized_isk_str"]
        assert "m-only" not in k2["attrs"].get("class", "").split()   # the desktop cell
        colour = "pnl-profit" if src["profit_positive"] else "pnl-loss"
        assert k2["attrs"]["class"].split() == [colour]


def test_pnl_opened_rows_start_with_name_and_show_split_only_where_there_is_one():
    for r, src in _pnl_rows():
        order = [c["attrs"]["data-m-label"] for c in r["cells"] if "data-m-label" in c["attrs"]]
        expected = ["Name", "Units", "Avg margin"] + (["Split"] if src["split_note"] else []) + ["Market"]
        assert order == expected
        lab = row_labelled(r)
        assert lab["Name"]["text"] == src["type_name"]
        assert "m-only" in lab["Name"]["attrs"]["class"].split()
        assert lab["Units"]["text"] == src["qty_flipped_str"]
        assert lab["Avg margin"]["text"] == src["margin_pct_str"]
        if src["split_note"]:
            assert lab["Split"]["text"] == src["split_note"]
            assert "m-only" in lab["Split"]["attrs"]["class"].split()
        assert_single_value_child(r)


def test_pnl_market_line_is_phone_only_with_the_desktop_href():
    for r, src in _pnl_rows():
        href = f"/market/type/{src['type_id']}"
        market = row_labelled(r)["Market"]
        assert "m-only" in market["attrs"]["class"].split()
        link, = market["kids"]
        assert link["href"] == href
        assert "m-tap" in link.get("class", "").split()
        assert market["text"] == "Open market page →"
        # The desktop link cell: untagged (so phones hide it), still a link
        # to the same page, and still the row's first cell.
        first = r["cells"][0]
        assert not ({"data-m", "data-m-label"} & set(first["attrs"]))
        assert "m-only" not in first["attrs"].get("class", "").split()
        assert first["kids"][0]["href"] == href
        assert first["text"].startswith(src["type_name"])


def test_pnl_desktop_cells_are_unchanged_and_every_added_cell_is_phone_only():
    """Desktop sees only the four original columns, in order; anything else
    in a row is m-only (display:none above 640px)."""
    for r, src in _pnl_rows():
        desktop = [c for c in r["cells"] if "m-only" not in c["attrs"].get("class", "").split()]
        assert [c["tag"] for c in r["cells"]] == ["td"] * len(r["cells"])
        assert len(desktop) == 4
        name, isk, units, margin = desktop
        assert name["text"] == norm(f"{src['type_name']} {src['split_note'] or ''}")
        assert isk["text"] == src["realized_isk_str"]
        assert units["text"] == src["qty_flipped_str"]
        assert margin["text"] == src["margin_pct_str"]
        if src["split_note"]:
            assert any(k.get("style", "").startswith("color:var(--muted);font-size:9px") for k in name["kids"])


def test_pnl_list_shows_ten_rows_then_show_all_on_phones():
    html = _render_pnl(rows=_many_rows(12))
    assert_mrow(html, 12)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == 12
    btn, = c.showall
    assert btn["in_wrap"] and btn["text"] == "Show all 12"
    assert btn["attrs"]["class"].split() == ["m-only", "m-showall"]
    assert btn["attrs"]["data-click"] == "toggleExpanded"
    assert btn["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
    assert btn["attrs"]["type"] == "button"
    # The rows sit directly in the clamped <tbody>, so expanding the wrap
    # never opens them (.is-expanded > .m-row).
    root = _tree(html)
    tbody, = [n for n in root.find("m-clamp")]
    assert tbody.tag == "tbody"
    assert all("m-row" in tr.classes for tr in tbody.children)
    wrap, = root.find("m-clamp-wrap")
    assert wrap.attrs["style"] == "padding:0.25rem;overflow-x:auto;"


def test_pnl_list_of_ten_or_fewer_has_no_show_all():
    for n in (4, 10):
        c = clamps(_render_pnl(rows=_many_rows(n)))
        assert c.showall == [] and len(c.clamps) == 1


def test_pnl_empty_state_renders_no_rows():
    html = render_page(pnl_mod, "pnl.html", "/market/pnl", char_options=[],
                       selected_character_id=0, has_rows=False, rows=[], monthly=[],
                       totals=None, unmatched_total=0, assumptions=pnl_mod._assumptions(0))
    assert cells_rows(html) == []
    assert "pnl-chart" not in html


# ── stat cards → label · value lines ──────────────────────────────────

def _stat_lines(root, prefix):
    """Each .<prefix>-stats row as [(label, value, value classes, value id)]."""
    out = []
    for row in root.find(f"{prefix}-stats"):
        assert all(t.classes == [f"{prefix}-stat"] for t in row.children)
        lines = []
        for tile in row.children:
            assert [c.classes[0] for c in tile.children] == [f"{prefix}-stat-val", f"{prefix}-stat-label"]
            val, label = tile.children
            lines.append((label.full_text(), val.full_text(), val.classes, val.attrs.get("id")))
        out.append(lines)
    return out


def test_pnl_stat_cards_have_the_line_hooks():
    rows = _stat_lines(_tree(_content(_render_pnl())), "pnl")
    assert [[(l, v) for l, v, _, _ in r] for r in rows] == [
        [("Total realized profit", "527.40M"), ("Trading", "494.90M"), ("Industry", "-32.50M")],
        [("Units flipped", "17,544"), ("Item types traded", "4")],
    ]
    assert [r[2] for r in rows[0]] == [["pnl-stat-val", "pnl-profit"], ["pnl-stat-val", "pnl-profit"],
                                       ["pnl-stat-val", "pnl-loss"]]


def test_networth_stat_cards_have_the_line_hooks():
    rows = _stat_lines(_tree(_content(_render_nw())), "nw")
    assert rows == [[("Total net worth", "—", ["nw-stat-val"], "nw-total"),
                     ("Change over range", "—", ["nw-stat-val"], "nw-change"),
                     ("Unpriced items skipped", "—", ["nw-stat-val"], "nw-unpriced")]]


# ── Net Worth controls ────────────────────────────────────────────────

def test_networth_snapshot_group_has_its_hook():
    root = _tree(_content(_render_nw()))
    snap, = root.find("nw-snap")
    assert snap.attrs["style"] == "display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;"
    status, btn = snap.children
    assert status.tag == "span" and status.attrs == {"id": "nw-status"}
    assert btn.tag == "button" and btn.attrs["id"] == "nw-snapshot-btn"
    assert btn.classes == ["b-btn"]
    assert btn.attrs["hx-post"] == "/tools/networth/snapshot"
    assert btn.attrs["hx-target"] == "#nw-status"


def test_networth_range_buttons_are_unchanged():
    root = _tree(_content(_render_nw()))
    ranges, = root.by_id("nw-ranges")
    assert ranges.classes == ["nw-ranges"]
    assert [(b.tag, b.classes, b.attrs.get("data-range"), b.full_text()) for b in ranges.children] == [
        ("button", ["b-btn"], "30d", "30D"),
        ("button", ["b-btn", "is-active"], "90d", "90D"),
        ("button", ["b-btn"], "1y", "1Y")]


def test_snapshot_status_partial_is_one_span():
    for result, text in (({"written": 2, "date": "2026-10-04"}, "Snapshot taken · 2 characters · 2026-10-04"),
                         ({"written": 0}, "No character data to snapshot yet — sync a character first.")):
        html = render_page(nw_mod, "partials/networth_snapshot_status.html", "/", result=result)
        root = _tree(html)
        assert [c.tag for c in root.children] == ["span"]
        assert root.full_text() == text


# ── chart scripts: phone branch (text) ────────────────────────────────

def _script(html, marker):
    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, flags=re.S)
    found = [s for s in scripts if marker in s]
    assert len(found) == 1, marker
    return found[0]


def test_pnl_chart_script_has_a_phone_branch_for_fewer_month_labels():
    js = _script(_render_pnl(), "pnl-chart")
    assert f"window.{PHONE_MQ}" in js
    assert re.search(r"if \(phoneMq && phoneMq\.matches\) cfg\.options\.scales\.x\.ticks\.maxTicksLimit = 4;", js)
    # Desktop's x ticks stay as they were.
    assert "x: { stacked: true, ticks: { color: '#888' }, grid: { color: '#1a1a1a' } }," in js
    assert re.search(r"phoneMq\.addEventListener\('change', drawChart\)", js)


def test_networth_chart_script_has_a_phone_branch_for_legend_and_dates():
    js = _script(_render_nw(), "nw-chart")
    assert f"window.{PHONE_MQ}" in js
    branch = re.search(r"if \(phoneMq && phoneMq\.matches\) \{(.*?)\}", js, flags=re.S)
    assert branch
    assert "cfg.options.plugins.legend.position = 'bottom';" in branch.group(1)
    assert "cfg.options.scales.x.ticks.maxTicksLimit = 4;" in branch.group(1)
    # Desktop keeps Chart.js's default legend position and its 12 dates.
    assert "'top'" not in js
    assert "x: { ticks: { maxTicksLimit: 12, color: '#888' }, grid: { color: '#1a1a1a' } }," in js
    assert "legend: { labels: { color: '#ccc', boxWidth: 12 } }," in js
    assert re.search(r"phoneMq\.addEventListener\('change', \w+\)", js)


# ── chart scripts: behaviour (node, stub Chart) ───────────────────────

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const DATA = JSON.parse(process.argv[3]);

function run(startPhone) {
  const state = { phone: startPhone };
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
  function Chart(canvas, cfg) { this.canvas = canvas; this.cfg = cfg; made.push(this); }
  Chart.prototype.destroy = function () { this.destroyed = true; };
  const els = {};
  const el = id => els[id] || (els[id] = { id, style: {}, textContent: '',
    addEventListener() {}, querySelectorAll: () => [] });
  const document = { getElementById: el, body: el('#body'), addEventListener() {},
                     querySelectorAll: () => [], querySelector: () => null };
  const fetched = [];
  const fetch = url => { fetched.push(url);
    return Promise.resolve({ ok: true, json: () => Promise.resolve(JSON.parse(JSON.stringify(DATA))) }); };
  const sandbox = { window: { matchMedia, location: {} }, document, console, Chart, fetch, Number };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  const snap = cs => cs.map(c => ({ type: c.cfg.type, options: JSON.parse(JSON.stringify(c.cfg.options)),
    data: JSON.parse(JSON.stringify(c.cfg.data)), canvas: c.canvas.id }));
  const fire = phone => {
    const before = made.slice();
    state.phone = phone;
    made = [];
    mqls.forEach(m => m.ls.slice().forEach(fn => fn({ matches: m.matches, media: m.media })));
    return { charts: snap(made), oldDestroyed: before.every(c => c.destroyed) };
  };
  return new Promise(resolve => setImmediate(() => {
    const out = { first: snap(made), fetched: fetched.slice() };
    out.flip = fire(!startPhone);
    out.back = fire(startPhone);
    resolve(out);
  }));
}

(async () => {
  const out = { desktop: await run(false), phone: await run(true) };
  process.stdout.write(JSON.stringify(out));
})();
"""

_NW_DATA = {"range": "90d", "dates": ["2026-09-01", "2026-09-02", "2026-09-03"],
            "characters": [{"character_id": 90000001, "name": "Sample Pilot", "total": [1e9, 1.1e9, 1.2e9]},
                           {"character_id": 90000002, "name": "Sample Alt", "total": [5e8, None, 6e8]}],
            "total": [1.5e9, 1.1e9, 1.8e9], "unpriced_count": 3}

# The desktop options exactly as the base pages build them (callbacks drop
# out of JSON): the phone work must not change a single key.
_PNL_DESKTOP = {
    "responsive": True, "maintainAspectRatio": False, "animation": False,
    "scales": {"x": {"stacked": True, "ticks": {"color": "#888"}, "grid": {"color": "#1a1a1a"}},
               "y": {"stacked": True, "ticks": {"color": "#c8a951"}, "grid": {"color": "#1a1a1a"}}},
    "plugins": {"legend": {"display": True, "labels": {"color": "#888", "boxWidth": 10, "font": {"size": 10}}},
                "tooltip": {"callbacks": {}}},
}
_NW_DESKTOP = {
    "responsive": True, "maintainAspectRatio": False, "animation": False,
    "interaction": {"mode": "index", "intersect": False},
    "scales": {"x": {"ticks": {"maxTicksLimit": 12, "color": "#888"}, "grid": {"color": "#1a1a1a"}},
               "y": {"stacked": True, "position": "left", "ticks": {"color": "#c8a951"},
                     "grid": {"color": "#1a1a1a"}}},
    "plugins": {"legend": {"labels": {"color": "#ccc", "boxWidth": 12}}, "tooltip": {"callbacks": {}}},
}


def _phone_pnl():
    o = json.loads(json.dumps(_PNL_DESKTOP))
    o["scales"]["x"]["ticks"]["maxTicksLimit"] = 4
    return o


def _phone_nw():
    o = json.loads(json.dumps(_NW_DESKTOP))
    o["scales"]["x"]["ticks"]["maxTicksLimit"] = 4
    o["plugins"]["legend"]["position"] = "bottom"
    return o


def _run_harness(tmp_path, js, data):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS)
    script = tmp_path / "page.js"
    script.write_text(js)
    run = subprocess.run(["node", str(harness), str(script), json.dumps(data)],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _opts(run_part):
    return [c["options"] for c in run_part]


def test_pnl_chart_options_follow_the_breakpoint(tmp_path):
    """Desktop draws the base options exactly; a phone draw adds only the
    4-label cap; crossing the breakpoint either way redraws once with the
    other side's options and destroys the old chart."""
    out = _run_harness(tmp_path, _script(_render_pnl(), "pnl-chart"), {})
    d, p = out["desktop"], out["phone"]
    assert [c["type"] for c in d["first"]] == ["bar"]
    assert [c["canvas"] for c in d["first"]] == ["pnl-chart"]
    assert _opts(d["first"]) == [_PNL_DESKTOP]
    assert _opts(d["flip"]["charts"]) == [_phone_pnl()] and d["flip"]["oldDestroyed"]
    assert _opts(d["back"]["charts"]) == [_PNL_DESKTOP] and d["back"]["oldDestroyed"]
    assert _opts(p["first"]) == [_phone_pnl()]
    assert _opts(p["flip"]["charts"]) == [_PNL_DESKTOP]
    # The data is the same on both sides.
    ds = d["first"][0]["data"]
    assert ds["labels"] == ["2026-07", "2026-08", "2026-09"]
    assert [x["label"] for x in ds["datasets"]] == ["Trading", "Industry"]
    assert p["first"][0]["data"] == ds == d["flip"]["charts"][0]["data"]


def test_networth_chart_options_follow_the_breakpoint(tmp_path):
    """Desktop builds the base options exactly; a phone build puts the
    legend below and caps the dates at 4; crossing the breakpoint rebuilds
    the chart from the last data once, destroying the old one."""
    out = _run_harness(tmp_path, _script(_render_nw(), "nw-chart"), _NW_DATA)
    d, p = out["desktop"], out["phone"]
    assert d["fetched"] == ["/tools/networth/data.json?range=90d"]
    assert [c["type"] for c in d["first"]] == ["line"]
    assert _opts(d["first"]) == [_NW_DESKTOP]
    assert _opts(d["flip"]["charts"]) == [_phone_nw()] and d["flip"]["oldDestroyed"]
    assert _opts(d["back"]["charts"]) == [_NW_DESKTOP] and d["back"]["oldDestroyed"]
    assert _opts(p["first"]) == [_phone_nw()]
    assert _opts(p["flip"]["charts"]) == [_NW_DESKTOP]
    assert p["fetched"] == d["fetched"]                 # a rebuild doesn't refetch
    data = d["first"][0]["data"]
    assert data["labels"] == _NW_DATA["dates"]
    assert [x["label"] for x in data["datasets"]] == ["Sample Pilot", "Sample Alt"]
    assert p["first"][0]["data"] == data == d["flip"]["charts"][0]["data"]


def test_networth_breakpoint_change_without_a_chart_draws_nothing(tmp_path):
    empty = dict(_NW_DATA, dates=[], characters=[], total=[])
    out = _run_harness(tmp_path, _script(_render_nw(), "nw-chart"), empty)
    for side in ("desktop", "phone"):
        assert out[side]["first"] == []
        assert out[side]["flip"]["charts"] == [] and out[side]["back"]["charts"] == []


# ── CSS: R4 T3 section ────────────────────────────────────────────────

def _decl(body, prop, value):
    return re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*{re.escape(value)}\s*(?:;|$)", body)


def _phone():
    body, after = phone_block(_section("T3"))
    assert after.strip() == "", "no desktop rules: desktop renders as before"
    return body


def test_t3_section_is_one_phone_block():
    css = _section("T3")
    assert css.count("@media") == 1
    assert _phone().strip()


def test_t3_css_turns_stat_cards_into_lines():
    css = _phone()
    for p in ("pnl", "nw"):
        row = rule_bodies(css, f".{p}-stats")
        # The page's own <style> loads after site.css, and P&L's second row
        # has an inline 2-column grid: both must lose.
        assert _decl(row, "grid-template-columns", "minmax(0, 1fr) !important")
        assert _decl(row, "gap", "0 !important")
        tile = rule_bodies(css, f".{p}-stats > .{p}-stat")
        for prop, value in (("display", "flex"), ("justify-content", "space-between"),
                            ("align-items", "baseline")):
            assert _decl(tile, prop, value), (p, prop, value)
        assert _decl(rule_bodies(css, f".{p}-stats > .{p}-stat + .{p}-stat"), "border-top", "none")
        label = rule_bodies(css, f".{p}-stat > .{p}-stat-label")
        for prop, value in (("order", "-1"), ("flex", "none"), ("margin", "0"),
                            ("text-align", "left"), ("font-size", "11px")):
            assert _decl(label, prop, value), (p, prop, value)
        val = rule_bodies(css, f".{p}-stat > .{p}-stat-val")
        for prop, value in (("min-width", "0"), ("overflow-wrap", "anywhere"), ("text-align", "right")):
            assert _decl(val, prop, value), (p, prop, value)


def test_t3_css_draws_one_rule_per_pnl_row():
    css = _phone()
    assert _decl(rule_bodies(css, ".pnl-table tr.m-row"), "border-bottom", "1px solid var(--border)")
    cell = rule_bodies(css, ".pnl-table tr.m-row > td")
    for prop, value in (("padding", "0"), ("border-bottom", "none"), ("text-align", "left")):
        assert _decl(cell, prop, value), (prop, value)


def test_t3_css_makes_range_and_snapshot_buttons_40px():
    css = _phone()
    rng = rule_bodies(css, ".nw-ranges > button.b-btn")
    for prop, value in (("min-height", "40px"), ("min-width", "40px"), ("font-size", "12px")):
        assert _decl(rng, prop, value), (prop, value)
    snap = rule_bodies(css, ".nw-snap > button.b-btn")
    for prop, value in (("flex", "none"), ("min-height", "40px"), ("font-size", "12px"),
                        ("border", "1px solid var(--border)")):
        assert _decl(snap, prop, value), (prop, value)


def test_t3_css_wraps_the_snapshot_status_on_its_own_line():
    css = _phone()
    group = rule_bodies(css, ".nw-snap")
    assert _decl(group, "align-self", "stretch")
    assert _decl(group, "row-gap", "0 !important")         # beats the inline gap
    status = rule_bodies(css, ".nw-snap > #nw-status")
    for prop, value in (("order", "1"), ("flex-basis", "100%"), ("min-width", "0"),
                        ("overflow-wrap", "anywhere"), ("font-size", "12px")):
        assert _decl(status, prop, value), (prop, value)
    assert _decl(rule_bodies(css, ".nw-snap > #nw-status:not(:empty)"), "margin-top", "0.35rem")


def test_t3_css_keeps_the_long_notes_at_12px():
    css = _phone()
    for sel in ("p.pnl-note", "p.nw-note"):
        assert _decl(rule_bodies(css, sel), "font-size", "12px"), sel
