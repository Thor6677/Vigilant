"""The Skill Plan typeahead lists must not be clipped by their panel on desktop (ISS-114).

Add Skill and Add from Ship hang an absolutely positioned result list
(max-height 200px) inside a .b-panel, which is overflow:hidden: desktop showed
only the ~40px of the list that fit inside the panel. The two panels carry
`skp-ta-panel`, and a desktop-only rule (min-width: 641px) lets them overflow.
Phones are untouched: the list sits in the flow there (R2 T6) and the panel
keeps the overflow-x:auto safety net, so the hook must never reach a phone
block.

A companion click-away / Escape handler closes an open list, but only while it
is an overlay (computed position absolute). Its behaviour is checked in Node
against stubs when node is available.

Names and ids are invented."""
import json
import re
import shutil
import subprocess
import types
from html.parser import HTMLParser

import pytest

from app.routes import skill_plans as sp_mod
from tests._mobile import SITE_CSS, VOID, render_page, rule_bodies

_NS = types.SimpleNamespace
_CHARS = [_NS(character_id=90000101, character_name="Sample Pilot One")]
_ACL = [_NS(id=1, subject_type="alliance", subject_id=99000001, subject_name="Sample Alliance",
            permission="view")]


def _render_detail(*, can_edit=True, can_admin=False, visibility="personal"):
    plan = _NS(id=42, name="Sample Fleet Plan", visibility=visibility, owner_corp_id=None,
               owner_alliance_id=None, share_token=None, description="")
    entries = [{"id": 500, "skill_type_id": 3300, "skill_name": "Sample Gunnery Basics",
                "target_level": 5, "rank": 1.0, "primary_attr_name": "Perception",
                "secondary_attr_name": "Willpower"}]
    return render_page(
        sp_mod, "skill_plan_detail.html", "/skill-plans/42",
        plan=plan, entries=entries, characters=_CHARS, corp_names={}, alliance_names={},
        can_edit=can_edit, can_admin=can_admin, is_owner=can_admin,
        eligible_corps=[], eligible_alliances=[],
        acl_entries=list(_ACL) if visibility == "custom" else [], acl_err=None)


class _Panels(HTMLParser):
    """Each .b-panel (classes, label text) and, per element id, the index of
    its nearest enclosing .b-panel."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # (tag, panel_index_or_None)
        self.panels = []         # {"classes": [...], "label": str}
        self.owner = {}          # element id -> panel index
        self._in_label = False

    def _cur(self):
        for _tag, idx in reversed(self.stack):
            if idx is not None:
                return idx
        return None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        classes = a.get("class", "").split()
        idx = None
        if "b-panel" in classes:
            self.panels.append({"classes": classes, "label": ""})
            idx = len(self.panels) - 1
        if "id" in a:
            self.owner[a["id"]] = idx if idx is not None else self._cur()
        if "b-label" in classes:
            self._in_label = True
        if tag not in VOID:
            self.stack.append((tag, idx))

    def handle_endtag(self, tag):
        self._in_label = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_label:
            cur = self._cur()
            if cur is not None and not self.panels[cur]["label"]:
                self.panels[cur]["label"] = data.strip()


def _panels(html):
    p = _Panels()
    p.feed(html)
    p.close()
    return p


def _css():
    with open(SITE_CSS, encoding="utf-8") as fh:
        return re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)


def _media_blocks(css):
    """[(prelude, body)] for every @media block, brace-matched."""
    out = []
    for m in re.finditer(r"@media[^{]*\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append((m.group(0)[:-1].strip(), css[m.end():i - 1]))
    return out


def _desktop_bodies(css):
    return [b for pre, b in _media_blocks(css) if re.search(r"min-width:\s*641px", pre)]


def test_typeahead_panels_carry_the_overflow_hook():
    p = _panels(_render_detail(can_edit=True))
    for rid in ("skill-results", "ship-results"):
        idx = p.owner.get(rid)
        assert idx is not None, f"#{rid} is not inside a .b-panel"
        assert "skp-ta-panel" in p.panels[idx]["classes"], f"#{rid}'s panel lacks the hook"
    assert sum("skp-ta-panel" in x["classes"] for x in p.panels) == 2
    assert {p.panels[p.owner[r]]["label"] for r in ("skill-results", "ship-results")} == \
        {"Add Skill", "Add from Ship"}


def test_only_the_typeahead_panels_overflow():
    p = _panels(_render_detail(can_edit=True, can_admin=True, visibility="custom"))
    hooked = [x for x in p.panels if "skp-ta-panel" in x["classes"]]
    assert sorted(x["label"] for x in hooked) == ["Add Skill", "Add from Ship"]
    others = [x for x in p.panels if "skp-ta-panel" not in x["classes"]]
    assert len(others) >= 4, "expected the other panels (skills, gap, imports, ACL) to render"


def test_view_only_plan_has_no_hook():
    html = _render_detail(can_edit=False)
    assert "skp-ta-panel" not in html
    owner = _panels(html).owner   # element ids; the page script names them regardless
    assert "skill-results" not in owner and "ship-results" not in owner


def test_css_desktop_rule_lets_the_panels_overflow():
    bodies = "\n".join(rule_bodies(b, ".b-panel.skp-ta-panel") for b in _desktop_bodies(_css()))
    assert re.search(r"overflow:\s*visible\b", bodies), \
        "no min-width: 641px rule sets .b-panel.skp-ta-panel to overflow: visible"


def test_css_hook_never_reaches_phones():
    css = _css()
    whole = css.count("skp-ta-panel")
    assert whole >= 1
    in_desktop = sum(b.count("skp-ta-panel") for b in _desktop_bodies(css))
    assert whole == in_desktop, "skp-ta-panel appears outside a min-width: 641px block"
    for pre, body in _media_blocks(css):
        if "max-width" in pre:
            assert "skp-ta-panel" not in body, f"hook inside phone block {pre!r}"


# ── click-away / Escape handler ───────────────────────────────────────

def _handler_source():
    html = _render_detail(can_edit=True)
    m = re.search(r"/\* Click-away / Escape.*?\n\}\)\(\);", html, flags=re.S)
    assert m, "the click-away / Escape block is missing from the page script"
    return m.group(0)


def test_click_away_source_is_wired():
    src = _handler_source()
    assert "document.addEventListener('click'" in src
    assert "document.addEventListener('keydown'" in src and "'Escape'" in src
    assert "skill-results" in src and "ship-results" in src
    assert re.search(r"position\s*!==\s*'absolute'", src)


_HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(process.argv[2], 'utf8');
function mk(id, pos, parent) {
  return { id, pos, parent: parent || null, style: { display: 'block' },
           closest(sel) {
             const ids = sel.split(',').map(s => s.trim());
             for (let n = this; n; n = n.parent) if (ids.includes('#' + n.id)) return n;
             return null;
           } };
}
function run(pos) {
  const listeners = {};
  const els = {};
  ['skill-search', 'skill-results', 'ship-search', 'ship-results', 'elsewhere']
    .forEach(id => { els[id] = mk(id, pos); });
  els['row'] = mk('row', pos, els['skill-results']);   // a result row inside the skill list
  const doc = { getElementById: id => (id.endsWith('-results') ? els[id] : null),
                addEventListener(t, f) { listeners[t] = f; } };
  const sb = { document: doc, getComputedStyle: el => ({ position: el.pos }), clearTimeout() {},
               _skillTimer: null, _shipTimer: null, _skillSeq: 0, _shipSeq: 0 };
  vm.createContext(sb); vm.runInContext(src, sb);
  const shown = () => [els['skill-results'].style.display, els['ship-results'].style.display];
  const open = () => { els['skill-results'].style.display = 'block'; els['ship-results'].style.display = 'block'; };
  const out = {};
  open(); listeners.click({ type: 'click', target: els['skill-results'] });
  out.clickInsideSkillList = shown();
  open(); listeners.click({ type: 'click', target: els['row'] });
  out.clickRowInSkillList = shown();
  open(); listeners.click({ type: 'click', target: els['ship-search'] });
  out.clickShipInput = shown();
  open(); listeners.click({ type: 'click', target: els['elsewhere'] });
  out.clickAway = shown();
  open(); listeners.keydown({ type: 'keydown', key: 'a' });
  out.otherKey = shown();
  open(); listeners.keydown({ type: 'keydown', key: 'Escape' });
  out.escape = shown();
  open(); listeners.keydown({ type: 'keydown', key: 'Escape', target: els['skill-search'] });
  out.escapeInSkillInput = shown();
  open(); listeners.keydown({ type: 'keydown', key: 'Escape', target: els['ship-search'] });
  out.escapeInShipInput = shown();
  open(); listeners.click({ type: 'click', target: {} });   // no closest(): must not throw
  out.noClosest = shown();
  return out;
}
function runViewOnly() {          // no lists in the page: nothing to find, nothing may throw
  const listeners = {};
  const doc = { getElementById: () => null, addEventListener(t, f) { listeners[t] = f; } };
  const sb = { document: doc, getComputedStyle: () => { throw new Error('no list to style'); } };
  vm.createContext(sb); vm.runInContext(src, sb);
  listeners.click({ type: 'click', target: mk('elsewhere', 'absolute') });
  listeners.keydown({ type: 'keydown', key: 'Escape' });
  return 'ok';
}
console.log(JSON.stringify({ overlay: run('absolute'), inFlow: run('static'), viewOnly: runViewOnly() }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_click_away_closes_only_overlays(tmp_path):
    js = tmp_path / "handler.js"
    js.write_text(_handler_source(), encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), str(js)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    o, f = res["overlay"], res["inFlow"]
    opened, closed = ["block", "block"], ["none", "none"]
    # Overlay (desktop): a click outside, or Escape, closes both lists.
    assert o["clickAway"] == closed
    assert o["escape"] == closed
    assert o["noClosest"] == closed
    assert o["otherKey"] == opened
    # Escape closes both lists wherever focus is, including inside either input.
    assert o["escapeInSkillInput"] == closed
    assert o["escapeInShipInput"] == closed
    # A click on a row nested inside the skill list keeps it open (ship list closes).
    assert o["clickRowInSkillList"] == ["block", "none"]
    # View-only plan: no lists exist, and neither event may throw.
    assert res["viewOnly"] == "ok"
    # A click inside a list or on its own input keeps that list; the other closes.
    assert o["clickInsideSkillList"] == ["block", "none"]
    assert o["clickShipInput"] == ["none", "block"]
    # In the flow (phones): nothing is ever closed.
    for k, v in f.items():
        assert v == opened, k


# ── late responses must not reopen a dismissed list ───────────────────

def _search_source():
    """The page's state vars, both search/select blocks and the click-away
    handler, cut out of the rendered script."""
    html = _render_detail(can_edit=True)
    parts = [
        re.search(r"var _skillTimer.*?\n(?=\n/\* ── Skill search)", html, flags=re.S),
        re.search(r"/\* ── Skill search.*?(?=function addSkill)", html, flags=re.S),
        re.search(r"/\* ── Ship search.*?(?=function toggleMasteryLevel)", html, flags=re.S),
    ]
    assert all(parts), "could not cut the search code out of the page script"
    return "\n".join(m.group(0) for m in parts) + "\n" + _handler_source()


_SEARCH_HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const flush = () => new Promise(r => setImmediate(r));

async function scenario(which, how) {            // which: 'skill' | 'ship'; how: how the list gets dismissed
  const listeners = {};
  const el = (id, v) => ({ id, value: v || '', style: { display: 'none' }, innerHTML: '', dataset: {},
                           closest() { return null; } });
  const els = { 'skill-search': el('skill-search', 'gun'), 'skill-id': el('skill-id'),
                'skill-results': el('skill-results'), 'ship-search': el('ship-search', 'rif'),
                'ship-id': el('ship-id'), 'ship-results': el('ship-results'), 'ship-link': el('ship-link'),
                'elsewhere': el('elsewhere') };
  const timers = [];                              // pending setTimeout callbacks; clearTimeout nulls one
  const fetches = [];                             // one deferred per fetch()
  const sb = {
    document: { getElementById: id => els[id], addEventListener(t, f) { listeners[t] = f; } },
    getComputedStyle: () => ({ position: 'absolute' }),
    encodeURIComponent,
    setTimeout: f => { timers.push(f); return timers.length; },
    clearTimeout: h => { if (h) timers[h - 1] = null; },
    fetch: () => new Promise(res => fetches.push(res)),
  };
  vm.createContext(sb); vm.runInContext(src, sb);
  const search = which === 'skill' ? sb.searchSkills : sb.searchShips;
  const select = which === 'skill' ? sb.selectSkill : sb.selectShip;
  const results = els[which + '-results'];
  const respond = async () => { fetches.shift()({ text: async () => '<div>row</div>' }); await flush(); };

  search();                                       // debounce armed
  if (how === 'close-before-fetch') {             // dismissed while still in the debounce
    results.style.display = 'block';
    listeners.keydown({ type: 'keydown', key: 'Escape' });
    timers.forEach(f => f && f());                // the timer must have been cleared
    await flush();
    return { fetched: fetches.length, display: results.style.display };
  }
  timers.forEach(f => f && f()); await flush();   // debounce fires, fetch in flight
  if (how === 'normal') {
    await respond();
  } else if (how === 'escape') {
    results.style.display = 'block';
    listeners.keydown({ type: 'keydown', key: 'Escape' });
    await respond();
  } else if (how === 'click-away') {
    results.style.display = 'block';
    listeners.click({ type: 'click', target: els['elsewhere'] });
    await respond();
  } else if (how === 'select') {
    results.style.display = 'block';
    select.call({ dataset: { id: '7', name: 'Sample Pick' } });
    await respond();
  } else if (how === 'retype') {                  // a newer search supersedes the older response
    search(); timers.forEach(f => f && f()); await flush();
    await respond();                              // the OLD response
    const stale = results.style.display;
    await respond();                              // the newer one
    return { stale, display: results.style.display, html: results.innerHTML };
  }
  return { display: results.style.display, html: results.innerHTML };
}

(async () => {
  const out = {};
  for (const w of ['skill', 'ship'])
    for (const how of ['normal', 'escape', 'click-away', 'select', 'close-before-fetch', 'retype'])
      out[w + ':' + how] = await scenario(w, how);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_late_search_response_cannot_reopen_a_dismissed_list(tmp_path):
    js = tmp_path / "search.js"
    js.write_text(_search_source(), encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(_SEARCH_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), str(js)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    for w in ("skill", "ship"):
        # A normal response still opens the list.
        assert res[f"{w}:normal"] == {"display": "block", "html": "<div>row</div>"}, w
        # A response landing after Escape, a click-away or a pick must leave it closed.
        for how in ("escape", "click-away", "select"):
            assert res[f"{w}:{how}"]["display"] == "none", f"{w}:{how}"
        # Dismissed during the debounce: the timer is cancelled, no request goes out.
        assert res[f"{w}:close-before-fetch"] == {"fetched": 0, "display": "none"}, w
        # A superseded response is dropped; the newest one wins.
        assert res[f"{w}:retype"] == {"stale": "none", "display": "block", "html": "<div>row</div>"}, w
