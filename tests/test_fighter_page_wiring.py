"""The fighter code is wired into the page's own flows (ISS-136).

test_fighter_page_logic.py runs the fighter functions on their own. These run
the real recalcStats, fetchShipSlots, renderSlots, selectShip and applyState
against stubs (as test_fitting_stats_race.py does), so each fails if its call
into the fighter code is removed.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page
from tests.test_fighter_page_logic import NAMES as LOGIC_NAMES, _fn

NAMES = LOGIC_NAMES + ["selectShip", "applyState", "fetchShipSlots", "recalcStats", "renderSlots",
                       "_resetFighterShipData", "getSlotCount", "getSlotLimit"]

HARNESS = r"""
const names = %(names)s;
const src = %(src)s;
// Any element the page asks for: styles and children accept writes, lookups return more of the same.
const cache = {};
const gen = () => new Proxy({style: {}, classList: {add() {}, remove() {}}, dataset: {}}, {
  get(t, k) {
    if (k in t) return t[k];
    if (k === 'querySelector' || k === 'parentElement') return k === 'parentElement' ? gen() : () => gen();
    if (k === 'querySelectorAll') return () => [];
    return undefined;
  },
  set(t, k, v) { t[k] = v; return true; },
});
let statsAttrs = null;
global.document = {
  getElementById: (id) => cache[id] || (cache[id] = gen()),
  querySelector: () => statsAttrs && {getAttribute: (a) => a === 'data-fighter-info' ? statsAttrs.info : statsAttrs.hangar},
  querySelectorAll: () => [],
};
global.state = {shipTypeId: 593, items: []};
global.shipSlots = {high: 8, med: 8, low: 8, rig: 3, subsystem: 0};
global.saveState = () => {}; global.recalcStats = () => {};
global.escapeHtml = (s) => String(s);
global.getSelectedSkillCharId = () => null; global.getSelectedProfile = () => 'uniform';
global.getCustomWeights = () => null; global.getImplantTypeIds = () => []; global.getBoosterPayload = () => [];
global._fitCheckOk = (r) => r; global._fitErrorHtml = (s) => 'ERROR ' + s;
global._syncDroneVolumes = () => {}; global._fitSumBarUpdate = () => {}; global.refreshSkillWarnings = () => {};
global.renderImplantSlots = () => {}; global.renderBoosterSlots = () => {};
global.loadBrowseItems = () => {}; global.setTimeout = () => 0; global.clearTimeout = () => {};
const pending = [];
global.fetch = (url) => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
const ok = (v) => ({text: () => Promise.resolve(v), json: () => Promise.resolve(v)});
const tick = () => new Promise((r) => setImmediate(r));
// Test-only: evaluates the page's own function source, extracted from this repo's template.
eval(src + '\n' + names.map(n => 'global.' + n + ' = ' + n + ';').join('\n') +
     '\nglobal.getInfo = () => fighterInfo; global.setTotal = (v) => { fighterHangarTotal = v; };' +
     '\nglobal.getTotal = () => fighterHangarTotal;');
const CARRIER = {fighter_tubes: 4, fighter_light: 3, fighter_support: 2, fighter_heavy: 0};
const LIGHT = {'class': 'light', max_size: 6, volume: 1000};
const el = (id) => document.getElementById(id);
const out = {};
(async () => {
  // A: a stats response teaches the page its fighters and hangar, and redraws the fighter rows.
  state.items = [{type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: true}];
  statsAttrs = {info: JSON.stringify({7001: LIGHT}), hangar: '81250'};
  eval('recalcStats')();
  pending.shift().resolve(ok('<div></div>')); await tick();
  out.A = {info: getInfo()[7001], total: getTotal(), rows: String(el('slots-fighter').innerHTML).indexOf('light') >= 0};

  // B: a failed stats request forgets the hangar total.
  eval('recalcStats')();
  pending.shift().reject({status: 500}); await tick();
  out.B = getTotal();

  // C: ship slots copy the fighter limits, and an answer for another ship is ignored.
  Object.assign(shipSlots, {fighter_tubes: null, fighter_light: null, fighter_support: null, fighter_heavy: null});
  state.shipTypeId = 593;
  fetchShipSlots(593); pending.shift().resolve(ok(Object.assign({high: 8}, CARRIER))); await tick();
  out.C = [shipSlots.fighter_tubes, shipSlots.fighter_light, shipSlots.fighter_support, shipSlots.fighter_heavy];
  Object.assign(shipSlots, {fighter_tubes: null});
  fetchShipSlots(111); pending.shift().resolve(ok(Object.assign({high: 8}, CARRIER))); await tick();
  out.C2 = shipSlots.fighter_tubes;

  // D: renderSlots draws the fighter section and hides the drone section on a fighter hull.
  Object.assign(shipSlots, CARRIER, {drone_bay: 0});
  state.items = [];
  delete cache['section-fighter']; delete cache['section-drone'];
  renderSlots();
  out.D = {fighter: el('section-fighter').style.display, drone: el('section-drone').style.display};
  Object.assign(shipSlots, {fighter_tubes: 0});
  renderSlots();
  out.D2 = {fighter: el('section-fighter').style.display, drone: el('section-drone').style.display};

  // E: picking a ship, or restoring one, forgets the last ship's limits and hangar.
  const realApply = eval('applyState');
  const fresh = () => { Object.assign(shipSlots, CARRIER); setTotal(81250); };
  fresh();
  eval('applyState = () => {}');
  selectShip(777, 'Test Ship');
  out.E1 = [shipSlots.fighter_tubes, getTotal()];
  eval('applyState = ' + realApply.toString());
  fresh(); state.shipTypeId = 777; state.shipName = 'Test Ship';
  applyState();
  out.E2 = [shipSlots.fighter_tubes, getTotal()];
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def script():
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    body = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S) if "function renderSlots" in b]
    assert len(body) == 1
    return body[0]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var _limitNoteTimer = null;", "var fighterInfo = {};", "var fighterHangarTotal = null;",
             "var _lastMissing = {};", "var _statsSeq = 0;", "var currentBrowseGroup = null;",
             "var overheatCache = {};", "var FIGHTER_CLASSES = ['light', 'support', 'heavy'];", "var NOT_LAUNCHABLE = 'not a launchable fighter';"]
    parts += [_fn(script, n) for n in NAMES]
    harness = HARNESS % {"names": json.dumps(NAMES), "src": json.dumps("\n".join(parts))}
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(harness)
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_stats_response_feeds_fighter_info_and_redraws_the_rows(result):
    a = result["A"]
    assert a["info"]["max_size"] == 6 and a["total"] == 81250.0 and a["rows"] is True


def test_a_failed_stats_request_forgets_the_hangar_total(result):
    assert result["B"] is None


def test_ship_slots_copy_the_fighter_limits_and_ignore_another_ships_answer(result):
    assert result["C"] == [4, 3, 2, 0]
    assert result["C2"] is None


def test_render_slots_draws_the_fighter_section_and_hides_the_drone_section(result):
    assert result["D"] == {"fighter": "", "drone": "none"}
    assert result["D2"] == {"fighter": "none", "drone": ""}   # tubeless and empty: the reverse


def test_a_new_ship_forgets_the_previous_ships_fighter_data(result):
    assert result["E1"] == [None, None]
    assert result["E2"] == [None, None]
