"""Structure timer banners: countdown, colour tiers, 1h notification (ISS-035).

All three lived in the fragment's own script and had been dead since the CSP
went enforcing (a fragment's nonce never matches the page's). They now run
from static/js/actions.js as `window.styleTimerBanners`, and the browser
Notification goes through notifications.js's opt-in rather than asking for
permission itself.

The behavioural half runs the real actions.js in Node against a stub DOM —
no jsdom in this repo, and the code only touches querySelectorAll, dataset,
style, classList and localStorage — so the tiers are checked against the
clock, not against the source text.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIONS = os.path.join(ROOT, "static/js/actions.js")
NOTIFS = os.path.join(ROOT, "static/js/notifications.js")
BASE = os.path.join(ROOT, "app/templates/base.html")
PARTIAL = os.path.join(ROOT, "app/templates/partials/timer_alert_banners.html")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── contract: where things live ─────────────────────────────────────────────

def test_partial_carries_the_data_the_page_script_reads_and_no_script():
    body = _read(PARTIAL)
    assert "<script" not in body
    for attr in ("data-expires", "data-alert-id", "data-disposition",
                 "data-structure-type", "data-system", "data-name", "data-phase"):
        assert attr in body, attr
    assert 'class="timer-banner-countdown"' in body


def test_actions_defines_the_styler_and_base_calls_it_after_a_swap():
    assert "window.styleTimerBanners = function" in _read(ACTIONS)
    base = _read(BASE)
    fn = re.search(r"function applyDismissState\(\) \{(.*?)\n    \}\n", base, re.S).group(1)
    assert "window.styleTimerBanners()" in fn, "a fresh swap must be styled immediately, not on the next tick"


def test_notification_is_opt_in_through_notifications_js_and_never_asks_itself():
    actions = _read(ACTIONS)
    timer_block = actions[actions.index("Structure timer banners"):]
    assert "requestPermission" not in timer_block, "permission is asked from the bell click only"
    assert "vigilantNotifAllows('structure_timer')" in timer_block
    notifs = _read(NOTIFS)
    assert "structure_timer: true" in notifs, "default pref"
    assert "window.vigilantNotifAllows = function" in notifs
    assert 'data-notif-type="structure_timer"' in _read(BASE), "the mute checkbox"


# ── behaviour: the real JS against a stub DOM ───────────────────────────────

_HARNESS = r"""
const fs = require('fs');
const [srcPath, casesJson] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const cases = JSON.parse(casesJson);
const NOW = 1_800_000_000_000;

function makeEl(id, expiresIso) {
  const cd = { textContent: '', style: {} };
  const classes = new Set();
  return {
    dataset: { expires: expiresIso, disposition: 'hostile', structureType: 'Astrahus',
               system: 'J123456', name: 'Fort Kickass', phase: 'ARMOR' },
    style: { display: '' },
    classList: { toggle(c, on) { on ? classes.add(c) : classes.delete(c); }, has: c => classes.has(c) },
    getAttribute(a) { return a === 'data-alert-id' ? id : null; },
    querySelector() { return cd; },
    _cd: cd, _classes: classes,
  };
}
const els = cases.map(c => makeEl(c.id, new Date(NOW + c.offsetMs).toISOString().replace('.000Z','')));
const store = {};
const notifications = [];
const sandbox = {
  window: {}, console,
  document: { querySelectorAll: () => els, body: { addEventListener() {} }, addEventListener() {} },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  Notification: function (title, opts) { notifications.push({ title, tag: opts.tag }); },
  setInterval: () => 0, clearInterval() {}, Date,
};
sandbox.window.vigilantNotifAllows = () => true;
sandbox.Date.now = () => NOW;
sandbox.window.document = sandbox.document;
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
sandbox.window.styleTimerBanners();
sandbox.window.styleTimerBanners();   // second tick: notification must not repeat
console.log(JSON.stringify({
  out: els.map(e => ({ display: e.style.display, text: e._cd.textContent,
                      danger: e._classes.has('is-danger'), warn: e._classes.has('is-warn') })),
  notifications, notified: JSON.parse(store['vigilant_timer_notified'] || '{}'),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_tiers_countdown_expiry_and_single_notification():
    cases = [
        {"id": "timer-1", "offsetMs": 3 * 3600_000 + 5 * 60_000 + 7_000},  # 3h05m07s → neutral
        {"id": "timer-2", "offsetMs": 90 * 60_000},                        # 1h30m → warn
        {"id": "timer-3", "offsetMs": 20 * 60_000 + 30_000},               # 20m30s → danger + notify
        {"id": "timer-4", "offsetMs": -10 * 60_000},                       # expired 10m ago → (0m), still shown
        {"id": "timer-5", "offsetMs": -45 * 60_000},                       # expired 45m ago → hidden
    ]
    res = subprocess.run(
        ["node", "-e", _HARNESS, "--", ACTIONS, json.dumps(cases)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(res.stdout.strip().splitlines()[-1])
    out = data["out"]

    assert out[0] == {"display": "", "text": "(3h 05m 07s)", "danger": False, "warn": False}
    assert out[1] == {"display": "", "text": "(1h 30m 00s)", "danger": False, "warn": True}
    assert out[2] == {"display": "", "text": "(20m 30s)", "danger": True, "warn": False}
    assert out[3] == {"display": "", "text": "(0m)", "danger": True, "warn": False}
    assert out[4]["display"] == "none"

    # Exactly one notification, for the one timer inside the hour, once
    # across two ticks — and remembered so a later page load stays quiet.
    assert [n["tag"] for n in data["notifications"]] == ["timer-3"]
    assert data["notifications"][0]["title"] == "Timer Alert — 20m remaining"
    assert list(data["notified"]) == ["timer-3"]
