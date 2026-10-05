"""Mobile R3 T4: the compression optimiser and the hauling planner on phones.

Compression's two result lists (ores to buy, minerals produced) and the
hauling planner's resolved items and ship recommendations are `div.b-table-row`
lists, so they become expand-on-tap m-rows with `m-head` header rows (no
m-table). Icons sit inside the name span on desktop, so phones get their own
`m-only` lead image and the in-cell copy is `m-hide`.

User picks: key 2 is the ore's total ISK (D9 A), the item's total m³ (D10 A)
and the ship's trips (D11 A). Resolved items stop at 10 rows with "Show all N".
The skill chips built in `app/routes/industry.py` get a class hook and 40px
tap targets; the ship entry fields go two to a row; the JS-built trip
breakdown wraps without becoming tap-to-open. Desktop renders as before (D21).

The ore rows' `data-haul-*` attributes feed Send to Hauling, so they must come
out exactly as the base template rendered them. Names are invented."""
import asyncio
import functools
import re
import types
from html import unescape
from html.parser import HTMLParser

import pytest

import app.routes.industry as industry
from app.industry.hauling import (BAY_LABELS, CARGO_MODULES, CARGO_RIGS, HAULING_SHIPS,
                                  get_ships_by_group)
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows, clamps,
                           css_section, phone_block, render_page, request, row_keys,
                           row_labelled, row_lead, rule_bodies, source)

_section = functools.partial(css_section, release="R3")


def _isk(amount):
    """compression_results.html's format_isk macro."""
    if amount >= 1e9:
        return f"{amount / 1e9:.2f}B"
    if amount >= 1e6:
        return f"{amount / 1e6:.2f}M"
    if amount >= 1e3:
        return f"{amount / 1e3:.1f}K"
    return f"{amount:.0f}"


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _classes(attrs):
    return attrs.get("class", "").split()


class _Tree(HTMLParser):
    """Every element as (tag, attrs, parent index), in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.els, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.els.append((tag, a, self.stack[-1] if self.stack else None))
        if tag not in VOID:
            self.stack.append(len(self.els) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.els[self.stack[i]][0] == tag:
                del self.stack[i:]
                return


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.els


def _inside(els, idx, pred):
    """Whether any ancestor of element `idx` satisfies pred(tag, attrs)."""
    p = els[idx][2]
    while p is not None:
        if pred(els[p][0], els[p][1]):
            return True
        p = els[p][2]
    return False


# ── Compression results ──────────────────────────────────────────────

# 4 ores and 5 target minerals. One name carries an apostrophe and an
# ampersand so the data-haul-name escaping is compared too.
_ORES = [
    {"type_id": 62516, "name": "Compressed Sample Ore A", "quantity": 12500,
     "price_each": 1234.4, "total_price": 15_430_000.0, "volume": 187.5},
    {"type_id": 62520, "name": "Compressed Sample Ore B", "quantity": 420,
     "price_each": 87.6, "total_price": 36_792.0, "volume": 6.3},
    {"type_id": 62528, "name": "Compressed Sample Ore 'C' & D", "quantity": 7,
     "price_each": 99.0, "total_price": 693.0, "volume": 0.105},
    {"type_id": 62536, "name": "Compressed Sample Ore With A Rather Long Name", "quantity": 3_000_000,
     "price_each": 650.0, "total_price": 1_950_000_000.0, "volume": 45000.0},
]
_TARGETS = {"Tritanium": 1_000_000, "Pyerite": 250_000, "Mexallon": 80_000,
            "Isogen": 12_000, "Nocxium": 4_000}
_PRODUCED = {"Tritanium": 1_050_000, "Pyerite": 250_000, "Mexallon": 79_500,
             "Isogen": 15_250}   # Nocxium missing: 0 produced
_SURPLUS = {"Tritanium": 50_000, "Pyerite": 0, "Mexallon": 0, "Isogen": 3_250}
_MULTIBUY = "\n".join(f'{o["name"]} x{o["quantity"]}' for o in _ORES)


def _compression_ctx():
    return dict(ores=[dict(o) for o in _ORES],
                total_isk=sum(o["total_price"] for o in _ORES),
                total_volume=sum(o["volume"] for o in _ORES),
                minerals_produced=dict(_PRODUCED), minerals_surplus=dict(_SURPLUS),
                target_minerals=dict(_TARGETS), multibuy_text=_MULTIBUY,
                mode="isk", hub_label="Sample Hub", price_source="Sample price source")


def _compression():
    return render_page(industry, "partials/compression_results.html",
                       "/industry/compression/calculate", **_compression_ctx())


def _comp_lists(html):
    rows = cells_rows(html)
    assert len(rows) == len(_ORES) + len(_TARGETS)
    return rows[:len(_ORES)], rows[len(_ORES):]


def test_compression_lists_are_tap_to_open_rows():
    html = _compression()
    rows = assert_mrow(html, min_rows=len(_ORES) + len(_TARGETS))
    assert len(rows) == len(_ORES) + len(_TARGETS)
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in _classes(r["attrs"])
        assert "b-table-row" in _classes(r["attrs"]), "still the desktop flex row"
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not _untagged(r), "every desktop cell has a phone place"


def test_ore_rows_lead_keys_and_labels():
    ores, _ = _comp_lists(_compression())
    for row, ore in zip(ores, _ORES):
        (lead,) = row_lead(row)
        assert lead["tag"] == "img"
        assert "m-only" in _classes(lead["attrs"]), "phones get their own icon copy"
        assert f"/types/{ore['type_id']}/icon" in lead["attrs"]["src"]
        assert (lead["attrs"]["width"], lead["attrs"]["height"]) == ("20", "20")
        assert "data-on-error" not in lead["attrs"], (
            "an inline hide can't beat the lead's display:flex !important")

        k1, k2 = row_keys(row)
        assert k1["text"] == ore["name"]
        assert "comp-ore-key" in _classes(k1["attrs"]), "sized to its 11px name on phones"
        in_cell = [k for k in k1["kids"] if f"/types/{ore['type_id']}/icon" in k.get("src", "")]
        assert len(in_cell) == 1 and "m-hide" in _classes(in_cell[0]), (
            "the desktop icon stays in the name cell, hidden on phones")
        assert k2["text"] == _isk(ore["total_price"]), "key 2 is the total ISK (D9 A)"
        assert "color:var(--accent)" in k2["attrs"]["style"]

        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Qty", "Price / unit", "Volume"]
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == ore["name"]
        assert labelled["Qty"]["text"] == f"{ore['quantity']:,}"
        assert labelled["Price / unit"]["text"] == f"{ore['price_each']:,.0f}"
        assert labelled["Volume"]["text"] == f"{ore['volume']:,.1f} m3"


# The ore row's opening tag exactly as the base template (30f8534) wrote it.
_BASE_HAUL_ROWS = """{% for ore in ores %}<div class="b-table-row"
             data-haul-name="{{ ore.name }}" data-haul-qty="{{ ore.quantity }}"
             data-haul-volume="{{ ore.volume }}"></div>{% endfor %}"""


def _haul_attrs(html):
    return [{k: v for k, v in a.items() if k.startswith("data-haul-")}
            for _, a, _ in _tree(html) if "data-haul-name" in a]


def test_ore_rows_keep_the_haul_attributes_send_to_hauling_reads():
    """sendToHauling collects `[data-haul-name]` under `#comp-ore-rows` and
    reads only the three attributes, so they must match the base render and
    nothing the phone layer adds may carry one."""
    html = _compression()
    base = industry.templates.env.from_string(_BASE_HAUL_ROWS).render(ores=_ORES)
    assert _haul_attrs(html) == _haul_attrs(base)
    assert len(_haul_attrs(base)) == len(_ORES)

    els = _tree(html)
    (scope,) = [i for i, (_, a, _) in enumerate(els) if a.get("id") == "comp-ore-rows"]
    carriers = [i for i, (_, a, _) in enumerate(els)
                if any(k.startswith("data-haul-") and k != "data-haul-scope" for k in a)]
    assert all(els[i][2] == scope for i in carriers), "only the ore rows, direct in the scope"
    assert all("m-row" in _classes(els[i][1]) for i in carriers)
    (btn,) = [a for _, a, _ in els if a.get("data-click") == "sendToHauling"]
    assert btn["data-haul-scope"] == "#comp-ore-rows"


def test_copy_for_multibuy_still_copies_the_same_text():
    html = _compression()
    (btn,) = [a for _, a, _ in _tree(html) if a.get("data-click") == "copyToClipboard"]
    assert btn["id"] == "comp-copy-btn" and btn["data-copy-from"] == "comp-multibuy-text"
    m = re.search(r'<textarea id="comp-multibuy-text"[^>]*>(.*?)</textarea>', html, re.S)
    assert m and unescape(m.group(1)) == _MULTIBUY


def test_mineral_rows_keys_and_labels():
    _, minerals = _comp_lists(_compression())
    for row, (name, target) in zip(minerals, _TARGETS.items()):
        produced, surplus = _PRODUCED.get(name, 0), _SURPLUS.get(name, 0)
        assert not row_lead(row), "minerals have no icon"
        k1, k2 = row_keys(row)
        assert k1["text"] == name
        assert k2["text"] == f"{produced:,}", "key 2 is the produced amount"
        colour = "var(--success)" if produced >= target else "var(--danger)"
        assert f"color:{colour}" in k2["attrs"]["style"], "green or red as on desktop"
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Target", "Surplus"]
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == name
        assert labelled["Target"]["text"] == f"{target:,}"
        assert labelled["Surplus"]["text"] == (f"+{surplus:,}" if surplus > 0 else "—")


def test_compression_header_rows_are_m_head_and_the_total_row_stays_plain():
    els = _tree(_compression())
    heads = [a for _, a, _ in els if "m-head" in _classes(a)]
    assert len(heads) == 2, "the ore and mineral header rows"
    assert all("b-table-row" in _classes(a) and "border-bottom:2px" in a["style"] for a in heads)
    totals = [i for i, (_, a, _) in enumerate(els)
              if "b-table-row" in _classes(a) and "border-top:2px" in a.get("style", "")]
    assert len(totals) == 1
    total = els[totals[0]][1]
    assert "m-row" not in _classes(total) and "m-head" not in _classes(total)
    # Phones: the two empty spacer cells go, so ISK and volume keep one line.
    assert "comp-ore-total" in _classes(total)
    cells = [a for _, a, p in els if p == totals[0]]
    assert len(cells) == 5
    assert ["m-hide" in _classes(a) for a in cells] == [False, True, True, False, False]


def test_results_head_buttons_get_a_wrap_hook():
    els = _tree(_compression())
    (copy_idx,) = [i for i, (_, a, _) in enumerate(els) if a.get("id") == "comp-copy-btn"]
    (haul_idx,) = [i for i, (_, a, _) in enumerate(els) if a.get("id") == "comp-haul-btn"]
    group = els[copy_idx][2]
    assert els[haul_idx][2] == group
    assert "comp-head-actions" in _classes(els[group][1])


# ── Compression skill chips (built in app/routes/industry.py) ──────────

class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _DB:
    def __init__(self, char):
        self.char = char

    async def execute(self, stmt):
        return _Result(self.char)


_SKILL_LEVELS = {60377: 5, 60378: 4, 60379: 3, 60380: 2, 60381: 1, 12189: 0,
                 industry.SKILL_REPROCESSING: 5, industry.SKILL_REPROCESSING_EFFICIENCY: 4}


def _skills_html(monkeypatch):
    import app.esi.character as esi_character
    import app.esi.client as esi_client

    async def fake_refresh(char, db):
        return "sample-token"

    async def fake_get_skills(client, character_id):
        return {"skills": [{"skill_id": s, "active_skill_level": lv}
                           for s, lv in _SKILL_LEVELS.items()]}

    monkeypatch.setattr(esi_client, "refresh_token", fake_refresh)
    monkeypatch.setattr(esi_character, "get_skills", fake_get_skills)
    monkeypatch.setattr(industry, "ESIClient", lambda token, db=None: object())
    char = types.SimpleNamespace(character_id=90000001, user_id=1,
                                 scopes="esi-skills.read_skills.v1")
    resp = asyncio.run(industry.compression_skills(
        request("/industry/compression/skills/90000001"), 90000001, _DB(char)))
    return resp.body.decode()


def test_skill_chips_carry_a_class_hook(monkeypatch):
    html = _skills_html(monkeypatch)
    assert "Failed to load skills" not in html
    els = _tree(html)
    chips = [a for _, a, _ in els if "comp-skill-chip" in _classes(a)]
    assert len(chips) == 6, "Simple, Coherent, Variegated, Complex, Abyssal, Mercoxit"
    for label, lv in (("Simple", 5), ("Coherent", 4), ("Mercoxit", 0)):
        assert re.search(rf'class="comp-skill-chip"[^>]*>{label} <strong>{lv}</strong></span>', html)
    # The form fields and the ore-skill JSON the page's afterSwap reads stay.
    names = {a.get("name") for t, a, _ in els if t == "input"}
    assert {"repro_level", "eff_level"} <= names
    assert any(a.get("id") == "skill-ore-json" for _, a, _ in els)
    assert 'value="5"' in html and 'value="4"' in html


def test_compression_page_keeps_its_skill_ids():
    src = source("compression.html")
    for hook in ('id="skill-repro"', 'id="skill-eff"', 'id="ore-skills-input"',
                 "getElementById('skill-ore-json')", 'id="skill-display"'):
        assert hook in src


# ── Hauling: resolved items and ship recommendations ──────────────────

def _items(n):
    bays = ["cargo", "ore", "fleet_hangar"]
    out = []
    for i in range(n):
        qty = 10 * (i + 1) ** 2
        vol = 0.01 + 1.5 * i
        out.append({"type_id": 34 + i, "name": f"Sample Item {i + 1:02d}", "qty": qty,
                    "volume": vol, "total_volume": round(vol * qty, 2), "bay": bays[i % 3]})
    return out


_RECS = [
    {"type_id": 20185, "name": "Sample Freighter", "group": "Freighter",
     "total_capacity": 1_100_000.0, "trips": 1},
    {"type_id": 12731, "name": "Sample Transport", "group": "Deep Space Transport",
     "total_capacity": 62_500.0, "trips": 2},
    {"type_id": 648, "name": "Sample Hauler", "group": "Hauler",
     "total_capacity": 38_000.0, "trips": 3},
    {"type_id": 649, "name": "Sample Industrial With A Long Name", "group": "Hauler",
     "total_capacity": 9_800.0, "trips": 12},
]


def _hauling(n=14, recs=_RECS, unresolved=("Unknown Sample Thing",)):
    items = _items(n)
    by_bay = {}
    for it in items:
        by_bay[it["bay"]] = by_bay.get(it["bay"], 0) + it["total_volume"]
    html = render_page(industry, "partials/hauling_resolved.html", "/industry/hauling/resolve",
                       items=items, unresolved=list(unresolved), items_by_bay=by_bay,
                       total_volume=sum(it["total_volume"] for it in items),
                       recommendations=[dict(r) for r in recs], bay_labels=BAY_LABELS)
    return html, items


def test_hauling_lists_are_tap_to_open_rows():
    html, items = _hauling()
    rows = assert_mrow(html, min_rows=len(items) + len(_RECS))
    assert len(rows) == len(items) + len(_RECS)
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "b-table-row" in _classes(r["attrs"])
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not _untagged(r)


def test_resolved_item_rows():
    html, items = _hauling()
    rows = cells_rows(html)[:len(items)]
    for row, it in zip(rows, items):
        (lead,) = row_lead(row)
        assert lead["tag"] == "img" and "m-only" in _classes(lead["attrs"])
        assert f"/types/{it['type_id']}/icon" in lead["attrs"]["src"]
        assert (lead["attrs"]["width"], lead["attrs"]["height"]) == ("16", "16")
        assert "data-on-error" not in lead["attrs"]
        k1, k2 = row_keys(row)
        assert k1["text"] == it["name"]
        in_cell = [k for k in k1["kids"] if "src" in k]
        assert len(in_cell) == 1 and "m-hide" in _classes(in_cell[0])
        assert k2["text"] == f"{it['total_volume']:,.1f}", "key 2 is the total m³ (D10 A)"
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Qty", "Unit m³", "Bay"]
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == it["name"]
        assert labelled["Qty"]["text"] == f"{it['qty']:,}"
        assert labelled["Unit m³"]["text"] == f"{it['volume']:,.2f}"
        assert labelled["Bay"]["text"] == BAY_LABELS[it["bay"]]


@pytest.mark.parametrize("n, button", [(14, "Show all 14"), (11, "Show all 11"), (10, None), (3, None)])
def test_resolved_items_show_all_past_ten(n, button):
    html, _ = _hauling(n)
    c = clamps(html)
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == n, "only the item rows are clamped"
    assert c.wraps == 1 and c.nested_wraps == 0
    if button is None:
        assert c.showall == []
    else:
        (b,) = c.showall
        assert b["text"] == button and b["in_wrap"]
        assert "m-only" in b["attrs"]["class"].split()
        assert b["attrs"]["data-click"] == "toggleExpanded"
        assert b["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
        assert b["attrs"]["type"] == "button"
    # The wrap is never an m-row's direct parent: `.is-expanded > .m-row`
    # would open every row when Show all is tapped.
    els = _tree(html)
    for i, (_, a, parent) in enumerate(els):
        if "m-row" in _classes(a):
            assert "m-clamp-wrap" not in _classes(els[parent][1])
    # The header row sits outside the clamp, so it isn't counted as row 1.
    (head,) = [i for i, (_, a, _) in enumerate(els)
               if "m-head" in _classes(a) and _inside(els, i, lambda t, p: "m-clamp-wrap" in _classes(p))]
    assert not _inside(els, head, lambda t, p: "m-clamp" in _classes(p))


def test_ship_recommendation_rows():
    html, items = _hauling(n=4)
    rows = cells_rows(html)[len(items):]
    assert len(rows) == len(_RECS)
    for row, rec in zip(rows, _RECS):
        (lead,) = row_lead(row)
        assert lead["tag"] == "img" and "m-only" in _classes(lead["attrs"])
        assert f"/types/{rec['type_id']}/render" in lead["attrs"]["src"]
        assert (lead["attrs"]["width"], lead["attrs"]["height"]) == ("20", "20")
        assert "data-on-error" not in lead["attrs"]
        k1, k2 = row_keys(row)
        assert k1["text"] == rec["name"]
        in_cell = [k for k in k1["kids"] if "src" in k]
        assert len(in_cell) == 1 and "m-hide" in _classes(in_cell[0])
        assert k2["text"] == str(rec["trips"]), "key 2 is the trips (D11 A)"
        colour = ("var(--success)" if rec["trips"] <= 1 else
                  "var(--accent)" if rec["trips"] <= 3 else "var(--text)")
        assert f"color:{colour}" in k2["attrs"]["style"], "colour-coded as on desktop"
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Type", "Capacity"]
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == rec["name"]
        assert labelled["Type"]["text"] == rec["group"]
        assert labelled["Capacity"]["text"] == f"{rec['total_capacity']:,.0f} m³"
    assert "background:rgba(var(--accent-rgb" in rows[0]["attrs"].get("style", ""), (
        "the best ship keeps its highlight")


def test_hauling_header_rows_are_m_head_and_bay_volumes_stay():
    html, _ = _hauling()
    els = _tree(html)
    heads = [a for _, a, _ in els if "m-head" in _classes(a)]
    assert len(heads) == 2, "the items and recommendations header rows"
    assert all("b-table-row" in _classes(a) for a in heads)
    (bay,) = [a for _, a, _ in els if "data-bay-volumes" in a]
    assert bay["data-bay-volumes"].startswith("{")


# ── Hauling page: ship entry fields and the trip breakdown ────────────

def _hauling_page():
    return render_page(industry, "hauling.html", "/industry/hauling",
                       ships_by_group=get_ships_by_group(), all_ships=HAULING_SHIPS,
                       cargo_modules=CARGO_MODULES, cargo_rigs=CARGO_RIGS,
                       bay_labels=BAY_LABELS)


def test_ship_entry_fields_get_a_two_up_hook():
    html = _hauling_page()
    tpl = html[html.index('<template id="ship-entry-tpl">'):html.index("</template>")]
    els = _tree(tpl)
    (row,) = [i for i, (_, a, _) in enumerate(els) if "haul-entry-fields" in _classes(a)]
    fields = [i for i, (_, _, p) in enumerate(els) if p == row]
    assert len(fields) == 8, "Qty, Ship, Expander, Mods, Rig, Rigs, Skill, remove"
    # The fields JS reads are all still there.
    data_fields = {a.get("data-field") for _, a, _ in els if a.get("data-field")}
    assert data_fields == {"qty", "ship", "mod", "modCount", "rig", "rigCount", "skill"}
    # The remove control: its wrapper is the last field, and the button has
    # an accessible name (its only text is the ✕ glyph).
    (btn,) = [i for i, (_, a, _) in enumerate(els) if a.get("data-click") == "removeShipEntry"]
    wrap = els[btn][2]
    assert wrap == fields[-1] and "haul-entry-remove" in _classes(els[wrap][1])
    assert els[btn][1].get("aria-label") == "Remove ship"


def test_trip_breakdown_wraps_but_is_not_tap_to_open():
    src = source("hauling.html")
    start = src.index("breakdownHtml += ")
    block = src[start:src.index("});", start)]
    assert "haul-breakdown-row" in block and "haul-breakdown-bays" in block
    assert "haul-breakdown-trip" in block
    assert "m-row" not in block and "toggleMRow" not in block and "data-click" not in block
    assert "+ bayDetail +" in block


def test_hauling_tabs_stay_as_they_are():
    html = _hauling_page()
    for tab in ("manual", "paste"):
        assert re.search(rf'<button type="button" data-click="showTabFromEl" data-tab="{tab}" '
                         rf'id="tab-{tab}" class="b-btn"', html)
    assert 'id="panel-manual"' in html and 'id="panel-paste"' in html
    assert "container.querySelector('[data-bay-volumes]')" in source("hauling.html")


# ── CSS ───────────────────────────────────────────────────────────────

def _phone():
    body, after = phone_block(_section("T4"))
    assert after.strip() == "", "no desktop rules: desktop renders as before (D21)"
    return body


def _decls(css, selector):
    body = rule_bodies(css, selector)
    assert body, f"no phone rule for {selector}"
    return re.sub(r"\s+", " ", body)


def test_css_skill_chips_stay_compact():
    """The chips are read-only spans with no handler, so they keep their
    natural height on phones (a 40px tap size only added height). Only the
    label grows, from the inline 10px to 12px, for legibility."""
    d = _decls(_phone(), ".comp-skill-chip")
    assert "font-size: 12px !important" in d
    assert "min-height" not in d and "min-width" not in d and "display" not in d


def test_css_ore_key_one_lines_up_with_key_two():
    """The ore name cell has no font size of its own (its inner span is
    11px), so as a phone block its line box would be the page's 16px one and
    the name would sit 4px off the 11px total ISK beside it."""
    assert "font-size: 11px" in _decls(_phone(), ".comp-ore-key")


def test_css_results_head_buttons_wrap():
    """.b-btn is flex:1 (basis 0%), which shares the line out equally and
    wraps "Copy for Multibuy" inside its button; a content basis and nowrap
    let the pair wrap as whole buttons instead."""
    css = _phone()
    assert "flex-wrap: wrap" in _decls(css, ".comp-head-actions")
    btn = _decls(css, ".comp-head-actions > .b-btn")
    assert "flex: 1 1 auto" in btn and "white-space: nowrap" in btn


def test_css_ore_total_row_keeps_its_figures_on_one_line():
    css = _phone()
    assert "flex: 0 0 auto !important" in _decls(css, ".comp-ore-total > *"), "beats the inline flex:1"
    assert "flex: 1 1 auto !important" in _decls(css, ".comp-ore-total > :first-child")


def test_css_ship_entry_fields_go_two_up():
    css = _phone()
    d = _decls(css, ".haul-entry-fields")
    assert "display: grid !important" in d, "beats the inline display:flex"
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in d
    kids = _decls(css, ".haul-entry-fields > *")
    assert "min-width: 0 !important" in kids, "beats the inline 50/140/160px minimums"
    assert "width: 100% !important" in _decls(css, ".haul-entry-fields input"), (
        "beats the inline 50px inputs")
    sel = _decls(css, ".haul-entry-fields select")
    assert "width: 100%" in sel and "!important" not in sel, "the selects have no inline width"


def test_css_remove_button_is_content_width():
    """In the two-up grid the ✕ would stretch to a whole column (about
    149px) and read like an empty field; it stays a 44px square."""
    css = _phone()
    assert "align-items: flex-start" in _decls(css, ".haul-entry-fields > .haul-entry-remove")
    btn = _decls(css, ".haul-entry-remove > button")
    assert "min-width: 44px" in btn
    assert "font-size: 14px !important" in btn, "beats the inline 11px"


def test_css_trip_breakdown_wraps():
    css = _phone()
    assert "flex-wrap: wrap" in _decls(css, ".haul-breakdown-row")
    bays = _decls(css, ".haul-breakdown-row > .haul-breakdown-bays")
    assert re.search(r"flex: 1 1 100% !important", bays), "own line, beating the inline flex:2"
    trip = _decls(css, ".haul-breakdown-row > .haul-breakdown-trip")
    assert "flex: 0 0 auto !important" in trip and "white-space: nowrap" in trip, (
        "m³/trip keeps one line instead of a squeezed third of the row")
