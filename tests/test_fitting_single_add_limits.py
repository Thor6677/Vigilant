"""Single adds obey the same limits as Fill.

A browser row click and a rack search result each add ONE module, and only
when a turret/launcher hardpoint and the module's group/type allowance are
free. The page functions run in node against stubs. Type ids and names are
invented.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page

GUN, MISSILE, PLATE, DC, CLOAK, SUBSYS = 7001, 7003, 7004, 7007, 7008, 7006

HARNESS = r"""
const src = %(src)s;
const notes = [], calls = {fetch: [], confirm: 0, browserNotes: []};
let checkFit = {can_fit: true}, confirmAnswer = true, capsFail = false;
let fetchCaps = () => ({});
global.window = global;
global.confirm = () => { calls.confirm++; return confirmAnswer; };
global.fetch = (url) => {
  calls.fetch.push(url);
  if (url.indexOf('check-fit') >= 0) return Promise.resolve({json: () => Promise.resolve(checkFit)});
  if (capsFail) return Promise.reject(new Error('offline'));
  return Promise.resolve({json: () => Promise.resolve(fetchCaps(url))});
};
global._limitNote = (t, near) => notes.push(t + (near ? '|near' : ''));
global._noteBrowserAdd = (t) => calls.browserNotes.push(t);
global._origAddModule = (t, n, s) => { state.items.push({type_id: t, type_name: n, slot: s, quantity: 1}); };
global.state = {shipTypeId: 1, items: []};
global.shipSlots = {};
global.overheatCache = {};
eval(src + `
global.addModuleFromBrowser = addModuleFromBrowser; global.addModule = addModule;`);

const cap = (o) => Object.assign({group_id: 1, hardpoint: null, max_group_fitted: 0, max_type_fitted: 0}, o);
const CAPS = {
  %(GUN)d: cap({hardpoint: 'turret', group_id: 1}),
  %(MISSILE)d: cap({hardpoint: 'launcher', group_id: 2}),
  %(PLATE)d: cap({group_id: 3}),
  %(DC)d: cap({group_id: 4, max_group_fitted: 1}),
  %(CLOAK)d: cap({group_id: 5, max_type_fitted: 1}),
};
const item = (t, slot) => ({type_id: t, type_name: 'n' + t, slot: slot || 'high', quantity: 1});
const row = (t, name, slot) => ({dataset: {typeId: String(t), typeName: name, slotType: slot}});
const many = (n, t, slot) => Array.from({length: n}, () => item(t, slot));
function reset(items, slots, caps, shipTypeId) {
  global.state = {shipTypeId: shipTypeId === undefined ? 1 : shipTypeId, items: items || []};
  global.shipSlots = Object.assign({high: 8, med: 8, low: 8, rig: 3, subsystem: 0, turret: 8, launcher: 8}, slots || {});
  global.overheatCache = caps === undefined ? Object.assign({}, CAPS) : caps;
  notes.length = 0; calls.fetch.length = 0; calls.confirm = 0; calls.browserNotes.length = 0;
  checkFit = {can_fit: true}; confirmAnswer = true; capsFail = false; fetchCaps = () => ({});
}
const tick = () => new Promise((r) => setTimeout(r, 20));
const snap = () => ({n: state.items.length, notes: notes.slice(), browser: calls.browserNotes.slice(),
                     checks: calls.fetch.filter((u) => u.indexOf('check-fit') >= 0).length,
                     caps: calls.fetch.filter((u) => u.indexOf('module-caps') >= 0)});
const out = {};
(async () => {
  // browser, a hardpoint free
  reset([item(%(GUN)d)], {turret: 2});
  addModuleFromBrowser.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.browseOk = snap();

  // browser, turret hardpoints used up
  reset(many(2, %(GUN)d), {turret: 2});
  addModuleFromBrowser.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.browseTurretFull = snap();

  // browser, launcher hardpoints used up
  reset([item(%(MISSILE)d)], {launcher: 1});
  addModuleFromBrowser.call(row(%(MISSILE)d, 'Test Launcher', 'high'), {});
  await tick(); out.browseLauncherFull = snap();

  // rack search, a hardpoint free
  reset([item(%(GUN)d)], {turret: 2});
  addModule.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.searchOk = snap();

  // rack search, turret hardpoints used up
  reset(many(2, %(GUN)d), {turret: 2});
  addModule.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.searchTurretFull = snap();

  // rack search, the fit check says no and the user declines
  reset([], {turret: 2});
  checkFit = {can_fit: false}; confirmAnswer = false;
  addModule.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.searchDeclined = Object.assign(snap(), {confirm: calls.confirm});

  // no ship chosen: no fit check, still adds
  reset([], {}, undefined, null);
  addModule.call(row(%(PLATE)d, 'Test Plate', 'low'), {});
  await tick(); out.noShip = snap();

  // group and type limits
  reset([item(%(DC)d, 'low')]);
  addModuleFromBrowser.call(row(%(DC)d, 'Test Control', 'low'), {});
  await tick(); out.groupFull = snap();
  reset([item(%(CLOAK)d)]);
  addModule.call(row(%(CLOAK)d, 'Test Cloak', 'high'), {});
  await tick(); out.typeFull = snap();

  // a plain module is not held back by full turret hardpoints
  reset(many(2, %(GUN)d), {turret: 2});
  addModuleFromBrowser.call(row(%(PLATE)d, 'Test Plate', 'low'), {});
  await tick(); out.plain = snap();

  // caps not cached: fetched first, then the limit applies
  reset(many(2, %(GUN)d), {turret: 2}, {});
  fetchCaps = () => ({[%(GUN)d]: CAPS[%(GUN)d]});
  addModuleFromBrowser.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.uncached = snap();

  // caps fetch fails: the add goes through
  reset(many(2, %(GUN)d), {turret: 2}, {});
  capsFail = true;
  addModuleFromBrowser.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.capsFail = snap();

  // already over the limit: refused, nothing removed
  reset(many(5, %(GUN)d), {turret: 4});
  addModuleFromBrowser.call(row(%(GUN)d, 'Test Gun', 'high'), {});
  await tick(); out.overLimit = snap();

  // full rack, both paths
  reset(many(8, %(PLATE)d, 'low'));
  addModuleFromBrowser.call(row(%(PLATE)d, 'Test Plate', 'low'), {});
  await tick(); out.browseRackFull = snap();
  reset(many(8, %(PLATE)d, 'low'));
  addModule.call(row(%(PLATE)d, 'Test Plate', 'low'), {});
  await tick(); out.searchRackFull = snap();

  // a subsystem is not a rack module: added with no caps lookup
  reset([], {subsystem: 4}, {});
  addModuleFromBrowser.call(row(%(SUBSYS)d, 'Test Sub', 'subsystem'), {});
  await tick(); out.subsystem = snap();

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


def _assigned(script, name):
    """The `name = function(...) {...}` reassignment (the addModule wrapper)."""
    m = re.search(r"\n" + re.escape(name) + r"\s*=\s*function\s*\([^)]*\)\s*\{", script)
    assert m, f"no `{name} = function` in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    return script[m.start():i] + ";"


@pytest.fixture(scope="module")
def script():
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    return blocks[0]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var RACK_NAMES = {high: 'high', med: 'mid', low: 'low', rig: 'rig', subsystem: 'subsystem'};",
             "var FILL_SLOTS = ['high', 'med', 'low', 'rig'];",
             "var addModule;"]
    parts += [_fn(script, n) for n in (
        "getSlotCount", "getSlotLimit", "isSlotFull", "_rackFill", "_browserAddNoteText",
        "_fillLimits", "_fillNoteText", "_withRackCaps", "_addOnePush", "_addOneWithCaps",
        "addModuleFromBrowser")]
    parts.append(_assigned(script, "addModule"))
    path = tmp_path_factory.mktemp("node") / "harness.js"
    ids = dict(GUN=GUN, MISSILE=MISSILE, PLATE=PLATE, DC=DC, CLOAK=CLOAK, SUBSYS=SUBSYS)
    path.write_text(HARNESS % dict(src=json.dumps("\n".join(parts)), **ids))
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


NO_TURRET = "✖ Not added · no free turret hardpoints"
NO_LAUNCHER = "✖ Not added · no free launcher hardpoints"
LIMIT = "✖ Not added · fitting limit reached"


def test_browser_add_with_a_free_hardpoint_adds_one(result):
    r = result["browseOk"]
    assert r["n"] == 2
    assert r["browser"] == ["✓ Added Test Gun · high 2/8"]
    assert r["notes"] == [] and r["checks"] == 0


def test_browser_add_is_refused_at_the_turret_limit(result):
    r = result["browseTurretFull"]
    assert r["n"] == 2
    assert r["notes"] == [NO_TURRET + "|near"], "shown beside the legend on every screen size"
    assert r["browser"] == [NO_TURRET], "and on the phone overlay"


def test_browser_add_is_refused_at_the_launcher_limit(result):
    r = result["browseLauncherFull"]
    assert r["n"] == 1 and r["notes"] == [NO_LAUNCHER + "|near"]


def test_search_add_with_a_free_hardpoint_adds_one_quietly(result):
    r = result["searchOk"]
    assert r["n"] == 2 and r["checks"] == 1
    assert r["notes"] == [] and r["browser"] == []


def test_search_add_is_refused_at_the_turret_limit_after_the_fit_check(result):
    r = result["searchTurretFull"]
    assert r["n"] == 2 and r["checks"] == 1
    assert r["notes"] == [NO_TURRET + "|near"] and r["browser"] == []


def test_search_add_declined_at_the_fit_check_adds_nothing(result):
    r = result["searchDeclined"]
    assert r["n"] == 0 and r["confirm"] == 1 and r["notes"] == []


def test_search_add_without_a_ship_skips_the_fit_check(result):
    assert result["noShip"]["n"] == 1 and result["noShip"]["checks"] == 0


def test_group_and_type_limits_refuse_a_single_add(result):
    assert result["groupFull"]["n"] == 1 and result["groupFull"]["notes"] == [LIMIT + "|near"]
    assert result["typeFull"]["n"] == 1 and result["typeFull"]["notes"] == [LIMIT + "|near"]


def test_a_plain_module_is_not_held_back_by_full_hardpoints(result):
    r = result["plain"]
    assert r["n"] == 3 and r["notes"] == []
    assert r["browser"] == ["✓ Added Test Plate · low 1/8"]


def test_missing_caps_are_fetched_before_counting(result):
    r = result["uncached"]
    assert len(r["caps"]) == 1 and str(GUN) in r["caps"][0]
    assert r["n"] == 2 and r["notes"] == [NO_TURRET + "|near"]


def test_a_failed_caps_fetch_does_not_block_the_add(result):
    assert result["capsFail"]["n"] == 3


def test_an_over_limit_fit_refuses_more_and_keeps_what_it_has(result):
    r = result["overLimit"]
    assert r["n"] == 5 and r["notes"] == [NO_TURRET + "|near"]


def test_a_full_rack_still_says_full(result):
    assert result["browseRackFull"]["n"] == 8
    assert result["browseRackFull"]["browser"] == ["✖ Not added · low 8/8 full"]
    assert result["searchRackFull"]["n"] == 8
    assert result["searchRackFull"]["notes"] == ["✖ Not added · low 8/8 full|near"]


def test_a_subsystem_add_skips_the_rack_limits(result):
    r = result["subsystem"]
    assert r["n"] == 1 and r["caps"] == []
    assert r["browser"] == ["✓ Added Test Sub · subsystem 1/4"]


def test_fill_and_single_add_share_one_caps_fetch(script):
    assert "_withRackCaps(typeId, function()" in _fn(script, "_fillWithCaps")
    assert "_withRackCaps(typeId, function()" in _fn(script, "_addOneWithCaps")
    assert "module-caps" not in _fn(script, "_fillWithCaps")
