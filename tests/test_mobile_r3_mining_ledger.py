"""Mobile R3 T5: the Mining Ledger (`/industry/mining-ledger`) on phones.

Three templates:
- `partials/mining_ledger_corp.html`, an opened corp card's character rows.
  On phones each is an expand-on-tap row (user decision D12 A): the lead is
  the `.ml-check` checkbox inside a 40px `<label>` (a tap there ticks the
  box, and toggleMRow ignores taps on labels and inputs, so ticking never
  opens the row), key 1 the name, key 2 the ISK value. An opened row lists
  Name, Units, Days active and Ledger (the View button).
- `partials/mining_ledger_data.html`, the combined ledger: the same four
  lists as R2 T4's `mining.html`, converted the same way (key 2 the ISK
  value; Daily Summary and Full Detail show 10 rows plus "Show all N").
- `mining_ledger.html`: the chart sits in a box that is 280px tall on
  phones, where the chart fills it, shows fewer dates and cuts legend names
  to 20 characters; the selection bar wraps, with 40px buttons.

Desktop renders as before (D21). Names and ids are invented."""
import functools
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from app.routes import mining as mining_mod
from app.routes import mining_ledger as ledger_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, mrows, phone_block, render_page,
                           row_keys, row_labelled, row_lead, rule_bodies, SITE_CSS)

_section = functools.partial(css_section, release="R3")


def _isk(amount):
    """The partials' format_isk macro."""
    if amount >= 1e9:
        return f"{amount / 1e9:.2f}B"
    if amount >= 1e6:
        return f"{amount / 1e6:.2f}M"
    if amount >= 1e3:
        return f"{amount / 1e3:.1f}K"
    return f"{amount:.0f}"


def _cls(a):
    return a.get("class", "").split()


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _assert_never_hidden_inline(rows):
    """The phone rules use display:… !important, which would override an
    inline display:none on a row or tagged cell; only wrappers hide."""
    for r in rows:
        for a in [r["attrs"]] + [c["attrs"] for c in r["cells"]
                                 if c["attrs"].keys() & {"data-m", "data-m-label"}]:
            assert "display:none" not in a.get("style", "").replace(" ", "")
            assert "hidden" not in a


class _Tree(HTMLParser):
    """Every start tag with its attrs and the attrs of its open ancestors."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a, [s[1] for s in self.stack]))
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _tree(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.tags


# ══ Corp card: character rows (partials/mining_ledger_corp.html) ══════

_CHARS = [
    {"character_id": 90000011, "character_name": "Sample Prospector",
     "value": 1_234_567_890.0, "quantity": 2_500_000, "days": 41},
    {"character_id": 90000012, "character_name": "Sample Driller With A Long Name",
     "value": 45_600_000.0, "quantity": 380_000, "days": 9},
    {"character_id": 90000013, "character_name": "Sample Rookie",
     "value": 812.0, "quantity": 120, "days": 1},
]


def _render_corp(chars=_CHARS, error=None):
    stats = [dict(c) for c in chars]
    return render_page(
        ledger_mod, "partials/mining_ledger_corp.html", "/industry/mining-ledger/corp/98000001",
        corp_id=98000001, char_stats=stats,
        corp_total_value=sum(c["value"] for c in stats),
        corp_total_quantity=sum(c["quantity"] for c in stats),
        corp_days=max((c["days"] for c in stats), default=0), error=error)


def _corp_rows():
    """The corp partial's rows, one per character (never zero, so a loop
    over them can't pass vacuously)."""
    rows = cells_rows(_render_corp())
    assert len(rows) == len(_CHARS)
    return rows


def test_character_rows_are_tap_to_open_rows():
    html = _render_corp()
    rows = assert_mrow(html, min_rows=3)
    assert len(rows) == 3
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert {"b-table-row", "m-row", "ml-char-row"} <= set(_cls(r["attrs"]))
        assert "m-row--link" not in _cls(r["attrs"])
    for r in cells_rows(html):
        assert_single_value_child(r)
    _assert_never_hidden_inline(cells_rows(html))


def test_lead_is_the_checkbox_inside_a_label():
    """D12 A: the checkbox stays on the row as its lead. The <label> is the
    40px tap target (site.css) and ticks the box; toggleMRow skips taps on
    labels and inputs, so ticking never opens the row. The input keeps every
    hook the page script reads, and its desktop size."""
    for row, c in zip(_corp_rows(), _CHARS):
        lead = row_lead(row)
        assert len(lead) == 1
        assert lead[0]["tag"] == "label"
        assert lead[0]["attrs"].get("style") == "flex:0 0 20px;", "desktop column unchanged"
        assert len(lead[0]["kids"]) == 1
        box = lead[0]["kids"][0]
        assert box.get("type") == "checkbox"
        assert "ml-check" in _cls(box)
        assert box.get("data-charid") == box.get("data-char-id") == str(c["character_id"])
        assert box.get("data-click") == "toggleChar"
        assert "width:14px;height:14px" in box.get("style", "")
        assert box.get("aria-label") == f"Select {c['character_name']}", "the label has no text"


def test_keys_are_the_name_and_the_isk_value():
    for row, c in zip(_corp_rows(), _CHARS):
        k1, k2 = row_keys(row)
        assert k1["text"] == c["character_name"]
        assert "text-overflow:ellipsis" in k1["attrs"]["style"]
        assert k2["text"] == _isk(c["value"])
        assert "color:var(--accent)" in k2["attrs"]["style"]


def test_opened_row_lists_name_units_days_and_ledger():
    html = _render_corp()
    rows = cells_rows(html)
    assert len(rows) == len(_CHARS)
    for row, c in zip(rows, _CHARS):
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Units", "Days active", "Ledger"]
        name = labelled["Name"]
        assert name["text"] == c["character_name"]
        assert "m-only" in _cls(name["attrs"])
        # Phone-only twins of the desktop cells, with the bare numbers: the
        # labels already say "units" and "days".
        assert labelled["Units"]["text"] == f"{c['quantity']:,.0f}"
        assert labelled["Days active"]["text"] == str(c["days"])
        for label in ("Units", "Days active"):
            cell = labelled[label]
            assert "m-only" in _cls(cell["attrs"]) and not cell["kids"], label
            assert cell["attrs"].get("style") == "color:var(--muted);", "muted, as on desktop"


def test_view_button_sits_in_the_ledger_cell():
    for row, c in zip(_corp_rows(), _CHARS):
        cell = row_labelled(row)["Ledger"]
        assert len(cell["kids"]) == 1
        btn = cell["kids"][0]
        assert btn.get("data-click") == "viewLedger"
        assert btn.get("data-char-ids") == str(c["character_id"])
        assert "data-stop" in btn
        assert "b-btn" in _cls(btn)
        assert "m-tap" not in _cls(btn), "m-tap's 40px would shrink the 44px button"
        assert cell["text"] == "View"


def test_untagged_cells_are_the_portrait_and_the_desktop_numbers():
    """Phones hide untagged cells. The checkbox is the lead (one per row), so
    the portrait has no tag. The desktop units and days cells keep their
    markup exactly, suffixes and all: their m-only twins are the labelled
    lines. (A suffix split into its own m-hide span shifts the desktop
    glyphs' anti-aliasing.)"""
    for row, c in zip(_corp_rows(), _CHARS):
        portrait, units, days = _untagged(row)
        assert any(f"/characters/{c['character_id']}/portrait" in k.get("src", "")
                   for k in portrait["kids"])
        assert units["text"] == f"{c['quantity']:,.0f} units" and not units["kids"]
        assert units["attrs"]["style"] == "flex:1;text-align:right;font-size:10px;color:var(--muted);"
        assert days["text"] == f"{c['days']}d" and not days["kids"]
        assert days["attrs"]["style"] == "flex:0 0 50px;text-align:right;font-size:10px;color:var(--muted);"


def test_corp_summary_and_empty_states_have_no_rows():
    html = _render_corp()
    assert "View Corp Ledger" in html
    assert 'data-char-ids="90000011,90000012,90000013"' in html
    # The phone CSS lifts its 9px label to 12px by this class.
    assert re.search(r'class="b-btn ml-corp-view"[^>]*>\s*View Corp Ledger', html)
    assert mrows(_render_corp(chars=[])) == []
    assert mrows(_render_corp(chars=[], error="boom")) == []


# ══ Combined ledger (partials/mining_ledger_data.html) ═══════════════

_ORES = {1001 + i: f"Sample Ore {name}" for i, name in enumerate("ABCDEF")}
_PRICES = {tid: 100.0 * (n + 1) for n, tid in enumerate(_ORES)}
_SYSTEMS = {30000001: "Sample System One", 30000002: "Sample System Two",
            30000003: "Sample System Three"}


def _raw(days):
    """One ledger row per day, cycling through six ores and three systems,
    so `days` rows give `days` dates and `days` detail entries."""
    tids, sids = list(_ORES), list(_SYSTEMS)
    return [{"date": f"2026-09-{d + 1:02d}", "type_id": tids[d % 6],
             "solar_system_id": sids[d % 3], "quantity": 25000 + 1500 * d}
            for d in range(days)]


def _render_data(days=14, raw=None):
    """As mining_ledger_view builds it: aggregated lists, the chart series
    and the contributing characters' names."""
    raw = _raw(days) if raw is None else raw
    data = mining_mod._aggregate_ledger(raw, _ORES, _SYSTEMS, _PRICES)
    chart = ledger_mod._build_chart_data(raw, _ORES, _PRICES)
    html = render_page(ledger_mod, "partials/mining_ledger_data.html",
                       "/industry/mining-ledger/view", data=data,
                       characters=["Sample Prospector", "Sample Rookie"],
                       chart_data_json=json.dumps(chart))
    return html, data


def _lists(html, data):
    """The rendered m-rows split into the four lists, in page order."""
    rows = cells_rows(html)
    sizes = [("ore", len(data["by_ore"])), ("system", len(data["by_system"])),
             ("daily", len(data["by_date"])), ("detail", len(data["entries"]))]
    assert len(rows) == sum(n for _, n in sizes)
    out, at = {}, 0
    for name, n in sizes:
        out[name], at = rows[at:at + n], at + n
    return out


def _pct(value, total):
    return f"{value / total * 100:.0f}%"


def test_all_four_ledger_lists_are_tap_to_open_rows():
    html, _ = _render_data()
    rows = assert_mrow(html, min_rows=6 + 3 + 14 + 14)
    assert len(rows) == 6 + 3 + 14 + 14
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in _cls(r["attrs"])
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not row_lead(r), "ore icons stay inside key 1, not leads"
    _assert_never_hidden_inline(cells_rows(html))


def test_by_ore_type_rows():
    html, data = _render_data()
    for row, ore in zip(_lists(html, data)["ore"], data["by_ore"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == ore["name"]
        assert "ml-ore" in _cls(k1["attrs"])
        imgs = [k for k in k1["kids"] if f"/types/{ore['type_id']}/icon" in k.get("src", "")]
        assert len(imgs) == 1, "the ore icon stays inline in key 1"
        assert "flex-shrink:0" in imgs[0]["style"], "keeps its size in the phone flex line"
        assert k2["text"] == _isk(ore["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Units", "Share"]
        assert labelled["Name"]["text"] == ore["name"]
        assert "m-only" in _cls(labelled["Name"]["attrs"])
        assert labelled["Units"]["text"] == f"{ore['quantity']:,.0f}"
        assert labelled["Share"]["text"] == _pct(ore["value"], data["total_value"])
        assert not _untagged(row)


def test_by_system_rows():
    html, data = _render_data()
    for row, sys in zip(_lists(html, data)["system"], data["by_system"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == sys["name"]
        assert k2["text"] == _isk(sys["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Units", "Share"]
        assert labelled["Name"]["text"] == sys["name"]
        assert labelled["Units"]["text"] == f"{sys['quantity']:,.0f}"
        assert labelled["Share"]["text"] == _pct(sys["value"], data["total_value"])
        assert not _untagged(row)


def test_daily_summary_rows_leave_the_bar_untagged():
    html, data = _render_data()
    for row, day in zip(_lists(html, data)["daily"], data["by_date"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == day["date"]
        assert k2["text"] == _isk(day["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Units", "Types"]
        assert labelled["Units"]["text"] == f"{day['quantity']:,.0f}"
        assert labelled["Types"]["text"] == str(day["types"])
        bar = _untagged(row)
        assert len(bar) == 1, "only the bar cell is untagged, so phones hide it"
        assert any("height:4px" in k.get("style", "") for k in bar[0]["kids"])


def test_full_detail_rows():
    html, data = _render_data()
    for row, e in zip(_lists(html, data)["detail"], data["entries"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == e["ore_name"]
        assert "ml-ore" in _cls(k1["attrs"])
        imgs = [k for k in k1["kids"] if f"/types/{e['type_id']}/icon" in k.get("src", "")]
        assert len(imgs) == 1 and "flex-shrink:0" in imgs[0]["style"]
        assert k2["text"] == _isk(e["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Date", "System", "Quantity"]
        assert labelled["Name"]["text"] == e["ore_name"]
        assert labelled["Date"]["text"] == e["date"]
        assert labelled["System"]["text"] == e["system_name"]
        assert labelled["Quantity"]["text"] == f"{e['quantity']:,.0f}"
        assert not _untagged(row)


def test_every_ledger_table_row_is_an_m_row_or_an_m_head():
    html, _ = _render_data()
    table_rows = [a for _, a, _ in _tree(html) if "b-table-row" in _cls(a)]
    heads = [a for a in table_rows if "m-head" in _cls(a)]
    assert len(heads) == 2, "Daily Summary and Full Detail each have one header row"
    for a in table_rows:
        assert "m-row" in _cls(a) or "m-head" in _cls(a), a


def test_daily_summary_and_full_detail_show_all_past_ten():
    html, data = _render_data()
    c = clamps(html)
    assert c.wraps == 2 and c.nested_wraps == 0
    assert [k["children"] for k in c.clamps] == [len(data["by_date"]), len(data["entries"])] == [14, 14]
    assert [b["text"] for b in c.showall] == ["Show all 14", "Show all 14"]
    for b in c.showall:
        assert b["in_wrap"]
        assert {"m-only", "m-showall"} <= set(_cls(b["attrs"]))
        assert b["attrs"].get("type") == "button"
        assert b["attrs"].get("data-click") == "toggleExpanded"
        assert b["attrs"].get("data-toggle-target") == ".m-clamp-wrap"
    for _, a, anc in _tree(html):
        if "m-showall" in _cls(a):
            assert {"m-clamp-wrap", "ml-clamp"} <= set(_cls(anc[-1])), (
                "the button is a direct child of its page-scoped wrap")


def test_no_show_all_at_ten_rows():
    html, _ = _render_data(days=10)
    c = clamps(html)
    assert [k["children"] for k in c.clamps] == [10, 10]
    assert c.showall == []


def test_show_all_counts_follow_each_list():
    """Daily Summary counts days, Full Detail counts entries: two rows on
    one day make 12 entries over 11 days."""
    raw = _raw(11) + [{"date": "2026-09-01", "type_id": 1002,
                       "solar_system_id": 30000002, "quantity": 5000}]
    html, _ = _render_data(raw=raw)
    assert [b["text"] for b in clamps(html).showall] == ["Show all 11", "Show all 12"]


def test_full_detail_clamp_sits_inside_the_toggled_body():
    html, data = _render_data()
    tags = _tree(html)
    heads = [a for _, a, _ in tags if a.get("data-toggle-panel") == "ml-detail-table"]
    assert len(heads) == 1 and heads[0].get("data-click") == "togglePanel"
    body = [a for _, a, _ in tags if a.get("id") == "ml-detail-table"]
    assert len(body) == 1 and body[0].get("style", "").replace(" ", "") == "display:none;"
    wraps = [anc for _, a, anc in tags if "m-clamp-wrap" in _cls(a)]
    assert len(wraps) == 2
    assert not any(x.get("id") == "ml-detail-table" for x in wraps[0]), "Daily Summary isn't toggled"
    assert any(x.get("id") == "ml-detail-table" for x in wraps[1])
    detail_rows = [anc for _, a, anc in tags
                   if "m-row" in _cls(a) and any(x.get("id") == "ml-detail-table" for x in anc)]
    assert len(detail_rows) == len(data["entries"])
    for anc in detail_rows:
        assert "m-clamp" in _cls(anc[-1]), "detail rows are direct children of the clamp"


def test_ledger_keeps_its_chips_and_chart_data():
    html, _ = _render_data()
    assert "Includes:" in html and "Sample Rookie" in html
    island = [a for _, a, _ in _tree(html) if a.get("id") == "mining-chart-data"]
    assert len(island) == 1 and "hidden" in island[0]
    assert json.loads(island[0]["data-chart"])["dates"][0] == "2026-09-01"


# ══ The page (mining_ledger.html) ═════════════════════════════════════

_CORPS = [{"corp_id": 98000001, "corp_name": "Sample Mining Corp", "total": 3,
           "with_scope": 2, "characters": []}]


def _render_page():
    return render_page(ledger_mod, "mining_ledger.html", "/industry/mining-ledger",
                       corps=[dict(c) for c in _CORPS])


def _script(html):
    m = re.search(r"<script nonce=\"[^\"]*\">\s*\(function\(\) \{\s*var SEL_KEY(.*?)</script>", html, re.S)
    assert m, "the page script"
    return m.group(1)


def test_chart_canvas_sits_in_its_own_box():
    """Chart.js sizes a canvas from its parent, so a fixed phone height needs
    a box holding only the canvas. The box has no inline style: above 640px
    it is as tall as the canvas, as before."""
    tags = _tree(_render_page())
    canvas = [(a, anc) for t, a, anc in tags if a.get("id") == "mining-chart"]
    assert len(canvas) == 1
    a, anc = canvas[0]
    assert a.get("height") == "220"
    box = anc[-1]
    assert _cls(box) == ["ml-chart-box"] and "style" not in box
    assert any(x.get("id") == "chart-section" for x in anc)
    inside = [t for t, _, anc2 in tags if any("ml-chart-box" in _cls(x) for x in anc2)]
    assert inside == ["canvas"], "the box holds only the canvas"


def test_empty_chart_box_is_marked_for_phones():
    """With no data the box is marked is-empty, which hides it on phones
    only, so a phone doesn't keep an empty 280px box under the message."""
    s = _script(_render_page())
    assert "var box = canvas.closest('.ml-chart-box');" in s
    assert "box.classList.add('is-empty');" in s
    assert "box.classList.remove('is-empty');" in s


# The page's own script, run under node against a stub Chart, a stub DOM and
# a matchMedia whose `change` listeners the harness fires. It records every
# chart the script builds: the options and how many dates it shows.
_CHART_HARNESS = r"""
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
const made = [];
function Chart(canvas, cfg) { this.cfg = cfg; made.push(this); }
Chart.prototype.destroy = function () { this.destroyed = true; };
// Chart.js's default legend generator: one item per dataset.
Chart.defaults = { plugins: { legend: { labels: { generateLabels: chart =>
  chart.data.datasets.map((ds, i) => ({ text: ds.label, datasetIndex: i, hidden: false })) } } } };

const DAY = 86400000;
const dates = [];
for (let i = 59; i >= 0; i--) dates.push(new Date(Date.now() - i * DAY).toISOString().slice(0, 10));
const ORES = ['Compressed Sample Ore With A Long Name',   // 37 characters
              'Twenty Character Ore',                     // 20 characters
              'Compressed Samples Kernite',               // a space at character 19
              'Other'];
const chartData = { dates, ores: ORES, stacks: {}, ore_colors: {}, isk_values: dates.map(() => 100) };
ORES.forEach((o, i) => { chartData.stacks[o] = dates.map(() => i + 1); chartData.ore_colors[o] = '#12345' + i; });

const cls = () => ({ add() {}, remove() {} });
const box = { classList: cls() };
const els = {
  'mining-chart': { style: {}, closest: sel => (sel === '.ml-chart-box' ? box : null) },
  'chart-empty': { style: {} }, 'chart-section': { style: {} },
  'selection-bar': { style: {} }, 'sel-count': {},
  'mining-chart-data': { dataset: { chart: JSON.stringify(chartData) } },
};
const buttons = ['7', '14', '30', '90', '180', '365'].map(d =>
  ({ dataset: { days: d }, textContent: d === '180' ? '6m' : d === '365' ? '1y' : d + 'd', classList: cls() }));
const docLs = {};
const document = {
  getElementById: id => els[id] || null,
  querySelectorAll: sel => (sel === '.range-btn' ? buttons : []),
  addEventListener(t, fn) { (docLs[t] = docLs[t] || []).push(fn); },
};
const sandbox = { document, console, Date, JSON, Set, Array, parseInt, Chart,
  localStorage: { getItem: () => null, setItem() {} } };
sandbox.window = sandbox;
sandbox.matchMedia = matchMedia;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const last = () => {
  const c = made[made.length - 1];
  const o = c.cfg.options;
  const labels = o.plugins.legend.labels;
  const gen = typeof labels.generateLabels === 'function'
    ? labels.generateLabels({ data: c.cfg.data }).map(i => [i.text, i.datasetIndex]) : null;
  return JSON.parse(JSON.stringify({ maintainAspectRatio: o.maintainAspectRatio,
    xticks: o.scales.x.ticks, legend: labels, legendKeys: Object.keys(labels), gen,
    datasetLabels: c.cfg.data.datasets.map(d => d.label), dates: c.cfg.data.labels.length }));
};
const fire = phone => { state.phone = phone; mqls.forEach(m => m.ls.forEach(fn => fn({ matches: m.matches, media: m.media }))); };
const settle = () => (docLs['htmx:afterSettle'] || []).forEach(fn => fn({ detail: { target: { id: 'ledger-data' } } }));

const out = { mqls: mqls.map(m => m.media) };
fire(true); out.beforeData = made.length;            // no ledger yet: nothing to rebuild
fire(false);
settle(); out.desktop30 = last(); out.builds = [made.length];
sandbox.window.filterRange.call(buttons[0]); out.desktop7 = last();
fire(true); out.phone7 = last(); out.builds.push(made.length);
fire(false); out.back7 = last(); out.builds.push(made.length);
fire(true); settle(); out.phoneNewLedger = last();   // a new ledger starts at 30 days again
// A change event that leaves the chart on the side it was built for (fired
// here by hand) builds nothing; only a real crossing rebuilds.
let n = made.length; fire(true); out.samePhone = made.length - n;
n = made.length; fire(false); out.toDesktop = made.length - n;
n = made.length; fire(false); out.sameDesktop = made.length - n;
out.destroyed = made.slice(0, -1).every(c => c.destroyed);
process.stdout.write(JSON.stringify(out));
"""

_DESKTOP_X = {"color": "#474747", "font": {"family": "'JetBrains Mono', monospace", "size": 9},
              "maxTicksLimit": 10}
_DESKTOP_LEGEND = {"color": "#474747", "font": {"family": "'JetBrains Mono', monospace", "size": 9},
                   "boxWidth": 10, "padding": 8, "usePointStyle": True}


@pytest.fixture(scope="module")
def chart_run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    m = re.search(r'<script nonce="test-nonce">\s*(\(function\(\) \{\s*var SEL_KEY.*?)</script>',
                  _render_page(), re.S)
    assert m, "the page script"
    d = tmp_path_factory.mktemp("r3-t5-chart")
    (d / "page.js").write_text(m.group(1))
    (d / "harness.js").write_text(_CHART_HARNESS)
    run = subprocess.run(["node", str(d / "harness.js"), str(d / "page.js")],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_desktop_chart_options_are_unchanged(chart_run):
    """Above 640px the chart keeps the canvas's aspect ratio, 10 dates and
    the 9px legend, exactly as before (D21), and so after a round trip
    through a phone width."""
    for key in ("desktop30", "desktop7", "back7"):
        run = chart_run[key]
        assert run["maintainAspectRatio"] is True, key
        assert run["xticks"] == _DESKTOP_X, key
        assert run["legend"] == _DESKTOP_LEGEND, key
        assert run["legendKeys"] == list(_DESKTOP_LEGEND), "no generateLabels: Chart.js's default runs"
        assert run["gen"] is None, key


_ORE_LONG, _ORE_EXACT, _ORE_SPACED = ("Compressed Sample Ore With A Long Name",
                                      "Twenty Character Ore", "Compressed Samples Kernite")


def test_phone_chart_fills_its_box_with_fewer_dates_and_a_compact_legend(chart_run):
    """On phones the chart fills its 280px box and shows at most 5 unrotated
    dates. Its legend is 10px text with 6px padding (boxWidth stays 10) and
    names cut to 20 characters (below), so at 360px all twelve items fit in
    the half of the chart Chart.js allows a legend."""
    for key in ("phone7", "phoneNewLedger"):
        run = chart_run[key]
        assert run["maintainAspectRatio"] is False, key
        assert run["xticks"] == dict(_DESKTOP_X, maxTicksLimit=5, maxRotation=0), key
        assert run["legend"] == dict(_DESKTOP_LEGEND, font=dict(_DESKTOP_LEGEND["font"], size=10),
                                     padding=6), key
        assert run["legendKeys"] == list(_DESKTOP_LEGEND) + ["generateLabels"], key


def test_phone_legend_cuts_names_to_twenty_characters(chart_run):
    """As R2's combat stream legend does (actions.js _shortLegendLabels): a
    name over 20 characters becomes its first 19, less any trailing space,
    plus an ellipsis, so two fit per legend row and all twelve items show.
    Items keep their datasetIndex, so a tap still hides the right series,
    and the datasets keep their full names, which the tooltip reads."""
    assert _ORE_SPACED[18] == " " and len(_ORE_EXACT) == 20
    run = chart_run["phone7"]
    assert run["gen"] == [[_ORE_LONG[:19] + "…", 0], [_ORE_EXACT, 1], ["Compressed Samples…", 2],
                          ["Other", 3], ["ISK Value", 4]]
    assert len(run["gen"][0][0]) == 20
    assert run["datasetLabels"] == [_ORE_LONG, _ORE_EXACT, _ORE_SPACED, "Other", "ISK Value"]
    assert chart_run["back7"]["gen"] is None, "back above 640px, no cut"


def test_phone_legend_cut_reads_the_default_generator_lazily():
    """Chart.js's own generator is looked up when the legend is built, never
    when the script runs or a chart is configured, and the tooltip label
    still reads the dataset's full name."""
    s = _script(_render_page())
    assert s.count("Chart.defaults.plugins.legend.labels.generateLabels(chart)") == 1
    assert "Chart.defaults" not in s.replace("Chart.defaults.plugins.legend.labels.generateLabels(chart)", "")
    assert "if (phone) {" in s and "legendLabels.generateLabels = shortLegendLabels;" in s
    assert "item.dataset.label + ': ' + formatQty(item.parsed.y)" in s


def test_crossing_640px_rebuilds_the_chart_at_the_range_on_show(chart_run):
    """The script listens on one (max-width: 640px) query. Crossing it
    rebuilds the chart with that width's options, keeping the range the
    viewer picked (7 days here); a new ledger starts at 30 days. With no
    ledger loaded there is nothing to rebuild."""
    assert chart_run["mqls"] == ["(max-width: 640px)"]
    assert chart_run["beforeData"] == 0
    assert chart_run["builds"] == [1, 3, 4]
    assert chart_run["desktop7"]["dates"] < chart_run["desktop30"]["dates"]
    assert chart_run["phone7"]["dates"] == chart_run["back7"]["dates"] == chart_run["desktop7"]["dates"]
    assert chart_run["phoneNewLedger"]["dates"] == chart_run["desktop30"]["dates"]
    assert chart_run["destroyed"], "each rebuild destroys the chart it replaces"


def test_a_media_change_on_the_same_side_never_rebuilds_the_chart(chart_run):
    """The listener rebuilds only when the query's answer differs from the
    side the chart was last built for (lastBuiltSide). A change event whose
    answer, read when it is handled, matches that side keeps the chart it
    has."""
    assert chart_run["samePhone"] == 0
    assert chart_run["toDesktop"] == 1
    assert chart_run["sameDesktop"] == 0
    s = _script(_render_page())
    assert s.count("phoneMq.matches !== lastBuiltSide") == 1
    assert s.count("lastBuiltSide = phone;") == 1


def test_selection_bar_keeps_its_hooks_with_40px_buttons():
    tags = _tree(_render_page())
    bar = [a for _, a, _ in tags if a.get("id") == "selection-bar"]
    assert len(bar) == 1 and "ml-selection-bar" in _cls(bar[0])
    assert any(a.get("id") == "sel-count" for _, a, _ in tags)
    btns = {a.get("data-click"): a for t, a, anc in tags
            if t == "button" and any(x.get("id") == "selection-bar" for x in anc)}
    assert set(btns) == {"clearSelection", "viewCombined"}
    for a in btns.values():
        assert "b-btn" in _cls(a)
        assert "m-tap" not in _cls(a), "m-tap's 40px would shrink the 44px button"


def test_corp_card_head_is_not_an_m_row():
    """The card head keeps its own lazy-loading toggleExpanded."""
    html = _render_page()
    assert mrows(html) == []
    heads = [a for _, a, _ in _tree(html) if "ml-corp-top" in _cls(a)]
    assert len(heads) == 1
    assert heads[0].get("data-click") == "toggleExpanded"
    assert heads[0].get("hx-get") == "/industry/mining-ledger/corp/98000001"


# ══ CSS (R3 T5's site.css section) ════════════════════════════════════

def _phone():
    body, desktop = phone_block(_section("T5"))
    return body, desktop


def test_checkbox_lead_is_a_40px_tap_target():
    """The label is at least 40×40 and reaches into the row's padding, so the
    row stays one 40px line; the box itself grows from 14px to 20px (the
    global 44px input min-height would stretch it)."""
    css, _ = _phone()
    lead = rule_bodies(css, '.m-row.ml-char-row > [data-m="lead"]')
    assert "min-width: 40px !important" in lead
    assert "min-height: 40px" in lead
    assert "justify-content: center" in lead
    assert "margin: -0.6rem 0.4rem -0.6rem -0.75rem" in lead
    # In an open row the margins take it 5px into the Name line below; it
    # stays on top there, so a tap in that strip still ticks the box.
    assert "z-index: 1" in lead
    box = rule_bodies(css, ".ml-char-row .ml-check")
    for decl in ("width: 20px !important", "height: 20px !important", "min-height: 0"):
        assert decl in box, decl


def test_view_button_does_not_stretch_across_the_open_row():
    css, _ = _phone()
    body = rule_bodies(css, ".ml-char-row > [data-m-label] > .b-btn")
    assert "flex: none" in body and "min-width: 40px" in body
    assert "height" not in body, "the global phone button rule gives 44px"


def test_selection_bar_and_corp_ledger_buttons_have_12px_labels():
    css, _ = _phone()
    bar = rule_bodies(css, ".ml-selection-bar .b-btn")
    for decl in ("flex: none", "min-width: 40px", "font-size: 12px !important"):
        assert decl in bar, decl
    assert "height" not in bar
    assert "font-size: 12px !important" in rule_bodies(css, ".ml-corp-view")


def test_ore_key_keeps_its_icon_beside_an_ellipsised_name():
    css, _ = _phone()
    cell = rule_bodies(css, '.m-row > [data-m="key"].ml-ore')
    assert "display: flex !important" in cell and "align-items: center" in cell
    name = rule_bodies(css, ".m-row > .ml-ore > span")
    for decl in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"):
        assert decl in name, decl


def test_show_all_is_inset_inside_its_panel():
    css, _ = _phone()
    body = rule_bodies(css, ".ml-clamp > .m-showall")
    assert "width: calc(100% - 1.5rem)" in body
    assert "margin: 0.4rem 0.75rem 0.6rem" in body


def test_chart_box_is_280px_on_phones():
    """280px: with legend names cut to 20 characters all twelve items fit in
    the half Chart.js allows a legend, and the plot keeps over 120px."""
    css, _ = _phone()
    body = rule_bodies(css, ".ml-chart-box")
    assert "height: 280px" in body and "position: relative" in body
    assert "display: none" in rule_bodies(css, ".ml-chart-box.is-empty")


def test_selection_bar_wraps():
    """The buttons wrap below the count, right-aligned, at their own width:
    .b-btn's flex:1 would split the line evenly and wrap "View Combined
    Ledger" onto three lines."""
    css, _ = _phone()
    assert "flex-wrap: wrap" in rule_bodies(css, ".ml-selection-bar")
    group = rule_bodies(css, ".ml-selection-bar > div")
    for decl in ("margin-left: auto", "flex-wrap: wrap", "justify-content: flex-end"):
        assert decl in group, decl
    assert "flex: none" in rule_bodies(css, ".ml-selection-bar .b-btn")


def test_ledger_classes_have_no_desktop_rules():
    """Every class this task adds acts on phones only, so desktop renders
    exactly as before (D21). The section's desktop slot is empty."""
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    body, desktop = _phone()
    assert not desktop.strip(), "T5's desktop slot holds no rules"
    for cls in (".ml-char-row", ".ml-ore", ".ml-clamp", ".ml-chart-box", ".is-empty",
                ".ml-selection-bar", ".ml-check"):
        assert css.count(cls) == body.count(cls) > 0, cls
