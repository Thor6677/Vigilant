"""T-086 WP2: load a charge in every matching weapon, and unload all charges.

The helpers run in node against stubs; the markup is checked in the rendered
page. Type ids and names are invented.
"""
import json
import re
import shutil
import subprocess

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page

GUN, GUN_B, OTHER_GUN, BOOSTER = 8001, 8002, 8003, 8004
AMMO, AMMO_ALT, ODD_AMMO = 8101, 8102, 8103

HARNESS = r"""
const src = %(src)s;
const notes = [], calls = {render: 0, recalc: 0, saved: 0, closed: 0, fetch: []};
global.window = global;
global.document = {getElementById: () => null};
global.saveState = () => calls.saved++;
global.renderSlots = () => calls.render++;
global.recalcStats = () => calls.recalc++;
global._limitNote = (t, near) => notes.push(t + (near ? '|near' : ''));
global.fetch = (url) => {
  calls.fetch.push(url);
  const tid = parseInt(url.split('/').pop(), 10);
  return Promise.resolve({json: () => Promise.resolve((global.charges[tid] || []).map((t) => ({type_id: t})))});
};
global.charges = {};
global.state = {items: []};
global.overheatCache = {};
eval(src + `
global.api = {_chargeCandidates, _chargeTargets, _loadChargeFits, selectCharge, clearCharge,
              unloadAllCharges, toggleChargeAll, setCtx: (c) => { _chargeCtx = c; }, getCtx: () => _chargeCtx,
              fits: () => _chargeFits};`);

const it = (t, slot, extra) => Object.assign({type_id: t, type_name: 'n' + t, slot: slot || 'high', quantity: 1}, extra || {});
function reset(items, caps) {
  global.state = {items: items};
  global.overheatCache = caps || {};
  notes.length = 0; calls.render = calls.recalc = calls.saved = 0; calls.fetch.length = 0;
  api.setCtx(null);
}
const out = {};
(async () => {
  const caps = {%(GUN)d: {group_id: 10}, %(GUN_B)d: {group_id: 10}, %(OTHER)d: {group_id: 11}, %(BOOSTER)d: {group_id: 12}};
  // matching: same group, high/med only, others excluded
  const mixed = () => [it(%(GUN)d), it(%(GUN_B)d), it(%(GUN)d, 'med'), it(%(OTHER)d), it(%(BOOSTER)d, 'med'),
                       it(%(GUN)d, 'low'), it(%(GUN)d, 'cargo')];
  reset(mixed(), caps);
  out.cands = api._chargeCandidates(0);
  out.candsNoGroup = (reset([it(%(GUN)d), it(%(GUN)d), it(%(GUN_B)d)], {}), api._chargeCandidates(0));

  // fetch: once per distinct other type, cached
  global.charges = {%(GUN)d: [%(AMMO)d, %(ALT)d], %(GUN_B)d: [%(AMMO)d], %(OTHER)d: [%(AMMO)d]};
  reset(mixed(), caps);
  api.fits()[%(GUN)d] = [%(AMMO)d, %(ALT)d];
  await api._loadChargeFits(0);
  out.fetched = calls.fetch.slice();
  calls.fetch.length = 0;
  await api._loadChargeFits(0);
  out.refetched = calls.fetch.length;
  out.targetsAmmo = api._chargeTargets(0, %(AMMO)d);
  out.targetsAlt = api._chargeTargets(0, %(ALT)d);

  // selectCharge with the toggle on: loads every match, replaces a different charge
  reset(mixed(), caps);
  state.items[2].charge_type_id = %(ODD)d; state.items[2].charge_name = 'Odd';
  api.setCtx({itemIndex: 0, all: true});
  api.selectCharge(0, %(ALT)d, 'Alt Ammo');
  out.allOn = {charged: state.items.map((i) => i.charge_type_id || null), notes: notes.slice(),
               calls: [calls.saved, calls.render, calls.recalc], ctx: api.getCtx()};
  // the full-fit note, 4 of 6
  const six = [it(%(GUN)d), it(%(GUN)d), it(%(GUN)d), it(%(GUN)d), it(%(GUN_B)d), it(%(GUN_B)d)];
  reset(six, caps);
  api.fits()[%(GUN_B)d] = [%(ODD)d];
  api.setCtx({itemIndex: 0, all: true});
  api.selectCharge(0, %(AMMO)d, 'Ammo');
  out.fourOfSix = {notes: notes.slice(), loaded: state.items.filter((i) => i.charge_type_id).length};
  // toggle off: only the one
  reset(mixed(), caps);
  api.setCtx({itemIndex: 0, all: false});
  api.selectCharge(0, %(AMMO)d, 'Ammo');
  out.allOff = {charged: state.items.map((i) => i.charge_type_id || null), notes: notes.slice()};
  // toggle on but for a different opened item: ignored
  reset(mixed(), caps);
  api.setCtx({itemIndex: 1, all: true});
  api.selectCharge(0, %(AMMO)d, 'Ammo');
  out.otherCtx = state.items.filter((i) => i.charge_type_id).length;
  // toggleChargeAll flips the context
  api.setCtx({itemIndex: 0, all: true});
  api.toggleChargeAll();
  out.toggled = api.getCtx().all;

  // Clear with the toggle on clears every module of the same type
  reset(mixed(), caps);
  state.items.forEach((i) => { i.charge_type_id = %(AMMO)d; i.charge_name = 'Ammo'; });
  api.setCtx({itemIndex: 0, all: true});
  api.clearCharge(0);
  out.clearAll = {charged: state.items.map((i) => i.charge_type_id || null), notes: notes.slice()};
  reset(mixed(), caps);
  state.items.forEach((i) => { i.charge_type_id = %(AMMO)d; i.charge_name = 'Ammo'; });
  api.setCtx({itemIndex: 0, all: false});
  api.clearCharge(0);
  out.clearOne = state.items.map((i) => i.charge_type_id || null);

  // unload all: charges only, modules stay
  reset(mixed(), caps);
  state.items[0].charge_type_id = %(AMMO)d; state.items[0].charge_name = 'Ammo';
  state.items[1].charge_type_id = %(AMMO)d; state.items[1].charge_name = 'Ammo';
  state.items[4].charge_type_id = %(ODD)d; state.items[4].charge_name = 'Odd';
  const before = state.items.length;
  api.unloadAllCharges();
  out.unload = {n: state.items.length === before, left: state.items.filter((i) => i.charge_type_id || i.charge_name).length,
                notes: notes.slice(), calls: [calls.saved, calls.render, calls.recalc]};
  reset(mixed(), caps);
  state.items[0].charge_type_id = %(AMMO)d; state.items[0].charge_name = 'Ammo';
  api.unloadAllCharges();
  out.unloadOne = notes.slice();
  reset(mixed(), caps);
  api.unloadAllCharges();
  out.unloadNone = {notes: notes.slice(), calls: [calls.saved, calls.render, calls.recalc]};
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
def script(page):
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    return blocks[0]


@pytest.fixture(scope="module")
def result(script, tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = ["var chargeModal = null;", "function closeChargeDropdown() { _chargeCtx = null; }"]
    parts += [_fn(script, n) for n in (
        "_chargeCandidates", "_chargeTargets", "_loadChargeFits", "toggleChargeAll", "_chargeAllOn",
        "selectCharge", "clearCharge", "unloadAllCharges")]
    parts.insert(1, "var _chargeFits = {}; var _chargeCtx = null;")
    src = "\n".join(parts)
    path = tmp_path_factory.mktemp("node") / "harness.js"
    ids = dict(GUN=GUN, GUN_B=GUN_B, OTHER=OTHER_GUN, BOOSTER=BOOSTER, AMMO=AMMO, ALT=AMMO_ALT, ODD=ODD_AMMO)
    path.write_text(HARNESS % dict(src=json.dumps(src), **ids))
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_candidates_are_same_group_high_or_med_only(result):
    # GUN x2 (high, med), GUN_B (same group) and the opened item; not the
    # other group, the booster, a low-slot copy or a cargo stack.
    assert result["cands"] == [0, 1, 2]
    # no group known: only the same type counts
    assert result["candsNoGroup"] == [0, 1]


def test_other_types_are_fetched_once_and_cached(result):
    assert result["fetched"] == [f"/tools/fitting/charges/{GUN_B}"]
    assert result["refetched"] == 0


def test_targets_need_the_charge_to_fit(result):
    assert result["targetsAmmo"] == {"total": 3, "targets": [0, 1, 2]}
    assert result["targetsAlt"] == {"total": 3, "targets": [0, 2]}   # GUN_B cannot take it


def test_select_with_toggle_on_loads_every_match_and_replaces(result):
    a = result["allOn"]
    assert a["charged"][0] == AMMO_ALT
    assert a["charged"][1] is None, "GUN_B cannot take it"
    assert a["charged"][2] == AMMO_ALT, "a different charge already loaded is replaced"
    assert a["charged"][3] is None and a["charged"][4] is None, "other groups untouched"
    assert a["charged"][5] is None and a["charged"][6] is None
    assert a["calls"] == [1, 1, 1]
    assert a["ctx"] is None


def test_note_counts_loaded_and_blocked(result):
    assert result["fourOfSix"]["notes"] == ["Loaded in 4 of 6 · 2 can't use it|near"]
    assert result["fourOfSix"]["loaded"] == 4


def test_toggle_off_or_other_item_loads_one(result):
    assert result["allOff"]["charged"].count(AMMO) == 1 and result["allOff"]["notes"] == []
    assert result["otherCtx"] == 1
    assert result["toggled"] is False


def test_clear_with_toggle_on_clears_the_type(result):
    c = result["clearAll"]["charged"]
    assert c[0] is None and c[2] is None and c[5] is None   # every GUN, high, med and low
    assert c[1] == AMMO and c[3] == AMMO, "other types keep theirs"
    assert result["clearAll"]["notes"] == ["Cleared the charge from 3 modules|near"]
    assert result["clearOne"].count(None) == 1


def test_unload_all_clears_charges_and_keeps_modules(result):
    u = result["unload"]
    assert u["n"] is True and u["left"] == 0
    assert u["notes"] == ["Unloaded 3 charges|near"]
    assert u["calls"] == [1, 1, 1]
    assert result["unloadOne"] == ["Unloaded 1 charge|near"]


def test_unload_all_with_nothing_loaded_says_so(result):
    assert result["unloadNone"] == {"notes": ["No charges loaded|near"], "calls": [0, 0, 0]}


# ── markup ───────────────────────────────────────────────────────────

def test_selector_header_has_the_pressed_toggle(script):
    show = _fn(script, "showChargeDropdown")
    assert 'data-click="toggleChargeAll"' in show and 'aria-pressed=' in show
    assert "fit-ctl" in show and "m-tap" not in show
    assert "that fit" in show


def test_unload_button_is_a_fit_ctl_in_the_legend_row(page):
    m = re.search(r'<button[^>]*data-click="unloadAllCharges"[^>]*>([^<]*)</button>', page)
    assert m and m.group(1) == "Unload all charges"
    tag = m.group(0)
    assert 'class="fit-ctl fit-unload"' in tag and "m-tap" not in tag
    assert page.index("Module state:") < page.index('data-click="unloadAllCharges"') < page.index('id="fit-state-note"')


def test_new_css_sits_before_the_last_phone_block():
    css = open("static/css/site.css", encoding="utf-8").read()
    assert css.index("T-086 · ammo") < css.index("m-hide wins ties")
