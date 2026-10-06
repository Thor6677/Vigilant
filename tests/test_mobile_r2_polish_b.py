"""Mobile R2 Polish B: R1 leftovers and small R2 follow-ups at phone width.

  1. Wallet journal rows (journal.html and the overview's Recent
     Transactions). Key 1's cell holds only a 9px type badge, but it
     inherited the page's 16px / 26.4px line box, so it stood about 8px
     taller than key 2 (11px). Centred in the grid row, it started 4px
     higher, and on the overview the inline badge also sat about 2px low.
     On phones the badge is a shrink-wrapped block, so the cell is exactly
     the badge's height.
  2. Dashboard Combat Profile. On phones the Ship Usage Stream box is 280px,
     since its legend sits below the plot there, and the stream legend cuts
     long ship names to 20 characters so all ten fit. Tooltips keep the full
     names.
  3. Dashboard group toggle. dashSetGroupToggle keeps the glyph, title and
     aria-expanded together everywhere the glyph changes. The server
     renders the initial aria-expanded.
  4. Dashboard phone toolbar, Table view. The column Sort group takes 1.5
     shares of the row to View's 1, so every column name fits at 360px.
  5. Fittings. A per-fit status message gets its own full-width line under
     the buttons.
  6. This section, R2 P, is pinned like T1–T6. tests._mobile.phone_block.

Desktop renders identically (D21). Every new class has rules only in this
section's phone block, and the legend change acts only below 640px. The
group toggle's title and aria-expanded sync is the fix itself, and it is
not visible. Names and ids are invented."""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.dashboard import prefs as prefs_mod
from app.routes import character_detail as cd_mod
from app.routes import dashboard as dash_mod
from app.routes import fittings as fit_mod
from app.routes import journal as journal_mod
from tests._dashboard_fixture import render_full
from tests._mobile import (SITE_CSS, cells_rows, css_section, phone_block, render_page, row_keys,
                           row_labelled, rule_bodies, source)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ACTIONS_JS = os.path.join(_ROOT, "static", "js", "actions.js")


def _flat(s):
    return re.sub(r"\s+", " ", s).strip()


def _sec():
    return css_section("P")


def _phone():
    return phone_block(_sec())[0]


def _css_no_comments():
    with open(SITE_CSS, encoding="utf-8") as fh:
        return re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)


def _count(cls, css):
    return len(re.findall(re.escape(cls) + r"(?![\w-])", css))


def _all_phone_bodies(css):
    """Every `(max-width: 640px)` @media body in `css`, brace-matched."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if "max-width: 640px" not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return "\n".join(out)


# ── 6. The section and the phone_block helper ─────────────────────────

def test_phone_block_splits_a_section_into_its_phone_body_and_the_rest():
    sample = ("\n@media (max-width: 640px) {\n    .a { color: red; }\n"
              "    @supports (display: grid) { .b { display: grid; } }\n}\n.desk { color: blue; }\n")
    phone, after = phone_block(sample)
    assert phone == "\n    .a { color: red; }\n    @supports (display: grid) { .b { display: grid; } }\n"
    assert after == "\n.desk { color: blue; }\n"


@pytest.mark.parametrize("text", [
    ".a { color: red; }\n@media (max-width: 640px) {\n}\n",      # a rule before the block
    "@media (max-width: 480px) {\n}\n",                          # not the phone query
    "@media (max-width: 640px) {\n    .a { color: red; }\n",     # never closed
])
def test_phone_block_rejects_a_malformed_section(text):
    with pytest.raises(AssertionError):
        phone_block(text)


def test_this_section_is_one_phone_block_and_nothing_else():
    phone, after = phone_block(_sec())
    assert phone.strip(), "the R2 P phone block is empty"
    assert after.strip() == "", f"rules after the phone block: {after.strip()[:80]!r}"
    assert "@media" not in phone


_HOOKS = (".dash-cp-stream", ".dash-phone-tsort", ".fit-actions")


def test_new_class_hooks_are_styled_only_in_this_sections_phone_block():
    css, phone = _css_no_comments(), _phone()
    for cls in _HOOKS:
        assert _count(cls, phone), cls
        assert _count(cls, css) == _count(cls, phone), f"{cls} is styled outside the R2 P phone block"
    # The overview's .ref-type is styled for desktop by character_detail.html's
    # own <style>; site.css styles it on phones only.
    assert _count(".ref-type", phone)
    assert _count(".ref-type", css) == _count(".ref-type", _all_phone_bodies(css))


# ── 1. Wallet journal: both keys on one line ──────────────────────────

_BADGES = ('.m-row > [data-m="key"] > .journal-type', '.m-row > [data-m="key"] > .ref-type')


def test_journal_type_badges_are_shrink_wrapped_blocks_on_phones():
    """A block badge leaves key 1's cell with no line box of its own, so
    the cell is the badge's height (about 19px, key 2 is about 18px) and
    the two centre on one line. fit-content keeps the border around the
    text, and a long type ellipsises inside it. !important beats the
    journal badge's inline display:inline-block."""
    for sel in _BADGES:
        body = _flat(rule_bodies(_phone(), sel))
        for decl in ("display: block !important", "width: fit-content", "max-width: 100% !important",
                     "overflow: hidden", "text-overflow: ellipsis"):
            assert decl in body, (sel, decl)


_PILOT = NS(character_id=90000001, character_name="Pilot Alpha", corporation_name="Sample Corp",
            alliance_name=None, security_status=1.5, birthday=None)


def _render_journal():
    entries = [
        {"id": 1, "date": "2026-10-01T12:00:00Z", "ref_type": "bounty_prizes",
         "ref_type_label": "Bounty Prizes", "category": "pve", "amount": 1500000.0,
         "balance": 9000000000.0, "description": "Bounty prize for clearing a sample site",
         "reason": "", "first_party": "Sample Agency", "second_party": "Pilot Alpha", "tax": 0},
        {"id": 2, "date": "2026-10-01T13:00:00Z", "ref_type": "corporation_account_withdrawal",
         "ref_type_label": "Corporation Account Withdrawal", "category": "corp", "amount": -2500000.0,
         "balance": None, "description": "", "reason": "", "first_party": "", "second_party": "", "tax": None},
    ]
    return render_page(journal_mod, "journal.html", "/character/90000001/journal",
                       char=_PILOT, entries=entries, error=None, page=1, has_more=False,
                       category="all", categories=journal_mod.CATEGORY_LABELS,
                       is_corp=False, corp_id=None, division=None)


def _render_overview():
    return render_page(
        cd_mod, "character_detail.html", "/character/90000001",
        char=_PILOT, killmails_enabled=False, current_wallet=1.0e9,
        journal=[{"amount": 1500000.0, "balance": 9000000000.0, "ref_type": "bounty_prizes",
                  "description": "Bounty prize for clearing a sample site", "date": "2026-10-01T12:00:00Z"},
                 {"amount": -2500000.0, "balance": None, "ref_type": "market_escrow",
                  "description": "", "date": "2026-10-01T13:00:00Z"}],
        journal_error=None, chart_data_json='{"labels": [], "values": []}',
        active_range="1m", ranges=["1d", "1w", "1m"], active_skill=None, skillqueue=[],
        completed_skills=[], corp_history=[], total_sp_in_queue=0, total_trained_sp=5000000,
        unallocated_sp=0, has_implants_scope=False, last_synced_str="5m ago", queue_remaining=0,
        zkill=[], kills=0, losses=0, has_assets_scope=True, docked_at=None, current_system=None,
        implants=[], jump_clones=[], now=datetime(2026, 10, 3))


def _badge_keys(html):
    """Key 1 of every journal row (the rows with a Balance after label)."""
    rows = [r for r in cells_rows(html) if "Balance after" in row_labelled(r)]
    assert len(rows) == 2
    return [row_keys(r)[0] for r in rows]


def test_journal_page_key_one_is_just_the_type_badge():
    for key in _badge_keys(_render_journal()):
        assert [k.get("class", "").split() for k in key["kids"]] == [["journal-type"]]


def test_overview_key_one_is_just_the_type_badge():
    """The overview's markup belongs to another task, and the fix there is
    CSS only, so pin the shape it relies on: a key cell whose one element
    child is the .ref-type badge."""
    for key in _badge_keys(_render_overview()):
        assert len(key["kids"]) == 1
        assert "ref-type" in key["kids"][0].get("class", "").split()


# ── 2. Dashboard Combat Profile stream chart ──────────────────────────

_SHIPS = ["Sample Navy Issue Hull Mark %02d" % n for n in range(10)]


def _render_combat_profile():
    return render_page(
        dash_mod, "partials/dashboard_combat_profile.html", "/dashboard/combat-profile",
        year=2026, current_year=2026, summary={"kills": 40, "losses": 9, "isk_destroyed": 9.1e9, "isk_lost": 1.2e9},
        ships=[{"ship_type_id": 587, "count": 5}], weapons=[], systems=[],
        autopsy={"solo": 4, "small_gang": 3, "fleet": 2, "smartbomb": 1, "npc": 2}, autopsy_total=12,
        type_names={587: "Sample Frigate"}, system_names={}, system_security={},
        gang_split={"solo": 1, "small": 2, "medium": 0, "fleet": 0}, gang_total=3, cal_cells=[], cal_max=0,
        untouchable=[], profitability=[], radar_labels=["Kills", "Eff"], radar_values=[1, 2], radar_raw=[1, 2],
        ts_datasets=[{"label": s, "data": [1, 2]} for s in _SHIPS], ts_weeks=2, char_count=3)


def test_dashboard_stream_box_carries_a_class_hook_and_keeps_its_desktop_height():
    html = _render_combat_profile()
    m = re.search(r'<div class="dash-cp-stream" style="([^"]*)">\s*<canvas id="dashboard-stream" data-chart-kind="stream"', html)
    assert m, "the stream chart's box needs the dash-cp-stream hook"
    assert m.group(1) == "position:relative;height:200px;"
    assert html.count("dash-cp-stream") == 1


def test_dashboard_stream_box_is_taller_on_phones():
    """As on the character overview (R2 T1's .ks-stream): the legend moves
    below the plot on phones and would take half of the 200px box."""
    assert "height: 280px !important" in _flat(rule_bodies(_phone(), ".dash-cp-stream"))


def _combat_js():
    with open(_ACTIONS_JS, encoding="utf-8") as fh:
        js = fh.read()
    start = js.index("// ── Combat-profile charts")
    return js[start:js.index("// ── Activity history panel", start)]


def test_stream_legend_shortening_reads_the_default_generator_lazily():
    """T1's legend test runs actions.js against a stub Chart with no
    `defaults`. The default label generator is looked up when a legend is
    built, never when the file loads or a chart is configured."""
    block = _combat_js()
    assert block.count("Chart.defaults.plugins.legend.labels.generateLabels(chart)") == 1
    assert "Chart.defaults" not in block.replace("Chart.defaults.plugins.legend.labels.generateLabels(chart)", "")


_LEGEND_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const LONG = 'Sample Navy Issue Hull Mark 07';          // 30 characters
const EXACT = 'Twenty Character Nam';                   // 20 characters
const SHORT = 'Sample Frigate';
const SPACED = 'Charlie Navy Issue Hookbill';            // a space at character 19

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
// Chart.js's default legend generator for a line chart: one item per dataset.
Chart.defaults = { plugins: { legend: { labels: { generateLabels: chart =>
  chart.data.datasets.map((ds, i) => ({ text: ds.label, datasetIndex: i, hidden: false })) } } } };
const canvases = [
  { dataset: { chartKind: 'autopsy', chart: JSON.stringify({ solo: 1, small_gang: 0, fleet: 0, smartbomb: 0, npc: 0 }) } },
  { dataset: { chartKind: 'stream', chart: JSON.stringify({
      datasets: [{ label: LONG, data: [1] }, { label: EXACT, data: [1] }, { label: SHORT, data: [1] },
                 { label: SPACED, data: [1] }], weeks: 1 }) } },
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

const legendOf = type => made.filter(c => c.cfg.type === type).map(c => {
  const lg = c.cfg.options.plugins.legend;
  const labels = Object.assign({}, lg.labels);
  const gen = typeof labels.generateLabels === 'function'
    ? labels.generateLabels({ data: c.cfg.data }).map(i => [i.text, i.datasetIndex]) : null;
  delete labels.generateLabels;
  return { position: lg.position, labels, keys: Object.keys(lg.labels), gen,
           datasetLabels: c.cfg.data.datasets.map(d => d.label) };
})[0];
const fire = phone => {
  state.phone = phone;
  made = [];
  mqls.forEach(m => m.ls.slice().forEach(fn => fn({ matches: m.matches, media: m.media })));
  return { stream: legendOf('line'), doughnut: legendOf('doughnut') };
};
sandbox.window.renderCombatCharts(document);
const out = { desktop: { stream: legendOf('line'), doughnut: legendOf('doughnut') } };
out.phone = fire(true);
out.back = fire(false);
process.stdout.write(JSON.stringify(out));
"""

_LONG, _EXACT, _SHORT = "Sample Navy Issue Hull Mark 07", "Twenty Character Nam", "Sample Frigate"
_SPACED = "Charlie Navy Issue Hookbill"
_LABELS = [_LONG, _EXACT, _SHORT, _SPACED]
_DESKTOP_STREAM = {"position": "right", "labels": {"color": "#bfbfbf", "font": {"size": 9}, "boxWidth": 8},
                   "keys": ["color", "font", "boxWidth"], "gen": None, "datasetLabels": _LABELS}


@pytest.fixture(scope="module")
def legend_run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("pb-legend") / "harness.js"
    harness.write_text(_LEGEND_HARNESS)
    run = subprocess.run(["node", str(harness), _ACTIONS_JS], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_stream_legend_options_are_unchanged_on_desktop(legend_run):
    """No generateLabels key at all above 640px: Chart.js's own default runs,
    exactly as before. Crossing back from a phone restores the same options."""
    assert legend_run["desktop"]["stream"] == _DESKTOP_STREAM
    assert legend_run["back"]["stream"] == _DESKTOP_STREAM


def test_stream_legend_shortens_long_names_on_phones(legend_run):
    """Ten 29-character names sit one per row, and Chart.js caps a bottom
    legend at half the chart's height, so only 7 of 10 showed at 360px. At
    20 characters two fit per row. The datasets keep their full labels, and
    tooltips read those. Legend items keep their datasetIndex, so a tap still
    hides the right series. A cut that ends on a space drops it, so the
    ellipsis sits against the last word."""
    assert _SPACED[18] == " "
    stream = legend_run["phone"]["stream"]
    assert stream["position"] == "bottom"
    assert stream["labels"] == _DESKTOP_STREAM["labels"]          # colour, font and box as before
    assert stream["gen"] == [[_LONG[:19] + "…", 0], [_EXACT, 1], [_SHORT, 2], ["Charlie Navy Issue…", 3]]
    assert len(stream["gen"][0][0]) == 20
    assert stream["datasetLabels"] == _LABELS


def test_only_the_stream_legend_is_shortened(legend_run):
    """The doughnut's five labels are fixed and short; its legend options
    are untouched at both widths."""
    for side in ("desktop", "phone", "back"):
        doughnut = legend_run[side]["doughnut"]
        assert doughnut["gen"] is None, side
        assert doughnut["keys"] == ["color", "font", "boxWidth"], side


# ── 3. Dashboard group toggle: glyph, title and aria-expanded together ─

def _group_toggles(html):
    return re.findall(
        r'<button type="button" class="dash-group-toggle b-btn m-tap" data-click="toggleDashGroup" '
        r'data-group="([^"]*)"\s+title="([^"]*)" aria-expanded="(true|false)"[^>]*>([^<]*)</button>', html)


@pytest.mark.parametrize("mode", ["cards", "compact"])
@pytest.mark.parametrize("collapsed", [[], ["Sample Corp"]])
def test_group_toggles_render_title_and_aria_expanded_for_their_state(mode, collapsed):
    html = render_full("custom", dash_mode=mode, prefs_patch={"collapsed_groups": collapsed})
    toggles = _group_toggles(html)
    assert toggles and len(toggles) == html.count('data-click="toggleDashGroup"')
    for group, title, aria, glyph in toggles:
        if group in collapsed:
            assert (title, aria, glyph) == (f"Expand {group}", "false", "▸")
        else:
            assert (title, aria, glyph) == (f"Collapse {group}", "true", "▾")


def _body(html, head):
    return html.split(head, 1)[1].split("\n}", 1)[0]


def test_group_toggle_state_is_set_in_one_place_by_every_path():
    """toggleDashGroup, Edit's force-expand and its restore, a group added
    by + Add Account, and a rename (which rewrites data-group) all go
    through dashSetGroupToggle. Before, they set only the glyph, so the
    title said "Collapse …" on a collapsed group and aria-expanded never
    existed."""
    html = render_full("custom", dash_mode="cards")
    helper = _body(html, "function dashSetGroupToggle(btn, expanded) {")
    assert "btn.textContent = expanded ? '▾' : '▸';" in helper
    assert "btn.title = (expanded ? 'Collapse ' : 'Expand ') + btn.dataset.group;" in helper
    assert "btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');" in helper
    assert "dashSetGroupToggle(this, !willCollapse);" in _body(html, "function toggleDashGroup() {")
    edit = _body(html, "function toggleEditMode() {")
    assert edit.count("if (toggleBtn) dashSetGroupToggle(toggleBtn, true);") == 1    # force-expand
    assert edit.count("if (toggleBtn) dashSetGroupToggle(toggleBtn, false);") == 1   # restore
    add = html.split("if (addBtn) addBtn.addEventListener('click', function() {", 1)[1]
    add = add.split("function attachLabelEdit(label) {", 1)[0]
    assert "dashSetGroupToggle(toggleBtn, true);" in add
    assert "toggleBtn.title" not in add
    rename = html.split("function attachLabelEdit(label) {", 1)[1].split("document.querySelectorAll('.group-label').forEach(attachLabelEdit);", 1)[0]
    assert "dashSetGroupToggle(groupToggle, groupBody.style.display !== 'none');" in rename


def test_only_the_two_toggle_helpers_write_a_chevron():
    html = render_full("custom", dash_mode="cards")
    writes = re.findall(r"\.(?:textContent|innerText|innerHTML)\s*=\s*[^;\n]*[▸▾]", html)
    assert writes == [".textContent = expanded ? '▾' : '▸"] * 2, writes
    assert html.index("function dashSetSectionToggle(") < html.index("function dashSetGroupToggle(")


# ── 4. Phone Sort select, Table view ──────────────────────────────────

def _table_mode(**prefs):
    return render_full("custom", dash_mode="table", TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
                       table_rows=[], prefs_patch=prefs or None)


def test_table_phone_sort_group_carries_a_class_hook():
    html = _table_mode(table_columns=list(prefs_mod.TABLE_COLUMNS), table_sort={"key": "corporation", "dir": "asc"})
    m = re.search(r'<div class="dash-phone-tsort" style="([^"]*)">\s*<label[^>]*>\s*Sort\s*'
                  r'<select data-change="dashTableSortSelect"', html)
    assert m, "the Table view's sort group needs the dash-phone-tsort hook"
    assert m.group(1).startswith("flex:1 1 0;min-width:0;")
    assert html.count("dash-phone-tsort") == 1
    # Full column names: the select only stands in for the header row.
    assert '<option value="corporation" selected>Corporation</option>' in html
    for mode in ("cards", "compact", "detailed"):
        assert "dash-phone-tsort" not in render_full("custom", dash_mode=mode), mode


def test_table_phone_sort_group_takes_more_of_the_row_than_view():
    """At 360px the select was 119px wide, about 84px of it for text, while
    "Corporation" needs 106px and "Net Worth", "Queue End" and "Last Sync"
    87px each. View's longest option ("Detailed") needs 77px. With 1.5
    shares to 1 the select is about 152px. !important beats the inline flex
    shorthand. Other views keep 1:1: their Sort options are short."""
    body = _flat(rule_bodies(_phone(), ".dash-phone-toolbar > .dash-phone-tsort"))
    assert "flex-grow: 1.5 !important" in body


# ── 5. Fittings: per-fit status on its own line ───────────────────────

def _render_fittings():
    names = {3001: "Sample Blaster II", 3003: "Sample Armor Plate II"}
    slots = {"high": 5, "med": 4, "low": 4, "rig": 3}
    fits = []
    for fid, name, imported in ((7001, "Alpha Brawler", True), (7002, "Bravo Kiter", False)):
        raw = {"fitting_id": fid, "name": name, "description": "", "ship_type_id": 620,
               "items": [{"type_id": 3001, "flag": "HiSlot0", "quantity": 1},
                         {"type_id": 3003, "flag": "LoSlot0", "quantity": 1}]}
        fit = fit_mod._parse_fitting(raw, names, "Sample Cruiser", slots)
        fit["already_imported"] = imported
        fits.append(fit)
    return render_page(fit_mod, "fittings.html", "/character/90000001/fittings",
                       char={"character_id": 90000001, "character_name": "Pilot Alpha"},
                       fittings=fits, ship_groups={"Sample Cruiser": fits}, error=None,
                       slot_labels=fit_mod.SLOT_LABELS)


def test_fit_actions_row_is_hooked_and_ends_with_its_status():
    """saveOneToMyFits finds the status through the button's parent, so the
    hooked row must be that parent, with the status as its last child."""
    html = _render_fittings()
    rows = re.findall(r'<div class="fit-actions" style="([^"]*)">(.*?)</div>', html, re.S)
    assert len(rows) == 2
    for style, inner in rows:
        assert style == "padding:0.5rem 0.75rem;display:flex;align-items:center;gap:0.5rem;"
        tags = re.findall(r"<(\w+)([^>]*)>", inner)
        assert 'data-click="copyEFT"' in tags[0][1]
        assert tags[-1][0] == "span" and 'class="save-one-status"' in tags[-1][1]
    assert "var status = btn.parentElement.querySelector('.save-one-status');" in source("fittings.html")


def test_fit_status_takes_its_own_full_width_line_on_phones():
    """As T3 did for Save all: the row wraps, the status takes a whole line
    under the buttons, and the buttons keep the first line between them.
    row-gap 0 beats the inline gap, so an empty status (an aria-live region,
    always displayed) adds no height; a message gets a small top margin."""
    phone = _phone()
    row = _flat(rule_bodies(phone, ".fit-actions"))
    assert "flex-wrap: wrap" in row and "row-gap: 0 !important" in row
    assert "flex-basis: 100%" in _flat(rule_bodies(phone, ".fit-actions > .save-one-status"))
    assert re.search(r"margin-top: [\d.]+rem", rule_bodies(phone, ".fit-actions > .save-one-status:not(:empty)"))
