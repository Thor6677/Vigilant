"""Expanded admin sections survive the auto-refresh (ISS-049).

#admin-content re-renders its section every 5-10 s and the updater panel
re-renders while polling, so every <details> used to come back closed. The fix
is `<details id=... data-keep-open>` plus a memory in static/js/actions.js that
re-applies the reader's choice after each htmx swap.

The behaviour is checked by running the real actions.js in Node against a stub
DOM (same approach as test_actions_dispatch_guard.py), not by reading source.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIONS = os.path.join(ROOT, "static/js/actions.js")

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const docListeners = {};    // type -> [fn]
const bodyListeners = {};
const timeouts = [];
const byId = {};

function details(id, open, keep = true) {
  const d = { id, open, tagName: 'DETAILS',
              hasAttribute: a => (a === 'data-keep-open' ? keep : false) };
  const summary = { tagName: 'SUMMARY', parentElement: d };
  summary.closest = sel => (sel === 'summary' ? summary : null);
  const span = { closest: sel => (sel === 'summary' ? summary : null) };
  d.span = span;
  byId[id] = d;
  return d;
}

const sandbox = {
  window: {}, console, Date,
  document: {
    querySelectorAll: () => [], querySelector: () => null,
    getElementById: id => byId[id] || null,
    body: { addEventListener(type, fn) { (bodyListeners[type] = bodyListeners[type] || []).push(fn); } },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  },
  localStorage: { getItem: () => null, setItem() {} },
  setInterval: () => 0, clearInterval() {},
  setTimeout: fn => { timeouts.push(fn); return timeouts.length; },
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const click = target => (docListeners.click || []).forEach(fn => fn({ target, preventDefault() {} }));
const flush = () => { while (timeouts.length) timeouts.shift()(); };
const swap = () => (bodyListeners['htmx:afterSwap'] || []).forEach(fn => fn({ detail: {} }));
const out = {};

// 1. Open a section, then a refresh brings back a closed copy: it must reopen.
let a = details('upd-policy', false);
click(a.span); a.open = true; flush();            // native toggle, then our read
a = details('upd-policy', false); swap();
out.reopened = a.open;

// 2. Close it; the next render arrives open (server default): it must stay closed.
click(a.span); a.open = false; flush();
a = details('upd-policy', true); swap();
out.stays_closed = a.open === false;

// 3. A section never touched keeps the server's default.
let b = details('esi-archived', true); swap();
out.untouched_default = b.open;

// 4. Only opted-in elements are managed.
let c = details('plain', false, false);
click(c.span); c.open = true; flush();
c = details('plain', false, false); swap();
out.opt_out_left_alone = c.open === false;

// 5. Non-element targets (Document, text nodes) must not throw (ISS-041).
let threw = false;
try { click({ nodeType: 9 }); click({ nodeType: 3 }); } catch (e) { threw = String(e); }
out.threw = threw;

process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("keepopen") / "harness.js"
    harness.write_text(_HARNESS)
    run = subprocess.run(["node", str(harness), ACTIONS], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_an_opened_section_reopens_after_a_refresh(result):
    assert result["reopened"] is True


def test_a_closed_section_stays_closed_even_if_the_server_renders_it_open(result):
    assert result["stays_closed"] is True


def test_untouched_sections_keep_the_server_default(result):
    assert result["untouched_default"] is True


def test_only_opted_in_details_are_managed(result):
    assert result["opt_out_left_alone"] is True


def test_non_element_click_targets_do_not_throw(result):
    assert result["threw"] is False


# ── Markup: every refreshing admin <details> opts in ─────────────────────────

_ADMIN_PARTIALS = [
    Path("app/templates/partials/updater_panel.html"),
    Path("app/templates/partials/admin_esi.html"),
] + sorted(Path("app/templates/partials").glob("admin_*.html"))


@pytest.mark.parametrize("path", sorted(set(_ADMIN_PARTIALS)), ids=str)
def test_every_admin_details_keeps_its_state(path):
    """Every <details> rendered into #admin-content must carry an id and
    data-keep-open — except the finished-run log, which uses hx-preserve."""
    src = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)   # drop Jinja comments
    for tag in re.findall(r"<details\b[^>]*>", src):
        if "hx-preserve" in tag:
            continue
        assert re.search(r'\bid="[^"]+"', tag) and "data-keep-open" in tag, (str(path), tag)


def test_ids_are_unique_across_admin_sections():
    ids = []
    for path in set(_ADMIN_PARTIALS):
        ids += re.findall(r'<details\b[^>]*\bid="([^"{]+)"', path.read_text())
    assert len(ids) == len(set(ids)), ids
