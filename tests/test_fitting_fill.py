"""T-086 WP1: the Fill button on rack modules.

fillCount() and fillRack() run in node against stubs; the buttons are checked
in the rendered markup. Type ids and names are invented.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page

GUN, GUN2, MISSILE, PLATE, RIGGY, SUBSYS = 7001, 7002, 7003, 7004, 7005, 7006

HARNESS = r"""
const src = %(src)s;
const notes = [], calls = {fetch: [], confirm: 0, render: 0, recalc: 0, saved: 0, refresh: 0, browserNotes: []};
let checkFit = {can_fit: true};
global.window = global;
global.document = {querySelectorAll: () => []};
global.confirm = () => { calls.confirm++; return confirmAnswer; };
let confirmAnswer = true;
global.fetch = (url) => {
  calls.fetch.push(url);
  const body = url.indexOf('check-fit') >= 0 ? checkFit : fetchCaps(url);
  return Promise.resolve({json: () => Promise.resolve(body)});
};
let fetchCaps = () => ({});
global.saveState = () => calls.saved++;
global.renderSlots = () => calls.render++;
global.recalcStats = () => calls.recalc++;
global.refreshOverheatCache = () => calls.refresh++;
global._limitNote = (t, near) => notes.push(t + (near ? '|near' : ''));
global._noteBrowserAdd = (t) => calls.browserNotes.push(t);
global.state = {shipTypeId: 1, items: []};
global.shipSlots = {high: 8, med: 8, low: 8, rig: 3, subsystem: 0, turret: 8, launcher: 8};
global.overheatCache = {};
eval(src + `
global.fillCount = fillCount; global.fillRack = fillRack; global._fillWithCaps = _fillWithCaps;`);

const cap = (o) => Object.assign({group_id: 1, hardpoint: null, max_group_fitted: 0, max_type_fitted: 0}, o);
const item = (t, slot) => ({type_id: t, type_name: 'n' + t, slot: slot || 'high', quantity: 1});
function reset(items, slots, caps) {
  global.state = {shipTypeId: 1, items: items || []};
  global.shipSlots = Object.assign({high: 8, med: 8, low: 8, rig: 3, subsystem: 0, turret: 8, launcher: 8}, slots || {});
  global.overheatCache = caps || {};
  notes.length = 0; calls.fetch.length = 0; calls.confirm = 0; calls.render = 0;
  calls.recalc = 0; calls.saved = 0; calls.refresh = 0; calls.browserNotes.length = 0;
}
const out = {};
(async () => {
  // fillCount
  reset([item(%(GUN)d), item(%(GUN)d)], {}, {%(GUN)d: cap({hardpoint: 'turret'}), %(PLATE)d: cap({})});
  out.free = fillCount(%(GUN)d, 'high');
  out.plain = fillCount(%(PLATE)d, 'low');
  reset([item(%(GUN)d)], {turret: 4}, {%(GUN)d: cap({hardpoint: 'turret'})});
  out.turretCap = fillCount(%(GUN)d, 'high');
  reset([item(%(GUN)d), item(%(MISSILE)d)], {launcher: 3, turret: 1},
        {%(GUN)d: cap({hardpoint: 'turret'}), %(MISSILE)d: cap({hardpoint: 'launcher', group_id: 2})});
  out.launcherCap = fillCount(%(MISSILE)d, 'high');
  out.turretLeft = fillCount(%(GUN)d, 'high');
  reset([item(%(GUN)d)], {turret: undefined}, {%(GUN)d: cap({hardpoint: 'turret'})});
  out.unknownHardpoints = fillCount(%(GUN)d, 'high');
  // group limit counts other types of the same group, across racks
  reset([item(%(GUN)d), item(%(GUN2)d, 'med')], {}, {%(GUN)d: cap({max_group_fitted: 3}), %(GUN2)d: cap({})});
  out.groupLimit = fillCount(%(GUN)d, 'high');
  reset([item(%(GUN)d), item(%(GUN)d)], {}, {%(GUN)d: cap({max_type_fitted: 5, group_id: 9})});
  out.typeLimit = fillCount(%(GUN)d, 'high');
  reset([], {}, {});
  out.noCaps = fillCount(%(GUN)d, 'high');
  reset([], {subsystem: 5}, {});
  out.subsystem = fillCount(%(SUBSYS)d, 'subsystem');
  out.drone = [fillCount(1, 'drone'), fillCount(1, 'cargo'), fillCount(1, 'fighter')];
  reset([item(%(RIGGY)d, 'rig')], {rig: 3}, {%(RIGGY)d: cap({})});
  out.rig = fillCount(%(RIGGY)d, 'rig');

  // fillRack from a search result: caps missing, so they are fetched first
  reset([item(%(PLATE)d, 'low')], {low: 4}, {});
  fetchCaps = () => ({[%(PLATE)d]: cap({}), [%(GUN)d]: cap({})});
  const btn = {dataset: {typeId: '%(PLATE)d', typeName: 'Test Plate', slotType: 'low'}};
  fillRack.call(btn, {});
  await new Promise((r) => setTimeout(r, 20));
  out.search = {items: state.items.map((i) => JSON.parse(JSON.stringify(i))), distinct: new Set(state.items).size,
                fetch: calls.fetch.slice(), confirm: calls.confirm, render: calls.render, recalc: calls.recalc,
                saved: calls.saved, refresh: calls.refresh, notes: notes.slice(), browser: calls.browserNotes.length};

  // not fitting: one confirm, declined -> nothing added
  reset([], {low: 4}, {%(PLATE)d: cap({})});
  checkFit = {can_fit: false}; confirmAnswer = false;
  fillRack.call(btn, {});
  await new Promise((r) => setTimeout(r, 20));
  out.declined = {n: state.items.length, confirm: calls.confirm};
  confirmAnswer = true;
  fillRack.call(btn, {});
  await new Promise((r) => setTimeout(r, 20));
  out.accepted = {n: state.items.length, confirm: calls.confirm, checks: calls.fetch.filter((u) => u.indexOf('check-fit') >= 0).length};
  checkFit = {can_fit: true};

  // hardpoint-limited note, from Browse (no check-fit, note on the overlay too)
  reset([item(%(GUN)d)], {turret: 4, high: 8}, {%(GUN)d: cap({hardpoint: 'turret'})});
  _fillWithCaps(%(GUN)d, 'Test Gun', 'high', true);
  await new Promise((r) => setTimeout(r, 20));
  out.browse = {n: state.items.length, notes: notes.slice(), browser: calls.browserNotes.slice(),
                checks: calls.fetch.filter((u) => u.indexOf('check-fit') >= 0).length};

  // rack full and subsystem: nothing happens to the items
  reset(Array.from({length: 8}, () => item(%(PLATE)d, 'low')), {}, {%(PLATE)d: cap({})});
  fillRack.call({dataset: {typeId: '%(PLATE)d', typeName: 'Test Plate', slotType: 'low'}}, {});
  await new Promise((r) => setTimeout(r, 20));
  out.full = {n: state.items.length, notes: notes.slice(), render: calls.render};
  reset([], {subsystem: 5}, {});
  fillRack.call({dataset: {typeId: '%(SUBSYS)d', typeName: 'Test Sub', slotType: 'subsystem'}}, {});
  await new Promise((r) => setTimeout(r, 20));
  out.sub = {n: state.items.length, fetch: calls.fetch.length};
  console.log(JSON.stringify(out));
})();
"""


def _fn(script, name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    assert m, f"no function {name}() in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    return script[m.start():i]


@pytest.fixture(scope="module")
def page():
    return render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])


@pytest.fixture(scope="module")
def result(page, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    script = blocks[0]
    parts = ["var RACK_NAMES = {high: 'high', med: 'mid', low: 'low', rig: 'rig', subsystem: 'subsystem'};",
             "var FILL_SLOTS = ['high', 'med', 'low', 'rig'];"]
    parts += [_fn(script, n) for n in (
        "getSlotCount", "getSlotLimit", "_rackFill", "_fillLimits", "fillCount",
        "_fillNoteText", "_fillPush", "_fillWithCaps", "fillRack")]
    src = "\n".join(parts)
    path = tmp_path_factory.mktemp("node") / "harness.js"
    ids = dict(GUN=GUN, GUN2=GUN2, MISSILE=MISSILE, PLATE=PLATE, RIGGY=RIGGY, SUBSYS=SUBSYS)
    path.write_text(HARNESS % dict(src=json.dumps(src), **ids))
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_fill_count_is_the_free_slots(result):
    assert result["free"] == 6          # 8 high, 2 fitted
    assert result["plain"] == 8
    assert result["rig"] == 2
    assert result["drone"] == [0, 0, 0]
    assert result["noCaps"] == 8        # nothing known yet: unlimited
    assert result["subsystem"] == 0


def test_fill_count_respects_hardpoints(result):
    assert result["turretCap"] == 3     # 4 turret hardpoints, 1 fitted
    assert result["launcherCap"] == 2   # 3 launcher hardpoints, 1 fitted; the turret does not use one
    assert result["turretLeft"] == 0    # 1 turret hardpoint, 1 fitted
    assert result["unknownHardpoints"] == 7


def test_fill_count_respects_group_and_type_limits(result):
    assert result["groupLimit"] == 1    # 3 per group; one in high, one in med
    assert result["typeLimit"] == 3     # 5 of the type; 2 fitted


def test_search_fill_pushes_separate_items_and_checks_once(result):
    s = result["search"]
    assert len(s["items"]) == 1 + 3     # 4 low slots, 1 fitted -> 3 added
    assert s["distinct"] == len(s["items"]), "every item is its own object"
    assert all(i["quantity"] == 1 and i["slot"] == "low" for i in s["items"])
    assert sum(1 for u in s["fetch"] if "check-fit" in u) == 1
    assert sum(1 for u in s["fetch"] if "module-caps" in u) == 1
    assert (s["render"], s["recalc"], s["saved"], s["refresh"]) == (1, 1, 1, 1)
    assert s["notes"] == ["✓ Added 3× Test Plate · low 4/4|near"]
    assert s["browser"] == 0


def test_not_fitting_asks_once_and_a_no_adds_nothing(result):
    assert result["declined"] == {"n": 0, "confirm": 1}
    assert result["accepted"]["n"] == 4 and result["accepted"]["confirm"] == 2
    assert result["accepted"]["checks"] == 2   # one check per click, however many are added


def test_browse_fill_skips_the_fit_check_and_names_the_hardpoints(result):
    b = result["browse"]
    assert b["n"] == 1 + 3 and b["checks"] == 0
    assert b["notes"] == ["✓ Added 3× Test Gun · high 4/8 · 3 turret hardpoints free|near"]
    assert b["browser"] == [b["notes"][0].replace("|near", "")]


def test_full_rack_and_subsystem_add_nothing(result):
    assert result["full"]["n"] == 8 and result["full"]["render"] == 0
    assert result["full"]["notes"] == ["✖ Not added · low 8/8 full|near"]
    assert result["sub"] == {"n": 0, "fetch": 0}


# ── markup ──────────────────────────────────────────────────────────

def _search(rows):
    return render_page(fitting_mod, "partials/fitting_search_results.html",
                       results=rows, search_type="module")


def test_search_results_have_fill_on_rack_slots_only(page):
    rows = [{"type_id": 7001 + i, "type_name": f"Test {s}", "slot_type": s, "volume": 1}
            for i, s in enumerate(["high", "med", "low", "rig", "subsystem", "unknown"])]
    html = _search(rows)
    buttons = re.findall(r"<button[^>]*data-click=\"fillRack\"[^>]*>Fill</button>", html)
    assert len(buttons) == 4
    for b in buttons:
        assert 'class="fit-ctl fit-fill"' in b and "data-stop" in b
        assert "m-tap" not in b
    assert "subsystem" not in " ".join(re.findall(r'data-slot-type="(\w+)"[^>]*>Fill', html))
    drones = render_page(fitting_mod, "partials/fitting_search_results.html",
                         results=[{"type_id": 1, "type_name": "Test Drone", "volume": 5}],
                         search_type="drone")
    assert "fillRack" not in drones


def test_browse_rows_carry_a_fill_button_for_rack_modules(page):
    script = "\n".join(re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S))
    items = _fn(script, "renderBrowseItems")
    assert 'data-click="addModuleFromBrowser" data-fill="1" data-stop' in items
    assert "FILL_SLOTS.indexOf(item.slot_type) >= 0" in items
    assert "m-tap" not in items
    assert 'class="fit-bname' in items
    add = _fn(script, "addModuleFromBrowser")
    assert "this.dataset.fill === '1'" in add


def test_fill_css_is_a_marked_t086_section():
    css = (__import__("pathlib").Path(__file__).parents[1] / "static" / "css" / "site.css").read_text()
    assert css.count("/* ── T-086 · fill a rack ── */") == 1 and css.count("/* ── end T-086 ── */") == 1
    sect = css[css.index("/* ── T-086"):css.index("/* ── end T-086")]
    assert "min-width: 0" in sect and "text-overflow: ellipsis" in sect
    assert not re.search(r"(?:^|;)\s*(?:min-)?height\s*:", sect), "button height comes from the phone floor"
