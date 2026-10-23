"""Fighters in the fitting page: adding, launching, limits and the section (ISS-136).

The page's own functions run in node against stubs (the same approach as
test_drone_bay_limits.py). The numbers are a carrier-like hull: 4 launch tubes,
3 light and 2 support squadron slots, no heavy slots.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page


@pytest.fixture(scope="module")
def script():
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    blocks = re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
    body = [b for b in blocks if "function renderSlots" in b]
    assert len(body) == 1
    return body[0]


def _fn(script, name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    assert m, f"no function {name}() in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    return script[m.start():i]


def test_the_page_has_a_fighter_section_and_its_handlers_are_bound():
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    for needle in ('id="section-fighter"', 'id="slots-fighter"', 'id="fighter-limit-note"'):
        assert needle in page
    # The browser no longer turns fighters away.
    assert "fighters are not supported" not in page


HARNESS = r"""
const names = %(names)s;
const src = %(src)s;
const els = {};
let lastRows = [];
const mk = (id) => els[id] || (els[id] = {id, textContent: '', innerHTML: '', hidden: true, style: {},
  // Rows as the browser would find them in the rendered markup: a fresh set per call.
  querySelectorAll() {
    lastRows = (this.innerHTML.match(/class="b-table-row" data-type-id="\d+"/g) || []).map((m) => {
      const warn = {style: {display: 'none'}, title: ''};
      return {dataset: {typeId: m.match(/\d+/)[0]}, querySelector: () => warn, warn,
              classList: {add() {}, remove() {}}};
    });
    return lastRows;
  }});
let statsAttrs = null;   // {info, hangar} for the stats panel, or null for none
global.document = {
  getElementById: mk,
  querySelector: () => statsAttrs && {getAttribute: (a) => a === 'data-fighter-info' ? statsAttrs.info : statsAttrs.hangar},
  querySelectorAll: (sel) => sel.indexOf('.b-table-row[data-type-id]') >= 0 ? mk('slots-fighter').querySelectorAll() : [],
};
global._formatMissing = (e) => 'Missing: ' + e.length;
global.getSelectedSkillCharId = () => 5;
let skillAnswer = {missing: {}};
global.fetch = () => Promise.resolve({json: () => Promise.resolve(skillAnswer)});
const tick = () => new Promise((r) => setImmediate(r));
global.state = {shipTypeId: 9001, items: []};
global.shipSlots = {};
let renders = 0, stats = 0;
global.saveState = () => {}; global.renderSlots = () => { renders++; }; global.recalcStats = () => { stats++; };
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
let browserNote = null;
global._noteBrowserAdd = (t) => { browserNote = t; };
global.isSlotFull = () => false;
global._origAddModule = () => { state.items.push({slot: 'other'}); };
// Test-only: evaluates the page's own function source, extracted from this repo's template.
eval(src + '\n' + names.map(n => 'global.' + n + ' = ' + n + ';').join('\n') +
     '\nglobal.getInfo = () => fighterInfo; global.setTotal = (v) => { fighterHangarTotal = v; };' +
     '\nglobal.getTotal = () => fighterHangarTotal;');
const out = {};
const note = () => (els['fighter-limit-note'] || {}).textContent || '';
const CARRIER = {fighter_tubes: 4, fighter_light: 3, fighter_support: 2, fighter_heavy: 0};
const LIGHT = {'class': 'light', max_size: 6, volume: 1000};
const SUPPORT = {'class': 'support', max_size: 3, volume: 3000};
const HEAVY = {'class': 'heavy', max_size: 6, volume: 1800};
const STANDUP = {'class': 'standup', max_size: 9, volume: 1600};
const reset = (slots, total) => {
  state.items = []; state.shipTypeId = 9001; global.shipSlots = shipSlots = Object.assign({drone_bay: 0}, slots);
  Object.keys(getInfo()).forEach(k => delete getInfo()[k]);
  setTotal(total === undefined ? null : total); statsAttrs = null;
  Object.keys(els).forEach(k => { els[k].textContent = ''; els[k].innerHTML = ''; els[k].hidden = true; els[k].style = {}; });
  browserNote = null; renders = 0; stats = 0;
};
const fighters = () => state.items.map(i => ({t: i.type_id, q: i.quantity, on: i.online}));
(async () => {
%(scenarios)s
console.log(JSON.stringify(out));
})();
"""

SCENARIOS = r"""
// A: a hull with no tubes refuses outright, once its data is known.
reset({fighter_tubes: 0, fighter_light: 0, fighter_support: 0, fighter_heavy: 0});
out.A = {r: addFighter(7001, 'Test Light', LIGHT), n: state.items.length, note: note()};

// B: structure fighters are refused on a ship that has tubes.
reset(CARRIER);
out.B = {r: addFighter(7004, 'Test Standup', STANDUP), n: state.items.length, note: note()};

// C: a class the hull has no slots for (heavy on a carrier) is refused.
reset(CARRIER);
out.C = {r: addFighter(7003, 'Test Heavy', HEAVY), n: state.items.length, note: note()};

// D: a new squadron lands at full size and launched while a tube and a class slot are free;
// the 4th light squadron (L3) goes to the hangar with a note.
reset(CARRIER);
for (let i = 0; i < 4; i++) addFighter(7001, 'Test Light', LIGHT);
out.D = {items: fighters(), note: note()};

// E: tubes run out before the class does: light x3 + support x1 fill 4 tubes,
// a second support squadron (S2) is a reserve because no tube is free.
reset(CARRIER);
for (let i = 0; i < 3; i++) addFighter(7001, 'Test Light', LIGHT);
addFighter(7002, 'Test Support', SUPPORT);
const fifthTube = addFighter(7002, 'Test Support', SUPPORT);
out.E = {fifthTube, items: fighters(), note: note()};

// F: the + button stops at the squadron maximum, - keeps one fighter.
reset(CARRIER);
addFighter(7001, 'Test Light', LIGHT);
changeFighterQty(0, -1); changeFighterQty(0, -1);
const afterTwoMinus = state.items[0].quantity;
changeFighterQty(0, 1);
const afterPlus = state.items[0].quantity;
changeFighterQty(0, 1); changeFighterQty(0, 1);
const atMax = state.items[0].quantity;
changeFighterQty(0, 1);
out.F = {afterTwoMinus, afterPlus, atMax, final: state.items[0].quantity, note: note()};
reset(CARRIER);
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 1, online: true});
getInfo()[7001] = LIGHT;
for (let i = 0; i < 3; i++) changeFighterQty(0, -1);
out.F2 = state.items[0].quantity;

// G: the hangar is a volume limit over every squadron, reserves included, once the total is known.
reset(CARRIER, 10000);
const g1 = addFighter(7001, 'Test Light', LIGHT);   // 6 x 1000
const g2 = addFighter(7001, 'Test Light', LIGHT);   // would be 12000
const gNote = note();
changeFighterQty(0, -1); changeFighterQty(0, -1);   // 4 x 1000 used
const g3 = addFighter(7001, 'Test Light', LIGHT);   // 4000 + 6000 = 10000: fits exactly
out.G = {g1, g2, gNote, g3, n: state.items.length};
// The + button obeys it too.
reset(CARRIER, 5500);
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 5, online: true});
getInfo()[7001] = LIGHT;                             // 5000 used, one more would make 6000
changeFighterQty(0, 1);
out.G2 = {q: state.items[0].quantity, note: note()};
// Not known yet (null): nothing is refused on volume.
reset(CARRIER, null);
for (let i = 0; i < 20; i++) addFighter(7001, 'Test Light', LIGHT);
out.G3 = state.items.length;

// H: launching needs a free tube; returning to the hangar always works.
reset(CARRIER);
for (let i = 0; i < 3; i++) addFighter(7001, 'Test Light', LIGHT);
addFighter(7002, 'Test Support', SUPPORT);           // 4 launched, all tubes in use
addFighter(7002, 'Test Support', SUPPORT);           // reserve
toggleFighterLaunch(4);
const blocked = {on: state.items[4].online, note: note()};
toggleFighterLaunch(3);                              // return a support squadron to the hangar
const offState = state.items[3].online;
toggleFighterLaunch(4);                              // its tube is free again
out.H = {blocked, offState, afterFree: state.items[4].online};
// ...and a free tube does not help a class that is full: L3 launched, a 4th light stays down.
reset(CARRIER);
for (let i = 0; i < 4; i++) addFighter(7001, 'Test Light', LIGHT);
toggleFighterLaunch(3);
out.H2 = {on: state.items[3].online, note: note()};

// I: the same type twice gives two separate squadrons.
reset(CARRIER);
addFighter(7001, 'Test Light', LIGHT); addFighter(7001, 'Test Light', LIGHT);
out.I = {items: fighters(), distinct: state.items[0] !== state.items[1]};

// J: slot data not loaded yet (null): nothing is refused, everything is launched.
reset({fighter_tubes: null, fighter_light: null, fighter_support: null, fighter_heavy: null});
for (let i = 0; i < 6; i++) addFighter(7001, 'Test Light', LIGHT);
out.J = {n: state.items.length, allOn: state.items.every(i => i.online === true)};

// K: from the browser, class and size ride on the clicked row's dataset.
reset(CARRIER);
addModuleFromBrowser.call({dataset: {typeId: '7002', typeName: 'Test Support', slotType: 'fighter',
                                     volume: '3000', fighterClass: 'support', squadronMax: '3'}});
out.K = {items: fighters(), info: getInfo()[7002], note: browserNote};
// ...and a refused add says nothing was added to the browser overlay.
addModuleFromBrowser.call({dataset: {typeId: '7003', typeName: 'Test Heavy', slotType: 'fighter',
                                     volume: '1800', fighterClass: 'heavy', squadronMax: '6'}});
out.K2 = {n: state.items.length, browserNote, note: note()};

// L: the stats response teaches the page fighters it did not browse (imports, restores).
reset(CARRIER);
statsAttrs = {info: JSON.stringify({7001: {'class': 'light', max_size: 6, volume: 1000}}), hangar: '81250.0'};
_syncFighterInfo();
out.L = {info: getInfo()[7001], total: getTotal()};
statsAttrs = {info: '0', hangar: '0'};              // a hull with no hangar is "unknown", not "full"
_syncFighterInfo();
out.L2 = {total: getTotal(), kept: !!getInfo()[7001]};
statsAttrs = {info: 'not json', hangar: 'x'};
_syncFighterInfo();
out.L3 = {total: getTotal(), kept: !!getInfo()[7001]};

// M: the section shows on a hull with tubes, or whenever a fighter is fitted, and warns on a tubeless hull.
reset(CARRIER);
renderFighters();
out.M1 = els['section-fighter'].style.display;
reset({fighter_tubes: 0, fighter_light: 0, fighter_support: 0, fighter_heavy: 0});
renderFighters();
out.M2 = els['section-fighter'].style.display;
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6});
getInfo()[7001] = LIGHT;
renderFighters();
out.M3 = {display: els['section-fighter'].style.display, warnHidden: els['fighter-warn'].hidden,
          warn: els['fighter-warn'].textContent, rows: (els['slots-fighter'].innerHTML.match(/b-table-row/g) || []).length};
reset({fighter_tubes: null});
renderFighters();
out.M4 = els['section-fighter'].style.display;

// N: rows carry what the skill check and the click dispatch need; launch state is shown per squadron.
reset(CARRIER, 81250);
for (let i = 0; i < 4; i++) addFighter(7001, 'Test Light', LIGHT);
renderFighters();
const html = els['slots-fighter'].innerHTML;
out.N = {
  rows: (html.match(/class="b-table-row" data-type-id="7001"/g) || []).length,
  skillWarn: (html.match(/skill-warn/g) || []).length,
  clicks: ['toggleFighterLaunch', 'changeFighterQty', 'removeItem', 'showModuleInfo'].filter(c => html.indexOf('data-click="' + c + '"') >= 0),
  launched: (html.match(/ · launched/g) || []).length,
  hangarRows: (html.match(/ · hangar/g) || []).length,
  summary: els['fighter-summary'].textContent,
};
// A squadron the page would launch beyond the limits is shown as not counted.
state.items[3].online = true;
renderFighters();
out.N2 = (els['slots-fighter'].innerHTML.match(/ · not counted/g) || []).length;

// O: the drone section goes on a fighter hull with no bay, but never hides fitted drones.
reset({fighter_tubes: 4, drone_bay: 0});
out.O1 = _droneSectionHidden();
state.items.push({type_id: 1001, slot: 'drone', quantity: 1});
out.O2 = _droneSectionHidden();
reset({fighter_tubes: 0, drone_bay: 0});
out.O3 = _droneSectionHidden();
reset({fighter_tubes: 4, drone_bay: 40});
out.O4 = _droneSectionHidden();

// P: browse rows carry the class and size, and teach the page its volume.
reset(CARRIER);
renderBrowseItems([{type_id: 7001, type_name: 'Test Light', slot_type: 'fighter', fighter_class: 'light',
                    squadron_max: 6, volume: 1000, can_fit: true}]);
const row = els['browse-items'].innerHTML;
out.P = {class: row.indexOf('data-fighter-class="light"') >= 0, max: row.indexOf('data-squadron-max="6"') >= 0,
         info: getInfo()[7001], badge: row.indexOf('light ×6') >= 0};
// R: four lights launched while the limits were unknown; then the carrier limits land.
reset({fighter_tubes: null, fighter_light: null, fighter_support: null, fighter_heavy: null});
for (let i = 0; i < 4; i++) addFighter(7001, 'Test Light', LIGHT);
Object.assign(shipSlots, CARRIER);
addFighter(7002, 'Test Support', SUPPORT);
renderFighters();
out.R = {items: fighters(), summary: _fighterSummary(), status: state.items.map((_, i) => _fighterWalk(-1).status[i])};

// S: an online structure fighter and an online unknown-class one take no tube.
reset(CARRIER);
getInfo()[7004] = STANDUP; getInfo()[7009] = {'class': null, max_size: 6, volume: 100};
state.items.push({type_id: 7004, type_name: 'Test Standup', slot: 'fighter', quantity: 9, online: true},
                 {type_id: 7009, type_name: 'Test Odd', slot: 'fighter', quantity: 6, online: true},
                 {type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: true},
                 {type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: false});
getInfo()[7001] = LIGHT;
out.S = {summary: _fighterSummary()};
toggleFighterLaunch(0);
state.items[0].online = false;
out.S.toggle = (() => { toggleFighterLaunch(0); return note(); })();

// T: a class the server reports as null cannot be launched.
reset(CARRIER);
addFighter(7009, 'Test Odd', {'class': null, max_size: 6, volume: 100});
out.T = {items: fighters(), note: note()};

// U: online read as the engine reads it.
reset(CARRIER);
getInfo()[7001] = LIGHT;
[undefined, 0, null, false].forEach(v => {
  const it = {type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6};
  if (v !== undefined) it.online = v;
  state.items.push(it);
});
out.U = state.items.map((_, i) => _fighterWalk(-1).status[i] === '');

// V: quantity above the maximum counts as the maximum.
reset(CARRIER, 81250);
getInfo()[7001] = LIGHT;
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 9, online: true});
const vHangar = _fighterHangarUsed();
renderFighters();
const vHtml = els['slots-fighter'].innerHTML;
changeFighterQty(0, 1);
const noPlus = state.items[0].quantity;
changeFighterQty(0, -1);
out.V = {hangar: vHangar, red: vHtml.indexOf('color:var(--danger);min-width:16px') >= 0,
         title: vHtml.indexOf('Above the squadron maximum') >= 0, afterMinus: state.items[0].quantity, noPlus: 6};

// W: launched but not counted is amber, not the legend's overheat red.
reset(CARRIER);
getInfo()[7001] = LIGHT;
for (let i = 0; i < 4; i++) state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: true});
renderFighters();
const wHtml = els['slots-fighter'].innerHTML;
out.W = {warning: wHtml.indexOf('color:var(--warning') >= 0, danger: /color:var\(--danger\);cursor/.test(wHtml)};

// Q: the skill check answered before the stats response redrew the rows.
reset(CARRIER, 81250);
getInfo()[7001] = LIGHT;
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: true});
renderFighters();
skillAnswer = {missing: {7001: [{skill: 'x'}]}};
refreshSkillWarnings();
await tick(); await tick();
const qBefore = lastRows[0].warn.style.display;
statsAttrs = {info: JSON.stringify({7001: LIGHT}), hangar: '81250'};
lastRows = [];   // only rows looked up from here on count
_syncFighterInfo();
out.Q = {before: qBefore, afterSync: lastRows.length ? lastRows[0].warn.style.display : 'no rows checked'};
// X: a class-less fighter on a tubeless hull gets the engine's reason; an empty squadron is skipped.
reset({fighter_tubes: 0, fighter_light: 0, fighter_support: 0, fighter_heavy: 0});
getInfo()[7009] = {'class': null, max_size: 6, volume: 100};
state.items.push({type_id: 7009, type_name: 'Test Odd', slot: 'fighter', quantity: 6, online: true});
const xWhy = _fighterWalk(-1).status[0];
reset(CARRIER);
getInfo()[7001] = LIGHT;
state.items.push({type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 0, online: true},
                 {type_id: 7001, type_name: 'Test Light', slot: 'fighter', quantity: 6, online: true});
out.X = {why: xWhy, empty: _fighterWalk(-1).status[0], used: _fighterWalk(-1).used.tubes};

// Y: an import's warnings are shown as text.
reset(CARRIER);
_importWarnings({warnings: ['Kept 2 in the hangar', '<b>x</b>']});
out.Y = [note(), (els['fighter-limit-note'] || {}).hidden];
_limitNote('');
_importWarnings({});
out.Y2 = note();
"""

NAMES = ["_fmtM3", "_limitNote", "_fighterClass", "_fighterHardProblem", "_fighterFit",
         "_fighterWanted", "_fighterWalk", "fighterLaunchProblem", "_fighterHangarUsed",
         "fighterRoomProblem", "addFighter", "changeFighterQty", "toggleFighterLaunch",
         "_syncFighterInfo", "_importWarnings", "_applySkillWarnings", "_clearSkillWarnings",
         "refreshSkillWarnings", "_droneSectionHidden", "_fighterSummary", "renderFighters",
         "renderBrowseItems", "_browserAddNoteText", "addModuleFromBrowser"]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var _limitNoteTimer = null;", "var fighterInfo = {};", "var fighterHangarTotal = null;", "var _lastMissing = {};",
             "var FIGHTER_CLASSES = ['light', 'support', 'heavy'];", "var NOT_LAUNCHABLE = 'not a launchable fighter';"]
    parts += [_fn(script, n) for n in NAMES]
    harness = HARNESS % {"names": json.dumps(NAMES), "src": json.dumps("\n".join(parts)),
                         "scenarios": SCENARIOS}
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(harness)
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_a_hull_with_no_tubes_refuses_fighters(result):
    r = result["A"]
    assert r["r"] is False and r["n"] == 0
    assert "no fighter tubes" in r["note"]


def test_structure_fighters_are_refused_on_a_ship(result):
    r = result["B"]
    assert r["r"] is False and r["n"] == 0
    assert "Upwell structures" in r["note"]


def test_a_class_the_hull_has_no_slots_for_is_refused(result):
    r = result["C"]
    assert r["r"] is False and r["n"] == 0
    assert "cannot launch heavy fighters" in r["note"]


def test_a_squadron_lands_at_full_size_and_the_fourth_light_one_goes_to_the_hangar(result):
    r = result["D"]
    assert r["items"] == [{"t": 7001, "q": 6, "on": True}] * 3 + [{"t": 7001, "q": 6, "on": False}]
    assert "Added to the hangar" in r["note"] and "no free light squadron slot" in r["note"]


def test_a_squadron_beyond_the_tubes_goes_to_the_hangar(result):
    r = result["E"]
    assert r["fifthTube"] is True      # added, not refused
    assert [i["on"] for i in r["items"]] == [True, True, True, True, False]
    assert "no free launch tube" in r["note"]


def test_plus_stops_at_the_squadron_maximum_and_minus_keeps_one_fighter(result):
    r = result["F"]
    assert (r["afterTwoMinus"], r["afterPlus"], r["atMax"], r["final"]) == (4, 5, 6, 6)
    assert "squadron is full (6" in r["note"]
    assert result["F2"] == 1


def test_the_hangar_counts_every_squadron_and_refuses_what_does_not_fit(result):
    g = result["G"]
    assert g["g1"] is True and g["g2"] is False and g["g3"] is True
    assert "fighter hangar full (6000 / 10000" in g["gNote"]
    assert g["n"] == 2          # the refused one never landed
    assert result["G2"]["q"] == 5 and "fighter hangar full" in result["G2"]["note"]


def test_nothing_is_refused_on_volume_before_the_total_is_known(result):
    assert result["G3"] == 20


def test_launching_needs_a_free_tube_and_a_free_class_slot(result):
    h = result["H"]
    assert h["blocked"]["on"] is False and "no free launch tube" in h["blocked"]["note"]
    assert h["offState"] is False and h["afterFree"] is True
    assert result["H2"]["on"] is False and "no free light squadron slot" in result["H2"]["note"]


def test_the_same_type_added_twice_is_two_squadrons(result):
    r = result["I"]
    assert r["items"] == [{"t": 7001, "q": 6, "on": True}] * 2 and r["distinct"] is True


def test_unknown_ship_data_refuses_nothing(result):
    assert result["J"] == {"n": 6, "allOn": True}


def test_a_browser_add_uses_the_rows_class_and_size_and_a_refusal_says_so(result):
    k = result["K"]
    assert k["items"] == [{"t": 7002, "q": 3, "on": True}]
    assert k["info"] == {"class": "support", "max_size": 3, "volume": 3000.0}
    assert k["note"] == "\u2713 Added Test Support \u00b7 squadron \u00d73 (launched)"
    k2 = result["K2"]
    assert k2["n"] == 1 and "cannot launch heavy fighters" in k2["note"]


def test_stats_response_supplies_info_and_the_hangar_total(result):
    assert result["L"]["info"] == {"class": "light", "max_size": 6, "volume": 1000}
    assert result["L"]["total"] == 81250.0
    assert result["L2"] == {"total": None, "kept": True}
    assert result["L3"] == {"total": None, "kept": True}


def test_the_section_shows_for_tubes_or_any_fighter_and_warns_on_a_tubeless_hull(result):
    assert result["M1"] == ""            # tubes, no fighters
    assert result["M2"] == "none"        # tubeless and empty
    m3 = result["M3"]
    assert m3["display"] == "" and m3["warnHidden"] is False and m3["rows"] == 1
    assert "no fighter tubes" in m3["warn"]
    assert result["M4"] == "none"        # nothing known yet


def test_rows_carry_the_type_id_the_skill_check_and_clicks_need(result):
    n = result["N"]
    assert n["rows"] == 4 and n["skillWarn"] == 4
    assert n["clicks"] == ["toggleFighterLaunch", "changeFighterQty", "removeItem", "showModuleInfo"]
    assert n["launched"] == 3 and n["hangarRows"] == 1
    assert n["summary"].startswith("Tubes 3/4 · Light 3/3 · Support 0/2")
    assert "Hangar 24000/81250 m\u00b3" in n["summary"]
    assert result["N2"] == 1


def test_the_drone_section_hides_only_on_a_fighter_hull_with_no_drones(result):
    assert (result["O1"], result["O2"], result["O3"], result["O4"]) == (True, False, False, False)


def test_browse_rows_carry_class_and_size(result):
    p = result["P"]
    assert p["class"] and p["max"] and p["badge"]
    assert p["info"] == {"class": "light", "max_size": 6, "volume": 1000}


def test_counts_follow_the_engines_walk_not_raw_online_flags(result):
    # Four lights launched while the ship data was unknown; when the limits land,
    # the engine counts three, so a support squadron still gets the fourth tube.
    r = result["R"]
    assert [i["on"] for i in r["items"]] == [True] * 5
    assert r["summary"].startswith("Tubes 4/4 \u00b7 Light 3/3 \u00b7 Support 1/2")
    assert r["status"] == ["", "", "", "no free light squadron slot", ""]


def test_structure_and_unknown_class_squadrons_never_take_a_tube(result):
    s = result["S"]
    assert s["summary"].startswith("Tubes 1/4")
    assert s["toggle"] and "Upwell" in s["toggle"]


def test_a_fighter_without_a_launchable_class_goes_to_the_hangar(result):
    t = result["T"]
    assert t["items"] == [{"t": 7009, "q": 6, "on": False}]
    assert "not a launchable fighter" in t["note"]


def test_online_is_read_the_way_the_engine_reads_it(result):
    # missing flag = launched; 0, null and false = reserve
    assert result["U"] == [True, False, False, False]


def test_a_squadron_over_its_maximum_counts_as_the_maximum_and_shows_red(result):
    v = result["V"]
    assert v["hangar"] == 6000 and v["red"] and v["title"]
    assert v["afterMinus"] == 6
    assert v["noPlus"] == 6


def test_the_not_counted_dot_is_not_the_overheat_red(result):
    assert result["W"] == {"warning": True, "danger": False}


def test_skill_warnings_survive_a_stats_response_redrawing_the_fighter_rows(result):
    assert result["Q"] == {"before": "inline", "afterSync": "inline"}


def test_page_reasons_match_the_engines_wording_and_order(result):
    assert result["X"] == {"why": "not a launchable fighter", "empty": None, "used": 1}


def test_import_warnings_are_shown_as_text(result):
    assert result["Y"] == ["Kept 2 in the hangar \u00b7 <b>x</b>", False]
    assert result["Y2"] == ""
