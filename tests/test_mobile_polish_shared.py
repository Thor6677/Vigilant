"""v1.9.0 polish A: central fixes to the shared phone layer (mobile design §4).

All of it lives in the R1 layer of static/css/site.css (before the first
`/* ── R2 T1` section), static/js/actions.js, tests/_mobile.py and
scripts/mobile-audit.js. Every CSS rule here sits inside a
`@media (max-width: 640px)` block, so desktop is unchanged by construction.

1. m-tap no longer shrinks buttons: the shared .m-tap rule forces
   min-height 40px !important, but every <button> and .b-btn already has the
   44px phone floor. `button.m-tap, .b-btn.m-tap` keeps them at 44px.
2. A non-button .b-btn (mostly <a class="b-btn">) centres its label in the
   44px phone box with `align-content: center`, which leaves the box's
   display, and so its width, exactly as it was.
3. toggleExpanded (actions.js) keeps its trigger's aria-expanded in step with
   the target's is-expanded, sets it once on load and after htmx swaps, and
   moves focus to the first revealed row when a Show all hides itself. Run
   in Node against a stub DOM (as test_details_keep_open.py does).
4. Entity-link chips (_entity_links.html) are 40px phone targets, 8px apart.
5. The drawn .b-switch / .b-check keep their own size instead of the 44px
   every phone input gets; the label around them keeps 44px. Native
   checkboxes and radios keep the 44px box (Chrome draws them centred in it,
   and it is what gives their labels a 44px tap height)."""
import json
import os
import re
import shutil
import subprocess

import pytest

from tests._mobile import SITE_CSS, selectors

PHONE = "max-width: 640px"
ACTIONS = os.path.join(os.path.dirname(SITE_CSS), "..", "js", "actions.js")
_R2_FIRST = "/* ── R2 T1 · character overview ── */"


def _raw() -> str:
    with open(SITE_CSS, encoding="utf-8") as fh:
        return fh.read()


def _strip(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _media_bodies(css: str, query: str) -> list[str]:
    """Bodies of every @media block whose prelude contains `query`, brace-matched."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if query not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return out


def _r1_layer() -> str:
    """The R1 shared layer: everything before the first R2 page section."""
    raw = _raw()
    assert raw.count(_R2_FIRST) == 1
    return _strip(raw[:raw.index(_R2_FIRST)])


def _rules(css: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield selectors(m.group(1)), m.group(2)


def _r1_phone_rule(sel_list: list[str]) -> str:
    """The body of the R1 phone-layer rule whose selector list is exactly
    `sel_list` (there must be exactly one)."""
    bodies = [body for block in _media_bodies(_r1_layer(), PHONE)
              for sels, body in _rules(block) if sels == sel_list]
    assert len(bodies) == 1, f"expected one R1 phone rule for {sel_list}, found {len(bodies)}"
    return bodies[0]


def _decl(body: str, prop: str) -> str | None:
    m = re.search(rf"(?:^|[;\s]){re.escape(prop)}\s*:\s*([^;]+?)\s*(?:;|$)", body)
    return m.group(1) if m else None


def _outside_phone(css: str) -> str:
    """`css` with every phone @media block removed."""
    out, i = [], 0
    for m in re.finditer(r"@media([^{]*)\{", css):
        if m.start() < i or PHONE not in m.group(1):
            continue
        depth, j = 1, m.end()
        while depth and j < len(css):
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        out.append(css[i:m.start()])
        i = j
    out.append(css[i:])
    return "".join(out)


# ── 1. m-tap keeps buttons at the 44px floor ─────────────────────────

def test_m_tap_buttons_keep_the_44px_floor():
    body = _r1_phone_rule(["button.m-tap", ".b-btn.m-tap"])
    assert _decl(body, "min-height") == "44px !important"
    # Width is still m-tap's 40px minimum: the rule only restores the height.
    assert _decl(body, "min-width") is None


def test_the_shared_m_tap_rule_still_gives_links_40px():
    """Links and other non-button targets keep the R1 contract (40x40,
    12px glyph); test_dashboard_modes_render pins the same body."""
    body = _r1_phone_rule([".m-tap", ".dash-sec-toggle", ".dash-sec-hide"])
    assert _decl(body, "min-height") == "40px !important"
    assert _decl(body, "min-width") == "40px !important"


def test_the_button_rule_is_phone_only():
    assert "button.m-tap" not in _outside_phone(_strip(_raw()))
    assert ".b-btn.m-tap" not in _outside_phone(_strip(_raw()))


# ── 2. a.b-btn labels sit in the middle of their 44px box ─────────────

_POLISH_HEAD = "/* ═══ v1.9.0 polish A — shared phone-layer fixes"


def _polish_block() -> str:
    """The polish-A phone block: after its header, before R2 T1."""
    raw = _raw()
    assert raw.count(_POLISH_HEAD) == 1
    start, stop = raw.index(_POLISH_HEAD), raw.index(_R2_FIRST)
    assert start < stop, "the polish-A block belongs to the R1 layer, before R2 T1"
    blocks = _media_bodies(_strip(raw[start:stop]), PHONE)
    assert len(blocks) == 1, "one phone @media block"
    return blocks[0]


def _polish_rule(sel_list: list[str]) -> str:
    bodies = [body for sels, body in _rules(_polish_block()) if sels == sel_list]
    assert len(bodies) == 1, f"expected one polish-A rule for {sel_list}, found {len(bodies)}"
    return bodies[0]


def test_non_button_b_btn_labels_are_centred_without_a_display_change():
    body = _polish_rule([".b-btn:not(button)"])
    assert _decl(body, "align-content") == "center"
    # No display (block stays full width, inline-block keeps its width), no
    # sizes, no !important: a page section that makes one flex still wins.
    for prop in ("display", "width", "min-width", "height", "min-height", "justify-content"):
        assert _decl(body, prop) is None, prop
    assert "!important" not in body


def test_the_polish_block_is_phone_only():
    raw = _raw()
    block = _strip(raw[raw.index(_POLISH_HEAD):raw.index(_R2_FIRST)])
    assert _outside_phone(block).strip() == "", "every polish-A rule sits in the phone block"


# ── 3. toggleExpanded: aria-expanded and focus after Show all ─────────

_TOGGLE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const docListeners = {};
let focused = null;
function el(tag, opts) {
  opts = opts || {};
  const cls = new Set(opts.cls || []);
  const attrs = Object.assign({}, opts.attrs || {});
  const e = {
    tagName: tag, dataset: Object.assign({}, opts.dataset || {}), children: [], parent: null,
    classList: { contains: c => cls.has(c), add: c => cls.add(c), remove: c => cls.delete(c),
                 toggle: c => (cls.has(c) ? (cls.delete(c), false) : (cls.add(c), true)) },
    getAttribute: a => (a in attrs ? attrs[a] : null),
    setAttribute: (a, v) => { attrs[a] = String(v); },
    hasAttribute: a => a in attrs,
    attrs,
    // Rendered unless the stub says otherwise (a Show all hides once its target opens).
    getClientRects: () => (opts.hiddenWhen && opts.hiddenWhen() ? [] : [{}]),
    focus() { focused = e.name; },
    name: opts.name || tag,
  };
  e.matches = sel => {
    if (sel === '[data-click="toggleExpanded"]') return e.getAttribute('data-click') === 'toggleExpanded';
    if (sel.indexOf('[tabindex]') >= 0) return 'tabindex' in attrs || /^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(tag);
    if (sel[0] === '.') return cls.has(sel.slice(1));
    return false;
  };
  e.closest = sel => { for (let n = e; n; n = n.parent) if (n.matches(sel)) return n; return null; };
  e.querySelectorAll = sel => { const out = []; const walk = n => n.children.forEach(k => { if (k.matches(sel)) out.push(k); walk(k); }); walk(e); return out; };
  e.querySelector = sel => e.querySelectorAll(sel)[0] || null;
  e.add = k => { k.parent = e; e.children.push(k); return k; };
  return e;
}
const root = el('DIV', { name: 'root' });
const sandbox = {
  window: {}, console, Date,
  document: {
    querySelectorAll: sel => root.querySelectorAll(sel), querySelector: () => null,
    getElementById: () => null,
    body: { addEventListener() {} },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
  },
  localStorage: { getItem: () => null, setItem() {} },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const W = sandbox.window;
const fire = (type, ev) => (docListeners[type] || []).forEach(fn => fn(ev || {}));
const TE = { 'data-click': 'toggleExpanded' };
const out = {};

// A disclosure button whose card starts closed, and one whose card starts open.
const card = root.add(el('DIV', { cls: ['acct-char'] }));
const btn = card.add(el('BUTTON', { attrs: TE, dataset: { toggleTarget: '.acct-char' }, name: 'btn' }));
const openCard = root.add(el('DIV', { cls: ['acct-char', 'is-expanded'] }));
const openBtn = openCard.add(el('BUTTON', { attrs: TE, dataset: { toggleTarget: '.acct-char' } }));
// An m-row trigger (mSyncAria's), a plain div trigger, a role=button div.
const piRow = root.add(el('DIV', { cls: ['pi-row'] }));
const mrow = piRow.add(el('DIV', { cls: ['m-row'], attrs: TE, dataset: { toggleTarget: '.pi-row' } }));
const corp = root.add(el('DIV', { cls: ['ml-corp-card'] }));
const div = corp.add(el('DIV', { attrs: TE, dataset: { toggleTarget: '.ml-corp-card' } }));
const roleCard = root.add(el('DIV', { cls: ['x-card'] }));
const roleDiv = roleCard.add(el('DIV', { attrs: Object.assign({ role: 'button' }, TE), dataset: { toggleTarget: '.x-card' } }));

fire('DOMContentLoaded');
out.init = { btn: btn.attrs['aria-expanded'], open: openBtn.attrs['aria-expanded'],
             mrow: mrow.attrs['aria-expanded'] || null, div: div.attrs['aria-expanded'] || null,
             role: roleDiv.attrs['aria-expanded'] };
W.toggleExpanded.call(btn); out.afterOpen = btn.attrs['aria-expanded'];
W.toggleExpanded.call(btn); out.afterClose = btn.attrs['aria-expanded'];
W.toggleExpanded.call(mrow); W.toggleExpanded.call(div);
out.mrowAfter = mrow.attrs['aria-expanded'] || null;
out.divAfter = div.attrs['aria-expanded'] || null;
out.visibleTriggerKeepsFocus = focused;

// Show all over a 13-row clamp: rows are divs (no tabindex) except the 11th's own.
function clamp(n, rowTabindex) {
  const wrap = root.add(el('DIV', { cls: ['m-clamp-wrap'] }));
  const list = wrap.add(el('DIV', { cls: ['m-clamp'], name: 'list' }));
  for (let i = 0; i < n; i++) list.add(el('DIV', { name: 'row' + (i + 1), attrs: rowTabindex ? { tabindex: '0' } : {} }));
  const all = wrap.add(el('BUTTON', { cls: ['m-showall'], attrs: TE, dataset: { toggleTarget: '.m-clamp-wrap' },
                                     hiddenWhen: () => wrap.classList.contains('is-expanded') }));
  return { wrap, list, all };
}
focused = null;
const a = clamp(13, false);
W.toggleExpanded.call(a.all);
out.showAll = { focused, tabindex: a.list.children[10].attrs.tabindex, aria: a.all.attrs['aria-expanded'] };
focused = null;
const b = clamp(13, true);
W.toggleExpanded.call(b.all);
out.showAllMrows = { focused, tabindex: b.list.children[10].attrs.tabindex };
focused = null;
const c = clamp(10, false);
W.toggleExpanded.call(c.all);
out.showAllShort = { focused, tabindex: c.list.attrs.tabindex };
// Closing with a hidden trigger never moves focus.
focused = null;
W.toggleExpanded.call(a.all);
out.closeHidden = focused;

// htmx:afterSettle initialises triggers inside the swapped subtree only.
const swapped = root.add(el('DIV', { name: 'swapped' }));
const late = swapped.add(el('DIV', { cls: ['acct-char', 'is-expanded'] })).add(
  el('BUTTON', { attrs: TE, dataset: { toggleTarget: '.acct-char' } }));
const outside = root.add(el('DIV', { cls: ['acct-char'] })).add(
  el('BUTTON', { attrs: TE, dataset: { toggleTarget: '.acct-char' } }));
fire('htmx:afterSettle', { target: swapped });
out.settle = { late: late.attrs['aria-expanded'] || null, outside: outside.attrs['aria-expanded'] || null };
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def toggle(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("toggle") / "harness.js"
    harness.write_text(_TOGGLE_HARNESS)
    run = subprocess.run(["node", str(harness), ACTIONS], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_toggle_expanded_initialises_aria_on_load(toggle):
    """Templates needn't render aria-expanded: the load pass reads each
    target's starting state."""
    assert toggle["init"]["btn"] == "false"
    assert toggle["init"]["open"] == "true"
    assert toggle["init"]["role"] == "false"


def test_toggle_expanded_keeps_aria_in_step(toggle):
    assert toggle["afterOpen"] == "true"
    assert toggle["afterClose"] == "false"


def test_toggle_expanded_leaves_m_rows_and_plain_divs_alone(toggle):
    """m-rows are mSyncAria's (phone-only, removed on desktop); aria-expanded
    isn't allowed on a plain div with no role."""
    assert toggle["init"]["mrow"] is None and toggle["mrowAfter"] is None
    assert toggle["init"]["div"] is None and toggle["divAfter"] is None


def test_a_trigger_that_stays_visible_keeps_focus_where_it_is(toggle):
    assert toggle["visibleTriggerKeepsFocus"] is None


def test_show_all_moves_focus_to_the_first_revealed_row(toggle):
    assert toggle["showAll"] == {"focused": "row11", "tabindex": "-1", "aria": "true"}
    # A row that is already focusable (a phone m-row's tabindex 0) keeps it.
    assert toggle["showAllMrows"] == {"focused": "row11", "tabindex": "0"}


def test_show_all_with_nothing_past_ten_focuses_the_list(toggle):
    assert toggle["showAllShort"] == {"focused": "list", "tabindex": "-1"}


def test_closing_never_moves_focus(toggle):
    assert toggle["closeHidden"] is None


def test_htmx_settle_initialises_the_swapped_subtree_only(toggle):
    assert toggle["settle"] == {"late": "true", "outside": None}


# ── 4. entity-link chips are tap targets on phones ────────────────────

def test_entity_link_chips_are_40px_targets():
    body = _polish_rule([".el-chip"])
    for prop, value in (("display", "inline-flex"), ("align-items", "center"),
                        ("justify-content", "center"), ("min-height", "40px"),
                        ("min-width", "40px"), ("font-size", "12px")):
        assert _decl(body, prop) == value, prop
    # The is-ext arrow is a flex item now; the gap keeps the space before it.
    assert _decl(body, "gap") == "0.3em"


def test_entity_link_chips_are_8px_apart():
    assert _decl(_polish_rule([".el-chips"]), "gap") == "8px"


def test_entity_link_chip_desktop_rule_is_untouched():
    """Desktop keeps the small muted chip: the base rule is outside every
    media query and still 10px with 2px 7px padding."""
    base = [body for sels, body in _rules(_outside_phone(_strip(_raw()))) if sels == [".el-chip"]]
    assert len(base) == 1
    assert _decl(base[0], "font-size") == "10px"
    assert _decl(base[0], "padding") == "2px 7px"


# ── 5. drawn switches and checkboxes keep their size ──────────────────

def test_drawn_switches_and_checks_are_exempt_from_the_44px_input_height():
    assert _decl(_polish_rule(["input.b-switch", "input.b-check"]), "min-height") == "0"


def test_their_labels_keep_the_44px_tap_target():
    body = _polish_rule(["label:has(> input.b-switch)", "label:has(> input.b-check)"])
    assert _decl(body, "min-height") == "44px"


def test_native_checkboxes_and_radios_keep_the_44px_box():
    """Only the drawn controls are exempt. A blanket input[type=checkbox] /
    [type=radio] exemption would drop labels whose only height is that box
    (notification settings, the dashboard column picker, the D-Scan dedup
    toggle) under the 40px floor; pages that size their own natives (R3 T3,
    R3 T5, R6 T2, R6 T5) already do it in their sections."""
    block = _polish_block()
    assert not re.search(r"\[type=[\"']?(?:checkbox|radio)", block)
    r1 = "\n".join(_media_bodies(_r1_layer(), PHONE))
    assert not re.search(r"input\[type=[\"']?(?:checkbox|radio)[^{]*\{[^}]*min-height:\s*0", r1)
