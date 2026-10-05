"""Mobile R6 T2: Structure Timers at phone width (mobile design §4.3, §7;
user decisions D3 A, D4 A, D5 A).

- Active timers are m-rows (D3 A). The lead is a short phase code (S, A, H,
  AN, UN), key 1 the structure name and key 2 the live countdown: a
  phone-only .timer-countdown copy that the page's 1s ticker already
  reaches. The open row lists Name, System, Type, Phase, Priority, EVE time,
  Local, Owner, Notes and Source (when present), then Actions.
- Actions sit inside the opened row (D4 A): Edit, Discord and × at the
  phone layer's 44px floor. The delete keeps its form and data-confirm.
- The filter and Copy for Discord hide and skip rows through the `hidden`
  property. The phone grid's display:grid !important beats an inline
  display:none, so the old style.display writes would stop filtering on
  phones. One !important rule hides a [hidden] row at every width.
- Archived timers are m-rows keyed on name and a short MM-DD date (D5 A),
  clamped to 10 with "Show all N".
- The Add Timer, Edit and ACL forms stack to one column with 16px fields.
  The small buttons become tap targets through the page's own `st-tap`, not
  the shared `m-tap`: m-tap forces 40px !important, which would shrink a
  button below the 44px every button and .b-btn already gets on phones.

Desktop must render exactly as before (D21): every phone-only cell is
m-only, the desktop blocks keep their markup, and every new hook class has
rules only in the phone block. The site-wide timer banner is not this
page's: no rule here may reach it.

Contexts follow the shapes app/routes/structure_timers.py builds. Names and
ids are invented."""
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from app.routes import structure_timers as st_mod
from tests._mobile import (SITE_CSS, VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, norm, phone_block, render_page, row_keys,
                           row_labelled, row_lead, rule_bodies, selectors, source)

_NS = types.SimpleNamespace
_section = functools.partial(css_section, release="R6")

_BASE = datetime(2030, 1, 2, 3, 4, 5)
_NOW = datetime(2030, 1, 1, 0, 0, 0)   # the Node harness's clock (UTC)


def _timer(tid, name, disposition, phase, priority, expires, *, structure_type="fortizar",
           system="Sample-01", region="Sample Region", owner="Sample Hostile Corp",
           notes=None, source_="manual", created_by=1):
    return _NS(id=tid, structure_name=name, structure_type=structure_type, system_name=system,
               region_name=region, owner_name=owner, disposition=disposition,
               timer_phase=phase, priority=priority, timer_expires=expires, notes=notes,
               source=source_, created_by=created_by, acl_group_id=None)


# 1: hostile, critical, the viewer's own. 2: friendly, no region, the
# viewer's own. 3: someone else's, with notes and the ESI tag, inside the
# hour on the harness clock.
ACTIVE = [
    _timer(11, "Sample Fortizar Alpha", "hostile", "shield", "critical", _BASE),
    _timer(12, "Sample Raitaru Beta", "friendly", "armor", "normal", _BASE + timedelta(hours=2),
           structure_type="raitaru", system="Sample-02", region=None, owner="Sample Friendly Corp"),
    _timer(13, "Sample Athanor Gamma", "hostile", "anchoring", "low", _NOW + timedelta(minutes=30),
           structure_type="athanor", system="Sample-03", notes="Sample note text",
           source_="esi", created_by=2),
]
ARCHIVED = [
    _timer(100 + i, f"Sample Archived {i:02d}", "hostile", "hull", "normal",
           datetime(2029, 12, 1 + i, 18, 30))
    for i in range(1, 13)
]
GROUP = _NS(id=7, name="Sample Group", entries=[
    _NS(id=3, entry_type="corporation", eve_id=98000001, name="Sample Corp")])


def _render(active=ACTIVE, archived=ARCHIVED, *, is_privileged=False, user_id=1, groups=(GROUP,)):
    return render_page(st_mod, "structure_timers.html", "/structure-timers",
                       active_timers=list(active), archived_timers=list(archived),
                       acl_groups=list(groups), user_id=user_id, is_privileged=is_privileged)


def _cls(attrs):
    return attrs.get("class", "").split()


def _active_rows(html):
    return [r for r in cells_rows(html) if "timer-row" in _cls(r["attrs"])]


def _archived_rows(html):
    return [r for r in cells_rows(html) if "st-arch" in _cls(r["attrs"])]


# ── active rows (D3 A) ────────────────────────────────────────────────


def test_both_lists_follow_the_m_row_contract():
    html = _render()
    rows = assert_mrow(html, 15)
    assert len(rows) == 15
    assert len(_active_rows(html)) == 3
    assert len(_archived_rows(html)) == 12
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
    for r in cells_rows(html):
        assert_single_value_child(r)


def test_active_row_keys_are_the_name_then_the_live_countdown():
    for row, t in zip(_active_rows(_render()), ACTIVE):
        k1, k2 = row_keys(row)
        assert k1["text"] == t.structure_name
        assert "m-only" in _cls(k1["attrs"])
        # key 2 is a .timer-countdown copy on the row's own expiry, which is
        # what the ticker selects and reads.
        assert "m-only" in _cls(k2["attrs"])
        assert "timer-countdown" in _cls(k2["attrs"])
        assert k2["attrs"]["data-expires"] == row["attrs"]["data-expires"] == t.timer_expires.isoformat()
        assert k2["text"] == ""   # filled by the ticker


@pytest.mark.parametrize("phase, code", [("shield", "S"), ("armor", "A"), ("hull", "H"),
                                         ("anchoring", "AN"), ("unanchoring", "UN")])
def test_active_row_lead_is_a_short_phase_code(phase, code):
    t = _timer(21, "Sample Keepstar", "hostile", phase, "normal", _BASE)
    (row,) = _active_rows(_render([t], []))
    (lead,) = row_lead(row)
    assert lead["text"] == code
    assert "m-only" in _cls(lead["attrs"])
    assert lead["tag"] == "span"
    # the desktop phase chip's colour, drawn as the badge
    colour = {"shield": "var(--accent)", "armor": "var(--warn, var(--accent))",
              "hull": "var(--danger)", "anchoring": "var(--success)",
              "unanchoring": "var(--muted)"}[phase]
    assert f"background:{colour}" in lead["attrs"]["style"]


def test_active_row_labels_open_with_name_in_the_briefed_order():
    a, b, c = _active_rows(_render())
    plain = ["Name", "System", "Type", "Phase", "Priority", "EVE time", "Local", "Owner", "Actions"]
    assert list(row_labelled(a)) == plain
    assert list(row_labelled(b)) == plain
    assert list(row_labelled(c)) == plain[:-1] + ["Notes", "Source", "Actions"]
    for row in (a, b, c):
        for cell in row_labelled(row).values():
            assert "m-only" in _cls(cell["attrs"])


def test_active_row_label_values():
    a, b, c = _active_rows(_render())
    la = {k: v["text"] for k, v in row_labelled(a).items()}
    assert la["Name"] == "Sample Fortizar Alpha"
    assert la["System"] == "Sample-01 · Sample Region"
    assert la["Type"] == "Fortizar"
    assert la["Phase"] == "Shield"
    assert la["Priority"] == "Critical"
    assert la["EVE time"] == "2030-01-02 03:04"
    assert la["Owner"] == "Sample Hostile Corp"
    lb = {k: v["text"] for k, v in row_labelled(b).items()}
    assert lb["System"] == "Sample-02"          # no region, no trailing dot
    assert lb["Type"] == "Raitaru"
    assert lb["Priority"] == "Normal"
    lc = {k: v["text"] for k, v in row_labelled(c).items()}
    assert lc["Notes"] == "Sample note text"
    assert lc["Source"] == "ESI (auto-detected)"
    assert lc["Phase"] == "Anchoring"


def test_local_copy_is_ticker_filled_without_its_prefix():
    """The open row labels it "Local", so the copy carries an empty
    data-prefix; the desktop span has none and keeps "Local: "."""
    html = _render()
    for row, t in zip(_active_rows(html), ACTIVE):
        cell = row_labelled(row)["Local"]
        assert "timer-local" in _cls(cell["attrs"])
        assert cell["attrs"]["data-expires"] == t.timer_expires.isoformat()
        assert cell["attrs"].get("data-prefix") == ""
    script = _page_script(html)
    assert "el.hasAttribute('data-prefix')" in script, "an empty prefix is falsy: test presence, not ||"
    assert html.count('class="timer-local"') == 3, "the desktop spans keep their markup"


def test_active_row_data_attributes_and_border_are_unchanged():
    a, b, c = _active_rows(_render())
    want = {"data-disposition": "hostile", "data-priority": "critical",
            "data-expires": "2030-01-02T03:04:05", "data-structure-type": "Fortizar",
            "data-system": "Sample-01", "data-name": "Sample Fortizar Alpha",
            "data-owner": "Sample Hostile Corp", "data-phase": "shield"}
    assert {k: v for k, v in a["attrs"].items() if k.startswith("data-") and k != "data-click"} == want
    assert _cls(a["attrs"]) == ["b-table-row", "timer-row", "m-row"]
    assert "border-left:3px solid var(--danger)" in a["attrs"]["style"]
    assert "border-left:3px solid var(--success)" in b["attrs"]["style"]
    assert b["attrs"]["data-disposition"] == "friendly"
    assert c["attrs"]["data-priority"] == "low"


def test_desktop_blocks_stay_untagged():
    """The three desktop blocks (main, countdown, actions) keep their markup
    and carry no tags, so phones hide them and desktop is unchanged."""
    a, _, c = _active_rows(_render())
    desktop = [x for x in a["cells"] if "m-only" not in _cls(x["attrs"])]
    assert len(desktop) == 3
    for cell in desktop:
        assert "data-m" not in cell["attrs"] and "data-m-label" not in cell["attrs"]
    assert len([x for x in c["cells"] if "m-only" not in _cls(x["attrs"])]) == 2   # no actions


class _Actions(HTMLParser):
    """Each Actions cell's controls: (tag, attrs) of every element inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if self.depth:
            self.cells[-1].append((tag, a))
            if tag not in VOID:
                self.depth += 1
        elif a.get("data-m-label") == "Actions":
            self.cells.append([])
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1


def _actions(html):
    p = _Actions()
    p.feed(html)
    return p.cells


def test_actions_cell_holds_edit_discord_and_the_delete_form():
    own, _, other = _actions(_render())
    tags = [t for t, _ in own]
    assert tags[0] == "div" and "st-actions" in _cls(own[0][1])
    edit = [a for t, a in own if t == "button" and a.get("data-click") == "editTimer"]
    assert len(edit) == 1 and edit[0]["data-timer-id"] == "11" and edit[0]["type"] == "button"
    links = [a for t, a in own if t == "a"]
    unix = int(_BASE.timestamp())
    assert [a["href"] for a in links] == [f"/tools/discordtime#t={unix}"]
    forms = [a for t, a in own if t == "form"]
    assert len(forms) == 1
    assert forms[0]["action"] == "/structure-timers/11/delete"
    assert forms[0]["method"] == "POST"
    assert forms[0]["data-confirm"] == "Delete this timer?"
    buttons = [a for t, a in own if t in ("button", "a")]
    assert len(buttons) == 3
    for a in buttons:
        assert "st-tap" in _cls(a) and "b-btn" in _cls(a)
        assert "m-tap" not in _cls(a)
    # Someone else's timer, viewer not privileged: Discord only.
    assert [t for t, _ in other if t in ("button", "a", "form")] == ["a"]


def test_privileged_viewer_gets_edit_and_delete_on_every_row():
    for cell in _actions(_render(is_privileged=True)):
        assert [t for t, _ in cell if t in ("button", "a", "form")] == ["button", "a", "form", "button"]


def test_phone_copies_add_no_duplicate_ids():
    ids = re.findall(r'\sid="([^"]+)"', _render())
    assert len(ids) == len(set(ids))


# ── filter, copy and ticker ───────────────────────────────────────────


def _page_script(html):
    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, re.S)
    (script,) = [s for s in scripts if "function filterTimers" in s]
    return script


def _fn_body(script, name):
    m = re.search(rf"function {name}\([^)]*\) \{{\n(.*?)\n\}}\n", script, re.S)
    assert m, name
    return m.group(1)


def test_filter_and_copy_use_hidden_not_style_display():
    script = _page_script(_render())
    filt = _fn_body(script, "filterTimers")
    copy = _fn_body(script, "copyAllTimers")
    for body in (filt, copy):
        assert "style.display" not in body
    assert "row.hidden" in filt
    assert "if (row.hidden) return;" in copy


def test_ticker_selects_every_countdown_and_local_element():
    script = _page_script(_render())
    tick = _fn_body(script, "updateCountdowns")
    assert "document.querySelectorAll('.timer-countdown')" in tick
    assert "document.querySelectorAll('.timer-local')" in tick
    assert "el.closest('.timer-row')" in tick


class _RowDom(HTMLParser):
    """Each .timer-row's data-* attributes, and the .timer-countdown and
    .timer-local elements inside it, for the Node stub DOM."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        cls = a.get("class", "").split()
        data = {re.sub(r"-(\w)", lambda m: m.group(1).upper(), k[5:]): v
                for k, v in a.items() if k.startswith("data-")}
        row = "timer-row" in cls
        if row:
            self.rows.append({"dataset": data, "countdowns": [], "locals": []})
        inside = any(self.stack)
        if inside and "timer-countdown" in cls:
            self.rows[-1]["countdowns"].append({"dataset": data, "mOnly": "m-only" in cls})
        if inside and "timer-local" in cls:
            self.rows[-1]["locals"].append({"dataset": data, "hasPrefix": "data-prefix" in a,
                                            "mOnly": "m-only" in cls})
        if tag not in VOID:
            self.stack.append(row or inside)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [srcPath, specPath] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
const NOW = spec.now;

function classList(initial) {
  const s = new Set(initial || []);
  return { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c),
           toggle: (c, on) => (on === undefined ? (s.has(c) ? s.delete(c) : s.add(c)) : (on ? s.add(c) : s.delete(c))),
           list: () => [...s].sort() };
}
function el(dataset, extra) {
  const e = Object.assign({ dataset: Object.assign({}, dataset), style: {}, hidden: false,
                            textContent: '', classList: classList() }, extra || {});
  e.hasAttribute = a => a.startsWith('data-') &&
      Object.prototype.hasOwnProperty.call(e.dataset,
          a.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase()));
  return e;
}
const rows = spec.rows.map(r => el(r.dataset));
const countdowns = [], locals = [];
spec.rows.forEach((r, i) => {
  r.countdowns.forEach(c => countdowns.push(Object.assign(el(c.dataset), {
    row: i, mOnly: c.mOnly, closest: sel => (sel === '.timer-row' ? rows[i] : null) })));
  r.locals.forEach(c => {
    const e = el(c.dataset);
    if (c.hasPrefix && !('prefix' in e.dataset)) e.dataset.prefix = '';
    locals.push(Object.assign(e, { row: i, mOnly: c.mOnly }));
  });
});
// 'all' runs last, so it must bring back the rows the others hid.
const buttons = ['hostile', 'friendly', 'critical', 'all'].map(f => el({ filter: f }));
const unknown = [];
const document = {
  querySelectorAll(sel) {
    if (sel === '.timer-row') return rows;
    if (sel === '.timer-countdown') return countdowns;
    if (sel === '.timer-local') return locals;
    if (sel === '.range-btn') return buttons;
    unknown.push(sel);
    return [];
  },
  getElementById: () => null,
  addEventListener() {},
};
const RealDate = Date;
class FakeDate extends RealDate {
  constructor(...a) { if (a.length === 0) super(NOW); else super(...a); }
  static now() { return NOW; }
}
let clip = null;
const sandbox = {
  document, console, Date: FakeDate, String, Math, parseInt,
  setInterval: () => 0, setTimeout: () => 0, clearTimeout() {},
  navigator: { clipboard: { writeText: t => { clip = t; return { then: cb => cb() }; } } },
  alert() {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);   // runs updateCountdowns() once, as on load

const out = {
  countdowns: countdowns.map(c => ({ row: c.row, mOnly: c.mOnly, text: c.textContent })),
  locals: locals.map(c => ({ row: c.row, mOnly: c.mOnly, text: c.textContent })),
  rowClasses: rows.map(r => r.classList.list()),
  filters: {},
};
for (const b of buttons) {
  sandbox.filterTimers.call(b, {});
  const copyBtn = el({}, { tagName: 'BUTTON' });
  clip = null;
  sandbox.copyAllTimers.call(copyBtn, {});
  out.filters[b.dataset.filter] = {
    hidden: rows.map(r => r.hidden),
    styleDisplay: rows.map(r => ('display' in r.style) ? r.style.display : null),
    active: buttons.map(x => x.classList.contains('is-active')),
    clip,
  };
}
out.unknown = unknown;
console.log(JSON.stringify(out));
"""


def _run_harness(html):
    dom = _RowDom()
    dom.feed(html)
    now_ms = int(_NOW.replace(tzinfo=timezone.utc).timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmp:
        src, spec = os.path.join(tmp, "page.js"), os.path.join(tmp, "spec.json")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(_page_script(html))
        with open(spec, "w", encoding="utf-8") as fh:
            json.dump({"now": now_ms, "rows": dom.rows}, fh)
        res = subprocess.run(["node", "-e", _HARNESS, "--", src, spec], capture_output=True,
                             text=True, check=True, env=dict(os.environ, TZ="UTC"))
    return json.loads(res.stdout.strip().splitlines()[-1])


def _line(t):
    unix = int(t.timer_expires.replace(tzinfo=timezone.utc).timestamp())
    stype = {"fortizar": "Fortizar", "raitaru": "Raitaru", "athanor": "Athanor"}[t.structure_type]
    return (f"{t.disposition.capitalize()} {stype} in {t.system_name} belonging to "
            f"{t.owner_name} {t.timer_phase} timer <t:{unix}:R> <t:{unix}:F>")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_page_script_filters_copies_and_ticks_through_the_real_markup():
    """The page's own script against a stub DOM built from the rendered rows:
    each filter hides the same rows as before (through `hidden`, never
    style.display), Copy for Discord skips exactly those, and one tick
    fills both countdowns in a row and both local times."""
    out = _run_harness(_render())
    assert out["unknown"] == []

    # Two countdowns per row (desktop + the phone key), the same text.
    by_row = {}
    for c in out["countdowns"]:
        by_row.setdefault(c["row"], []).append(c)
    assert sorted(by_row) == [0, 1, 2]
    want = ["1d 3h 04m 05s", "1d 5h 04m 05s", "0h 30m 00s"]
    for i, cs in by_row.items():
        assert [c["mOnly"] for c in cs] == [False, True]
        assert [c["text"] for c in cs] == [want[i]] * 2
    # Row 3 is inside the hour: the ticker still marks its row urgent.
    assert "timer-urgent" in out["rowClasses"][2]
    assert "timer-urgent" not in out["rowClasses"][0]

    # Local: the desktop span keeps "Local: ", the phone copy drops it.
    for i in range(3):
        desk, phone = [c for c in out["locals"] if c["row"] == i]
        assert desk["mOnly"] is False and phone["mOnly"] is True
        assert desk["text"].startswith("Local: ")
        assert phone["text"] and not phone["text"].startswith("Local")
        assert desk["text"] == "Local: " + phone["text"]

    f = out["filters"]
    assert f["all"]["hidden"] == [False, False, False]
    assert f["hostile"]["hidden"] == [False, True, False]
    assert f["friendly"]["hidden"] == [True, False, True]
    assert f["critical"]["hidden"] == [False, True, True]
    for name, res in f.items():
        assert res["styleDisplay"] == [None, None, None], name
        assert res["active"] == [n == name for n in ("hostile", "friendly", "critical", "all")]

    a, b, c = ACTIVE
    assert f["all"]["clip"] == "\n".join([_line(a), _line(b), _line(c)])
    assert f["hostile"]["clip"] == "\n".join([_line(a), _line(c)])
    assert f["friendly"]["clip"] == _line(b)
    assert f["critical"]["clip"] == _line(a)


# ── archived rows (D5 A) ──────────────────────────────────────────────


def test_archived_rows_key_on_name_and_a_short_muted_date():
    for row, t in zip(_archived_rows(_render()), ARCHIVED):
        k1, k2 = row_keys(row)
        assert k1["text"] == t.structure_name
        assert "m-only" not in _cls(k1["attrs"]), "key 1 is the desktop name cell"
        assert k2["text"] == t.timer_expires.strftime("%m-%d")
        assert "m-only" in _cls(k2["attrs"])
        assert "color:var(--muted)" in k2["attrs"]["style"]
        assert row_lead(row) == []


def test_archived_row_labels_and_delete():
    row = _archived_rows(_render())[0]
    t = ARCHIVED[0]
    labels = row_labelled(row)
    assert list(labels) == ["Name", "System", "Phase", "Expired", "Delete"]
    assert labels["Name"]["text"] == t.structure_name
    assert "m-only" in _cls(labels["Name"]["attrs"])
    assert labels["System"]["text"] == t.system_name
    assert labels["Phase"]["text"] == "Hull"
    assert labels["Expired"]["text"] == "2029-12-02 18:30 UTC"
    delete = labels["Delete"]
    assert delete["tag"] == "form"
    assert delete["attrs"]["action"] == f"/structure-timers/{t.id}/delete"
    (btn,) = delete["kids"]
    assert "st-tap" in _cls(btn) and "m-tap" not in _cls(btn)


def test_archived_list_shows_all_past_ten():
    c = clamps(_render())
    assert c.wraps == 1 and c.nested_wraps == 0
    (clamp,) = c.clamps
    assert clamp["children"] == 12
    (btn,) = c.showall
    assert btn["in_wrap"]
    assert btn["text"] == "Show all 12"
    assert _cls(btn["attrs"]) == ["m-only", "m-showall"]
    assert btn["attrs"]["data-click"] == "toggleExpanded"
    assert btn["attrs"]["data-toggle-target"] == ".m-clamp-wrap"


class _Parents(HTMLParser):
    """For each element, its tag, classes and its parent's index; and each
    element's direct children's indexes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.els, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        parent = self.stack[-1] if self.stack else None
        self.els.append({"tag": tag, "cls": a.get("class", "").split(), "parent": parent, "kids": []})
        if parent is not None:
            self.els[parent]["kids"].append(len(self.els) - 1)
        if tag not in VOID:
            self.stack.append(len(self.els) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.els[self.stack[i]]["tag"] == tag:
                del self.stack[i:]
                return


def test_archived_clamp_wraps_the_details_not_the_rows_parent():
    """The wrap can't be the rows' own parent (`.is-expanded > .m-row` would
    open every row), and the button sits outside the panel so the last row
    keeps :last-child (its desktop border)."""
    p = _Parents()
    p.feed(_render())
    els = p.els
    (wrap,) = [i for i, e in enumerate(els) if "m-clamp-wrap" in e["cls"]]
    (clamp,) = [i for i, e in enumerate(els) if "m-clamp" in e["cls"]]
    (btn,) = [i for i, e in enumerate(els) if "m-showall" in e["cls"]]
    assert els[wrap]["tag"] == "details" and "b-section" in els[wrap]["cls"]
    assert els[clamp]["parent"] == wrap and "b-panel" in els[clamp]["cls"]
    assert els[btn]["parent"] == wrap
    assert els[wrap]["kids"].index(btn) > els[wrap]["kids"].index(clamp)
    kids = els[clamp]["kids"]
    assert len(kids) == 12 and all("st-arch" in els[k]["cls"] for k in kids)
    for k in kids:
        assert "m-clamp-wrap" not in els[k]["cls"] and "is-expanded" not in els[k]["cls"]


def test_ten_archived_timers_get_no_show_all_button():
    assert clamps(_render(archived=ARCHIVED[:10])).showall == []


# ── forms and tap targets ─────────────────────────────────────────────


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def _tags(html):
    p = _Tags()
    p.feed(html)
    return p.tags


def _buttons(html):
    """Every <button> in the page's own content as (text, attrs)."""
    content = html[html.index('<h1 class="b-page-title">Structure Timers</h1>'):]
    out = []
    for attrs, text in re.findall(r"<button([^>]*)>(.*?)</button>", content, re.S):
        out.append((norm(text).replace("&times;", "×"), dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))))
    return out


@pytest.mark.parametrize("text, count", [
    ("All", 1), ("Hostile", 1), ("Friendly", 1), ("Critical", 1),   # filters
    ("Copy for Discord", 1), ("Add Timer", 1),
    ("Save", 2), ("Cancel", 2),                                     # the two editable rows
    ("Create", 1), ("Add", 1),                                      # ACL
])
def test_small_controls_are_tap_targets(text, count):
    got = [a for t, a in _buttons(_render()) if t == text]
    assert len(got) == count
    for a in got:
        assert "st-tap" in _cls(a), a


def test_every_x_a_phone_can_see_is_a_tap_target():
    """ACL group and entry ×, the phone Actions × and the archived ×. The
    desktop row × sits in the untagged actions block, hidden on phones, and
    keeps its markup."""
    xs = [a for t, a in _buttons(_render()) if t == "×"]
    assert len(xs) == 1 + 1 + 2 + 2 + 12
    desktop = [a for a in xs if "st-tap" not in _cls(a)]
    assert len(desktop) == 2
    for a in desktop:
        assert a["style"].startswith("padding:0.15rem 0.35rem;")


def test_desktop_row_controls_keep_their_markup():
    """The desktop Edit and × inside the untagged actions block are hidden on
    phones; they keep their classes so desktop renders as before."""
    html = _render()
    assert html.count('<button class="b-btn" data-click="editTimer" data-timer-id="11"') == 1
    assert html.count('<button class="b-btn" style="padding:0.15rem 0.35rem;'
                      'border:1px solid var(--danger);font-size:8px;color:var(--danger);">&times;</button>') == 2


def test_no_button_or_b_btn_carries_m_tap():
    """The shared .m-tap rule forces min-height 40px !important, which beats
    the phone layer's 44px floor on button and .b-btn. Nothing on this page
    may carry it on a button or a .b-btn (the Discord link is an
    a.b-btn)."""
    html = _render(is_privileged=True)
    content = html[html.index('<h1 class="b-page-title">Structure Timers</h1>'):]
    tagged = [(t, a) for t, a in _tags(content) if "m-tap" in _cls(a)
              and (t == "button" or "b-btn" in _cls(a))]
    assert tagged == []
    # ...and the page's own tap class reaches every one the phone shows.
    assert len([a for t, a in _tags(content) if "st-tap" in _cls(a)]) > 0


def test_forms_stack_on_phones():
    html = _render()
    forms = [a for t, a in _tags(html) if "st-form" in _cls(a)]
    # Add Timer, two edit forms, ACL create, ACL add row
    assert len(forms) == 5
    add = re.search(r'<form method="POST" action="/structure-timers/create"[^>]*>(.*?)</form>', html, re.S)
    assert "st-form" in add.group(0).split(">")[0]
    assert add.group(1).count('class="m-stack"') == 5
    edit = re.search(r'<form method="POST" action="/structure-timers/11/edit"[^>]*>(.*?)</form>', html, re.S)
    assert edit.group(1).count('class="m-stack"') == 5
    create = re.search(r'<form method="POST" action="/structure-timers/acl/create"([^>]*)>', html)
    assert "m-stack" in create.group(1) and "st-form" in create.group(1)
    assert re.search(r'<div class="st-form m-stack" style="margin-top:0.35rem;', html)


def test_utc_time_fields_stay_plain_text():
    assert "datetime-local" not in source("structure_timers.html")
    html = _render()
    assert re.search(r'<input type="text" id="absolute-input" name="datetime_utc"', html)
    assert len(re.findall(r'<input type="text" name="datetime_utc"', html)) == 2


# ── CSS ───────────────────────────────────────────────────────────────


def _phone():
    return phone_block(_section("T2"))[0]


def _after():
    return phone_block(_section("T2"))[1]


def test_css_hidden_row_rule_holds_at_every_width():
    """Outside the phone block: on desktop .b-table-row's display:flex beats
    the browser's own [hidden]; on phones (0,2,0) beats .m-row's
    display:grid !important (0,1,0)."""
    assert "display: none !important" in rule_bodies(_after(), ".timer-row[hidden]")
    assert re.fullmatch(r"\s*\.timer-row\[hidden\] \{ display: none !important; \}\s*", _after())


def test_css_forms_take_16px_fields_with_room():
    for sel in ('.st-form input:not([type="radio"]):not([type="hidden"])',
                ".st-form select", ".st-form textarea"):
        body = rule_bodies(_phone(), sel)
        assert "font-size: 16px !important" in body, sel
        assert re.search(r"padding: [^;]+ !important", body), sel


def test_css_side_radios_sit_on_40px_labels():
    """The phone layer's 44px input min-height dropped each radio below its
    label's text; the label is the tap target instead."""
    label = rule_bodies(_phone(), ".st-form label")
    assert "min-height: 40px" in label and "font-size: 12px !important" in label
    assert "min-height: 0" in rule_bodies(_phone(), '.st-form input[type="radio"]')


def test_action_buttons_have_a_visible_border():
    """var(--border) all but vanishes on an open row; the phone copies of
    Edit and Discord are outlined in the muted colour."""
    own = _actions(_render())[0]
    (edit,) = [a for t, a in own if a.get("data-click") == "editTimer"]
    (discord,) = [a for t, a in own if t == "a"]
    (delete,) = [a for t, a in own if a.get("aria-label") == "Delete timer"]
    assert "border:1px solid var(--muted)" in edit["style"]
    assert "border:1px solid var(--muted)" in discord["style"]
    assert "border:1px solid var(--danger)" in delete["style"]


def test_css_tap_class_keeps_the_44px_floor():
    """st-tap widens and enlarges the small buttons but sets no height of its
    own, so every button and .b-btn keeps the phone layer's 44px
    min-height. inline-flex centres the Discord link's text (.b-btn is
    display:block). Open rows stop .b-btn's flex:1 stretching them."""
    body = rule_bodies(_phone(), ".st-tap")
    assert "min-width: 44px" in body
    assert "font-size: 12px !important" in body
    assert "display: inline-flex" in body
    assert "align-items: center" in body and "justify-content: center" in body
    assert "height" not in body
    assert "flex: none" in rule_bodies(_phone(), ".st-actions > .b-btn")
    assert "flex: none" in rule_bodies(_phone(), ".st-actions > form > .b-btn")
    assert "flex: none" in rule_bodies(_phone(), ".st-arch > [data-m-label] > .st-tap")


def test_css_sets_no_height_under_44px_on_a_control():
    """No rule in the section lowers a button, .b-btn, input or select below
    the 44px floor. The one exception is the Side radio, whose label is the
    tap target (test_css_side_radios_sit_on_40px_labels)."""
    phone = _phone()
    for prelude, body in re.findall(r"([^{}]+)\{([^{}]*)\}", phone):
        for m in re.finditer(r"(?<![\w-])(min-height|height)\s*:\s*(\d+(?:\.\d+)?)(px)?", body):
            if float(m.group(2)) >= 44:
                continue
            for sel in selectors(prelude):
                if sel == '.st-form input[type="radio"]':
                    continue
                last = re.split(r"[\s>+~]+", sel.strip())[-1]
                assert not re.match(r"(button|input|select|textarea)\b", last), (sel, m.group(0))
                assert ".b-btn" not in last and ".st-tap" not in last and ".range-btn" not in last, \
                    (sel, m.group(0))


def test_css_row_keys_and_lead():
    lead = rule_bodies(_phone(), ".timer-row > .st-lead")
    assert "min-width: 22px !important" in lead      # beats .m-row > * { min-width: 0 !important }
    assert "font-size: 12px" in rule_bodies(_phone(), ".timer-row > [data-m=\"key\"]")
    assert "font-variant-numeric: tabular-nums" in rule_bodies(_phone(), ".timer-row > .st-cd")
    # the archived name is the desktop cell, inline 10px
    assert "font-size: 12px !important" in rule_bodies(_phone(), ".st-arch > [data-m=\"key\"]")


_HOOKS = (".timer-row", ".st-")


def test_css_rules_cannot_reach_the_site_wide_banner():
    """Every selector names one of this page's hooks, and none of those
    hooks appear in the banner partial (which carries data-system,
    data-name and friends, so a bare attribute selector could reach it)."""
    section = _section("T2")
    preludes = [p for p in re.findall(r"([^{}]+)\{", section) if not p.strip().startswith("@")]
    assert preludes
    for prelude in preludes:
        for sel in selectors(prelude):
            assert any(h in sel for h in _HOOKS), sel
    banner = source("partials/timer_alert_banners.html")
    classes = set(" ".join(re.findall(r'class="([^"]*)"', banner)).split())
    assert "timer-row" not in classes
    assert not [c for c in classes if c.startswith("st-")]
    assert "st-" not in banner and "timer-row" not in banner


def test_new_hook_classes_have_no_desktop_rules():
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    outside = css.replace(_section("T2"), "")
    assert ".st-" not in outside
    assert ".timer-row" not in outside
    # the only rule after the phone block is the [hidden] one
    assert norm(_after()) == ".timer-row[hidden] { display: none !important; }"


def test_archived_delete_has_a_name_and_the_filter_separator_hides_on_phones():
    src = open("app/templates/structure_timers.html", encoding="utf-8").read()
    arch = src[src.index('action="/structure-timers/{{ t.id }}/delete" data-m-label="Delete"'):]
    assert 'aria-label="Delete timer"' in arch[:arch.index("</form>")]
    assert '<span class="st-sep"' in src
    body, _ = phone_block(_section("T2"))
    assert "display: none" in rule_bodies(body, ".st-sep")
