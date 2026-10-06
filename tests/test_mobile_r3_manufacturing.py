"""Mobile R3 T1: the tool landings and the Manufacturing calculator on phones.

`tool_landing.html` serves /industry, /intel and /tools. On phones its card
grid is one column and each card keeps its title and description; the
features list is hidden (user decision D1 A).

The Manufacturing calculator (`industry.html` and its htmx fragments):
- the bill of materials (`calc_results.html`) and each component panel's
  materials are expand-on-tap rows: icon lead, name, needed quantity
  (D2 A), then Name, Base, Saved, Unit and Total when opened. Total rows
  lose their empty columns;
- the build-time boxes become label/value lines (D3 A);
- the shopping list rows key on name and quantity (D4 A) and keep the
  data-haul-* attributes Send to Hauling reads;
- the controls stack, sliders go full width, and the Build/Buy, Build All,
  ↓ Parent buttons keep the 44px phone button height (no m-tap) and the
  blueprint-search rows are 40px tap targets.

Desktop renders as before (D21): phone-only cells are m-only, and the rest
is CSS inside the R3 T1 section of site.css. Names and ids are invented."""
import asyncio
import functools
import re
from html.parser import HTMLParser

import pytest

from app.routes import industry as industry_mod
from app.routes import landings as landings_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           css_section, norm, phone_block, render_page, request,
                           row_keys, row_labelled, row_lead, rule_bodies)

_section = functools.partial(css_section, release="R3")


# ── A small element tree ──────────────────────────────────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def all(self):
        for c in self.children:
            yield c
            yield from c.all()

    def find(self, cls):
        return [n for n in self.all() if cls in n.classes]

    def by_id(self, id_):
        found = [n for n in self.all() if n.attrs.get("id") == id_]
        assert len(found) == 1, f"expected one #{id_}, found {len(found)}"
        return found[0]

    def full_text(self):
        return norm(self.text + " " + " ".join(c.full_text() for c in self.children))


class _Tree(HTMLParser):
    """No implied end tags: fine for the hand-written templates here."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


# ── Tool landings (D1 A) ──────────────────────────────────────────────

_LANDINGS = {
    "/industry": ("Industry", landings_mod.INDUSTRY_TOOLS),
    "/intel": ("Intel", landings_mod.INTEL_TOOLS),
    "/tools": ("Tools", landings_mod.TOOLS_TOOLS),
}


def _render_landing(path):
    title, tools = _LANDINGS[path]
    return render_page(landings_mod, "tool_landing.html", path, page_title=title,
                       page_subtitle="Sample subtitle", tools=tools)


@pytest.mark.parametrize("path", _LANDINGS)
def test_landing_cards_hide_their_features_on_phones(path):
    tools = _LANDINGS[path][1]
    assert tools and any(t["features"] for t in tools)
    root = _tree(_render_landing(path))
    grids = root.find("tl-grid")
    assert len(grids) == 1
    cards = grids[0].children
    assert len(cards) == len(tools)
    for card, tool in zip(cards, tools):
        # The whole card is still the link, and only the link.
        assert card.tag == "a" and "tl-card" in card.classes
        assert card.attrs["href"] == tool["url"]
        assert "data-click" not in card.attrs
        assert card.find("tl-card-title") and card.find("tl-card-desc")
        assert card.find("tl-card-desc")[0].full_text() == norm(tool["desc"])
        feats = card.find("tl-card-features")
        if tool["features"]:
            assert len(feats) == 1
            assert "m-hide" in feats[0].classes, "the features list is desktop-only"
        else:
            assert not feats
        # Nothing else in the card is phone-hidden: title and description stay.
        assert all("m-hide" not in n.classes for n in card.all() if n not in feats)


@pytest.mark.parametrize("path", _LANDINGS)
def test_landing_subtitle_has_its_phone_hook(path):
    root = _tree(_render_landing(path))
    subs = root.find("tl-subtitle")
    assert len(subs) == 1 and subs[0].full_text() == "Sample subtitle"


# ── Bill of materials (D2 A) ──────────────────────────────────────────

_RUNS = 2


def _mat(type_id, name, base, adjusted, unit, buildable):
    return {"type_id": type_id, "name": name, "base_qty": base, "adjusted_qty": adjusted,
            "saved": base * _RUNS - adjusted, "unit_price": unit,
            "line_cost": unit * adjusted, "buildable": buildable,
            "sub_bp_id": type_id + 100 if buildable else None,
            "build_time_str": None, "build_time_secs": 0}


_MATERIALS = [
    _mat(2001, "Sample Alloy", 1_000_000, 1_800_000, 5.5, False),       # saved
    _mat(2002, "Sample Plating", 50, 100, 12_000.0, True),              # nothing saved
    _mat(2003, "Sample Conduit Assembly With A Long Name", 400, 760, 840.25, True),
    _mat(2004, "Sample Coolant", 30, 60, 99.0, False),
    _mat(2005, "Sample Lattice", 12_345, 23_456, 1.0, False),
    _mat(2006, "Sample Unpriced Part", 8, 16, 0, False),                # no market price
]


def _render_calc(**over):
    ctx = dict(bp_name="Sample Hull Blueprint", rows=_MATERIALS, runs=_RUNS, me=10, te=20,
               total_cost=1_234_567_890.0, total_saved_cost=4_560_000.0,
               modifiers=["ME 10"], structures=industry_mod.STRUCTURES, rigs=industry_mod.RIGS,
               sec_statuses=industry_mod.SEC_STATUS, main_time_str="1d 2h 0m",
               parallel_time_str="2d 4h 0m", sequential_time_str="3d 6h 0m",
               main_time_secs=93_600)
    ctx.update(over)
    return render_page(industry_mod, "partials/calc_results.html", "/industry/calculate", **ctx)


def _expected_mat_cells(m, base_qty, saved):
    priced = m["unit_price"] > 0
    return {
        "Name": m["name"],
        "Base": f"{base_qty:,}",
        "Saved": f"-{saved:,}" if saved > 0 else "—",
        "Unit": f"{m['unit_price']:,.0f}" if priced else "—",
        "Total": f"{m['line_cost']:,.0f}" if priced else "—",
    }


def _assert_material_rows(html, mats, base_of, saved_of):
    rows = assert_mrow(html, min_rows=len(mats))
    assert len(rows) == len(mats)
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in r["attrs"]["class"].split()
        assert "mfg-mat" in r["attrs"]["class"].split()
    for row, m in zip(cells_rows(html), mats):
        assert_single_value_child(row)
        # Lead: an m-only wrapper holding a sized copy of the icon. The
        # wrapper, not the img, is the tagged cell, so data-on-error="hide"
        # on the img can still hide it.
        lead, = row_lead(row)
        assert lead["tag"] == "span" and "m-only" in lead["attrs"]["class"].split()
        img, = lead["kids"]
        assert f"/types/{m['type_id']}/icon" in img["src"]
        assert (img.get("width"), img.get("height")) == ("20", "20")
        assert img.get("data-on-error") == "hide"
        k1, k2 = row_keys(row)
        assert k1["text"] == m["name"]
        # The desktop icon inside the name cell is hidden on phones (the lead
        # replaces it).
        icons = [k for k in k1["kids"] if "/icon" in k.get("src", "")]
        assert len(icons) == 1 and "m-hide" in icons[0]["class"].split()
        assert k2["text"] == f"{m['adjusted_qty']:,}", "key 2 is the needed quantity"
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Base", "Saved", "Unit", "Total"]
        assert "m-only" in labelled["Name"]["attrs"]["class"].split()
        expected = _expected_mat_cells(m, base_of(m), saved_of(m))
        assert {k: c["text"] for k, c in labelled.items()} == expected
        saved_style = labelled["Saved"]["attrs"]["style"].replace(" ", "")
        if saved_of(m) > 0:
            assert "color:var(--success)" in saved_style, "Saved stays green"
        if m["unit_price"] > 0:
            assert "color:var(--accent)" in labelled["Total"]["attrs"]["style"].replace(" ", "")
        # Every cell is tagged: nothing desktop-only is left to hide.
        assert all("data-m" in c["attrs"] or "data-m-label" in c["attrs"] for c in row["cells"])


def test_bill_of_materials_rows():
    html = _render_calc()
    _assert_material_rows(html, _MATERIALS, lambda m: m["base_qty"] * _RUNS,
                          lambda m: m["saved"])


def test_bill_of_materials_header_is_phone_hidden():
    root = _tree(_render_calc())
    heads = root.find("m-head")
    assert len(heads) == 1
    assert [c.full_text() for c in heads[0].children] == [
        "Material", "Base", "Needed", "Saved", "Unit", "Total"]


def test_bill_of_materials_build_toggles_keep_their_hooks():
    root = _tree(_render_calc())
    buildable = [m for m in _MATERIALS if m["buildable"]]
    btns = root.find("build-toggle-btn")
    assert len(btns) == len(buildable)
    for btn, m in zip(btns, buildable):
        assert btn.tag == "button"
        assert "m-tap" not in btn.classes, "m-tap's 40px would shrink the 44px button"
        assert btn.attrs["data-click"] == "toggleComponentFromEl"
        assert btn.attrs["data-type-id"] == str(m["type_id"])
        assert btn.attrs["data-needed"] == str(m["adjusted_qty"])
        assert btn.attrs["data-qty"] == str(m["adjusted_qty"])
        assert "mfg-build" in btn.parent.classes
        assert "m-row" not in btn.parent.classes, "Build stays visible, outside the row"
        panel = root.by_id(f"component-{m['type_id']}")
        assert panel.attrs["style"].replace(" ", "") == "display:none;"
        assert not panel.classes, "JS shows and hides the panel inline"
    build_all = root.by_id("build-all-btn")
    assert "m-tap" not in build_all.classes
    assert build_all.attrs["data-click"] == "toggleBuildAll"


def test_bill_of_materials_totals_are_label_value_lines():
    root = _tree(_render_calc())
    totals = root.find("mfg-total")
    assert [t.children[0].full_text() for t in totals] == [
        "Total Material Cost (Buy All)", "Material savings from bonuses"]
    assert [t.children[-1].full_text() for t in totals] == ["1.23B ISK", "-4.56M ISK"]
    for t in totals:
        assert "m-row" not in t.classes
        spacers = t.children[1:-1]
        assert len(spacers) == 4
        assert all("m-hide" in s.classes and not s.full_text() for s in spacers)


def test_build_time_boxes_have_line_hooks():
    root = _tree(_render_calc())
    rows = root.find("mfg-tiles")
    assert len(rows) == 1
    tiles = rows[0].children
    assert all("mfg-tile" in t.classes for t in tiles)
    lines = []
    for tile in tiles:
        assert [c.classes[0] for c in tile.children] == [
            "mfg-tile-label", "mfg-tile-value", "mfg-tile-sub"]
        lines.append([c.full_text() for c in tile.children])
    assert lines == [["Parallel Build", "2d 4h 0m", "components + final"],
                     ["Sequential Build", "3d 6h 0m", "one at a time"],
                     ["Final Assembly", "1d 2h 0m", "after components"]]


def test_bill_of_materials_with_nothing_saved_has_no_savings_row():
    root = _tree(_render_calc(total_saved_cost=0))
    assert len(root.find("mfg-total")) == 1


# ── Component panels ──────────────────────────────────────────────────

_NEEDED = 6
_SUB = [
    {"type_id": 3001, "name": "Sample Component Alpha", "base_qty": 60, "adjusted_qty": 54,
     "unit_price": 1_500.0, "line_cost": 81_000.0, "buildable": True},
    {"type_id": 3002, "name": "Sample Component Beta", "base_qty": 12, "adjusted_qty": 12,
     "unit_price": 220.5, "line_cost": 2_646.0, "buildable": False},
    {"type_id": 3003, "name": "Sample Component Gamma", "base_qty": 6, "adjusted_qty": 6,
     "unit_price": 0, "line_cost": 0, "buildable": False},
]


def _render_component():
    return render_page(industry_mod, "partials/component_panel.html", "/industry/component",
                       product_name="Sample Plating", type_id=2002, needed=_NEEDED, me=10,
                       structure="npc_station", rig="none", security="highsec",
                       sub_rows=_SUB, total_build_cost=83_646.0, total_buy_cost=1_200_000.0,
                       buy_unit_price=200_000.0, structures=industry_mod.STRUCTURES,
                       rigs=industry_mod.RIGS, sec_statuses=industry_mod.SEC_STATUS)


def test_component_material_rows_match_the_bill_of_materials():
    _assert_material_rows(_render_component(), _SUB, lambda m: m["base_qty"],
                          lambda m: max(0, m["base_qty"] - m["adjusted_qty"]))


def test_component_panel_phone_hooks():
    root = _tree(_render_component())
    panel = root.children[0]
    assert "mfg-comp" in panel.classes, "its 2.25rem indent shrinks on phones"
    settings = panel.children[0]
    assert "m-stack" in settings.classes, "the settings stack on phones"
    assert settings.find("mfg-fill") == [root.by_id("sub-me-2002")]
    # "× N" stays whole beside a long product name.
    qty, = settings.find("mfg-comp-qty")
    assert qty.full_text() == f"× {_NEEDED:,}" and "b-muted-sm" in qty.classes
    parent_btn, = [n for n in settings.all() if n.attrs.get("data-click") == "copyParentSettings"]
    assert "m-tap" not in parent_btn.classes
    sub_btn, = [n for n in root.all() if n.attrs.get("data-click") == "toggleSubComponent"]
    assert "m-tap" not in sub_btn.classes
    # toggleBuildAll collects every .build-toggle-btn on the page: a nested
    # Build button must never join them.
    assert "build-toggle-btn" not in sub_btn.classes
    assert sub_btn.attrs["data-type-id"] == "3001" and sub_btn.attrs["data-qty"] == "54"
    assert "mfg-build" in sub_btn.parent.classes
    sub = root.by_id("subcomp-3001")
    assert sub.attrs["style"].replace(" ", "") == "display:none;" and not sub.classes


# ── Shopping list (D4 A) ──────────────────────────────────────────────

_ITEMS = [
    {"name": "Sample Alloy", "qty": 1_800_000, "volume": 0.01, "total_volume": 18_000.0,
     "type_id": 2001},
    {"name": "Sample Coolant", "qty": 60, "volume": 5.0, "total_volume": 300.0, "type_id": 2004},
    {"name": "Sample Lattice", "qty": 23_456, "volume": 0.5, "total_volume": 11_728.0,
     "type_id": 2005},
    {"name": "Sample Unpriced Part", "qty": 16, "volume": 10, "total_volume": 160.0,
     "type_id": 2006},
]
_PRICES = {"Sample Alloy": 5.5, "Sample Coolant": 99.0, "Sample Lattice": 1.0,
           "Sample Unpriced Part": 0}


def _render_shopping():
    return render_page(industry_mod, "partials/shopping_list.html", "/industry/shopping-list",
                       items=_ITEMS, item_count=len(_ITEMS), total_cost=9_929_396.0,
                       total_volume=30_188.0, multibuy_text="Sample Alloy x1800000",
                       price_map=_PRICES, mineral_items={34: 1000})


def _cost(p, qty):
    cost = p * qty
    if cost >= 1e9:
        return f"{cost / 1e9:.1f}B"
    if cost >= 1e6:
        return f"{cost / 1e6:.1f}M"
    return f"{cost:,.0f}"


def test_shopping_list_rows():
    html = _render_shopping()
    rows = assert_mrow(html, min_rows=len(_ITEMS))
    assert len(rows) == len(_ITEMS)
    for r, item in zip(rows, _ITEMS):
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "mfg-shop" in r["attrs"]["class"].split()
        # Send to Hauling reads these, unchanged.
        assert r["attrs"]["data-haul-name"] == item["name"]
        assert r["attrs"]["data-haul-qty"] == str(item["qty"])
        assert r["attrs"]["data-haul-volume"] == str(item["volume"])
    for row, item in zip(cells_rows(html), _ITEMS):
        assert_single_value_child(row)
        assert not row_lead(row)
        k1, k2 = row_keys(row)
        assert k1["text"] == item["name"]
        assert k2["text"] == f"{item['qty']:,}"
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Volume", "Cost"]
        assert "m-only" in labelled["Name"]["attrs"]["class"].split()
        assert labelled["Name"]["text"] == item["name"]
        assert labelled["Volume"]["text"] == f"{item['total_volume']:,.1f} m³"
        p = _PRICES[item["name"]]
        assert labelled["Cost"]["text"] == (_cost(p, item["qty"]) if p > 0 else "—")


def test_shopping_list_keeps_its_hauling_scope_and_wraps_its_buttons():
    root = _tree(_render_shopping())
    scope = root.by_id("shopping-items")
    assert [n for n in scope.all() if "data-haul-name" in n.attrs] == scope.children
    haul = root.by_id("haul-btn")
    assert haul.attrs["data-haul-scope"] == "#shopping-items"
    actions = root.find("mfg-shop-actions")
    assert len(actions) == 1
    assert {b.attrs["id"] for b in actions[0].children if b.tag == "button"} == {
        "copy-btn", "compress-btn", "haul-btn"}
    total, = root.find("mfg-total")
    assert [c.full_text() for c in total.children] == ["Estimated Total", "30,188.0 m³",
                                                       "9.93M ISK"]


# ── Controls and blueprint search ─────────────────────────────────────

def test_manufacturing_controls_stack_on_phones():
    html = render_page(industry_mod, "industry.html", "/industry/manufacturing",
                       structures=industry_mod.STRUCTURES, rigs=industry_mod.RIGS,
                       sec_statuses=industry_mod.SEC_STATUS)
    root = _tree(html)
    settings = root.by_id("settings-panel")
    # JS shows the panel with style.display; m-stack forces display, so it
    # sits on the control row inside, never on the panel.
    assert "m-stack" not in settings.classes
    stacks = settings.find("m-stack")
    assert len(stacks) == 1
    groups = stacks[0].children
    assert len(groups) == 6
    controls = [n.attrs["id"] for g in groups for n in g.all() if n.tag in ("input", "select")]
    assert controls == ["inp-runs", "inp-me", "inp-te", "inp-structure", "inp-rig",
                        "inp-security"]
    assert [n.attrs["id"] for n in stacks[0].find("mfg-fill")] == ["inp-runs", "inp-me",
                                                                   "inp-te"]


def test_blueprint_search_rows_are_tap_targets(monkeypatch):
    async def search_types(db, q, limit=10):
        return [{"type_id": 4001, "type_name": "Sample Frigate Blueprint"},
                {"type_id": 4002, "type_name": "Sample Cruiser Blueprint"}]

    async def get_blueprint_materials(db, type_id):
        return [{"type_id": 2001, "name": "Sample Alloy", "quantity": 1}]

    monkeypatch.setattr(industry_mod.sde, "search_types", search_types)
    monkeypatch.setattr(industry_mod.sde, "get_blueprint_materials", get_blueprint_materials)
    resp = asyncio.run(industry_mod.industry_search(request("/industry/search"), q="Sample",
                                                    db=None))
    root = _tree(resp.body.decode())
    rows = root.find("mfg-pick")
    assert [r.attrs["data-type-id"] for r in rows] == ["4001", "4002"]
    for r in rows:
        assert "b-table-row" in r.classes
        assert r.attrs["data-click"] == "selectBlueprintFromEl"
        assert "m-row" not in r.classes, "a pick, not an expand-on-tap row"
    assert [r.full_text() for r in rows] == ["Sample Frigate Blueprint",
                                             "Sample Cruiser Blueprint"]


# ── CSS: R3 T1 section ────────────────────────────────────────────────

def _decl(body, prop, value):
    return re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*{re.escape(value)}\s*(?:;|$)", body)


def _phone():
    phone, after = phone_block(_section("T1"))
    assert not after.strip(), "R3 T1 has no desktop rules: desktop renders as before"
    return phone


def test_t1_css_landing_grid_is_one_column():
    css = _phone()
    # !important: the landing page's own <style> loads after site.css.
    assert _decl(rule_bodies(css, ".tl-grid"), "grid-template-columns", "minmax(0, 1fr) !important")
    for sel in (".tl-card-url", ".tl-subtitle"):
        assert _decl(rule_bodies(css, sel), "font-size", "11px !important"), sel
    assert _decl(rule_bodies(css, ".tl-card-title"), "flex-wrap", "wrap")
    # Compact cards: the desktop padding is 1rem 1.1rem.
    assert _decl(rule_bodies(css, ".tl-card"), "padding", "0.75rem !important")
    # No landing card is external (_landing_cards never sets it), so no rule
    # for its arrow.
    assert not rule_bodies(css, ".tl-card-external::after")


def test_t1_css_landing_description_is_one_line():
    """D1 A: title and a one-line description. The full text stays on each
    tool's own page, so the card ends the line with an ellipsis."""
    desc = rule_bodies(_phone(), ".tl-card-desc")
    for prop, value in (("white-space", "nowrap"), ("overflow", "hidden"),
                        ("text-overflow", "ellipsis"), ("min-width", "0")):
        assert _decl(desc, prop, value), (prop, value)


def test_t1_css_controls_go_full_width():
    css = _phone()
    assert _decl(rule_bodies(css, ".mfg-fill"), "width", "100% !important")
    pick = rule_bodies(css, ".mfg-pick")
    assert _decl(pick, "min-height", "40px")
    assert _decl(rule_bodies(css, ".mfg-pick > span"), "font-size", "12px !important")


def test_t1_css_nested_panels_indent_less():
    css = _phone()
    assert _decl(rule_bodies(css, ".mfg-comp"), "margin-left", "0.75rem !important")
    qty = rule_bodies(css, ".mfg-comp-qty")
    assert _decl(qty, "white-space", "nowrap") and _decl(qty, "flex", "none")
    assert _decl(rule_bodies(css, ".mfg-build"), "padding-left", "0.75rem !important")


def test_t1_css_material_rows():
    css = _phone()
    lead = rule_bodies(css, '.mfg-mat.m-row > [data-m="lead"]')
    assert _decl(lead, "min-width", "20px !important"), "keeps names aligned if an icon fails"
    keys = rule_bodies(css, '.mfg-mat > [data-m="key"]')
    assert _decl(keys, "font-size", "12px !important")
    assert rule_bodies(css, '.mfg-mat > [data-m="key"] > *') == keys
    assert _decl(rule_bodies(css, '.mfg-shop > [data-m="key"]'), "font-size", "12px !important")


def test_t1_css_time_boxes_become_lines():
    css = _phone()
    assert _decl(rule_bodies(css, ".mfg-tiles"), "flex-direction", "column")
    assert _decl(rule_bodies(css, ".mfg-tiles"), "gap", "0 !important")
    tile = rule_bodies(css, ".mfg-tiles > .mfg-tile")
    for prop, value in (("display", "flex"), ("flex-wrap", "wrap"),
                        ("justify-content", "space-between"), ("flex", "none !important"),
                        ("text-align", "left !important")):
        assert _decl(tile, prop, value), (prop, value)
    assert _decl(rule_bodies(css, ".mfg-tiles > .mfg-tile + .mfg-tile"),
                 "border-top", "none !important")
    assert _decl(rule_bodies(css, ".mfg-tile > .mfg-tile-value"), "text-align", "right")
    assert _decl(rule_bodies(css, ".mfg-tile > .mfg-tile-sub"), "flex-basis", "100%")


def test_t1_css_total_rows_and_shopping_buttons():
    css = _phone()
    assert _decl(rule_bodies(css, ".mfg-total > :first-child"), "flex", "1 1 auto !important")
    assert _decl(rule_bodies(css, ".mfg-total > :not(:first-child)"), "flex", "none !important")
    actions = rule_bodies(css, ".mfg-shop-actions")
    assert _decl(actions, "flex-wrap", "wrap")
    # At least 8px between the wrapped 44px buttons; the inline gap is 6px.
    assert _decl(actions, "gap", "0.5rem !important")


def test_t1_css_buttons_keep_the_44px_floor_with_12px_labels():
    """Build ▸, Build All and ↓ Parent: no m-tap, so the global phone button
    rule's 44px height applies; this section only widens them to 40px and
    lifts their inline 8–9px labels to 12px."""
    css = _phone()
    for sel in (".mfg-build > .b-btn", "#build-all-btn", ".mfg-comp > .m-stack > .b-btn"):
        body = rule_bodies(css, sel)
        assert _decl(body, "min-width", "40px"), sel
        assert _decl(body, "font-size", "12px !important"), sel
        assert "min-height" not in body and not re.search(r"(?<![-\w])height\s*:", body), sel
    assert _decl(rule_bodies(css, "#build-all-btn"), "flex", "none")


def test_component_panel_build_buy_comparison_has_its_hook():
    """Phones lift the comparison's 9px labels and captions to 11px; the
    value (each column's second line) keeps its 13px."""
    root = _tree(_render_component())
    cmp_, = root.find("mfg-cmp")
    cols = cmp_.children
    assert [c.children[0].full_text() for c in cols] == ["Build Cost", "Buy Cost"]
    for col in cols:
        assert "font-size:13px" in col.children[1].attrs["style"].replace(" ", "")


def test_t1_css_small_text_is_11px():
    """Inline 8–9px text on the calculator: tile labels and captions, "has
    blueprint", a component panel's setting labels, its Build/Buy Cost
    labels and captions, and the Multibuy hint. !important beats inline."""
    css = _phone()
    for sel in (".mfg-tile > .mfg-tile-label", ".mfg-tile > .mfg-tile-sub", ".mfg-build > span",
                ".mfg-comp > .m-stack label", ".mfg-cmp > div > div:not(:nth-child(2))",
                "#multibuy-text + div"):
        assert _decl(rule_bodies(css, sel), "font-size", "11px !important"), sel
