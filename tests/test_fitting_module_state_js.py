"""The fitting page's module state logic, run under node (ISS-156).

moduleStates() must give exactly what app/fitting/module_state.resolve_states
gives, on the same case table (tests/fixtures/module_state_cases.json). The tap
cycle in toggleOnline() is checked step by step against invented caps.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import render_page

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "module_state_cases.json").read_text()
)

HARNESS = r"""
const src = %(src)s;
const cases = %(cases)s;
global.state = {items: []};
global.overheatCache = {};
const notes = [];
global.saveState = () => {}; global.renderSlots = () => {}; global.recalcStats = () => {};
global._limitNote = (t) => notes.push(t);
eval(src + '\nglobal.moduleStates = moduleStates; global.toggleOnline = toggleOnline;');

const out = {parity: cases.map((c) => moduleStates(c.items, c.caps)), cycles: {}};

const caps = (o) => Object.assign({can_activate: false, can_overheat: false, default_active: false,
                                   group_id: null, max_group_active: 0}, o);
function cycle(name, item, cap, taps) {
  global.state = {items: [item]};
  global.overheatCache = {1: cap};
  const seen = [];
  for (let i = 0; i < taps; i++) {
    toggleOnline(0);
    seen.push({state: moduleStates(state.items, overheatCache).states[0],
               item: JSON.parse(JSON.stringify(state.items[0]))});
  }
  out.cycles[name] = seen;
}
cycle('turret', {type_id: 1, slot: 'high'}, caps({can_activate: true, can_overheat: true, default_active: true}), 6);
cycle('plain', {type_id: 1, slot: 'low'}, caps({}), 3);
cycle('noheat', {type_id: 1, slot: 'mid', online: false}, caps({can_activate: true}), 4);
cycle('uncached', {type_id: 1, slot: 'mid'}, undefined, 2);

// Group limit: B is switched on, A (explicitly active) is switched off, with a note.
global.state = {items: [{type_id: 1, slot: 'low', active: true}, {type_id: 2, slot: 'low', active: false}]};
const g = {can_activate: true, can_overheat: false, default_active: false, group_id: 9, max_group_active: 1};
global.overheatCache = {1: g, 2: g};
notes.length = 0;
toggleOnline(1);
out.group = {items: JSON.parse(JSON.stringify(state.items)),
             states: moduleStates(state.items, overheatCache).states, notes: notes.slice()};
// Limit 2 with two already running: only one has to go.
global.state = {items: [{type_id: 1, slot: 'low', active: true}, {type_id: 1, slot: 'low', active: true},
                        {type_id: 1, slot: 'low', active: false}]};
global.overheatCache = {1: Object.assign({}, g, {max_group_active: 2})};
toggleOnline(2);
out.group2 = moduleStates(state.items, overheatCache).states;
console.log(JSON.stringify(out));
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
def result(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    page = render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])
    blocks = [b for b in re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
              if "function renderSlots" in b]
    assert len(blocks) == 1
    src = _fn(blocks[0], "moduleStates") + "\n" + _fn(blocks[0], "toggleOnline")
    path = tmp_path_factory.mktemp("node") / "harness.js"
    path.write_text(HARNESS % {"src": json.dumps(src), "cases": json.dumps(CASES)})
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_js_mirror_matches_the_shared_case_table(result):
    assert len(result["parity"]) == len(CASES)
    for case, got in zip(CASES, result["parity"]):
        assert got["states"] == case["states"], case["name"]
        assert got["warnings"] == case["warnings"], case["name"]


def test_tap_cycle_with_every_step(result):
    seq = result["cycles"]["turret"]
    # default active -> overheated -> offline -> online -> active -> overheated
    assert [s["state"] for s in seq] == [2, -1, 0, 1, 2, -1]
    assert seq[0]["item"]["overheated"] is True and seq[0]["item"]["active"] is True
    assert seq[1]["item"]["online"] is False and seq[1]["item"]["overheated"] is False
    assert seq[2]["item"]["online"] is True and seq[2]["item"]["active"] is False
    assert seq[3]["item"]["active"] is True


def test_module_that_cannot_activate_only_toggles_online_offline(result):
    seq = result["cycles"]["plain"]
    assert [s["state"] for s in seq] == [-1, 0, -1]
    assert all("active" not in s["item"] for s in seq)


def test_module_that_cannot_overheat_skips_that_step(result):
    seq = result["cycles"]["noheat"]
    assert [s["state"] for s in seq] == [0, 1, -1, 0]


def test_uncached_caps_fall_back_to_online_offline(result):
    assert [s["state"] for s in result["cycles"]["uncached"]] == [-1, 0]


def test_group_limit_switches_the_other_module_off_with_a_note(result):
    g = result["group"]
    assert g["states"] == [0, 1]
    assert g["items"][0]["active"] is False and g["items"][1]["active"] is True
    assert len(g["notes"]) == 1 and "1" in g["notes"][0]


def test_group_limit_only_demotes_as_many_as_needed(result):
    assert result["group2"] == [1, 0, 1]
