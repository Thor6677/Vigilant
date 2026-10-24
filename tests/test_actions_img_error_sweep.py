"""Images that failed before actions.js ran must still get data-on-error (ISS-110).

actions.js loads at the end of <body>, and the 'error' event is never replayed,
so an image that failed while the parser was still running (a cached 400 fails
in ~0 ms) never reached the capture listener. A DOMContentLoaded sweep now
applies the same handling to any img[data-on-error] that is already broken.

Runs the real actions.js in Node against a stub DOM, like
test_actions_dispatch_guard.py, so the check is behaviour, not source text.
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
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const listeners = [];   // {type, fn, capture}
const warnings = [];

function makeImg(name, attrs, state, tag) {
  const a = Object.assign({}, attrs);
  const img = {
    name, tagName: tag || 'IMG', style: {}, srcWrites: [],
    complete: state.complete, naturalWidth: state.naturalWidth,
    dataset: { fallbackSrc: a['data-fallback-src'] },
    getAttribute: k => (k in a ? a[k] : null),
    removeAttribute: k => { delete a[k]; },
    hasAttr: k => k in a,
  };
  Object.defineProperty(img, 'src', { set(v) { img.srcWrites.push(v); a.src = v; } });
  return img;
}

const imgs = [
  makeImg('brokenHide', { src: '/a.png', 'data-on-error': 'hide' }, { complete: true, naturalWidth: 0 }),
  makeImg('brokenFallback', { src: '/b.png', 'data-on-error': 'imgFallback', 'data-fallback-src': '/fallback.png' }, { complete: true, naturalWidth: 0 }),
  makeImg('loaded', { src: '/c.png', 'data-on-error': 'hide' }, { complete: true, naturalWidth: 32 }),
  makeImg('pending', { src: '/d.png', 'data-on-error': 'hide' }, { complete: false, naturalWidth: 0 }),
  makeImg('noSrc', { 'data-on-error': 'hide' }, { complete: true, naturalWidth: 0 }),
  makeImg('emptySrc', { src: '', 'data-on-error': 'hide' }, { complete: true, naturalWidth: 0 }),
  makeImg('notImg', { src: '/e.png', 'data-on-error': 'hide' }, { complete: true, naturalWidth: 0 }, 'DIV'),
  makeImg('unknownHandler', { src: '/f.png', 'data-on-error': 'noSuchHandler' }, { complete: true, naturalWidth: 0 }),
];

const fakeConsole = { log: console.log, warn: m => warnings.push(String(m)) };
let sweepQueries = 0;
const sandbox = {
  window: {}, console: fakeConsole,
  document: {
    querySelectorAll: sel => {
      if (sel !== 'img[data-on-error]') return [];
      sweepQueries++;
      return imgs.filter(i => i.hasAttr('data-on-error'));
    },
    querySelector: () => null,
    getElementById: () => null,
    body: { addEventListener() {} },
    addEventListener(type, fn, capture) { listeners.push({ type, fn, capture: !!capture }); },
  },
  localStorage: { getItem: () => null, setItem() {} },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, Date,
};
sandbox.window.document = sandbox.document;
sandbox.window.console = fakeConsole;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const dcl = listeners.filter(l => l.type === 'DOMContentLoaded');
const errListeners = listeners.filter(l => l.type === 'error');
const queriesBefore = sweepQueries;
for (const l of dcl) l.fn({ type: 'DOMContentLoaded' });
const sweepRan = sweepQueries - queriesBefore;

const byName = n => imgs.find(i => i.name === n);
const snap = i => ({
  display: i.style.display || null,
  srcWrites: i.srcWrites.slice(),
  hasOnError: i.hasAttr('data-on-error'),
});
const afterSweep = {};
for (const i of imgs) afterSweep[i.name] = snap(i);
const warnsAfterSweep = warnings.length;

// A target without getAttribute (window, XHR, ...) must not throw.
let nonElementThrew = null;
for (const l of errListeners) {
  for (const t of [{}, null, { nodeType: 9 }]) {
    try { l.fn({ type: 'error', target: t }); } catch (err) { nonElementThrew = err.message; }
  }
}

// Follow-up: the fallback itself fails -> the real listener fires on the
// same element. It must not swap src again or warn.
for (const l of errListeners) l.fn({ type: 'error', target: byName('brokenFallback') });

console.log(JSON.stringify({
  errorCaptureListeners: errListeners.filter(l => l.capture).length,
  dclCount: dcl.length, sweepRan, nonElementThrew,
  afterSweep, warnsAfterSweep,
  fallbackAfterFollowUp: snap(byName('brokenFallback')),
  warnings,
}));
"""


@pytest.fixture(scope="module")
def result():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    res = subprocess.run(
        ["node", "-e", _HARNESS, "--", ACTIONS],
        capture_output=True, text=True, check=True,
    )
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_positive_controls_listeners_registered(result):
    assert result["errorCaptureListeners"] >= 1, "error listener must be registered with capture=true"
    assert result["dclCount"] >= 1, "no DOMContentLoaded listener"
    assert result["sweepRan"] >= 1, "DOMContentLoaded did not query img[data-on-error]: the sweep is missing"


def test_broken_hide_image_is_hidden(result):
    assert result["afterSweep"]["brokenHide"]["display"] == "none"


def test_broken_fallback_image_gets_fallback_and_loses_binding(result):
    s = result["afterSweep"]["brokenFallback"]
    assert s["srcWrites"] == ["/fallback.png"]
    assert s["hasOnError"] is False


def test_loaded_image_untouched(result):
    assert result["sweepRan"] >= 1
    s = result["afterSweep"]["loaded"]
    assert s["display"] is None and s["srcWrites"] == [] and s["hasOnError"] is True


def test_pending_image_untouched(result):
    assert result["sweepRan"] >= 1
    s = result["afterSweep"]["pending"]
    assert s["display"] is None and s["srcWrites"] == [] and s["hasOnError"] is True


def test_image_without_src_untouched(result):
    assert result["sweepRan"] >= 1
    s = result["afterSweep"]["noSrc"]
    assert s["display"] is None and s["srcWrites"] == [] and s["hasOnError"] is True


def test_late_error_after_sweep_does_not_swap_twice_or_warn(result):
    assert result["fallbackAfterFollowUp"]["srcWrites"] == ["/fallback.png"]
    # Only the unknown-handler warning from the sweep; the fallback adds none.
    assert result["warnsAfterSweep"] == 1
    assert len(result["warnings"]) == 1


def test_image_with_empty_src_untouched(result):
    assert result["sweepRan"] >= 1
    s = result["afterSweep"]["emptySrc"]
    assert s["display"] is None and s["srcWrites"] == [] and s["hasOnError"] is True


def test_non_img_element_untouched(result):
    assert result["sweepRan"] >= 1
    s = result["afterSweep"]["notImg"]
    assert s["display"] is None and s["srcWrites"] == [] and s["hasOnError"] is True


def test_unknown_handler_warns(result):
    # Proves the console.warn assertions above can fail: the sweep applies the
    # unknown handler name and a warning is recorded.
    assert result["warnsAfterSweep"] == 1
    assert 'no global function named "noSuchHandler"' in result["warnings"][0]


def test_error_listener_ignores_non_element_targets(result):
    assert result["nonElementThrew"] is None
