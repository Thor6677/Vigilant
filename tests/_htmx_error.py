"""Runs base.html's ISS-007 "couldn't load" handlers under node against a stub DOM.

  iss007_script(html)           the ISS-007 <script> body, cut from a rendered page
  admin_page()                  admin.html rendered for an admin (so base.html in full)
  attrs_of(html, element_id)    an element's attributes, as rendered
  run_error_handlers(script, dom, events)
                                fire each event at the handlers on a fresh copy of
                                `dom`; returns, per event, {id: html} for every
                                element whose innerHTML was written

`dom` maps an id to {"attrs": {...}, "parent": id-or-None}. "html" and "body"
always exist ("body" inside "html") and are document.documentElement and
document.body. Each event is {"type": "responseError" | "sendError",
"elt": id, "target": id-or-None, "status": int}; it reaches the listeners as
htmx 1.9.12 hands it over: detail.elt, detail.target and detail.xhr.

htmx.ajax() issues its request with no element of its own, so htmx sets
detail.elt to document.body and the swap target goes in detail.target
(htmx-1.9.12.min.js: `if(n==null){n=re().body}`). That is the event every
failed section refresh on /admin produces (ISS-125).
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._mobile import render_page

_ADMIN_SESSION = {"user_id": 1, "is_admin": True,
                  "active_character_id": 90000001, "csrf_token": "t"}

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [script, dom, events] = process.argv.slice(2).map(p => fs.readFileSync(p, 'utf8'));
const DOM = JSON.parse(dom), EVENTS = JSON.parse(events);

function build() {
  const els = {}, listeners = {}, writes = {};
  const make = (id, attrs, parent) => ({
    id, attrs: attrs || {}, parent: parent || null, _html: 'original ' + id,
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; },
    contains(o) { for (let x = o; x; x = x.parent) if (x === this) return true; return false; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; writes[this.id] = v; },
  });
  els.html = make('html', {}, null);
  els.body = make('body', {}, els.html);
  els.body.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const pending = Object.keys(DOM);
  let spins = 0;
  while (pending.length) {                    // parents before children, any order given
    if (++spins > 10000) throw new Error('dom: unknown parent among ' + pending.join(', '));
    const id = pending.shift(), spec = DOM[id], p = spec.parent || 'body';
    if (!els[p]) { pending.push(id); continue; }
    els[id] = make(id, spec.attrs, els[p]);
  }
  const document = { body: els.body, documentElement: els.html };
  const ctx = { document, console };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return { els, listeners, writes };
}

const out = [];
for (const ev of EVENTS) {
  const { els, listeners, writes } = build();
  const detail = { elt: els[ev.elt], target: ev.target ? els[ev.target] : null,
                   xhr: ev.type === 'sendError' ? { status: 0 } : { status: ev.status } };
  const fns = listeners['htmx:' + ev.type] || [];
  if (!fns.length) throw new Error('no listener for htmx:' + ev.type);
  fns.forEach(fn => fn({ type: 'htmx:' + ev.type, detail }));
  out.push(writes);
}
process.stdout.write(JSON.stringify(out));
"""


def admin_page():
    from app.routes import admin as admin_mod
    return render_page(admin_mod, "admin.html", "/admin", session=dict(_ADMIN_SESSION),
                       sections=admin_mod.ADMIN_SECTIONS, initial_section="updates")


def iss007_script(html):
    m = re.search(r'<script nonce="[^"]*">\s*(/\* ISS-007:.*?)</script>', html, re.S)
    assert m, "base.html's ISS-007 script"
    return m.group(1)


def attrs_of(html, element_id):
    m = re.search(r'<[a-z]+\b([^>]*\bid="%s"[^>]*)>' % re.escape(element_id), html)
    assert m, element_id
    return dict(re.findall(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)="([^"]*)"', m.group(1)))


def run_error_handlers(script, dom, events):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "harness.js").write_text(_HARNESS)
        (d / "script.js").write_text(script)
        (d / "dom.json").write_text(json.dumps(dom))
        (d / "events.json").write_text(json.dumps(events))
        run = subprocess.run(["node", str(d / "harness.js"), str(d / "script.js"),
                              str(d / "dom.json"), str(d / "events.json")],
                             capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)
