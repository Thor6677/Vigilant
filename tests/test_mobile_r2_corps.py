"""Mobile R2 T5: the Corporations list and the corp detail panel at phone
width, plus the structure fuel warning (ISS-113).

The list (corporations.html) renders each corporation as a native
<details> accordion; on phones its header is one line (D9 B). The detail
panel (partials/corp_detail.html) is loaded into the accordion body by
htmx. On phones its structures and industry jobs become m-rows (D10 A,
D11 A), its corp page links a two-column grid (D12 A), and the fuel
colour comes from one Python helper shared with desktop (D16 A).

Contexts follow the shapes app/routes/corporations.py builds; every name
and id is invented."""
import os
import re
import types
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import pytest

from app.auth import scopes as perms
from app.routes import corporations as corps_mod
from tests._mobile import (SITE_CSS, VOID, assert_mrow, assert_single_value_child,
                           cells_rows, clamps, css_section, norm, row_keys,
                           row_labelled, rule_bodies, render_page)

_NS = types.SimpleNamespace

# ── ISS-113: the fuel level helper ────────────────────────────────────


@pytest.mark.parametrize("value, level", [
    ("EXPIRED", "danger"),
    ("0h", "warn"),
    ("18h", "warn"),
    ("23h", "warn"),
    ("1d 0h", "warn"),
    ("1d 23h", "warn"),
    ("2d 0h", ""),
    ("10d 0h", ""),
    ("12d 4h", ""),
    (None, ""),
    ("", ""),
    ("soon", ""),
    ("1d", ""),
    ("d 4h", ""),
])
def test_fuel_level_boundaries(value, level):
    assert corps_mod._fuel_level(value) == level


def test_fuel_under_a_day_is_a_warning_end_to_end():
    """The bug itself: under 24h _fuel_remaining says "18h", never "0d …",
    so the template's old startswith('0d') test let it through uncoloured."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=18, minutes=30)).isoformat()
    remaining = corps_mod._fuel_remaining(expires)
    assert remaining == "18h"
    assert corps_mod._fuel_level(remaining) == "warn"


@pytest.mark.parametrize("ahead, level", [
    (timedelta(minutes=30), "warn"),             # "0h"
    (timedelta(hours=23, minutes=30), "warn"),   # "23h"
    (timedelta(hours=24, minutes=30), "warn"),   # "1d 0h"
    (timedelta(hours=47, minutes=30), "warn"),   # "1d 23h"
    (timedelta(hours=48, minutes=30), ""),       # "2d 0h"
    (timedelta(minutes=-1), "danger"),           # "EXPIRED"
])
def test_fuel_level_round_trips_fuel_remaining(ahead, level):
    """_fuel_level reads _fuel_remaining's display string, so pin the pair
    together: a change to that string's format must not silently drop the
    warning. Each case sits 30 minutes inside its band, so the moment
    _fuel_remaining reads the clock can't move it."""
    expires = (datetime.now(timezone.utc) + ahead).isoformat()
    assert corps_mod._fuel_level(corps_mod._fuel_remaining(expires)) == level


def test_fuel_level_is_a_template_filter():
    assert corps_mod.templates.env.filters["fuel_level"] is corps_mod._fuel_level


# ── Corporations list (D9 B) ──────────────────────────────────────────

_LONG_NAME = "Sample Interstellar Logistics And Deep Space Salvage Consortium"


def _chars(n):
    return [_NS(character_id=90000001 + i, character_name=f"Pilot {i}") for i in range(n)]


def _render_list():
    player = [
        {"corp_id": 98000001, "corp_name": _LONG_NAME, "alliance_id": 99000001,
         "alliance_name": "Sample Alliance", "characters": _chars(3),
         "available_scopes": sorted(corps_mod.CORP_SCOPES)},
        {"corp_id": 98000002, "corp_name": "Sample Holding", "alliance_id": None,
         "alliance_name": None, "characters": _chars(1), "available_scopes": []},
    ]
    npc = [{"corp_id": 1000001, "corp_name": "Sample Navy Academy", "alliance_id": None,
            "alliance_name": None, "characters": _chars(2), "available_scopes": []}]
    return render_page(corps_mod, "corporations.html", "/corporations",
                       corps=player, npc_corps=npc, total_corps=3)


class _Summaries(HTMLParser):
    """Each corp accordion's <summary>: its text without m-only elements (as
    desktop reads it), its text without m-hide elements (as phones read
    it), and every element inside it as (tag, classes)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []      # [tag, classes]
        self.out = []
        self.cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "summary" and self.stack and "corp-accordion" in self.stack[-1][1]:
            self.cur = {"desktop": "", "phone": "", "els": [], "depth": len(self.stack)}
            self.out.append(self.cur)
        elif self.cur is not None:
            self.cur["els"].append((tag, cls))
        if tag not in VOID:
            self.stack.append([tag, cls])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if self.cur is not None and len(self.stack) <= self.cur["depth"]:
            self.cur = None

    def handle_data(self, data):
        if self.cur is None:
            return
        inner = self.stack[self.cur["depth"]:]
        if not any("m-only" in cls for _, cls in inner):
            self.cur["desktop"] += data
        if not any("m-hide" in cls for _, cls in inner):
            self.cur["phone"] += data


def _summaries(html):
    p = _Summaries()
    p.feed(html)
    p.close()
    for s in p.out:
        s["desktop"], s["phone"] = norm(s["desktop"]), norm(s["phone"])
    return p.out


def _has(els, cls):
    return [tag for tag, c in els if cls in c]


def test_list_renders_three_corp_headers():
    sums = _summaries(_render_list())
    assert len(sums) == 3
    assert _LONG_NAME in sums[0]["desktop"]
    assert "Sample Navy Academy" in sums[2]["desktop"]


def test_player_header_reads_n_chars_on_phones_and_unchanged_on_desktop():
    long_corp, small_corp, _ = _summaries(_render_list())
    assert "3 of our chars" in long_corp["desktop"]
    assert "1 of our char" in small_corp["desktop"]
    assert "3 chars" in long_corp["phone"] and "of our" not in long_corp["phone"]
    assert "1 char" in small_corp["phone"] and "of our" not in small_corp["phone"]


def test_player_header_count_copies_sit_back_to_back():
    """Desktop keeps "N of our chars" as one text run inside an m-hide copy;
    the m-only "N chars" copy follows with no whitespace between them, which
    would otherwise add a space at the end of the right-aligned line."""
    html = _render_list()
    got = re.findall(r'<div class="b-label">(<span class="m-hide">[^<]*</span>)(<span class="m-only">[^<]*</span>)</div>', html)
    assert got == [('<span class="m-hide">3 of our chars</span>', '<span class="m-only">3 chars</span>'),
                   ('<span class="m-hide">1 of our char</span>', '<span class="m-only">1 char</span>')]


def test_player_header_hides_the_drag_handle_on_phones():
    html = _render_list()
    for s in _summaries(html)[:2]:
        assert ("span", ["corp-drag-handle", "m-hide"]) in s["els"]
        assert "☰" not in s["phone"]


def test_player_header_scope_line_carries_the_hook():
    long_corp, small_corp, npc = _summaries(_render_list())
    assert len(_has(long_corp["els"], "scope-pip")) == len(corps_mod.CORP_SCOPES)
    assert "7 corp scopes" in long_corp["desktop"]
    assert "Public only" in small_corp["desktop"]
    for s in (long_corp, small_corp):
        assert _has(s["els"], "corp-scopes") == ["div"]
    assert not _has(npc["els"], "corp-scopes")


def test_every_header_has_the_name_and_side_hooks():
    for s in _summaries(_render_list()):
        assert _has(s["els"], "corp-sum-main") == ["div"]
        assert _has(s["els"], "corp-sum-side") == ["div"]
        assert _has(s["els"], "b-card-name") == ["div"]


def test_npc_header_keeps_its_n_chars_text():
    npc = _summaries(_render_list())[2]
    assert "2 chars" in npc["desktop"] and "2 chars" in npc["phone"]
    assert not _has(npc["els"], "corp-drag-handle")


def test_header_is_not_an_m_row():
    """D9 B: the <summary> stays a native accordion."""
    html = _render_list()
    assert not cells_rows(html)
    assert not re.search(r"<summary[^>]*\b(m-row|data-click)", html)


def test_wallet_chart_legend_moves_below_on_phones_only():
    html = _render_list()
    script = html[html.index("Corp wallet history chart"):html.index("Drag-and-drop reorder")]
    assert "window.matchMedia('(max-width: 640px)')" in script
    assert re.search(r"legend\.position\s*=\s*'bottom'", script)
    # Desktop keeps Chart.js's default position: nothing sets 'top'.
    assert "'top'" not in script
    assert re.search(r"spanGaps:\s*false", script)


def test_wallet_chart_x_ticks_thin_out_on_phones_only():
    """Phones get at most 4 x-axis labels ("MM-DD HH:MM" each), so they
    don't overlap in ~310px; desktop keeps its 8."""
    html = _render_list()
    script = html[html.index("Corp wallet history chart"):html.index("Drag-and-drop reorder")]
    assert re.search(r"if \(phone\) xTicks\.maxTicksLimit = 4;", script)
    assert re.search(r"var xTicks = \{[^;]*maxTicksLimit: 8,", script)
    assert re.search(r"x: \{ ticks: xTicks,", script)
    assert "var phone = !!(phoneMq && phoneMq.matches);" in script
    assert re.search(r"if \(phone\) legend\.position = 'bottom';", script)


# Behaviour: the chart script in node against a stub Chart and DOM. Two corp
# panels are open; the phone query flips; one panel is swapped out; a range
# button fetches a new window.
_CHART_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const startPhone = process.argv[3] === 'phone';
const state = { phone: startPhone };
const mqls = [];
function matchMedia(q) {
  const m = { media: q, ls: [],
    get matches() { return state.phone && q === '(max-width: 640px)'; },
    addEventListener(t, fn) { if (t === 'change') this.ls.push(fn); },
    addListener(fn) { this.ls.push(fn); } };
  mqls.push(m);
  return m;
}
let made = [];
const all = [];
const live = {};
function Chart(canvas, cfg) {
  if (live[canvas.id]) throw new Error('Canvas is already in use');
  live[canvas.id] = this; this.canvas = canvas; this.cfg = cfg; made.push(this); all.push(this);
}
Chart.prototype.destroy = function () { this.destroyed = true; delete live[this.canvas.id]; };
function series(n) { return { labels: ['2026-10-01T00:00', '2026-10-02T00:00'], total: [n, n + 1],
                              series: { '1000': [n, n], '1001': [1, 2] }, samples: 2 }; }
function panel(corpId) {
  const wrap = { dataset: { corpId: String(corpId) }, buttons: [],
                 querySelectorAll: () => wrap.buttons, querySelector: () => canvas };
  const canvas = { id: 'c' + corpId, isConnected: true, dataset: { chart: JSON.stringify(series(corpId)) },
                   parentElement: { style: {} }, closest: () => wrap };
  const btn = { dataset: { corpId: String(corpId), range: '1w' }, classList: { add() {}, remove() {} },
                closest: () => wrap };
  wrap.buttons.push(btn);
  return { wrap, canvas, btn };
}
const A = panel(98000001), B = panel(98000002);
const document = {
  body: { addEventListener() {} },
  querySelectorAll: sel => sel.indexOf('canvas.corp-wallet-chart') === 0 ? [A.canvas, B.canvas] : [],
};
const fetched = [];
const fetch = url => { fetched.push(url);
  return Promise.resolve({ ok: true, json: () => Promise.resolve(series(5)) }); };
const window = { matchMedia };
const sandbox = { window, document, console, Chart, fetch, JSON, Object, String, Math };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const snap = cs => cs.map(c => ({ canvas: c.canvas.id,
  legendPosition: c.cfg.options.plugins.legend.position || null,
  xTicksLimit: c.cfg.options.scales.x.ticks.maxTicksLimit,
  total: c.cfg.data.datasets[0].data }));
const fire = phone => {
  const before = all.filter(c => !c.destroyed);
  state.phone = phone; made = [];
  mqls.forEach(m => m.ls.slice().forEach(fn => fn({ matches: m.matches, media: m.media })));
  return { charts: snap(made), destroyed: before.filter(c => c.destroyed).map(c => c.canvas.id),
           live: Object.keys(live).sort() };
};
(async () => {
  const out = { listeners: mqls.reduce((n, m) => n + m.ls.length, 0), first: snap(made) };
  made = [];
  out.same = fire(startPhone);
  out.flip = fire(!startPhone);
  out.sameAfterFlip = fire(!startPhone);
  out.back = fire(startPhone);
  // A range button on panel A fetches a new window; the rebuild keeps the side.
  made = [];
  window.corpWalletRange.call(A.btn);
  await new Promise(r => setImmediate(r));
  out.range = { charts: snap(made), fetched };
  // Panel B is swapped out: a crossing rebuilds A (from its new data) only.
  B.canvas.isConnected = false;
  out.flipAfterSwap = fire(!startPhone);
  out.backAfterSwap = fire(startPhone);
  process.stdout.write(JSON.stringify(out));
})();
"""


def _chart_script():
    html = _render_list()
    start = html.index("/* ── Corp wallet history chart")
    return html[start:html.index("/* ── Drag-and-drop reorder", start)]


def _run_chart(tmp_path, side):
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_CHART_HARNESS)
    script = tmp_path / "page.js"
    script.write_text(_chart_script())
    run = subprocess.run(["node", str(harness), str(script), side], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _opts(side):
    return ("bottom", 4) if side == "phone" else (None, 8)


@pytest.mark.parametrize("side", ["desktop", "phone"])
def test_wallet_chart_rebuilds_once_per_breakpoint_crossing(tmp_path, side):
    """Each open panel's chart is rebuilt from its last data with the other
    side's options when the page crosses 640px; a change that stays on the
    same side rebuilds nothing; one query listener serves every panel."""
    other = "desktop" if side == "phone" else "phone"
    out = _run_chart(tmp_path, side)
    assert out["listeners"] == 1
    lines = lambda charts: [(c["canvas"], c["legendPosition"], c["xTicksLimit"]) for c in charts]
    both = ["c98000001", "c98000002"]
    assert lines(out["first"]) == [(c, *_opts(side)) for c in both]
    assert out["same"]["charts"] == [] and out["same"]["destroyed"] == []
    assert lines(out["flip"]["charts"]) == [(c, *_opts(other)) for c in both]
    assert out["flip"]["destroyed"] == both and out["flip"]["live"] == both
    assert out["sameAfterFlip"]["charts"] == [] and out["sameAfterFlip"]["destroyed"] == []
    assert lines(out["back"]["charts"]) == [(c, *_opts(side)) for c in both]
    # The data is the panel's own, before and after a crossing.
    assert [c["total"] for c in out["flip"]["charts"]] == [[98000001, 98000002], [98000002, 98000003]]


@pytest.mark.parametrize("side", ["desktop", "phone"])
def test_wallet_chart_rebuild_follows_a_range_change_and_skips_swapped_panels(tmp_path, side):
    other = "desktop" if side == "phone" else "phone"
    out = _run_chart(tmp_path, side)
    assert out["range"]["fetched"] == ["/corporations/98000001/wallet-history?range=1w"]
    assert [(c["canvas"], c["total"], c["legendPosition"]) for c in out["range"]["charts"]] == \
        [("c98000001", [5, 6], _opts(side)[0])]
    # Panel B's canvas left the page: only A is rebuilt, from the range's data.
    flip = out["flipAfterSwap"]
    assert [(c["canvas"], c["total"], c["legendPosition"], c["xTicksLimit"]) for c in flip["charts"]] == \
        [("c98000001", [5, 6], *_opts(other))]
    assert [c["canvas"] for c in out["backAfterSwap"]["charts"]] == ["c98000001"]


# ── Corp detail (D10 A, D11 A, D12 A) ─────────────────────────────────

_SCOPES = " ".join(list(corps_mod.CORP_SCOPES.values()) + [perms.CORP_ROLES])


def _struct(name, state, fuel, *, reinforce=None, timer=None, services=None):
    """One structure as corp_detail() enriches it."""
    return {"name": name, "type_name": "Sample Citadel", "system_name": "Sample System",
            "region": "Sample Region", "state": state,
            "state_class": corps_mod.STRUCTURE_STATE_CLASS.get(state, "is-warn"),
            "fuel_remaining": fuel, "fuel_expires": "2026-10-20" if fuel else None,
            "services": services if services is not None else [],
            "reinforce_hour": reinforce, "state_timer_end": timer}


_STRUCTS = [
    _struct("Sample Station Alpha", "shield_vulnerable", "12d 4h", reinforce=18,
            services=[{"name": "Manufacturing (Standard)", "state": "online"},
                      {"name": "Reprocessing", "state": "offline"}]),
    _struct("Sample Station Bravo", "armor_reinforce", "1d 6h", reinforce=2,
            timer="2026-10-06", services=[{"name": "Market", "state": "online"}]),
    _struct("Sample Station Charlie", "shield_vulnerable", "18h"),
    _struct("Sample Station Delta", "shield_vulnerable", "EXPIRED", reinforce=12),
]


def _job(i):
    return {"activity_name": corps_mod.ACTIVITY_NAMES[1 if i % 2 else 9],
            "product_name": None if i == 4 else f"Sample Product {i}",
            "runs": 10 + i,
            "time_remaining": "Ready" if i == 0 else (None if i == 5 else f"{i}d 3h"),
            "installer_id": 90000001}


def _render_detail(n_jobs=13, structures=None, **over):
    ctx = dict(
        corp_id=98000001,
        corp_info={"ticker": "SMPL", "member_count": 42, "tax_rate": 0.1,
                   "alliance_id": 99000001, "war_eligible": False,
                   "date_founded": "2020-01-01T00:00:00Z"},
        corp_chars=[_NS(character_id=90000001, character_name="Pilot Alpha",
                        alliance_name="Sample Alliance", scopes=_SCOPES, declined_scopes=""),
                    _NS(character_id=90000002, character_name="Pilot Beta",
                        alliance_name="Sample Alliance", scopes=_SCOPES, declined_scopes="")],
        ceo_name="Pilot Alpha", member_count=42,
        corp_wallets=[{"division": 1, "balance": 1.5e9}, {"division": 2, "balance": 2.0e8}],
        corp_wallet_total=1.7e9,
        wallet_history={"samples": 5, "labels": ["2026-10-01T00:00", "2026-10-01T01:00"],
                        "total": [1.6e9, 1.7e9], "series": {"1": [1.5e9, 1.5e9]}},
        corp_jobs=[_job(i) for i in range(n_jobs)],
        corp_orders={"sell_count": 3, "buy_count": 1, "sell_value": 1e9,
                     "buy_value": 2e8, "total_count": 4},
        corp_structures=_STRUCTS if structures is None else structures,
        corp_contracts={"active_count": 2, "by_type": {"Item Exchange": 2}},
        inv_alert_count=3, corp_roles={},
    )
    ctx.update(over)
    return render_page(corps_mod, "partials/corp_detail.html", "/corporations/98000001/detail", **ctx)


def _struct_rows(html):
    names = {s["name"] for s in _STRUCTS}
    out = {}
    for r in cells_rows(html):
        keys = row_keys(r)
        for n in names:
            if keys and n in keys[0]["text"]:
                out[n] = r
    return out


def _job_rows(html):
    return [r for r in cells_rows(html) if "Activity" in row_labelled(r)]


def test_detail_rows_meet_the_contract():
    html = _render_detail()
    assert_mrow(html, min_rows=len(_STRUCTS) + 13)
    assert len(cells_rows(html)) == len(_STRUCTS) + 13
    for r in cells_rows(html):
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert_single_value_child(r)


def test_detail_phone_cells_are_phone_only():
    """Desktop-identical technique: every tagged cell is an m-only copy, so
    the desktop wrappers stay untagged (and hidden on phones)."""
    for r in cells_rows(_render_detail()):
        for c in r["cells"]:
            a = c["attrs"]
            tagged = "data-m" in a or "data-m-label" in a
            assert tagged == ("m-only" in a.get("class", "").split()), (r["attrs"], a)


def test_structure_key_one_is_the_name_with_a_badge_only_off_normal():
    rows = _struct_rows(_render_detail())
    assert set(rows) == {s["name"] for s in _STRUCTS}
    for s in _STRUCTS:
        key1 = row_keys(rows[s["name"]])[0]
        badges = [k for k in key1["kids"] if "b-badge" in k.get("class", "").split()]
        if s["state"] == "shield_vulnerable":
            assert not badges, s["name"]
            assert key1["text"] == s["name"]
        else:
            assert len(badges) == 1, s["name"]
            assert s["state_class"] in badges[0]["class"].split()
            assert "ARMOR REINFORCE" in key1["text"]


def test_structure_key_two_is_fuel_coloured_by_fuel_level():
    rows = _struct_rows(_render_detail())
    expect = {"danger": "is-danger", "warn": "is-warn", "": None}
    seen = set()
    for s in _STRUCTS:
        keys = row_keys(rows[s["name"]])
        assert len(keys) == 2, s["name"]
        assert keys[1]["text"] == s["fuel_remaining"]
        cls = keys[1]["attrs"]["class"].split()
        level = corps_mod._fuel_level(s["fuel_remaining"])
        seen.add(level)
        for want in ("is-danger", "is-warn"):
            assert (want in cls) == (expect[level] == want), (s["name"], cls)
    assert seen == {"danger", "warn", ""}


def test_desktop_fuel_span_uses_the_same_level():
    """ISS-113 on desktop: the 18h structure's Fuel line turns amber too."""
    html = _render_detail()
    for s in _STRUCTS:
        m = re.search(r'<span class="([^"]*)">\s*Fuel: ' + re.escape(s["fuel_remaining"]) + r"\s*</span>", html)
        assert m, s["name"]
        cls = m.group(1).split()
        level = corps_mod._fuel_level(s["fuel_remaining"])
        assert "b-row-label" in cls
        assert ("is-warn" in cls) == (level == "warn"), s["name"]
        assert ("is-danger" in cls) == (level == "danger"), s["name"]


def test_structure_without_fuel_keeps_one_key():
    lowpower = _struct("Sample Station Echo", "shield_vulnerable", None)
    html = _render_detail(structures=[lowpower])
    assert_mrow(html, min_rows=1)
    srow = [r for r in cells_rows(html) if "Sample Station Echo" in row_keys(r)[0]["text"]]
    assert len(srow) == 1 and len(row_keys(srow[0])) == 1
    assert "Fuel:" not in html


def test_structure_labelled_cells():
    rows = _struct_rows(_render_detail())
    for s in _STRUCTS:
        lab = row_labelled(rows[s["name"]])
        want = {"Name", "State", "Type", "System"}
        if s["reinforce_hour"] is not None:
            want.add("Reinforce")
        if s["state_timer_end"]:
            want.add("Timer")
        if s["services"]:
            want.add("Services")
        assert set(lab) == want, s["name"]
        assert lab["Name"]["text"] == s["name"]
        assert lab["Type"]["text"] == "Sample Citadel"
        assert lab["System"]["text"] == "Sample System — Sample Region"
        state = lab["State"]
        assert len(state["kids"]) == 1 and "b-badge" in state["kids"][0]["class"].split()
        assert state["text"] == s["state"].replace("_", " ").upper()
        if "Reinforce" in want:
            assert lab["Reinforce"]["text"] == "%02d:00 EVE" % s["reinforce_hour"]
        if "Timer" in want:
            assert lab["Timer"]["text"] == s["state_timer_end"]
        if "Services" in want:
            assert all(svc["name"] in lab["Services"]["text"] for svc in s["services"])


def test_structure_system_without_region_and_reinforce_at_midnight():
    """A reinforce hour of 0 is still shown, and a system with no region
    reads as the system alone, as on desktop."""
    odd = dict(_struct("Sample Station Golf", "shield_vulnerable", "4d 0h", reinforce=0), region=None)
    html = _render_detail(structures=[odd])
    row = [r for r in cells_rows(html) if "Sample Station Golf" in row_keys(r)[0]["text"]][0]
    lab = row_labelled(row)
    assert lab["System"]["text"] == "Sample System"
    assert lab["Reinforce"]["text"] == "00:00 EVE"
    assert "Reinforce: 00:00 EVE" in html          # desktop line


def test_structure_services_badges_sit_in_one_wrapper():
    html = _render_detail()
    svc = row_labelled(_struct_rows(html)["Sample Station Alpha"])["Services"]
    assert len(svc["kids"]) == 1 and "corp-struct-svcs" in svc["kids"][0]["class"].split()
    wraps = _hooked(html, "corp-struct-svcs")
    assert len(wraps) == 2                      # Alpha and Bravo have services
    badges = [k for k in wraps[0]["kids"] if "b-badge" in k[1]]
    assert len(badges) == len(wraps[0]["kids"]) == 2
    assert "is-danger" not in badges[0][1] and "is-danger" in badges[1][1]   # Reprocessing is offline


def test_structure_with_unknown_state_shows_its_badge():
    odd = _struct("Sample Station Foxtrot", "some_new_state", "3d 1h")
    html = _render_detail(structures=[odd])
    row = [r for r in cells_rows(html) if "Sample Station Foxtrot" in row_keys(r)[0]["text"]][0]
    badges = [k for k in row_keys(row)[0]["kids"] if "b-badge" in k.get("class", "")]
    assert len(badges) == 1 and "is-warn" in badges[0]["class"].split()


def test_job_rows_keys_and_labels():
    jobs = [_job(i) for i in range(13)]
    rows = _job_rows(_render_detail())
    assert len(rows) == 13
    for job, r in zip(jobs, rows):
        keys = row_keys(r)
        assert keys[0]["text"] == (job["product_name"] or "—")
        if job["time_remaining"]:
            assert len(keys) == 2 and keys[1]["text"] == job["time_remaining"]
            style = keys[1]["attrs"].get("style", "")
            assert ("var(--success)" in style) == (job["time_remaining"] == "Ready")
        else:
            assert len(keys) == 1
        lab = row_labelled(r)
        assert set(lab) == {"Name", "Activity", "Runs"}
        assert lab["Name"]["text"] == (job["product_name"] or "—")
        assert lab["Activity"]["text"] == job["activity_name"]
        assert lab["Runs"]["text"] == str(job["runs"])


def test_jobs_clamp_with_show_all_past_ten():
    c = clamps(_render_detail(n_jobs=13))
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == 13
    assert "corp-jobs" in c.clamps[0]["classes"]
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.showall) == 1
    btn = c.showall[0]
    assert btn["in_wrap"] and btn["text"] == "Show all 13"
    assert btn["attrs"]["class"].split() == ["m-only", "m-showall"]
    assert btn["attrs"]["type"] == "button"
    assert btn["attrs"]["data-click"] == "toggleExpanded"
    assert btn["attrs"]["data-toggle-target"] == ".m-clamp-wrap"


def test_jobs_no_show_all_for_five():
    c = clamps(_render_detail(n_jobs=5))
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == 5
    assert not c.showall


def test_no_jobs_renders_the_empty_state_without_a_clamp():
    html = _render_detail(n_jobs=0)
    assert "No active industry jobs." in html
    assert not clamps(html).clamps


class _Hooked(HTMLParser):
    """Direct element children (tag, classes) and the text of every element
    carrying `cls`."""

    def __init__(self, cls):
        super().__init__(convert_charrefs=True)
        self.cls, self.stack, self.found = cls, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if self.stack and self.stack[-1][1] is not None and len(self.stack) == self.stack[-1][2] + 1:
            self.found[self.stack[-1][1]]["kids"].append((tag, classes, a))
        ref = None
        if self.cls in classes:
            self.found.append({"tag": tag, "attrs": a, "kids": [], "text": ""})
            ref = len(self.found) - 1
        if tag not in VOID:
            parent = self.stack[-1] if self.stack else None
            if ref is None and parent and parent[1] is not None:
                self.stack.append([tag, parent[1], parent[2]])
            else:
                self.stack.append([tag, ref, len(self.stack)])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] is not None:
            self.found[self.stack[-1][1]]["text"] += data


def _hooked(html, cls):
    p = _Hooked(cls)
    p.feed(html)
    p.close()
    for f in p.found:
        f["text"] = norm(f["text"])
    return p.found


def test_corp_page_links_carry_the_grid_hook():
    nav = _hooked(_render_detail(), "corp-subnav")
    assert len(nav) == 1
    links = [k for k in nav[0]["kids"] if k[0] == "a"]
    assert len(links) == len(nav[0]["kids"]) == 5
    hrefs = [k[2]["href"] for k in links]
    assert hrefs == [f"/corporations/98000001/{p}" for p in
                     ("journal", "blueprints", "mining", "inventory", "contracts")]
    # The Inventory badge stays.
    assert re.search(r'Inventory <span class="b-badge is-danger"[^>]*>3</span>', _render_detail())


def test_our_characters_rows_carry_the_tap_hook():
    rows = _hooked(_render_detail(), "corp-char-row")
    assert len(rows) == 2
    for r in rows:
        links = [k for k in r["kids"] if k[0] == "a"]
        assert [k[2]["href"].split("/")[-2] for k in links] == ["character", "permissions"]


def test_wallet_head_wraps_with_the_caption_below_on_phones():
    head = _hooked(_render_detail(), "corp-wallet-head")
    assert len(head) == 1
    kids = head[0]["kids"]
    assert [k[0] for k in kids] == ["span", "span"]
    assert "corp-wallet-caption" in kids[0][1]
    assert "Balance history" in head[0]["text"]


def test_detail_partial_still_has_no_script():
    assert "<script" not in _render_detail()


# ── CSS: section R2 T5 ────────────────────────────────────────────────

def _t5():
    return css_section("T5")


def _split_media(section):
    """(before, inside, after) the section's phone @media block, found by
    matching braces from its opening `{`, so a rule after the block (a
    desktop rule) lands in `after` rather than inside the block."""
    at = section.index("@media")
    start = section.index("{", at) + 1
    depth = 1
    for i in range(start, len(section)):
        depth += (section[i] == "{") - (section[i] == "}")
        if depth == 0:
            return section[:at], section[start:i], section[i + 1:]
    raise AssertionError("R2 T5's @media block never closes")


def _phone_block():
    before, inside, after = _split_media(_t5())
    return inside


def _decl(body, prop):
    m = re.search(rf"(?:^|[;\s{{]){re.escape(prop)}\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else None


def test_css_header_hides_the_scope_line_and_shrinks_the_side_gap():
    css = _phone_block()
    scopes = rule_bodies(css, ".corp-accordion > summary .corp-scopes")
    assert _decl(scopes, "display") == "none !important"
    side = rule_bodies(css, ".corp-accordion > summary > .corp-sum-side")
    gap = _decl(side, "gap")
    assert gap and gap.endswith("!important") and gap.split()[0] in ("0.5rem", "0.4rem", "0.35rem")


def test_header_name_sits_in_a_shrinkable_line_and_ellipsises():
    """The name gets the width the hidden scope line frees: its line is the
    flexible one (flex:1; min-width:0), and .b-card-name ellipsises."""
    html = _render_list()
    for m in re.finditer(r'<div class="corp-sum-main" style="([^"]*)"', html):
        assert "flex:1" in m.group(1) and "min-width:0" in m.group(1)
    for m in re.finditer(r'<div class="corp-sum-side" style="([^"]*)"', html):
        assert "flex-shrink:0" in m.group(1)
    components_css = os.path.join(os.path.dirname(SITE_CSS), "..", "..",
                                  "design-system", "css", "components.css")
    with open(components_css, encoding="utf-8") as fh:
        components = fh.read()
    name = rule_bodies(components, ".b-card-name")
    assert _decl(name, "text-overflow") == "ellipsis"
    assert _decl(name, "white-space") == "nowrap"


def test_css_selectors_never_reach_nested_summaries():
    """The detail body has its own <details><summary> (the permission
    notice). A descendant `.corp-accordion summary` would style it too."""
    assert not re.search(r"\.corp-accordion(\[open\])?\s+summary", _t5())


def test_css_subnav_is_a_two_column_grid_of_40px_links():
    css = _phone_block()
    nav = rule_bodies(css, ".corp-subnav")
    assert _decl(nav, "display") == "grid !important"
    assert _decl(nav, "grid-template-columns") == "repeat(2, minmax(0, 1fr))"
    assert _decl(nav, "gap") == "1px !important"
    assert _decl(nav, "background") == "var(--border) !important"
    link = rule_bodies(css, ".corp-subnav > a")
    assert _decl(link, "min-height") == "40px"
    assert _decl(link, "justify-content") == "center"
    assert _decl(link, "background") == "var(--surface)"
    odd = rule_bodies(css, ".corp-subnav > a:last-child:nth-child(odd)")
    assert _decl(odd, "grid-column") == "1 / -1"


def test_css_our_characters_links_are_40px():
    css = _phone_block()
    link = rule_bodies(css, ".corp-char-row > a")
    assert _decl(link, "min-height") == "40px"
    assert _decl(link, "align-items") == "center"


def test_css_wallet_caption_takes_its_own_line_under_the_buttons():
    css = _phone_block()
    head = rule_bodies(css, ".corp-wallet-head")
    assert _decl(head, "flex-wrap") == "wrap"
    cap = rule_bodies(css, ".corp-wallet-head > .corp-wallet-caption")
    assert _decl(cap, "order") == "1"
    assert _decl(cap, "flex-basis") == "100%"


def test_css_wallet_range_buttons_match_the_other_range_buttons():
    """44px tall from the global button rule (no height here), at least
    40px wide with 12px labels, as Market's and Net Worth's."""
    body = rule_bodies(_phone_block(), ".b-btn.corp-wallet-range")
    assert _decl(body, "min-width") == "40px" and _decl(body, "font-size") == "12px !important"
    assert "height" not in body


def test_css_jobs_are_one_column_and_structure_key_one_truncates_the_name():
    css = _phone_block()
    jobs = rule_bodies(css, ".corp-jobs")
    assert _decl(jobs, "grid-template-columns") == "minmax(0, 1fr) !important"
    key = rule_bodies(css, '.m-row > .corp-struct-key[data-m="key"]')
    assert _decl(key, "display") == "flex !important"
    name = rule_bodies(css, ".corp-struct-key > .corp-struct-name")
    assert _decl(name, "text-overflow") == "ellipsis"
    assert _decl(name, "min-width") == "0"
    badge = rule_bodies(css, ".corp-struct-key > .b-badge")
    assert _decl(badge, "flex") == "none"


def test_css_has_no_desktop_rules():
    """D21: desktop renders as before, so the section is one phone @media
    block with only whitespace before it and after its closing brace, up to
    the end marker."""
    body = _t5()
    before, inside, after = _split_media(body)
    assert body[len(before):].startswith("@media (max-width: 640px) {")
    assert not before.strip(), before
    assert not after.strip(), f"rules after the phone block apply on desktop: {after.strip()!r}"
    assert "@media" not in inside


def test_split_media_catches_a_rule_after_the_block():
    """The guard above, against the mutation that slipped past a greedy
    match: a desktop rule after the phone block, inside the section."""
    sample = "\n@media (max-width: 640px) {\n    .a { color: red; }\n}\n.b { outline: 3px solid red; }\n"
    before, inside, after = _split_media(sample)
    assert ".b" not in inside and ".a" in inside
    assert after.strip() == ".b { outline: 3px solid red; }"
