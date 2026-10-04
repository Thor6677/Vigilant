"""Mobile R3 T6: the planetary tools on phones.

Four pages, each phone-only (desktop renders as before, D21):

* **Calculator.** The form stops being ~724px wide (ISS-111 part 2): row 1
  stacks, the advanced row goes two to a row. The P0, colony-plan and
  production-pipeline lists become expand-on-tap rows (pipeline key 2 is the
  quantity needed, D13 A). The multi-pilot flow chart is hover-only and
  ~920px wide, so it's hidden on phones (D14 A); its hand-off summary stays.
* **Chain Explorer.** The two-column grid stacks, the detail panel stops
  being sticky, and loading a detail on a phone scrolls it into view
  (D15 A). The node fragment's links and chips are 40px tap targets.
* **System Lookup.** The fetch-injected system fragment's planets become
  rows, so the page calls mRowInit after injecting it; tier chips and the
  search dropdown rows are 40px; hover-only hints are hidden.
* **Planet detail** (loaded into the Colonies page): pin rows keyed on
  structure and expiry (D16 A); the Contents <details> still opens.

Contexts are hand-built in the shapes pi.py's routes produce. Names are
invented."""
import functools
import re

from app.routes import pi as pi_mod
from tests._mobile import (Styled, assert_mrow, assert_single_value_child, cells_rows,
                           css_section, phone_block, render_page, row_keys, row_labelled,
                           row_lead, rule_bodies, source)

_section = functools.partial(css_section, release="R3")


def _phone():
    """The body of this task's phone @media block."""
    return phone_block(_section("T6"))[0]


def _tags(html):
    s = Styled()
    s.feed(html)
    s.close()
    return s.tags


def _with_class(html, cls):
    """(tag, classes, style) of every element carrying class `cls`."""
    return [t for t in _tags(html) if cls in t[1]]


def _hooked(html, hook):
    """The cells_rows rows carrying the hook class `hook`."""
    return [r for r in cells_rows(html) if hook in r["attrs"].get("class", "").split()]


def _labels(row):
    return [c["attrs"]["data-m-label"] for c in row["cells"] if "data-m-label" in c["attrs"]]


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _classes(cell):
    return cell["attrs"].get("class", "").split()


# ── Calculator fixture ───────────────────────────────────────────────

_P0 = [
    {"type_id": 91001, "name": "Sample Gas", "qty": 18000.0, "producible": True,
     "planet_sources": ["gas", "ice"]},
    {"type_id": 91002, "name": "Sample Ions", "qty": 12000.4, "producible": False,
     "planet_sources": ["plasma"]},
    {"type_id": 91003, "name": "Sample Microbes", "qty": 6000.0, "producible": True,
     "planet_sources": ["barren", "temperate"]},
]
_TARGET = 94001


def _tier_row(tid, name, qty, cycles, factories, inputs, cycle_time=3600, out=3):
    return {"type_id": tid, "name": name, "qty": qty, "cycles": cycles,
            "factories": factories, "cycle_time": cycle_time, "output_qty": out,
            "direct_inputs": [{"type_id": i, "name": n, "per_cycle": q} for i, n, q in inputs]}


_TIER_ROWS = {
    4: [_tier_row(_TARGET, "Sample Node", 2, 2, 1, [(93001, "Sample Unit", 6)], out=1)],
    3: [_tier_row(93001, "Sample Unit", 12, 4.0, 2, [(92001, "Sample Coolant", 10),
                                                     (92002, "Sample Mainframe", 10)])],
    2: [_tier_row(92001, "Sample Coolant", 40, 8.0, 3, [(95001, "Sample Oxidizer", 40)]),
        _tier_row(92002, "Sample Mainframe", 40, 8.0, 3, [(95002, "Sample Plasmoids", 40)])],
    1: [_tier_row(95001, "Sample Oxidizer", 320, 12.5, 6, [(91001, "Sample Gas", 3000)],
                  cycle_time=1800, out=20),
        _tier_row(95002, "Sample Plasmoids", 320, 12.5, 6, [(91002, "Sample Ions", 3000)],
                  cycle_time=1800, out=20)],
}
_PIPELINE = [(tier, row) for tier in (4, 3, 2, 1) for row in _TIER_ROWS[tier]]


def _bom():
    return {
        "target": {"type_id": _TARGET, "name": "Sample Node", "cycles": 2, "output_qty": 1,
                   "total_output": 2, "tier": 4},
        "target_cycle_time": 3600, "total_target_output": 2,
        "tier_rows": {0: [], **_TIER_ROWS},
        "p0_totals": {p["type_id"]: p for p in _P0},
        "p0_sorted": [dict(p) for p in _P0],
        "missing_p0_count": 1, "total_p0_volume": 36000,
        "p0_input_cost": 1234567.0, "revenue": 2345678.0, "margin": 1111111.0,
        "margin_pct": 90.0,
    }


def _slot(planet, ptype, role, *, preferred=True, shared=False, **extra):
    return {"planet_name": planet, "planet_type": ptype, "preferred": preferred,
            "shared": shared, "role": role, **extra}


# Char 1 makes the P3; Char 2 assembles the P4. Each slot exercises one
# Note case: fallback, shared, none, and no planet at all.
_SLOTS = [
    [_slot("Sample System I", "gas", "miner_p1", preferred=False,
           slots=[{"p0_name": "Sample Gas", "p1_name": "Sample Oxidizer"}]),
     _slot("Sample System II", "barren", "p2_p3_factory", shared=True, factory_count=4)],
    [_slot("Sample System III", "temperate", "p4_factory", factory_count=1),
     _slot(None, "lava", "p2_p3_factory", factory_count=2)],
]


def _node(tid, name, planet):
    return {"tid": tid, "name": name, "count": 1, "planets": [f"Sample System {planet}"],
            "planets_short": [planet], "surplus": False}


def _character_plan():
    flow = {
        "edges": [{"from_char": 0, "to_char": 1, "tid": 93001, "consumer_tid": _TARGET,
                   "intra": False, "tier_from": 3, "tier_to": 4, "name": "Sample Unit"}],
        "imports": [[], [{"tid": 93001, "name": "Sample Unit", "counterparts": [1]}]],
        "exports": [[{"tid": 93001, "name": "Sample Unit", "counterparts": [2]}], []],
        "items_by_char": [
            [[_node(91001, "Sample Gas", "I")], [_node(95001, "Sample Oxidizer", "I")],
             [], [_node(93001, "Sample Unit", "II")], []],
            [[], [], [], [], [_node(_TARGET, "Sample Node", "III")]],
        ],
    }
    return {
        "characters": [
            {"label": "Char 1 — Sample Unit producer", "role": "producer", "slots": _SLOTS[0]},
            {"label": "Char 2 — Assembler", "role": "assembler", "slots": _SLOTS[1]},
        ],
        "total_characters": 2, "total_planets_assigned": 3, "shared_slots": 1,
        "unassignable_slots": 1, "system_capacity_warning": None, "optimal_chars": 2,
        "effective_chars": 2, "planets_per_char": 6, "is_capped": False, "dropped_count": 0,
        "shortfalls": [], "achievable_ratio": None, "flow": flow,
    }


def _calc(system=True):
    return render_page(
        pi_mod, "planetary_calculator.html", "/industry/planetary/calculator",
        target=_TARGET, system="Sample System" if system else None, cycles=2,
        max_chars=None, ipc=5, ccu=5, planets_per_char=6,
        product_options={1: [], 2: [], 3: [], 4: [{"type_id": _TARGET, "name": "Sample Node"}]},
        bom=_bom(), colony_plan=None, character_plan=_character_plan(),
        system_info={"system_name": "Sample System", "security": 0.4} if system else None,
        space_type="lowsec" if system else None,
        system_p0_names=["Sample Gas", "Sample Microbes"] if system else [],
        no_sde=False,
        tier_names={0: "P0 · Raw", 1: "P1 · Basic", 2: "P2 · Basic",
                    3: "P3 · Specialized", 4: "P4 · Advanced"})


# ── Calculator: form (ISS-111 part 2) ────────────────────────────────

def test_calculator_form_grids_carry_class_hooks_and_keep_their_desktop_styles():
    html = _calc()
    (row1,) = _with_class(html, "pi-calc-row1")
    assert row1[2].startswith("display:grid;grid-template-columns:minmax(180px,1fr) "
                              "minmax(220px,1.3fr) 90px auto;")
    (row2,) = _with_class(html, "pi-calc-row2")
    assert row2[2].startswith("display:grid;grid-template-columns:120px 140px 140px auto;")
    (actions,) = _with_class(html, "pi-calc-actions")
    assert actions[2] == "display:flex;gap:0.4rem;"
    (note,) = _with_class(html, "pi-calc-note")
    assert note[2].startswith("font-size:10px;")
    # The four row-1 cells and four advanced cells are still the grids' children.
    assert re.search(r'class="pi-calc-row1"[^>]*>\s*<div style="position:relative;">', html)
    assert re.search(r'class="pi-calc-actions"[^>]*>\s*<button type="submit"', html)


def test_css_calculator_form_stacks_and_the_advanced_row_goes_two_up():
    css = _phone()
    assert "grid-template-columns: minmax(0, 1fr) !important" in rule_bodies(css, ".pi-calc-row1")
    assert ("grid-template-columns: repeat(2, minmax(0, 1fr)) !important"
            in rule_bodies(css, ".pi-calc-row2"))
    for sel in (".pi-calc-row1 > *", ".pi-calc-row2 > *", ".pi-calc-row1 input",
                ".pi-calc-row1 select", ".pi-calc-row2 input", ".pi-calc-row2 select"):
        assert "min-width: 0" in rule_bodies(css, sel), sel
    assert "grid-column: 1 / -1" in rule_bodies(css, ".pi-calc-row2 > .pi-calc-note")
    # Calculate and Reset share the full-width last line.
    assert "flex: 1 1 0" in rule_bodies(css, ".pi-calc-actions > .b-btn")


# ── Calculator: the three lists ──────────────────────────────────────

def test_calculator_lists_are_tap_to_open_rows():
    html = _calc()
    rows = assert_mrow(html, min_rows=len(_P0) + 4 + len(_PIPELINE))
    assert len(rows) == len(_P0) + 4 + len(_PIPELINE)
    for r in cells_rows(html):
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert not row_lead(r)
        assert_single_value_child(r)
        assert _labels(r)[0] == "Name", "an opened row starts with its full Name line"


def test_calculator_p0_rows_key_material_and_quantity():
    rows = _hooked(_calc(), "pi-p0-row")
    assert len(rows) == len(_P0)
    for r, p0 in zip(rows, _P0):
        name, qty = row_keys(r)
        assert name["text"] == p0["name"]
        assert qty["text"] == f"{round(p0['qty']):,}"
        labelled = row_labelled(r)
        assert list(labelled) == ["Name", "Planets", "Local"]
        assert labelled["Name"]["text"] == p0["name"]
        assert labelled["Planets"]["text"] == ", ".join(p0["planet_sources"]).capitalize()
        assert labelled["Local"]["text"] == ("✓ local" if p0["producible"] else "⚠ import")
        # Phone-only copies; the desktop card's two lines stay as they were.
        for c in row_keys(r) + list(labelled.values()):
            assert "m-only" in _classes(c)
        assert [c["tag"] for c in _untagged(r)] == ["div", "div"]
    # A material the system can't make keeps its red name in key 1 too.
    assert "#e58080" in row_keys(rows[1])[0]["attrs"]["style"]
    assert "var(--success)" in row_labelled(rows[0])["Local"]["attrs"]["style"]
    assert "var(--danger)" in row_labelled(rows[1])["Local"]["attrs"]["style"]


def test_calculator_p0_rows_skip_local_without_a_system():
    rows = _hooked(_calc(system=False), "pi-p0-row")
    assert len(rows) == len(_P0)
    for r in rows:
        assert _labels(r) == ["Name", "Planets"]


def test_calculator_colony_slots_key_planet_and_type():
    html = _calc()
    rows = _hooked(html, "pi-slot-row")
    slots = _SLOTS[0] + _SLOTS[1]
    assert len(rows) == len(slots)
    for i, (r, slot) in enumerate(zip(rows, slots)):
        planet, ptype = row_keys(r)
        shown = slot["planet_name"] or "[no planet available]"
        assert planet["text"].startswith(shown)
        assert ptype["text"] == slot["planet_type"]
        assert row_labelled(r)["Name"]["text"] == shown
        badges = (["fallback"] if not slot["preferred"] and slot["planet_name"] else []) + \
                 (["shared"] if slot["shared"] and slot["planet_name"] else [])
        assert _labels(r) == ["Name", "Role"] + (["Note"] if badges else [])
        if badges:
            note = row_labelled(r)["Note"]
            assert "m-only" in _classes(note)
            assert note["text"] == " ".join(badges)
            # Key 1 keeps its desktop badges, hidden on phones.
            assert len(planet["kids"]) == len(badges)
            assert all("m-hide" in k.get("class", "").split() for k in planet["kids"])
        # The # column is desktop-only.
        assert [c["text"] for c in _untagged(r)] == [str(i % 2 + 1)]
    role = row_labelled(rows[0])["Role"]
    assert role["text"].startswith("Extract: Sample Gas")
    assert "(P1 factory)" in role["text"]
    assert row_labelled(rows[1])["Role"]["text"] == "P2/P3 hub: 4 × Advanced Industrial Facility"
    assert row_labelled(rows[2])["Role"]["text"] == "P4 assembly: 1 × High-Tech Production Plant"


def test_calculator_column_headers_hide_on_phones():
    html = _calc()
    heads = _with_class(html, "m-head")
    # One per character in the colony plan, one per tier in the pipeline.
    assert len(heads) == len(_SLOTS) + len(_TIER_ROWS)
    styles = [h[2] for h in heads]
    assert sum("grid-template-columns:28px 130px 70px 1fr;" in s for s in styles) == len(_SLOTS)
    assert sum("minmax(160px,2fr) 90px 90px 90px minmax(220px,2fr)" in s for s in styles) == 4


def test_calculator_pipeline_keys_tier_and_product_then_quantity_needed():
    rows = _hooked(_calc(), "pi-pipe-row")
    assert len(rows) == len(_PIPELINE)
    for r, (tier, row) in zip(rows, _PIPELINE):
        product, qty = row_keys(r)
        (tag,) = product["kids"]
        assert "m-only" in tag.get("class", "").split()
        assert product["text"].startswith(f"P{tier}")
        assert product["text"].endswith(row["name"])
        assert qty["text"] == f"{round(row['qty']):,}"
        assert _labels(r) == ["Name", "Cycles", "Factories", "Recipe"]
        labelled = row_labelled(r)
        assert labelled["Name"]["text"] == row["name"]
        assert labelled["Factories"]["text"] == str(row["factories"])
        assert labelled["Recipe"]["text"].startswith(
            f"{row['direct_inputs'][0]['per_cycle']}× {row['direct_inputs'][0]['name']}")
    target = rows[0]
    assert "★" in row_keys(target)[0]["text"]
    assert "rgba(200,169,81,0.08)" in target["attrs"]["style"]   # target highlight kept
    assert row_labelled(rows[3])["Cycles"]["text"] == "8.00"
    assert row_labelled(rows[4])["Cycles"]["text"] == "12.5"


def test_calculator_flow_chart_hides_on_phones_but_the_hand_off_summary_stays():
    html = _calc()
    (container,) = _with_class(html, "pi-flow-container")
    assert "m-hide" in container[1]
    assert "overflow-x:auto" in container[2]          # desktop still scrolls it
    # The colour legend and the hover hint describe the chart: hidden with it.
    legend = re.search(r'cross-character handoffs?\s*<span class="m-hide">(.*?)</span>\s*</span>',
                       html, re.S)
    assert legend and "hover to trace" in legend.group(1) and "green" in legend.group(1)
    foot = re.search(r'<div class="m-hide"[^>]*>\s*Each cell lists the items', html)
    assert foot
    summary = re.search(r'<div style="[^"]*">\s*<div [^>]*>Hand-off summary</div>', html)
    assert summary and "m-hide" not in summary.group(0)
    assert "Char 1 — Sample Unit producer</span> ships" in html


def test_calculator_without_a_flow_chart_still_renders_its_rows():
    """Single-character plans have no cross-character edges and no chart."""
    html = _calc()
    assert "pi-flow-container" in html
    plan = _character_plan()
    plan["flow"]["edges"] = []
    html = render_page(
        pi_mod, "planetary_calculator.html", "/industry/planetary/calculator",
        target=_TARGET, system=None, cycles=2, max_chars=None, ipc=5, ccu=5,
        planets_per_char=6, product_options={}, bom=_bom(), colony_plan=None,
        character_plan=plan, system_info=None, space_type=None, system_p0_names=[],
        no_sde=False, tier_names={})
    assert "pi-flow-container" not in html
    assert_mrow(html, min_rows=len(_P0) + 4 + len(_PIPELINE))
