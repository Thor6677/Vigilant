"""T-086 WP3: the Cargo section, its capacity, and the EFT round trip.

The cargo helpers run in node against stubs; the markup is checked in the
rendered page. Type ids and names are invented.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.db.sde_models import SDEType
from app.fitting.engine import calculate_fitting_stats
from app.routes import fitting as fitting_mod
from app.routes.fitting import _autoload_cargo_charges
from tests._mobile import render_page
from tests.test_fitting_engine import SHIP, _Slice, _seed_rows

AMMO, ALT_AMMO = 8201, 8202

HARNESS = r"""
const src = %(src)s;
const notes = [], calls = {render: 0, recalc: 0, saved: 0, focus: [], selected: 0};
global.window = global;
let attrs = null;
const qtyEl = {focus() { calls.focus.push(1); }, select() { calls.selected++; }};
const sum = {textContent: '', style: {}};
const host = {innerHTML: ''};
global.document = {
  getElementById: (id) => id === 'slots-cargo' ? host : id === 'cargo-summary' ? sum : null,
  querySelector: (sel) => sel.indexOf('data-cargo-vols') >= 0 ? (attrs && {getAttribute: (a) => attrs[a]})
                        : sel.indexOf('.fit-cqty') >= 0 ? qtyEl : null,
  querySelectorAll: () => [],
};
global.saveState = () => calls.saved++;
global.renderSlots = () => calls.render++;
global.recalcStats = () => calls.recalc++;
global.escapeHtml = (s) => String(s);
global._limitNote = (t, near) => notes.push(t + (near ? '|near' : ''));
global.state = {items: []};
eval(src + `
global.api = {addCargo, setCargoQty, maxCargo, emptyCargo, renderCargo, _syncCargo, _cargoUsed, _cargoMaxQty,
              setCap: (c) => { cargoCapacity = c; }, getCap: () => cargoCapacity,
              setVol: (t, v) => { cargoVolumes[t] = v; }, getVols: () => cargoVolumes};`);

const cargo = (t, q) => ({type_id: t, type_name: 'n' + t, slot: 'cargo', quantity: q});
function reset(items, cap, vols) {
  global.state = {items: items};
  api.setCap(cap);
  Object.keys(api.getVols()).forEach((k) => delete api.getVols()[k]);
  Object.keys(vols || {}).forEach((k) => api.setVol(parseInt(k, 10), vols[k]));
  notes.length = 0; calls.render = calls.recalc = calls.saved = 0; calls.focus.length = 0; calls.selected = 0;
}
const out = {};

// addCargo: a new stack starts at 1, focuses the quantity box
reset([], 100, {});
out.addNew = [api.addCargo(%(AMMO)d, 'Ammo', 0.5), JSON.parse(JSON.stringify(state.items)),
              calls.saved, calls.render, calls.recalc, calls.focus.length, calls.selected];
out.volLearned = api.getVols()[%(AMMO)d];
// merge: the same type adds 1 to the stack, never a second stack
api.addCargo(%(AMMO)d, 'Ammo', 0.5); api.addCargo(%(AMMO)d, 'Ammo', 0.5);
out.merged = state.items.map((i) => [i.type_id, i.quantity]);
api.addCargo(%(ALT)d, 'Alt', 1);
out.twoTypes = state.items.map((i) => [i.type_id, i.quantity]);
// dataset form (the dispatcher calls it with the element as this)
reset([], null, {});
api.addCargo.call({dataset: {typeId: '%(AMMO)d', typeName: 'Ammo', volume: '2'}});
out.fromDataset = [state.items.length, api.getVols()[%(AMMO)d]];
// refused when the +1 does not fit
reset([cargo(%(AMMO)d, 10)], 5, {%(AMMO)d: 0.5});
out.refused = [api.addCargo(%(AMMO)d, 'Ammo', 0.5), state.items[0].quantity, notes.slice(), calls.saved];
reset([cargo(%(AMMO)d, 9)], 5, {%(AMMO)d: 0.5});   // 9 x 0.5 = 4.5 of 5: one more fits, two do not
out.lastFits = [api.addCargo(%(AMMO)d, 'Ammo', 0.5), state.items[0].quantity];
// unknown capacity refuses nothing; unknown or zero volume refuses nothing
reset([], null, {%(AMMO)d: 50});
out.nullCap = [api.addCargo(%(AMMO)d, 'Ammo', 50), api.addCargo(%(AMMO)d, 'Ammo', 50), state.items[0].quantity];
reset([], 1, {});
out.noVol = [api.addCargo(%(ALT)d, 'Alt', 0), api.addCargo(%(ALT)d, 'Alt', 0), state.items[0].quantity];

// quantity change handler (data-change): clamps up to 1, cuts to what fits, leaves the rest alone
reset([cargo(%(AMMO)d, 1), cargo(%(ALT)d, 4)], 20, {%(AMMO)d: 0.5, %(ALT)d: 2});
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '10'});
out.setOk = [state.items[0].quantity, notes.slice(), calls.saved, calls.render, calls.recalc];
notes.length = 0;
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '99999'});   // 20 - 8 (other stack) = 12 -> 24 units
out.setCut = [state.items[0].quantity, notes.slice()];
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '0'});
out.setZero = state.items[0].quantity;
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: 'abc'});
out.setNaN = state.items[0].quantity;
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '7.9'});
out.setFloat = state.items[0].quantity;
reset([cargo(%(AMMO)d, 3)], null, {%(AMMO)d: 0.5});
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '5000'});
out.setNoCap = [state.items[0].quantity, notes.slice()];
// another stack already fills the hold: the stack keeps at least 1 and says so
reset([cargo(%(AMMO)d, 3), cargo(%(ALT)d, 10)], 20, {%(AMMO)d: 0.5, %(ALT)d: 2});
api.setCargoQty.call({dataset: {itemIdx: '0'}, value: '4'});
out.setNoRoom = [state.items[0].quantity, notes.slice()];

// Max
reset([cargo(%(AMMO)d, 1), cargo(%(ALT)d, 4)], 20, {%(AMMO)d: 0.5, %(ALT)d: 2});
api.maxCargo.call({dataset: {itemIdx: '0'}});
out.max = [state.items[0].quantity, api._cargoUsed(), notes.slice(), calls.saved, calls.render, calls.recalc];
reset([cargo(%(AMMO)d, 1)], null, {%(AMMO)d: 0.5});
api.maxCargo.call({dataset: {itemIdx: '0'}});
out.maxUnknown = [state.items[0].quantity, notes.slice(), calls.saved];
reset([cargo(%(AMMO)d, 1), cargo(%(ALT)d, 10)], 20, {%(AMMO)d: 0.5, %(ALT)d: 2});
api.maxCargo.call({dataset: {itemIdx: '0'}});
out.maxNoRoom = [state.items[0].quantity, notes.slice()];

// Empty cargo: stacks only, modules stay
reset([{type_id: 1, type_name: 'Gun', slot: 'high', quantity: 1, charge_type_id: %(AMMO)d},
       cargo(%(AMMO)d, 3), cargo(%(ALT)d, 4), cargo(7, 1), {type_id: 2, type_name: 'Drone', slot: 'drone', quantity: 5}], 20, {});
api.emptyCargo();
out.emptied = [state.items.map((i) => i.slot), state.items[0].charge_type_id, notes.slice(),
               calls.saved, calls.render, calls.recalc];
notes.length = 0; calls.saved = calls.render = calls.recalc = 0;
api.emptyCargo();
out.emptyAgain = [notes.slice(), calls.saved, calls.render, calls.recalc];
reset([cargo(%(AMMO)d, 3)], 20, {});
api.emptyCargo();
out.emptyOne = notes.slice();

// The stats attributes feed capacity and volumes
reset([], null, {});
attrs = {'data-cargo-cap': '830.5', 'data-cargo-vols': '{"%(AMMO)d": 0.25}'};
api._syncCargo();
out.sync = [api.getCap(), api.getVols()[%(AMMO)d]];
attrs = {'data-cargo-cap': '0', 'data-cargo-vols': '{}'};
api._syncCargo();
out.syncZero = api.getCap();
attrs = {'data-cargo-cap': 'nan', 'data-cargo-vols': 'not json'};
api._syncCargo();
out.syncBad = api.getCap();

// renderCargo draws one row per stack and the summary
reset([cargo(%(AMMO)d, 4), {type_id: 1, type_name: 'Gun', slot: 'high', quantity: 1}], 10, {%(AMMO)d: 0.5});
api.renderCargo();
out.rows = [(host.innerHTML.match(/fit-cargo-row/g) || []).length, sum.textContent, sum.style.color];
reset([cargo(%(AMMO)d, 40)], 10, {%(AMMO)d: 0.5});
api.renderCargo();
out.over = [sum.textContent, sum.style.color];
reset([], null, {});
api.renderCargo();
out.emptyRender = [host.innerHTML, sum.textContent];
console.log(JSON.stringify(out));
"""

_FNS = ("_fmtM3", "_cargoUsed", "_cargoMaxQty", "_cargoFull", "_cargoStackIndex", "_syncCargo",
        "addCargo", "setCargoQty", "maxCargo", "emptyCargo", "renderCargo")


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
def script(page):
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    return blocks[0]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var cargoCapacity = null; var cargoVolumes = {};"] + [_fn(script, n) for n in _FNS]
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(HARNESS % dict(src=json.dumps("\n".join(parts)), AMMO=AMMO, ALT=ALT_AMMO))
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# ── addCargo ─────────────────────────────────────────────────────────

def test_add_starts_a_stack_at_one_and_focuses_its_quantity(result):
    ok, items, saved, render, recalc, focused, selected = result["addNew"]
    assert ok is True
    assert items == [{"type_id": AMMO, "type_name": "Ammo", "slot": "cargo", "quantity": 1}]
    assert (saved, render, recalc) == (1, 1, 1)
    assert focused == 1 and selected == 1
    assert result["volLearned"] == 0.5


def test_add_merges_by_type(result):
    assert result["merged"] == [[AMMO, 3]]
    assert result["twoTypes"] == [[AMMO, 3], [ALT_AMMO, 1]]


def test_add_reads_the_element_dataset(result):
    assert result["fromDataset"] == [1, 2.0]


def test_add_that_does_not_fit_is_refused_with_a_note(result):
    ok, qty, notes, saved = result["refused"]
    assert ok is False and qty == 10 and saved == 0
    assert notes == ["Not added · cargo hold full (5 / 5 m³)"]
    assert result["lastFits"] == [True, 10]


def test_unknown_capacity_or_volume_refuses_nothing(result):
    assert result["nullCap"] == [True, True, 2]
    assert result["noVol"] == [True, True, 2]


# ── quantity box and Max ─────────────────────────────────────────────

def test_quantity_change_is_stored_and_redraws_once(result):
    assert result["setOk"] == [10, [], 1, 1, 1]


def test_quantity_over_capacity_is_cut_to_what_fits_with_a_note(result):
    qty, notes = result["setCut"]
    assert qty == 24    # 20 m3 - 8 m3 for the other stack = 12 m3 at 0.5
    assert notes == ["Cut to 24 · cargo hold full (20 / 20 m³)|near"]


def test_quantity_input_is_clamped_to_a_whole_number_of_at_least_one(result):
    assert result["setZero"] == 1 and result["setNaN"] == 1 and result["setFloat"] == 7


def test_quantity_with_no_known_capacity_is_not_cut(result):
    assert result["setNoCap"] == [5000, []]


def test_a_stack_never_drops_below_one_when_the_hold_is_already_full(result):
    qty, notes = result["setNoRoom"]
    assert qty == 1 and notes and notes[0].startswith("Over capacity")


def test_max_fills_the_rest_of_the_hold(result):
    qty, used, notes, saved, render, recalc = result["max"]
    assert qty == 24 and used == 20.0
    assert notes == ["Set to 24 · hold now 20 / 20 m³|near"]
    assert (saved, render, recalc) == (1, 1, 1)


def test_max_without_a_capacity_or_room_changes_nothing(result):
    assert result["maxUnknown"] == [1, ["Capacity not known yet|near"], 0]
    qty, notes = result["maxNoRoom"]
    assert qty == 1 and notes[0].startswith("No room")


# ── Empty cargo ──────────────────────────────────────────────────────

def test_empty_cargo_clears_stacks_only(result):
    slots, charge, notes, saved, render, recalc = result["emptied"]
    assert slots == ["high", "drone"]
    assert charge == AMMO, "a loaded charge is not cargo"
    assert notes == ["Emptied cargo · 3 stacks|near"]
    assert (saved, render, recalc) == (1, 1, 1)
    assert result["emptyOne"] == ["Emptied cargo · 1 stack|near"]


def test_empty_cargo_on_an_empty_hold_says_so_and_does_nothing(result):
    assert result["emptyAgain"] == [["Cargo is already empty|near"], 0, 0, 0]


# ── stats feed and drawing ───────────────────────────────────────────

def test_stats_attributes_feed_capacity_and_volumes(result):
    assert result["sync"] == [830.5, 0.25]
    assert result["syncZero"] is None, "a hull with no hold is unknown, not full"
    assert result["syncBad"] is None


def test_render_draws_a_row_per_stack_and_the_summary(result):
    assert result["rows"] == [1, "2 / 10 m³", ""]
    assert result["over"] == ["20 / 10 m³", "var(--danger)"]
    assert result["emptyRender"] == ["", "0 / — m³"]


# ── markup ───────────────────────────────────────────────────────────

def test_section_markup(page):
    assert 'id="section-cargo"' in page and 'id="cargo-summary"' in page
    assert re.search(r'<div id="cargo-limit-note" class="fit-limit-note" role="status"', page)
    assert 'data-slot="cargo"' in page and 'data-input="searchCargo"' in page
    assert "+ Add ammo to cargo" in page
    m = re.search(r'<button[^>]*data-click="emptyCargo"[^>]*>([^<]*)</button>', page)
    assert m and m.group(1) == "Empty cargo"
    assert 'class="fit-ctl fit-empty-cargo"' in m.group(0) and "m-tap" not in m.group(0)


def test_row_markup(script):
    row = _fn(script, "renderCargo")
    inp = re.search(r"<input type=\"number\"[^>]*>", row.replace("' + ", "").replace(" + '", "")).group(0)
    assert 'inputmode="numeric"' in inp and 'min="1"' in inp
    assert 'data-change="setCargoQty"' in row and "data-input" not in row
    assert 'class="fit-ctl fit-cmax" data-click="maxCargo"' in row
    assert 'class="fit-ctl" data-click="removeItem"' in row
    assert "m-tap" not in row


def test_cargo_rows_are_not_built_inside_render_slots(script):
    body = _fn(script, "renderSlots")
    assert "renderCargo();" in body and "fit-cargo-row" not in body and "setCargoQty" not in body


def test_note_reaches_the_cargo_section(script):
    assert "'cargo-limit-note'" in _fn(script, "_limitNote")


def test_stats_sync_runs_after_every_stats_write(script):
    assert "_syncCargo();" in _fn(script, "recalcStats")


def test_charge_search_rows_add_to_cargo_with_the_volume(page):
    html = render_page(fitting_mod, "partials/fitting_search_results.html",
                       results=[{"type_id": AMMO, "type_name": "Ammo", "group_id": 1, "volume": 0.25}],
                       search_type="charge")
    assert 'data-click="addCargo"' in html and 'data-volume="0.25"' in html


def test_browse_charge_taps_go_through_add_cargo(script):
    body = _fn(script, "addModuleFromBrowser")
    assert "slotType === 'cargo'" in body and "addCargo(" in body


def test_cargo_css_sits_before_the_last_phone_block():
    css = open("static/css/site.css", encoding="utf-8").read()
    assert css.index("T-086 · cargo") < css.index("end T-086 cargo") < css.index("m-hide wins ties")
    assert ".fit-cqty" in css


# ── engine: cargo_used ───────────────────────────────────────────────

def _cargo_rows():
    rows = _seed_rows()
    rows += [SDEType(type_id=AMMO, type_name="Test Cargo Charge", group_id=9700, category_id=8, volume=0.25),
             SDEType(type_id=ALT_AMMO, type_name="Test Cargo Crate", group_id=9701, category_id=25, volume=None)]
    return rows


def _stats_with(items):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items)
    return _Slice().run(_calc, rows=_cargo_rows())


def test_engine_reports_cargo_used_and_volumes():
    stats = _stats_with([{"type_id": AMMO, "slot": "cargo", "quantity": 400},
                         {"type_id": ALT_AMMO, "slot": "cargo", "quantity": 3}])
    assert stats["cargo_used"] == pytest.approx(100.0)
    assert stats["cargo_volumes"] == {str(AMMO): 0.25, str(ALT_AMMO): 0}


def test_engine_with_no_cargo_reports_zero():
    stats = _stats_with([])
    assert stats["cargo_used"] == 0 and stats["cargo_volumes"] == {}


def test_engine_ignores_a_bad_cargo_quantity():
    stats = _stats_with([{"type_id": AMMO, "slot": "cargo", "quantity": -5}])
    assert stats["cargo_used"] == 0


# ── stats partial ────────────────────────────────────────────────────

def test_partial_exposes_capacity_and_flags_an_overfull_hold():
    from tests.test_mobile_r6_fitting import _stats
    over = render_page(fitting_mod, "partials/fitting_stats.html",
                       stats=_stats(cargo_capacity=10.0, cargo_used=12.5, cargo_volumes={str(AMMO): 0.25}),
                       character_name=None, ship_name="Rifter", ship_type_id=587)
    assert 'data-cargo-cap="10.0"' in over and "data-cargo-vols='{\"%d\": 0.25}'" % AMMO in over
    assert "12.5 / 10.0 m" in over and 'title="Over the cargo hold"' in over
    ok = render_page(fitting_mod, "partials/fitting_stats.html",
                     stats=_stats(cargo_capacity=10.0, cargo_used=2.0),
                     character_name=None, ship_name="Rifter", ship_type_id=587)
    assert "2.0 / 10.0 m" in ok and "Over the cargo hold" not in ok


def test_partial_still_renders_a_hand_built_stats_dict():
    from tests.test_mobile_r6_fitting import _stats
    s = _stats()
    s.pop("cargo_used", None)
    s.pop("cargo_volumes", None)
    html = render_page(fitting_mod, "partials/fitting_stats.html", stats=s,
                       character_name=None, ship_name="Rifter", ship_type_id=587)
    assert "data-cargo-vols=" in html and "Over the cargo hold" not in html


# ── EFT round trip ───────────────────────────────────────────────────

def _eft_fixture():
    from tests import test_fitting_char_import as ci
    return ci


def _round_trip(items, seed=None):
    ci = _eft_fixture()
    teardown, _SessionLocal, _engine = ci._seeded_db(seed or ci._seed_base)
    try:
        client = ci._authed_client(ci.USER_A)
        headers = {"X-CSRF-Token": ci._CSRF}
        r = client.post("/tools/fitting/export-eft", headers=headers,
                        json={"ship_type_id": ci.SHIP_FRIGATE, "name": "Round", "items": items})
        assert r.status_code == 200
        text = r.text
        r2 = client.post("/tools/fitting/import-eft", json={"eft": text}, headers=headers)
        assert r2.status_code == 200 and "error" not in r2.json(), r2.text
        return text, r2.json()["items"]
    finally:
        teardown()


def _shape(items):
    return sorted((i["slot"], i["type_id"], i.get("quantity", 1), i.get("charge_type_id"))
                  for i in items)


def test_export_writes_loaded_ammo_inline_and_import_reads_it_back():
    ci = _eft_fixture()
    items = [{"type_id": ci.TURRET, "type_name": "Test Autocannon", "slot": "high", "quantity": 1,
              "charge_type_id": ci.CHARGE, "charge_name": "Test EMP Charge"},
             {"type_id": ci.LOW_MOD, "type_name": "Test Damage Control", "slot": "low", "quantity": 1}]
    text, back = _round_trip(items)
    assert "Test Autocannon, Test EMP Charge" in text.splitlines()
    assert "Test Damage Control" in text.splitlines()
    assert _shape(back) == _shape(items)


def test_round_trip_keeps_both_the_loaded_charge_and_the_cargo_stack():
    ci = _eft_fixture()
    items = [{"type_id": ci.TURRET, "type_name": "Test Autocannon", "slot": "high", "quantity": 1,
              "charge_type_id": ci.CHARGE, "charge_name": "Test EMP Charge"},
             {"type_id": ci.CHARGE, "type_name": "Test EMP Charge", "slot": "cargo", "quantity": 3000},
             {"type_id": ci.DRONE, "type_name": "Test Hobgoblin", "slot": "drone", "quantity": 2}]
    text, back = _round_trip(items)
    assert "Test EMP Charge x3000" in text.splitlines()
    assert _shape(back) == _shape(items), "the hold must not come back empty"


def test_round_trip_of_a_module_with_no_charge_writes_a_bare_line():
    ci = _eft_fixture()
    items = [{"type_id": ci.TURRET, "type_name": "Test Autocannon", "slot": "high", "quantity": 1}]
    text, back = _round_trip(items)
    assert "Test Autocannon" in text.splitlines() and ", " not in text.split("\n", 1)[1]
    assert _shape(back) == _shape(items)


def test_round_trip_with_a_med_slot_charge():
    ci = _eft_fixture()
    items = [{"type_id": ci.MED_MOD, "type_name": "Test Injector", "slot": "med", "quantity": 1,
              "charge_type_id": ci.BOOSTER, "charge_name": "Test Booster"},
             {"type_id": ci.BOOSTER, "type_name": "Test Booster", "slot": "cargo", "quantity": 7}]
    text, back = _round_trip(items, ci._seed_med_slot)
    assert "Test Injector, Test Booster" in text.splitlines()
    assert _shape(back) == _shape(items)


# ── the autoload rule: EFT keeps cargo, the ESI path does not ────────

def _autoload_input():
    gun = {"type_id": 1, "type_name": "Gun", "slot": "high", "quantity": 1}
    stack = {"type_id": AMMO, "type_name": "Ammo", "slot": "cargo", "quantity": 500}
    return [gun, stack], {1: {AMMO}}


def test_autoload_default_drops_the_used_stack_for_the_esi_path():
    items, compat = _autoload_input()
    out = _autoload_cargo_charges(items, compat)
    assert out[0]["charge_type_id"] == AMMO and len(out) == 1


def test_autoload_keep_cargo_loads_the_weapon_and_keeps_the_stack():
    items, compat = _autoload_input()
    out = _autoload_cargo_charges(items, compat, keep_cargo=True)
    assert out[0]["charge_type_id"] == AMMO
    assert [i["quantity"] for i in out if i["slot"] == "cargo"] == [500]
