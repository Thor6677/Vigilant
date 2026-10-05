"""Mobile R4 T2: the LP Store at phone width (≤640px).

Offers (D5 A): each `.lp-row` is an expand-on-tap row. Key 1 is the offer
name, key 2 its ISK/LP (accent); opened, it reads Name (a phone-only full
copy), Qty, LP cost, ISK cost, Materials and Unit sell. Past 10 offers a
phone-only "Show all N" button shows the rest. The header row is m-head.

Corp picker (D6 A): on phones a pick folds the faction tree to one 44px
"Picked" line, a button that unfolds it again; the Offers heading names the
corp, and the offers scroll into view once they have loaded. All of it runs
in market_lp.html's own script, gated on the 640px media query.

Tree rows: the faction and corp buttons are 44px tap targets with 12px text.
The global phone rule `button { min-height: 44px }` already made them that
tall; this section pins it and leaves `display` alone, because the filter
hides buttons with an inline display:none.

Desktop renders as before (D21): the added cells, button and heading name
are m-only, the clamp wrappers are unstyled, and every new class is styled
only in this task's phone block. Names and ids are invented."""
import functools
import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from app.routes import market as market_mod
from tests._mobile import (SITE_CSS, VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, norm, phone_block, render_page, row_keys,
                           row_labelled, row_lead, rule_bodies)

_section = functools.partial(css_section, release="R4")

_LABELS = ["Name", "Qty", "LP cost", "ISK cost", "Materials", "Unit sell"]
_DESKTOP_CELLS = ["lp-col-item"] + ["lp-col-num"] * 5 + ["lp-col-ratio"]


# ── Fixtures ──────────────────────────────────────────────────────────

def _offers(n):
    """`n` offers in the route's display shape (`_display_row`), formatted
    with the route's own formatters, best ISK/LP first. Every fifth offer is
    unpriced, as an offer needing an unpriced item renders."""
    out = []
    for i in range(n):
        priced = i % 5 != 4
        isk_per_lp = 2400.0 - 97.5 * i if priced else None
        out.append({
            "item_name": f"Sample Navy Item {i + 1:02d} With A Long Descriptive Name",
            "quantity_str": market_mod._fmt_qty(1 + i * 5),
            "lp_cost_str": market_mod._fmt_qty(4000 + 250 * i),
            "isk_cost_str": market_mod._fmt_price(1_250_000.0 + 10_000 * i),
            "materials_cost_str": market_mod._fmt_price(None if i % 3 else 640_000.0),
            "unit_price_str": market_mod._fmt_price(9_800_000.0 + 125_000 * i if priced else None),
            "isk_per_lp_str": market_mod._fmt_isk_per_lp(isk_per_lp),
            "priced": priced,
        })
    return out


def _render_offers(n=14):
    rows = _offers(n)
    html = render_page(market_mod, "partials/market_lp_offers.html", "/market/lp/offers",
                       corporation_id=1000001, rows=rows)
    return html, rows


def _paired(html, rows):
    """Each rendered m-row with the offer it renders; one row per offer."""
    parsed = cells_rows(html)
    assert len(parsed) == len(rows), f"{len(parsed)} m-rows for {len(rows)} offers"
    return list(zip(parsed, rows))


_FACTIONS = [
    {"faction_name": "Sample Federation", "corps": [
        {"corporation_id": 1000001, "name": "Sample Navy Corp"},
        {"corporation_id": 1000002, "name": "Sample Trade Guild"},
        {"corporation_id": 1000003, "name": "Sample Research Bureau"},
    ]},
    {"faction_name": "Other", "corps": [
        {"corporation_id": 1000009, "name": "Sample Unaligned Traders"},
    ]},
]


def _render_tree():
    return render_page(market_mod, "partials/lp_corp_tree.html", "/market/lp/corps-tree",
                       factions=_FACTIONS, degraded=False, note=None)


def _render_page():
    return render_page(market_mod, "market_lp.html", "/market/lp")


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def iter(self):
        yield self
        for c in self.children:
            yield from c.iter()

    def all_text(self):
        return self.text + "".join(c.all_text() for c in self.children)


class _Tree(HTMLParser):
    """A minimal element tree: tag, attrs, children and own text per node."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _tree(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.root


def _by_id(root, ident):
    found = [n for n in root.iter() if n.attrs.get("id") == ident]
    assert len(found) == 1, f"expected one #{ident}, found {len(found)}"
    return found[0]


def _flat(s):
    return re.sub(r"\s+", " ", s).strip()


# ── Offers: rows ──────────────────────────────────────────────────────

def test_offers_are_tap_to_open_rows():
    html, rows = _render_offers(14)
    parsed = assert_mrow(html, min_rows=14)
    assert len(parsed) == 14
    for r in parsed:
        assert r["attrs"].get("data-click") == "toggleMRow"
        cls = r["attrs"]["class"].split()
        assert "lp-row" in cls, "the desktop row class (and its styles) stays"
        assert "m-row--link" not in cls
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not row_lead(r)


def test_offer_keys_are_the_name_and_isk_per_lp():
    html, rows = _render_offers(14)
    for row, offer in _paired(html, rows):
        k1, k2 = row_keys(row)
        assert k1["text"] == offer["item_name"]
        assert "lp-col-item" in k1["attrs"]["class"].split()
        assert k2["text"] == offer["isk_per_lp_str"]
        assert "lp-col-ratio" in k2["attrs"]["class"].split(), "key 2 keeps the accent ISK/LP cell"


def test_opened_offer_starts_with_a_full_name_line_then_the_costs():
    html, rows = _render_offers(14)
    for row, offer in _paired(html, rows):
        labelled = row_labelled(row)
        assert list(labelled) == _LABELS
        name = labelled["Name"]
        assert name["text"] == offer["item_name"]
        assert "m-only" in name["attrs"]["class"].split(), "the Name line is phone-only"
        assert "lp-col-item" not in name["attrs"]["class"].split(), "a plain copy, not the ellipsised cell"
        assert labelled["Qty"]["text"] == offer["quantity_str"]
        assert labelled["LP cost"]["text"] == offer["lp_cost_str"]
        assert labelled["ISK cost"]["text"] == offer["isk_cost_str"]
        assert labelled["Materials"]["text"] == offer["materials_cost_str"]
        assert labelled["Unit sell"]["text"] == offer["unit_price_str"]
        untagged = [c for c in row["cells"]
                    if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]
        assert not untagged, "every offer cell shows on phones"


def test_offer_rows_keep_their_desktop_cells_in_order():
    """Desktop sameness: without the phone-only Name copy, each row is the
    seven desktop cells it always was, in order, and the item cell keeps
    its hover title."""
    html, rows = _render_offers(14)
    for row, offer in _paired(html, rows):
        desk = [c for c in row["cells"] if "m-only" not in c["attrs"].get("class", "").split()]
        assert [c["attrs"]["class"] for c in desk] == _DESKTOP_CELLS
        assert all(c["tag"] == "span" for c in row["cells"])
        assert desk[0]["attrs"].get("title") == offer["item_name"]
        assert [c for c in row["cells"] if "m-only" in c["attrs"].get("class", "").split()] == \
            [row_labelled(row)["Name"]], "the Name copy is the only phone-only cell"


def test_unpriced_offers_stay_dimmed():
    html, rows = _render_offers(14)
    seen = 0
    for row, offer in _paired(html, rows):
        dimmed = "lp-unpriced" in row["attrs"]["class"].split()
        assert dimmed == (not offer["priced"])
        if not offer["priced"]:
            seen += 1
            assert row_keys(row)[1]["text"] == "—"
    assert seen == 2


def test_header_row_is_m_head():
    html, _ = _render_offers(14)
    root = _tree(html)
    heads = [n for n in root.iter() if "lp-head" in n.classes]
    assert len(heads) == 1
    head = heads[0]
    assert head.classes == ["lp-row", "lp-head", "m-head"]
    assert "m-row" not in head.classes
    assert [norm(c.all_text()) for c in head.children] == \
        ["Item", "Qty", "LP cost", "ISK cost", "Materials", "Unit sell", "ISK/LP"]
    assert not any("m-clamp" in a.classes for a in _ancestors(head)), (
        "the header stays outside the clamp, so it never counts as one of the 10")


def _ancestors(n):
    n = n.parent
    while n is not None:
        yield n
        n = n.parent


# ── Offers: Show all past 10 ──────────────────────────────────────────

@pytest.mark.parametrize("n", [14, 11])
def test_show_all_past_ten(n):
    html, _ = _render_offers(n)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    assert [k["children"] for k in c.clamps] == [n], "every offer row sits in the one clamp"
    assert len(c.showall) == 1
    btn = c.showall[0]
    assert btn["text"] == f"Show all {n}"
    assert btn["in_wrap"]
    a = btn["attrs"]
    assert a.get("type") == "button"
    assert a.get("data-click") == "toggleExpanded"
    assert a.get("data-toggle-target") == ".m-clamp-wrap"
    assert {"m-only", "m-showall"} <= set(a["class"].split())


@pytest.mark.parametrize("n", [10, 3])
def test_no_show_all_at_ten_or_fewer(n):
    html, _ = _render_offers(n)
    c = clamps(html)
    assert c.showall == []
    assert [k["children"] for k in c.clamps] == [n]
    assert len(assert_mrow(html, min_rows=n)) == n


def test_last_offer_is_the_last_child_of_the_clamp():
    """`.lp-row:last-child { border-bottom: none }` (the page's own style)
    must still reach the last offer, as it did when the rows were the
    panel's last children."""
    html, _ = _render_offers(14)
    root = _tree(html)
    clamp = [n for n in root.iter() if "m-clamp" in n.classes][0]
    assert all("m-row" in k.classes for k in clamp.children)
    assert len(clamp.children) == 14


def test_no_offers_renders_the_empty_message_alone():
    html = render_page(market_mod, "partials/market_lp_offers.html", "/market/lp/offers",
                       corporation_id=1000001, rows=[])
    assert "has no LP store offers" in html
    assert "m-row" not in html and "m-clamp" not in html and "m-head" not in html


# ── Corp tree ─────────────────────────────────────────────────────────

def test_tree_buttons_keep_their_hooks_for_the_44px_rule_and_the_filter():
    """The phone rule keys on `#lp-corp-tree .lpt-fac` / `.lpt-corp`, and
    the page script on `.lpt-fac[data-fac]` / `.lpt-corp[data-corp-id]`.
    No m-tap and no inline display: m-tap's `display: inline-flex
    !important` would beat the filter's inline display:none, and an inline
    display would beat the phone rule's cascade."""
    root = _tree(_render_tree())
    buttons = [n for n in root.iter() if n.tag == "button"]
    facs = [b for b in buttons if "lpt-fac" in b.classes]
    corps = [b for b in buttons if "lpt-corp" in b.classes]
    assert len(facs) == 2 and len(corps) == 4
    assert len(buttons) == len(facs) + len(corps)
    assert [b.attrs.get("data-fac") for b in facs] == ["1", "2"]
    assert [b.attrs.get("data-corp-id") for b in corps] == ["1000001", "1000002", "1000003", "1000009"]
    for b in buttons:
        assert b.attrs.get("type") == "button"
        assert "m-tap" not in b.classes
        assert "display" not in b.attrs.get("style", "")
    # The tree's own expand logic: each faction's corps start collapsed inline.
    groups = [n for n in root.iter() if "lpt-corps" in n.classes]
    assert [g.attrs.get("data-fac") for g in groups] == ["1", "2"]
    assert all(g.attrs.get("style", "").replace(" ", "") == "display:none;" for g in groups)


# ── Page: the Picked line and the Offers heading ──────────────────────

def test_corp_section_has_a_phone_only_picked_line_outside_the_folding_body():
    root = _tree(_render_page())
    sec = _by_id(root, "lp-corp-sec")
    assert {"b-section", "lp-corp-sec"} <= set(sec.classes)
    head, picked, body = sec.children
    assert "b-section-head" in head.classes and norm(head.all_text()) == "Corporation"
    # The one-line button that unfolds the tree.
    assert picked.tag == "button" and picked.attrs.get("id") == "lp-picked"
    assert picked.attrs.get("type") == "button"
    assert {"m-only", "lp-picked"} <= set(picked.classes)
    assert picked.attrs.get("aria-controls") == "lp-corp-body"
    assert picked.attrs.get("aria-expanded") == "false", "it only shows while the tree is folded"
    assert "data-click" not in picked.attrs, "the page script owns this button"
    label, name = picked.children
    assert "lp-picked-label" in label.classes and norm(label.all_text()) == "Picked"
    assert name.attrs.get("id") == "lp-picked-name" and name.all_text() == ""
    # The body that folds keeps every control the picker had.
    assert body.attrs.get("id") == "lp-corp-body" and "lp-corp-body" in body.classes
    assert body.attrs.get("style") == "padding:0.75rem;"
    ids = [n.attrs.get("id") for n in body.iter() if n.attrs.get("id")]
    assert ids[1:] == ["lp-corp-id", "lp-corp-filter", "lp-corp-tree"]


def test_offers_heading_has_an_empty_phone_only_corp_name():
    """No whitespace inside the span: its separator is drawn with
    `:not(:empty)::before`, so it must be truly empty until a pick."""
    html = _render_page()
    assert ('<span class="b-label">Offers<span class="m-only lp-offers-corp" '
            'id="lp-offers-corp"></span></span>') in html
    root = _tree(html)
    sec = _by_id(root, "lp-offers-sec")
    assert "b-section" in sec.classes
    assert sec.attrs.get("style") == "margin-top:0.75rem;"
    assert _by_id(root, "lp-offers-panel") in list(sec.iter())


def _page_script(html=None):
    root = _tree(html or _render_page())
    scripts = [n for n in root.iter() if n.tag == "script" and "lp-corp-tree" in n.text]
    assert len(scripts) == 1
    return scripts[0].text


def test_script_reads_the_640px_query_and_keeps_its_hooks():
    """Stable tokens only. The behaviour (the fold, focus, scroll and
    heading, and that only the 640px query turns them on: the stub's
    matchMedia matches nothing else) runs against a stub DOM below."""
    js = _flat(_page_script())
    assert "'(max-width: 640px)'" in js
    assert "'htmx:afterSettle'" in js
    # Every hook, old and new, is still read.
    for hook in (".lpt-fac", ".lpt-corp", "data-corp-id", "data-fac", "'lp-corp-id'",
                 "inp.dispatchEvent(new Event('change'))", "style.display", "'lp-offers-panel'",
                 "'lp-corp-sec'", "'lp-picked'", "'lp-picked-name'", "'lp-offers-corp'", "'lp-offers-sec'"):
        assert hook in js, hook
    assert "onclick" not in _render_page()


_PICKER_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

const state = { phone: false };
const window = { matchMedia: q => ({ media: q,
  get matches() { return q === '(max-width: 640px)' && state.phone; } }) };
class Event { constructor(type) { this.type = type; this.target = null; } }

const byId = {};
const log = { changes: [], scrolls: [], focus: [] };
function matches(n, sel) {
  const cls = sel.match(/\.[\w-]+/g) || [];
  const attrs = [...sel.matchAll(/\[([\w-]+)="([^"]*)"\]/g)];
  return cls.every(c => n.cls.has(c.slice(1))) && attrs.every(a => n.attrs[a[1]] === a[2]);
}
function el(id, cls, attrs, kids) {
  const n = { id, cls: new Set(cls || []), attrs: attrs || {}, style: {}, value: '', textContent: '',
    children: kids || [], parentNode: null, ls: {},
    addEventListener(t, fn) { (this.ls[t] = this.ls[t] || []).push(fn); },
    dispatchEvent(ev) {
      if (!ev.target) ev.target = this;
      for (let p = this; p; p = p.parentNode) (p.ls[ev.type] || []).forEach(fn => fn.call(p, ev));
      return true;
    },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    closest(sel) { for (let p = this; p; p = p.parentNode) if (matches(p, sel)) return p; return null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = p => p.children.forEach(c => { if (matches(c, sel)) out.push(c); walk(c); });
      walk(this);
      return { forEach: fn => out.forEach(fn), length: out.length, 0: out[0] };
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    getClientRects() { return this.parentNode && this.parentNode.style.display === 'none' ? [] : [{}]; },
    // Third field: was the tree folded? The Picked line only shows (so can
    // only take focus) once it is.
    focus(o) { log.focus.push([this.id, o || null, sec.cls.has('is-folded')]); },
    scrollIntoView(o) { log.scrolls.push([this.id, o || null]); },
  };
  n.classList = { add: c => n.cls.add(c), remove: c => n.cls.delete(c), contains: c => n.cls.has(c),
    toggle: (c, f) => { if (f === undefined) f = !n.cls.has(c); if (f) n.cls.add(c); else n.cls.delete(c); return f; } };
  n.children.forEach(c => { c.parentNode = n; });
  if (id) byId[id] = n;
  return n;
}
const fac = el('fac1', ['lpt-fac'], { 'data-fac': '1' });
fac.firstChild = { nodeType: 3, textContent: '▸ Sample Federation ' };
const corpA = el('corpA', ['lpt-corp'], { 'data-corp-id': '1000001' });
corpA.textContent = 'Sample Navy Corp';
const corpB = el('corpB', ['lpt-corp'], { 'data-corp-id': '1000002' });
corpB.textContent = ' Sample Trade Guild ';
const corpC = el('corpC', ['lpt-corp'], { 'data-corp-id': '1000003' });
corpC.textContent = 'Sample Research Bureau';
const corps = el('corps1', ['lpt-corps'], { 'data-fac': '1' }, [corpA, corpB, corpC]);
corps.style.display = 'none';
const tree = el('lp-corp-tree', [], {}, [fac, corps]);
const input = el('lp-corp-id');
input.addEventListener('change', () => log.changes.push(input.value));
const body = el('lp-corp-body', ['lp-corp-body'], {}, [input, el('lp-corp-filter'), tree]);
const picked = el('lp-picked', ['m-only', 'lp-picked'], {}, [el('lp-picked-name')]);
const sec = el('lp-corp-sec', ['b-section', 'lp-corp-sec'], {}, [el('', ['b-section-head']), picked, body]);
const rowInPanel = el('offer-row', ['lp-row', 'm-row']);
const panel = el('lp-offers-panel', [], {}, [rowInPanel]);
el('lp-offers-sec', ['b-section'], {}, [el('lp-offers-corp', ['m-only', 'lp-offers-corp']), panel]);
const document = { getElementById: id => byId[id] || null, addEventListener() {}, body: { addEventListener() {} } };

vm.runInContext(src, vm.createContext({ window, document, Event, console }));

const click = n => n.dispatchEvent({ type: 'click', target: n });
// htmx 1.9.12's afterSettle detail carries both the request's parameters and
// the XHR. how = 'url' or 'params' sends only that one, so each way of
// telling which corp a settle was for is exercised on its own.
const settle = (id, how, from) => (from || panel).dispatchEvent({ type: 'htmx:afterSettle', target: from || panel,
  detail: Object.assign({},
    how === 'params' ? {} : { xhr: { responseURL: 'http://x/market/lp/offers?corporation_id=' + id } },
    how === 'url' ? {} : { requestConfig: { parameters: { corporation_id: id } } }) });
const snap = () => ({ folded: sec.cls.has('is-folded'), changes: log.changes.slice(),
  scrolls: log.scrolls.length, focus: log.focus.length, lastFocus: log.focus[log.focus.length - 1] || null,
  value: input.value, selected: [corpA, corpB, corpC].filter(c => c.cls.has('is-selected')).map(c => c.id),
  pickedName: byId['lp-picked-name'].textContent, offersCorp: byId['lp-offers-corp'].textContent });
const out = {};

click(fac);                                  // the tree's own expand logic
out.facOpen = corps.style.display;

click(corpA); out.deskPick = snap();         // desktop: today's behaviour only
settle('1000001'); out.deskSettle = snap();

state.phone = true;
click(corpB); out.phonePick = snap();        // phone: fold, focus the Picked line, wait for the offers
// The same corp's settle, bubbling up from a node inside the panel.
rowInPanel.dispatchEvent({ type: 'htmx:afterSettle', target: rowInPanel,
  detail: { xhr: { responseURL: 'http://x/market/lp/offers?corporation_id=1000002' },
            requestConfig: { parameters: { corporation_id: '1000002' } } } });
out.childSettle = snap();
settle('1000002'); out.phoneSettle = snap(); out.scrollTo = log.scrolls[0];
settle('1000002'); out.secondSettle = snap(); // one scroll per pick

click(picked); out.unfold = snap();

// What htmx does: C is slow, so A's request queues behind it and C's
// offers settle first, while A is the current pick.
click(corpC); click(picked); click(corpA);
out.queuedPick = snap();
settle('1000003'); out.queuedFirst = snap();
settle('1000001', 'url'); out.queuedLast = snap();

// Synthetic (htmx's queue can't do it): slow C lands last, after B's offers
// and while a newer pick, A, is still waiting for its own.
click(picked); click(corpC);
click(picked); click(corpB); settle('1000002'); out.lateB = snap();
click(picked); click(corpA); out.lateAPick = snap();
settle('1000003'); out.lateC = snap();
settle('1000001', 'params'); out.lateA = snap();

click(picked);
click(corpB); click(picked); settle('1000002'); // unfolded before the offers came: no scroll
out.unfoldFirst = snap();

click(corpA); state.phone = false; settle('1000001'); // crossed to desktop meanwhile: no scroll
out.crossed = snap();
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def picker_run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("lp_picker")
    script = d / "page.js"
    script.write_text(_page_script())
    harness = d / "harness.js"
    harness.write_text(_PICKER_HARNESS)
    run = subprocess.run(["node", str(harness), str(script)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_picker_desktop_behaviour_is_unchanged(picker_run):
    """Behaviour, with the page script run against a stub DOM: on desktop
    a pick selects the corp and loads its offers exactly as before; nothing
    folds, takes focus or scrolls. The faction button still opens its
    corps. (The hidden phone-only heading name is still kept current.)"""
    assert picker_run["facOpen"] == "block"
    pick = picker_run["deskPick"]
    assert pick["value"] == "1000001" and pick["changes"] == ["1000001"]
    assert pick["selected"] == ["corpA"]
    assert pick["folded"] is False
    assert pick["focus"] == 0, "a desktop pick leaves focus where it was"
    settled = picker_run["deskSettle"]
    assert settled["scrolls"] == 0 and settled["folded"] is False and settled["focus"] == 0
    assert settled["offersCorp"] == "Sample Navy Corp"


def test_picker_folds_focuses_names_and_scrolls_on_phones(picker_run):
    pick = picker_run["phonePick"]
    assert pick["changes"] == ["1000001", "1000002"] and pick["selected"] == ["corpB"]
    assert pick["folded"] is True
    assert pick["pickedName"] == "Sample Trade Guild"
    assert pick["lastFocus"] == ["lp-picked", {"preventScroll": True}, True], (
        "the tapped corp just hid with the tree: focus moves to the Picked line, once it shows")
    assert pick["focus"] == 1
    assert pick["offersCorp"] == "Sample Navy Corp", "the heading changes with the offers, not before"
    assert pick["scrolls"] == 0, "the scroll waits for the offers"
    settled = picker_run["phoneSettle"]
    assert settled["scrolls"] == 1
    assert settled["offersCorp"] == "Sample Trade Guild"
    target, opts = picker_run["scrollTo"]
    assert target == "lp-offers-sec" and opts["block"] == "start"
    assert picker_run["secondSettle"]["scrolls"] == 1, "a later swap doesn't scroll again"


def test_a_settle_bubbling_up_from_inside_the_panel_is_ignored(picker_run):
    """The current pick's own settle, fired on a node inside the panel and
    bubbling up: the `e.target !== this` guard drops it."""
    child = picker_run["childSettle"]
    assert child["scrolls"] == 0
    assert child["offersCorp"] == "Sample Navy Corp"
    assert picker_run["phoneSettle"]["scrolls"] == 1, "the panel's own settle still scrolls"


def test_a_queued_earlier_pick_neither_scrolls_nor_names_the_corp(picker_run):
    """Pick a slow corp, then another: htmx queues the second request, so
    the first one's offers settle first. Only the latest pick's settle
    names the corp in the heading and scrolls; the response URL alone is
    enough to tell them apart."""
    before = picker_run["unfold"]["scrolls"]
    assert picker_run["queuedPick"]["pickedName"] == "Sample Navy Corp"
    first = picker_run["queuedFirst"]
    assert first["scrolls"] == before, "the slow pick's offers don't scroll"
    assert first["offersCorp"] == "Sample Trade Guild", "nor rename the heading"
    last = picker_run["queuedLast"]
    assert last["scrolls"] == before + 1
    assert last["offersCorp"] == "Sample Navy Corp"


def test_a_slow_pick_landing_last_neither_scrolls_nor_names_the_corp(picker_run):
    """Slow pick C, then B, whose offers land; then pick A. C's offers
    land now, while A is waiting: they mustn't scroll or rename the
    heading. A's settle, with only the request parameters to go on,
    still does both."""
    b = picker_run["lateB"]
    assert b["offersCorp"] == "Sample Trade Guild"
    a_pick = picker_run["lateAPick"]
    assert a_pick["folded"] is True and a_pick["scrolls"] == b["scrolls"]
    c = picker_run["lateC"]
    assert c["scrolls"] == b["scrolls"], "the stale settle doesn't scroll"
    assert c["offersCorp"] == "Sample Trade Guild", "nor rename the heading"
    a = picker_run["lateA"]
    assert a["scrolls"] == b["scrolls"] + 1
    assert a["offersCorp"] == "Sample Navy Corp"


def test_picked_line_unfolds_the_tree(picker_run):
    un = picker_run["unfold"]
    assert un["folded"] is False
    assert un["lastFocus"] == ["corpB", {"preventScroll": True}, False], (
        "focus moves to the picked corp, not to the page")
    before = picker_run["lateA"]["scrolls"]
    first = picker_run["unfoldFirst"]
    assert first["folded"] is False
    assert first["scrolls"] == before, "unfolding before the offers arrive cancels the scroll"
    assert first["offersCorp"] == "Sample Trade Guild", "the heading still follows the offers"
    crossed = picker_run["crossed"]
    assert crossed["scrolls"] == before, "no scroll once the viewport is desktop-wide"
    assert crossed["offersCorp"] == "Sample Navy Corp"


# ── CSS ───────────────────────────────────────────────────────────────

def _phone():
    return phone_block(_section("T2"))[0]


def test_section_is_one_phone_block_and_nothing_else():
    phone, after = phone_block(_section("T2"))
    assert phone.strip(), "the R4 T2 phone block is empty"
    assert after.strip() == "", f"rules after the phone block: {after.strip()[:80]!r}"
    assert "@media" not in phone


def test_css_tree_rows_are_44px_tap_targets_without_touching_display():
    phone = _phone()
    for sel in ("#lp-corp-tree .lpt-fac", "#lp-corp-tree .lpt-corp"):
        body = _flat(rule_bodies(phone, sel))
        assert "min-height: 44px" in body, sel
        assert "font-size: 12px" in body, sel
        assert "display" not in body, f"{sel}: the filter's inline display:none must keep working"


def test_css_picked_line_shows_only_while_folded():
    phone = _phone()
    assert _flat(rule_bodies(phone, ".lp-picked")) == "display: none;"
    assert "display: none" in rule_bodies(phone, ".lp-corp-sec.is-folded > .lp-corp-body")
    line = _flat(rule_bodies(phone, ".lp-corp-sec.is-folded > .lp-picked"))
    assert "display: flex" in line
    assert "min-height: 44px" in line
    assert "width: 100%" in line
    name = _flat(rule_bodies(phone, ".lp-picked > .lp-picked-name"))
    assert "min-width: 0" in name and "text-overflow: ellipsis" in name and "white-space: nowrap" in name


def test_css_offers_heading_and_scroll_stop():
    phone = _phone()
    sep = _flat(rule_bodies(phone, ".lp-offers-corp:not(:empty)::before"))
    assert "content:" in sep
    m = re.search(r"scroll-margin-top: (\d+)px", rule_bodies(phone, "#lp-offers-sec"))
    assert m and int(m.group(1)) >= 47, "clears the sticky 46px nav and its border"


def test_css_offer_keys_are_12px():
    body = _flat(rule_bodies(_phone(), '.lp-row.m-row > [data-m="key"]'))
    assert "font-size: 12px" in body


_HOOKS = (".lp-picked", ".lp-picked-label", ".lp-picked-name", ".lp-corp-sec", ".lp-corp-body",
          ".lp-offers-corp", "#lp-offers-sec", "#lp-corp-tree")


def test_new_hooks_are_styled_only_in_this_phone_block():
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    phone = _phone()

    def count(hook, text):
        return len(re.findall(re.escape(hook) + r"(?![\w-])", text))

    for hook in _HOOKS:
        assert count(hook, phone), hook
        assert count(hook, css) == count(hook, phone), f"{hook} is styled outside the R4 T2 phone block"
