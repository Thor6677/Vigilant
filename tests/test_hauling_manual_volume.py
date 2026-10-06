"""Typing a volume into Hauling's Manual m³ field updates Trips (ISS-131).

The field's `data-input` named `recalcTrips`, whose parameters are the
fleet's capacity and ship names. actions.js calls a data-input handler as
`fn.call(el, event)`, so the capacity was the event and the names were
undefined: every keystroke threw on `fleetShipNames.forEach` and Trips
never changed. Only editing a ship field (data-change="recalcAll")
refreshed it.

Runs the page's real inline script in Node against a stub DOM, like
test_actions_dispatch_guard.py: it renders hauling.html, reads the handler
the field names, and calls it the way the dispatcher does. One ship entry
of a single-bay hauler with no expanders or rigs, so the expected trip
count is worked out here independently of the page's JS.
"""
import json
import math
import re
import shutil
import subprocess

import pytest

from app.industry.hauling import (BAY_LABELS, CARGO_MODULES, CARGO_RIGS, HAULING_SHIPS,
                                  get_ships_by_group)
from app.routes import industry
from tests._mobile import render_page

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [srcPath, cfgPath] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));

function el(id, extra) {
  return Object.assign({
    id, value: '', textContent: '', innerHTML: '', style: {}, dataset: {}, children: [],
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  }, extra || {});
}
const byId = {};
['vol-total', 'trip-results', 'result-trips', 'result-total-vol', 'result-fleet-cap',
 'result-bay-breakdown', 'panel-manual', 'panel-paste', 'tab-manual', 'tab-paste',
 'fleet-entries', 'paste-text', 'resolved-items'].forEach(id => { byId[id] = el(id); });
byId['trip-results'].style.display = 'none';

const fields = {};
for (const [k, v] of Object.entries(cfg.entry)) fields[k] = el('field-' + k, { value: v, max: '' });
const capInner = el('cap-inner');
const capBox = el('cap', { querySelector: s => (s === 'div' ? capInner : null) });
const entry = el('entry', {
  querySelector(sel) {
    const m = sel.match(/^\[data-field="(\w+)"\]$/);
    if (m) return fields[m[1]] || null;
    return sel === '.ship-capacity' ? capBox : null;
  },
});

const store = {};
const sandbox = {
  console,
  document: {
    getElementById: id => byId[id] || null,
    querySelectorAll: sel => (sel === '.ship-entry' ? [entry] : []),
    querySelector: () => null,
    createElement: tag => el(tag),
    addEventListener() {},
    body: { addEventListener() {} },
  },
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  setTimeout: () => 0,
  htmx: { trigger() {} },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

function fire(handlerName, target, type) {
  const fn = sandbox[handlerName];
  if (typeof fn !== 'function') return 'no handler ' + handlerName;
  try { fn.call(target, { type, target }); return null; }
  catch (err) { return err.name + ': ' + err.message; }
}
function snapshot(error) {
  return {
    error,
    trips: String(byId['result-trips'].textContent),
    shown: byId['trip-results'].style.display,
    total: byId['result-total-vol'].textContent,
    capacity: byId['result-fleet-cap'].textContent,
  };
}

const out = {};
byId['vol-total'].value = cfg.volume;
out.typed = snapshot(fire(cfg.inputHandler, byId['vol-total'], 'input'));
out.saved = store['vigilant_haul_state'] ? JSON.parse(store['vigilant_haul_state']) : null;

byId['vol-total'].value = '0';
byId['trip-results'].style.display = 'block';
out.cleared = snapshot(fire(cfg.inputHandler, byId['vol-total'], 'input'));

// A ship-field change still recalculates (data-change on the entry's fields).
byId['vol-total'].value = cfg.volume;
byId['result-trips'].textContent = '';
out.shipChange = snapshot(fire(cfg.changeHandler, fields.ship, 'change'));
console.log(JSON.stringify(out));
"""


def _page():
    return render_page(industry, "hauling.html", "/industry/hauling",
                       ships_by_group=get_ships_by_group(), all_ships=HAULING_SHIPS,
                       cargo_modules=CARGO_MODULES, cargo_rigs=CARGO_RIGS,
                       bay_labels=BAY_LABELS)


def _attr(tag, name):
    m = re.search(rf'\s{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def _hauler():
    """A single-bay hauler with a big cargo hold and one skill bonus on it,
    so its capacity is base x (1 + per_level x skill), rounded to 0.1."""
    for type_id, ship in HAULING_SHIPS.items():
        bays = ship["bays"]
        bonus = ship.get("skill_bonus") or {}
        if (list(bays) == ["cargo"] and bays["cargo"]["base"] >= 100_000
                and bonus.get("bay") == "cargo" and not ship.get("extra_skill_bonus")):
            return type_id, ship
    raise AssertionError("no single-bay freighter in HAULING_SHIPS")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_typing_a_volume_updates_trips(tmp_path):
    html = _page()
    vol = re.search(r'<input[^>]*id="vol-total"[^>]*>', html, re.S).group(0)
    input_handler = _attr(vol, "data-input")
    assert input_handler, "the Manual m³ field recalculates as you type"
    ship_select = re.search(r'<select data-field="ship"[^>]*>', html).group(0)
    change_handler = _attr(ship_select, "data-change")

    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, re.S)
    (script,) = [s for s in scripts if "function recalcAll" in s]

    type_id, ship = _hauler()
    skill = 5
    capacity = round(ship["bays"]["cargo"]["base"]
                     * (1 + ship["skill_bonus"]["per_level"] * skill) * 10) / 10
    volume = 900_000
    trips = math.ceil(volume / capacity)
    assert trips >= 2, "a volume that needs more than one trip"

    src = tmp_path / "hauling.js"
    src.write_text(script, encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "inputHandler": input_handler, "changeHandler": change_handler,
        "volume": str(volume),
        "entry": {"ship": str(type_id), "qty": "1", "mod": "none", "modCount": "0",
                  "rig": "none", "rigCount": "0", "skill": str(skill)},
    }), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(harness), str(src), str(cfg)],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    typed = out["typed"]
    assert typed["error"] is None, typed["error"]
    assert typed["trips"] == str(trips)
    assert typed["shown"] == "block"
    assert typed["total"] == f"{volume:,} m³"
    assert typed["capacity"] == f"{capacity:,.1f}".rstrip("0").rstrip(".") + " m³"
    # The typed volume is saved with the fleet, which is what a reload restores.
    assert out["saved"]["totalVol"] == str(volume)

    cleared = out["cleared"]
    assert cleared["error"] is None, cleared["error"]
    assert cleared["shown"] == "none", "no volume, no trip summary"

    ship_change = out["shipChange"]
    assert ship_change["error"] is None, ship_change["error"]
    assert ship_change["trips"] == str(trips)
