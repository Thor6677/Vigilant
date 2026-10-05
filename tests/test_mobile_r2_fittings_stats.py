"""Mobile R2 T3: Fittings, Stats (intel entity) and the blueprint filter row.

None of these pages gets m-rows (user-approved): module rows already fit,
fits open on tap through toggleFit, and the stats lists are five short rows.
The work is class hooks in the templates plus phone-only rules in site.css's
`R2 T3` section, so desktop renders exactly as before (D21): the hooks add a
class and never change an inline style. Names and ids are invented."""
import re
from html.parser import HTMLParser

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import blueprints as blueprints_mod
from app.routes import fittings as fittings_mod
from app.routes import intel_entity as entity_mod
from tests._mobile import VOID, css_section, mrows, norm, render_page, rule_bodies


# ── HTML and CSS helpers ─────────────────────────────────────────────

class _Tree(HTMLParser):
    """Every element as {"tag", "attrs", "cls", "parent" (index or None),
    "text" (all descendant text)}, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.nodes.append({"tag": tag, "attrs": a, "cls": a.get("class", "").split(),
                           "parent": self.stack[-1] if self.stack else None, "text": ""})
        if tag not in VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[i]]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for i in self.stack:
            self.nodes[i]["text"] += data


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.nodes


def _by_class(nodes, cls):
    return [i for i, n in enumerate(nodes) if cls in n["cls"]]


def _children(nodes, idx):
    return [i for i, n in enumerate(nodes) if n["parent"] == idx]


def _decls(selector):
    """The declarations of R2 T3's rules for `selector`, as {property: value}
    with any !important kept on the value."""
    body = rule_bodies(css_section("T3"), selector)
    assert body.strip(), f"no rule for {selector!r} in the R2 T3 section"
    out = {}
    for part in body.split(";"):
        if ":" in part:
            prop, val = part.split(":", 1)
            out[prop.strip()] = norm(val)
    return out


def _bare(value):
    return value.replace("!important", "").strip()


# ── Fittings ─────────────────────────────────────────────────────────

_SLOTS = {"high": 5, "med": 4, "low": 4, "rig": 3}
_NAMES = {
    3001: "Sample Blaster II", 3002: "Sample Web I", 3003: "Sample Plate II",
    3004: "Sample Rig I", 3005: "Sample Booster II", 3006: "Sample Drone I",
}
_LONG_FIT = "Charlie Long Range Exploration Fit With Cloak, Probes And Spare Cargo v2"


def _raw(fid, name, ship, items):
    return {"fitting_id": fid, "name": name, "description": "", "ship_type_id": ship,
            "items": [{"type_id": t, "flag": f, "quantity": q} for t, f, q in items]}


def _fits():
    """Two hulls, three fits. Alpha has 12 fitted modules (more than 10);
    Charlie has a long name, the case that would push the arrow off line 1."""
    alpha = ([(3001, f"HiSlot{i}", 1) for i in range(5)]
             + [(3002, f"MedSlot{i}", 1) for i in range(4)]
             + [(3003, f"LoSlot{i}", 1) for i in range(3)]
             + [(3006, "DroneBay", 5)])
    bravo = [(3001, "HiSlot0", 1), (3005, "MedSlot0", 1), (3003, "LoSlot0", 1), (3004, "RigSlot0", 1)]
    charlie = [(3002, "MedSlot0", 1), (3003, "LoSlot0", 1), (3004, "RigSlot0", 1)]
    raws = [(_raw(7001, "Alpha Brawler", 620, alpha), "Sample Cruiser", True),
            (_raw(7002, "Bravo Kiter", 620, bravo), "Sample Cruiser", False),
            (_raw(7003, _LONG_FIT, 587, charlie), "Sample Frigate", False)]
    fits = []
    for raw, ship, imported in raws:
        fit = fittings_mod._parse_fitting(raw, _NAMES, ship, _SLOTS)
        fit["already_imported"] = imported
        fits.append(fit)
    groups = {}
    for fit in sorted(fits, key=lambda f: (f["ship_name"], f["name"])):
        groups.setdefault(fit["ship_name"], []).append(fit)
    return fits, groups


def _render_fittings():
    fits, groups = _fits()
    assert max(f["total_modules"] for f in fits) > 10
    char = {"character_id": 90000001, "character_name": "Pilot Alpha"}
    return render_page(fittings_mod, "fittings.html", "/character/90000001/fittings",
                       char=char, fittings=fits, ship_groups=groups, error=None,
                       slot_labels=fittings_mod.SLOT_LABELS)


def test_fittings_page_has_no_mrows():
    """User-approved: fits open on tap through toggleFit; no m-row layer."""
    assert mrows(_render_fittings()) == []


def test_fittings_save_all_group_is_hooked_and_parents_the_status():
    """saveAllToMyFits appends its failure <ul> to the status span's parent,
    so the hooked group must be that parent for the list to land full width."""
    nodes = _tree(_render_fittings())
    (group,) = _by_class(nodes, "fit-saveall")
    assert nodes[group]["attrs"]["style"] == "display:flex;align-items:center;gap:0.6rem;"
    kids = _children(nodes, group)
    status = next(i for i in kids if nodes[i]["attrs"].get("id") == "save-all-status")
    assert "fit-saveall-status" in nodes[status]["cls"]
    assert nodes[status]["attrs"]["role"] == "status"
    assert nodes[status]["text"] == ""                      # empty until a save runs
    btn = next(i for i in kids if nodes[i]["attrs"].get("data-click") == "saveAllToMyFits")
    assert "b-btn" in nodes[btn]["cls"]
    assert kids.index(status) < kids.index(btn)             # DOM order unchanged: CSS reorders on phones


def test_fittings_fit_heads_are_hooked():
    nodes = _tree(_render_fittings())
    heads = _by_class(nodes, "fit-head")
    assert len(heads) == 3
    for h in heads:
        head = nodes[h]
        assert "b-panel-head" in head["cls"]
        assert head["attrs"]["data-click"] == "toggleFit"
        assert head["attrs"]["style"] == "cursor:pointer;display:flex;justify-content:space-between;align-items:center;"
        kids = _children(nodes, h)
        assert len(kids) == 2
        title, arrow = (nodes[i] for i in kids)
        assert "fit-title" in title["cls"]
        assert title["attrs"]["style"] == "display:flex;align-items:center;gap:0.5rem;"
        assert "fit-arrow" in arrow["cls"]                  # toggleFit queries .fit-arrow
        assert arrow["text"] == "▸"
    texts = [norm(nodes[h]["text"]) for h in heads]
    assert any(t.startswith("Alpha Brawler 12 modules") for t in texts)
    assert any(t.startswith(_LONG_FIT) for t in texts)


def test_fittings_css_save_all_status_takes_its_own_line():
    group = _decls(".fit-saveall")
    assert group["flex-wrap"] == "wrap"
    assert group["align-self"] == "stretch"                 # full width in the stacked page header
    assert group["row-gap"] == "0 !important"               # beats the inline gap; empty status adds nothing
    assert _decls(".fit-saveall > .b-btn")["flex"] == "none"   # .b-btn is flex:1
    status = _decls(".fit-saveall > .fit-saveall-status")
    fails = _decls(".fit-saveall > ul")
    assert status["flex-basis"] == "100%" and fails["flex-basis"] == "100%"
    assert int(status["order"]) >= 1                        # below the button
    assert int(fails["order"]) >= int(status["order"])      # and the failure list below the status
    # A fit name with no spaces in the list must wrap, not widen the page.
    assert fails["min-width"] == "0" and fails["overflow-wrap"] == "anywhere"
    assert "margin-top" in _decls(".fit-saveall > .fit-saveall-status:not(:empty)")


def test_fittings_css_fit_head_is_a_44px_target_with_a_12px_arrow():
    head = _decls(".b-panel-head.fit-head")
    assert head["min-height"] == "44px"
    # R1 wraps every .b-panel-head on phones; a long fit name would push the
    # arrow onto its own line, so this head stays on one line and the title
    # side shrinks instead.
    assert head["flex-wrap"] == "nowrap"
    title = _decls(".fit-head > .fit-title")
    assert title["min-width"] == "0"
    assert title["flex-wrap"] == "wrap"
    # The name span's min-content is its longest word, so a long name with
    # no spaces would run past the arrow; it may shrink and break anywhere.
    name = _decls(".fit-head > .fit-title > :first-child")
    assert name["min-width"] == "0" and name["overflow-wrap"] == "anywhere"
    arrow = _decls(".fit-head .fit-arrow")
    assert arrow["font-size"] == "12px"
    assert arrow["flex"] == "none"


# ── Stats (intel_entity.html) ────────────────────────────────────────

_ENTITY_NAME = "Sample Entity With A Deliberately Long Display Name Here"


def _render_entity(kind):
    return render_page(entity_mod, "intel_entity.html", f"/intel/entity/{kind}/98000001",
                       kind=kind, kind_label=entity_mod._KIND_LABEL[kind], entity_id=98000001,
                       entity_name=_ENTITY_NAME, windows=(7, 30, 90), default_window=90)


@pytest.mark.parametrize("kind", ["character", "corporation", "alliance"])
def test_entity_zkillboard_chip_is_a_tap_target(kind):
    html = _render_entity(kind)
    assert mrows(html) == []
    nodes = _tree(html)
    (chip,) = [n for n in nodes if n["tag"] == "a" and "zkillboard.com" in n["attrs"].get("href", "")]
    assert chip["attrs"]["href"] == f"https://zkillboard.com/{kind}/98000001/"
    for cls in ("el-chip", "is-ext", "m-tap", "ent-zkb"):
        assert cls in chip["cls"], cls
    assert "style" not in chip["attrs"]                     # m-tap has no desktop rule; nothing inline to fight


def test_entity_css_zkillboard_chip_spaces_its_arrow():
    """m-tap makes the chip inline-flex, so its ::after arrow is a flex item
    and loses the leading space in its content; a gap puts it back."""
    assert "gap" in _decls(".ent-zkb")


# ── Blueprint filter row (ISS-111 part 1) ────────────────────────────

def _render_blueprints(is_corp, filter="all", group_by="type"):
    raw = [
        {"type_id": 691, "item_id": 1, "quantity": -1, "material_efficiency": 10,
         "time_efficiency": 20, "runs": -1, "location_flag": "Hangar", "location_id": 60000001},
        {"type_id": 692, "item_id": 2, "quantity": -2, "material_efficiency": 2,
         "time_efficiency": 4, "runs": 5, "location_flag": "Hangar", "location_id": 60000001},
    ]
    bps = blueprints_mod._process_blueprints(
        raw, {691: "Sample Frigate Blueprint", 692: "Sample Cruiser Blueprint"})
    if is_corp:
        char = {"character_id": 90000001, "character_name": "Pilot Alpha",
                "corporation_id": 98000001, "corporation_name": "Sample Corp"}
        path, corp_id = "/corporations/98000001/blueprints", 98000001
    else:
        char = {"character_id": 90000001, "character_name": "Pilot Alpha",
                "corporation_id": None, "corporation_name": None}
        path, corp_id = "/character/90000001/blueprints", None
    return render_page(blueprints_mod, "blueprints.html", path,
                       char=char, blueprints=bps, groups=blueprints_mod._group_blueprints(bps, group_by),
                       stats=blueprints_mod._compute_stats(bps), error=None, is_corp=is_corp,
                       corp_id=corp_id, filter=filter, group_by=group_by)


@pytest.mark.parametrize("is_corp", [False, True])
def test_blueprint_calc_links_name_their_blueprint(is_corp):
    """Every row's link reads "Calc →"; a screen reader's link list needs
    the blueprint in each name."""
    html = _render_blueprints(is_corp, "all", "type")
    links = re.findall(r'<a href="/industry\?type_id=(\d+)" class="m-tap" aria-label="([^"]*)"', html)
    assert sorted(links) == [("691", "Open Sample Frigate Blueprint in the calculator"),
                             ("692", "Open Sample Cruiser Blueprint in the calculator")]


@pytest.mark.parametrize("is_corp,filter,group_by", [
    (False, "all", "type"), (True, "all", "type"),
    (False, "unresearched", "location"), (True, "bpc", "location"),
])
def test_blueprint_filter_row_is_hooked(is_corp, filter, group_by):
    nodes = _tree(_render_blueprints(is_corp, filter, group_by))
    (row,) = _by_class(nodes, "bp-filters")
    assert nodes[row]["attrs"]["style"] == "display:flex;flex-wrap:wrap;gap:4px;margin-bottom:0.5rem;"
    kids = [nodes[i] for i in _children(nodes, row)]
    # Grid order on phones: All / BPO Only, BPC Only / Unresearched, the
    # separator's spacer row, then By Type / By Location.
    assert [norm(k["text"]) for k in kids] == [
        "All", "BPO Only", "BPC Only", "Unresearched", "|", "By Type", "By Location"]
    sep = kids[4]
    assert sep["tag"] == "span" and "bp-filter-sep" in sep["cls"]
    assert sep["attrs"]["style"] == "color:var(--border);margin:0 4px;"
    assert sep["attrs"].get("aria-hidden") == "true"         # a decorative glyph, not read aloud
    base = "/corporations/98000001/blueprints" if is_corp else "/character/90000001/blueprints"
    buttons = kids[:4] + kids[5:]
    keys = ["all", "bpo", "bpc", "unresearched", "type", "location"]
    for key, btn in zip(keys, buttons):
        assert btn["tag"] == "a" and "b-btn" in btn["cls"]
        assert btn["attrs"]["href"].startswith(base + "?")
        active = key in (filter, group_by)
        # The active accent stays inline, so the phone grid keeps it.
        assert ("border:1px solid var(--accent)" in btn["attrs"]["style"]) == active, key
        assert ("color:var(--accent)" in btn["attrs"]["style"]) == active, key


def test_blueprint_css_filter_row_is_a_two_column_grid():
    row = _decls(".bp-filters")
    assert row["display"] == "grid !important"              # beats the inline display:flex
    assert row["grid-template-columns"] == "repeat(2, minmax(0, 1fr))"
    assert row["gap"].endswith("!important")                # beats the inline gap
    btn = _decls(".bp-filters > .b-btn")
    assert btn["white-space"] == "nowrap"
    assert btn["font-size"] == "12px !important"            # beats the inline 10px
    # No height: the global phone .b-btn rule's 44px applies (a 40px
    # min-height here outranked it and shrank the links).
    assert "min-height" not in btn and "height" not in btn
    assert btn["display"] == "flex" and btn["align-items"] == "center"
    sep = _decls(".bp-filters > .bp-filter-sep")
    assert sep["grid-column"] == "1 / -1"                   # a spacer row between the two groups
    assert _bare(sep["height"]) == "0"
    assert sep["overflow"] == "hidden"                      # no visible glyph


# ── The section stays phone-only (D21) ───────────────────────────────

def test_section_has_no_desktop_rules():
    """Everything in the R2 T3 section sits inside its one phone media block."""
    css = css_section("T3")
    m = re.search(r"@media \(max-width: 640px\) \{", css)
    assert m and css.count("@media") == 1
    depth, i = 1, m.end()
    while depth:
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    assert css[:m.start()].strip() == "" and css[i:].strip() == ""
