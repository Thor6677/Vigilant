"""The delegated data-<event> dispatcher must ignore non-Element targets (ISS-041).

actions.js binds one document-level listener per event type (bubble for
click/change/input/submit/keydown/mousedown, capture for focus) and walks up
from `e.target` with `closest()`. An event whose target is the Document itself
or a text node reaches those listeners too, and neither has `closest()`, so
every such event threw `e.target.closest is not a function` into the console.
The 'submit' and 'error' listeners already guarded against this; the shared
dispatcher did not.

Runs the real actions.js in Node against a stub DOM, like
test_timer_banner_countdown.py, so the check is behaviour, not source text.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIONS = os.path.join(ROOT, "static/js/actions.js")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const srcPath = process.argv[process.argv.length - 1];
const src = fs.readFileSync(srcPath, 'utf8');

const listeners = [];   // {type, fn, capture}
const errors = [];
const calls = [];
const sandbox = {
  window: {}, console,
  document: {
    querySelectorAll: () => [],
    querySelector: () => null,
    getElementById: () => null,
    body: { addEventListener() {} },
    addEventListener(type, fn, capture) { listeners.push({ type, fn, capture: !!capture }); },
  },
  localStorage: { getItem: () => null, setItem() {} },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, Date,
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

// Positive control: a real binding still dispatches after the guard.
sandbox.window.probeHandler = function () { calls.push(this.id); };
const bound = {
  id: 'bound-button',
  hasAttribute: () => false,
  getAttribute: a => (a === 'data-click' ? 'probeHandler' : null),
};
const element = { closest: sel => (sel === '[data-click]' ? bound : null) };

const TYPES = ['click', 'change', 'input', 'submit', 'keydown', 'mousedown', 'focus'];
const targets = {
  document: { nodeType: 9 },   // Document: no closest()
  text: { nodeType: 3 },       // Text node: no closest()
};
for (const type of TYPES) {
  for (const l of listeners.filter(x => x.type === type)) {
    for (const [name, target] of Object.entries(targets)) {
      try { l.fn({ type, target, stopPropagation() {} }); }
      catch (err) { errors.push(`${type}/${name}: ${err.message}`); }
    }
  }
}
for (const l of listeners.filter(x => x.type === 'click')) {
  l.fn({ type: 'click', target: element, stopPropagation() {} });
}
console.log(JSON.stringify({
  types: [...new Set(listeners.map(l => l.type))],
  focusCapture: listeners.some(l => l.type === 'focus' && l.capture),
  errors, calls,
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dispatch_ignores_non_element_targets_and_still_dispatches():
    res = subprocess.run(
        ["node", "-e", _HARNESS, "--", ACTIONS],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(res.stdout.strip().splitlines()[-1])

    # The listeners this test exercises are actually registered — otherwise
    # an empty error list would prove nothing.
    for t in ("click", "change", "input", "submit", "keydown", "mousedown", "focus"):
        assert t in data["types"], f"no document listener for {t}"
    assert data["focusCapture"], "focus must be bound in the capture phase"

    assert data["errors"] == [], "dispatcher threw on a non-Element target:\n  " + "\n  ".join(data["errors"])
    assert data["calls"] == ["bound-button"], "the guard must not stop real bindings from dispatching"
