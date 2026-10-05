"""Mobile R6 T5: Saved Fits and Compare.

Saved fits become expand-on-tap rows on phones. D12 A: a tap opens the row,
with Open, Compare, Move and Delete inside it. D13 A: key 2 is DPS. Each row
keeps data-click="openSavedFit", which on phones hands the tap to
actions.js's toggleMRow, so a desktop click still loads the fit (D21).

Every phone-only cell is an m-only copy placed after the fifth desktop cell,
so the template's 720px rule (folder = td 3, cost = td 5) still hides the
cells it always did. Compare keeps its four-column table (D14 A). On phones
the head shows the letters A and B, and an m-only line above the table names
both fits.

Every CSS rule is anchored on an id only these pages carry. The Skill Farm
also uses .sf-table. Names and ids are invented."""
import functools
import re
from html.parser import HTMLParser

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import (VOID, assert_single_value_child,
                           cells_rows, css_section, mrows, norm, phone_block,
                           render_page, row_keys, row_labelled, row_lead,
                           rule_bodies, selectors, source)

_section = functools.partial(css_section, release="R6")


# ── Fixtures (invented) ──────────────────────────────────────────────

ROWS = [
    {"id": 701, "name": "Armor kite", "ship_type_id": 24702, "ship_name": "Hurricane",
     "folder_id": 3, "folder_path": "PvP/Small", "dps": None, "cost": 81234567.0,
     "updated_at": None},
    {"id": 702, "name": "Shield brawl", "ship_type_id": 24698, "ship_name": "Drake",
     "folder_id": None, "folder_path": "", "dps": None, "cost": 0.0,
     "updated_at": None},
    {"id": 703, "name": "Cheap tackle", "ship_type_id": 587, "ship_name": "Rifter",
     "folder_id": 4, "folder_path": "Tackle", "dps": None, "cost": 1500000.0,
     "updated_at": None},
]


def _saved(rows=ROWS):
    return render_page(
        fitting_mod, "fitting_saved.html", "/tools/fitting/saved",
        rows=rows, folders=[{"id": 3}, {"id": 4}],
        folder_paths=[{"id": 3, "path": "PvP/Small"}, {"id": 4, "path": "Tackle"}],
        total=len(rows))


def _fit(fid, name, ship, type_id):
    return {"id": fid, "name": name, "ship_name": ship, "ship_type_id": type_id,
            "implant_count": 0, "booster_count": 0}


SECTIONS = [
    {"name": "Defense", "rows": [
        {"label": "Total EHP", "a": "123,456", "b": "130,001", "delta": "+6,545", "cls": "better"},
        {"label": "Shield rep/s", "a": "0.0", "b": "0.0", "delta": "—", "cls": "same"},
    ]},
    {"name": "Capacitor", "rows": [
        {"label": "Cap stable", "a": "Stable", "b": "Unstable", "delta": "", "cls": "bool"},
    ]},
]


def _compare(fit_b=None):
    return render_page(
        fitting_mod, "fitting_compare.html", "/tools/fitting/compare",
        fit_a=_fit(801, "Armor kite", "Hurricane", 24702),
        fit_b=fit_b or _fit(802, "Shield brawl", "Hurricane", 24702),
        sections=SECTIONS)


# ── A small element tree ─────────────────────────────────────────────

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


def _children(nodes, idx):
    return [i for i, n in enumerate(nodes) if n["parent"] == idx]


def _descendants(nodes, idx):
    out = []
    for i in range(idx + 1, len(nodes)):
        p = nodes[i]["parent"]
        while p is not None and p != idx:
            p = nodes[p]["parent"]
        if p == idx:
            out.append(i)
    return out


def _saved_rows(nodes):
    rows = [i for i, n in enumerate(nodes) if n["tag"] == "tr" and "m-row" in n["cls"]]
    assert len(rows) == len(ROWS)
    return rows


def _saved_cells():
    """cells_rows of the 3-fit page; asserts there are 3 so a zip over them
    can't pass vacuously."""
    rows = cells_rows(_saved())
    assert len(rows) == len(ROWS)
    return rows


# ── Saved Fits: rows ─────────────────────────────────────────────────

def test_saved_rows_keep_open_saved_fit_and_meet_the_contract():
    """assert_mrow isn't used here. It requires an expandable row's
    data-click to be toggleMRow or toggleExpanded, but this row must keep
    data-click="openSavedFit": a desktop click still loads the fit (D21),
    and openSavedFit hands phone taps to toggleMRow (pinned by the script
    test below). The rest of assert_mrow's contract is checked here with the
    lower-level helpers."""
    html = _saved()
    rows = mrows(html)
    assert len(rows) == 3
    for r in rows:
        assert r["tag"] == "tr"
        assert r["attrs"]["data-click"] == "openSavedFit"
        assert "m-row--link" not in r["attrs"]["class"].split()
        for k in r["children"]:
            assert not ("data-m" in k and "data-m-label" in k)
    for row in cells_rows(html):
        assert len(row_keys(row)) == 2
        assert len(row_lead(row)) == 1
        assert all(label.strip() for label in row_labelled(row))
        assert_single_value_child(row)


def test_saved_table_is_an_m_table_with_an_m_head():
    nodes = _tree(_saved())
    table = next(n for n in nodes if n["tag"] == "table" and "sf-table" in n["cls"])
    assert "m-table" in table["cls"]
    thead = next(n for n in nodes if n["tag"] == "thead")
    assert "m-head" in thead["cls"]


def test_saved_keys_are_fit_name_then_dps():
    for row, fit in zip(_saved_cells(), ROWS):
        keys = row_keys(row)
        assert len(keys) == 2
        assert keys[0]["text"] == fit["name"]
        assert "sf-dps" in keys[1]["attrs"].get("class", "").split()
        # Key 2 is the cell fillDps writes into: no other cell may carry
        # .sf-dps, or tr.querySelector('.sf-dps') would fill the wrong one.
        dps_cells = [c for c in row["cells"] if "sf-dps" in c["attrs"].get("class", "").split()]
        assert len(dps_cells) == 1


def test_saved_lead_is_a_sized_ship_icon_without_on_error():
    for row, fit in zip(_saved_cells(), ROWS):
        leads = row_lead(row)
        assert len(leads) == 1
        lead = leads[0]
        assert "m-only" in lead["attrs"]["class"].split()
        assert "data-on-error" not in lead["attrs"]
        assert len(lead["kids"]) == 1
        img = lead["kids"][0]
        assert f"/types/{fit['ship_type_id']}/icon" in img["src"]
        assert img.get("width") == "24" and img.get("height") == "24"
        assert "data-on-error" not in img


def test_saved_labels_in_order_name_first():
    for row, fit in zip(_saved_cells(), ROWS):
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Ship", "Folder", "Est. cost", "Fit", "Actions"]
        assert labelled["Name"]["text"] == fit["name"]
        assert labelled["Ship"]["text"] == fit["ship_name"]
        assert labelled["Folder"]["text"] == (fit["folder_path"] or "—")
        assert_single_value_child(row)


def test_saved_cost_line_reads_isk_or_a_dash():
    rows = _saved_cells()
    assert row_labelled(rows[0])["Est. cost"]["text"] == "81,234,567 ISK"
    assert row_labelled(rows[1])["Est. cost"]["text"] == "—"


def test_saved_phone_copies_are_m_only():
    for row in _saved_cells():
        labelled = row_labelled(row)
        for label in ("Name", "Ship", "Folder", "Est. cost", "Fit"):
            assert "m-only" in labelled[label]["attrs"].get("class", "").split(), label
        # The Actions cell is the desktop cell itself: never a copy, since
        # the compare state lives in its checkbox.
        assert "sf-actions" in labelled["Actions"]["attrs"].get("class", "").split()
        assert "m-only" not in labelled["Actions"]["attrs"].get("class", "").split()


def test_saved_fit_link_opens_the_right_fit():
    for row, fit in zip(_saved_cells(), ROWS):
        cell = row_labelled(row)["Fit"]
        assert len(cell["kids"]) == 1
        assert cell["kids"][0]["href"] == f"/tools/fitting?load={fit['id']}"
        assert "m-tap" in cell["kids"][0].get("class", "").split()
        assert cell["text"] == "Open in tool →"


def test_saved_actions_cell_holds_one_wrapper_with_every_control():
    html = _saved()
    nodes = _tree(html)
    assert sum("sf-compare-check" in n["cls"] for n in nodes) == 3
    for r, fit in zip(_saved_rows(nodes), ROWS):
        actions = [i for i in _children(nodes, r) if nodes[i]["attrs"].get("data-m-label") == "Actions"]
        assert len(actions) == 1
        kids = _children(nodes, actions[0])
        assert len(kids) == 1 and nodes[kids[0]]["tag"] == "span"
        inner = [nodes[i] for i in _descendants(nodes, kids[0])]
        checks = [n for n in inner if "sf-compare-check" in n["cls"]]
        assert len(checks) == 1
        assert checks[0]["attrs"]["type"] == "checkbox"
        assert checks[0]["attrs"]["value"] == str(fit["id"])
        assert checks[0]["attrs"]["data-click"] == "noop" and "data-stop" in checks[0]["attrs"]
        buttons = [n for n in inner if n["tag"] == "button"]
        assert [b["attrs"]["data-click"] for b in buttons] == ["moveFit", "deleteFit"]
        for b in buttons:
            assert "data-stop" in b["attrs"]
            assert b["attrs"]["data-fit-id"] == str(fit["id"])
        # The checkbox sits alone in its label (the phone tap target), the
        # wrapper's first child; Move and Delete are the wrapper's own.
        wrap_kids = [nodes[i] for i in _children(nodes, kids[0])]
        assert [k["tag"] for k in wrap_kids] == ["label", "button", "button"]
        assert "sf-cmp" in wrap_kids[0]["cls"]
        label_kids = [nodes[i] for i in _children(nodes, _children(nodes, kids[0])[0])]
        assert len(label_kids) == 1 and "sf-compare-check" in label_kids[0]["cls"]


def test_compare_label_generates_no_box_on_desktop():
    """The label round the compare checkbox is display:contents in the
    page's own style (every width), so desktop lays the checkbox out as
    before; only the phone rule gives the label a box."""
    m = re.search(r"\.sf-actions \.sf-cmp\s*\{([^}]*)\}", _script())
    assert m and re.search(r"display:\s*contents", m.group(1))


def test_saved_desktop_cells_keep_their_positions():
    """The template's 720px block hides td:nth-child(3) (folder) and
    td:nth-child(5) (cost), so the first five cells must stay the desktop
    cells, in order. Phone copies go after them."""
    nodes = _tree(_saved())
    for r in _saved_rows(nodes):
        tds = [nodes[i] for i in _children(nodes, r)]
        assert all(t["tag"] == "td" for t in tds)
        first5 = tds[:5]
        assert not any("m-only" in t["cls"] for t in first5)
        assert "sf-ship" in " ".join(" ".join(nodes[i]["cls"]) for i in _descendants(nodes, _children(nodes, r)[0]))
        assert any("sf-folder" in nodes[i]["cls"] for i in _descendants(nodes, _children(nodes, r)[2]))
        assert "sf-dps" in first5[3]["cls"]
        assert "sf-cost" in first5[4]["cls"]
        assert "sf-actions" in tds[-1]["cls"]
        # Six desktop cells plus six phone-only ones (lead + five labels).
        shown = [t for t in tds if "m-only" not in t["cls"]]
        assert len(shown) == 6
        assert len(tds) == 12


def test_saved_empty_page_renders_without_rows():
    html = _saved(rows=[])
    assert mrows(html) == []
    assert "No saved fittings yet." in html


# ── Saved Fits: page script ─────────────────────────────────────────

def _script():
    return source("fitting_saved.html")


def _function_body(src, name):
    m = re.search(rf"function {name}\(e\) \{{(.*?)\n\}}", src, re.S)
    assert m, f"no top-level function {name}(e) in the page script"
    return m.group(1)


def test_open_saved_fit_hands_phone_taps_to_toggle_mrow():
    body = _function_body(_script(), "openSavedFit")
    guard = body.index("e.target.closest('button, .sf-actions')")
    phone = body.index("matchMedia('(max-width: 640px)')")
    toggle = body.index("window.toggleMRow.call(this, e)")
    nav = body.index("window.location = '/tools/fitting?load=' + this.dataset.fitId")
    assert guard < phone < toggle < nav
    # The phone branch returns before the navigation, toggleMRow or not.
    branch = body[phone:nav]
    assert "return;" in branch


def test_saved_filter_hides_rows_with_the_hidden_attribute():
    src = _script()
    start = src.index("/* --- Filter --- */")
    stop = src.index("/* --- Sort --- */")
    block = src[start:stop]
    assert "style.display" not in block
    assert re.search(r"tr\.hidden = ", block)


# ── Saved Fits: toolbar and dialog ───────────────────────────────────

def test_toolbar_controls_are_buttons_under_the_global_floor():
    """The global phone layer gives every <button> a 44px min-height, so the
    sort chips, + Folder and Compare are already tall enough. m-tap on a
    button would shrink it to 40px."""
    nodes = _tree(_saved())
    sorts = [n for n in nodes if "data-sort" in n["attrs"]]
    assert len(sorts) == 5 and all(n["tag"] == "button" for n in sorts)
    for nid in ("sf-new-folder", "sf-compare"):
        el = next(n for n in nodes if n["attrs"].get("id") == nid)
        assert el["tag"] == "button"
    for src in (source("fitting_saved.html"), source("fitting_compare.html")):
        for n in _tree(src):
            if n["tag"] == "button" or "b-btn" in n["cls"]:
                assert "m-tap" not in n["cls"]


# ── Compare ──────────────────────────────────────────────────────────

def _compare_heads(nodes):
    return [i for i, n in enumerate(nodes) if "fc-fit-head" in n["cls"]]


def test_compare_head_shows_letters_on_phones():
    nodes = _tree(_compare())
    heads = _compare_heads(nodes)
    assert len(heads) == 2
    for idx, letter in zip(heads, ("A", "B")):
        kids = [nodes[i] for i in _children(nodes, idx)]
        names = [k for k in kids if "fc-fit-name" in k["cls"]]
        imgs = [k for k in kids if k["tag"] == "img"]
        letters = [k for k in kids if "m-only" in k["cls"]]
        assert len(names) == 1 and "m-hide" in names[0]["cls"]
        assert len(imgs) == 1 and "m-hide" in imgs[0]["cls"]
        assert len(letters) == 1 and norm(letters[0]["text"]) == letter


def test_compare_names_line_same_ship():
    nodes = _tree(_compare())
    lines = [n for n in nodes if "fc-names" in n["cls"]]
    assert len(lines) == 1
    assert "m-only" in lines[0]["cls"]
    assert norm(lines[0]["text"]) == "A Armor kite · B Shield brawl · both Hurricane"


def test_compare_names_line_different_ships():
    nodes = _tree(_compare(fit_b=_fit(803, "Shield brawl", "Drake", 24698)))
    line = next(n for n in nodes if "fc-names" in n["cls"])
    assert norm(line["text"]) == "A Armor kite (Hurricane) · B Shield brawl (Drake)"


def test_compare_names_line_sits_above_the_table_in_the_panel():
    nodes = _tree(_compare())
    panel = next(i for i, n in enumerate(nodes) if n["attrs"].get("id") == "fit-compare")
    kids = [nodes[i] for i in _children(nodes, panel)]
    assert [k["tag"] for k in kids] == ["div", "table"]
    assert "fc-names" in kids[0]["cls"]


def test_compare_keeps_sections_and_delta_classes():
    html = _compare()
    assert html.count('class="fc-section"') == 2
    assert 'class="fc-delta-better"' in html
    assert 'class="fc-delta-bool"' in html


# ── CSS ──────────────────────────────────────────────────────────────

_ANCHORS = ("#sf-tbody", "input#sf-search", "#sf-toolbar", "#sf-head-actions",
            "#charimport-dialog", "#fit-compare")


def _rules(css):
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _phone():
    body, after = phone_block(_section("T5"))
    assert not after.strip(), "R6 T5 needs no desktop rule (D21)"
    return body


def test_section_rules_are_anchored_on_this_page():
    rules = _rules(_phone())
    assert rules
    for prelude, _ in rules:
        for sel in selectors(prelude):
            assert not sel.startswith((".sf-", ".fc-")), sel
            assert sel.startswith(_ANCHORS), f"{sel!r} isn't anchored on a page-only id"


def test_hidden_rows_stay_hidden_on_phones():
    body = rule_bodies(_phone(), "#sf-tbody > tr.m-row[hidden]")
    assert re.search(r"display:\s*none\s*!important", body)


def test_search_takes_its_own_line():
    body = rule_bodies(_phone(), "input#sf-search")
    assert re.search(r"min-width:\s*0", body)
    assert re.search(r"flex:\s*1 1 100%", body)


def test_row_controls_are_at_least_40px_wide():
    css = _phone()
    for sel in ("#sf-tbody .sf-act > .sf-cmp", "#sf-tbody .sf-act > button"):
        body = rule_bodies(css, sel)
        m = re.search(r"min-width:\s*(\d+)px", body)
        assert m and int(m.group(1)) >= 40, sel
    # The checkbox's label is as tall as the buttons beside it.
    m = re.search(r"min-height:\s*(\d+)px", rule_bodies(css, "#sf-tbody .sf-act > .sf-cmp"))
    assert m and int(m.group(1)) >= 44
    assert re.search(r"display:\s*inline-flex", rule_bodies(css, "#sf-tbody .sf-act > .sf-cmp"))


_CONTROL = re.compile(r"^(?:button|input|select|textarea)\b|\.(?:b-btn|sf-chip|sf-compare-check)\b")

# Checkboxes drawn smaller on purpose, each inside the <label> that is its
# tap target: {checkbox selector: label selector}.
_IN_LABEL = {
    "#sf-tbody .sf-act > .sf-cmp > .sf-compare-check": "#sf-tbody .sf-act > .sf-cmp",
    "#charimport-dialog fieldset > label > input": "#charimport-dialog fieldset > label",
}


def test_section_never_shrinks_a_tap_target():
    """The global phone layer gives every button, .b-btn, input and select
    a 44px min-height: no rule here may set a pixel (min-)height under that
    on one of them. Any other target (a label) is at least 40px. The two
    checkboxes in _IN_LABEL are the exception: each is drawn small inside a
    label whose own rule makes it a 40px+ target."""
    css = _phone()
    rules = _rules(css)
    assert rules
    for prelude, body in rules:
        sizes = [int(m.group(2)) for m in
                 re.finditer(r"(?<![-\w])(min-height|height):\s*(\d+)px", body)]
        for sel in selectors(prelude):
            if sel in _IN_LABEL:
                m = re.search(r"min-height:\s*(\d+)px", rule_bodies(css, _IN_LABEL[sel]))
                assert m and int(m.group(1)) >= 40, f"{sel}: its label isn't a 40px target"
                continue
            last = re.split(r"\s*[\s>+~]\s*", sel.strip())[-1]
            floor = 44 if _CONTROL.search(last) else 40
            for px in sizes:
                assert px >= floor, f"{sel}: {px}px < {floor}px"


def test_dialog_rows_are_40px_labels():
    css = _phone()
    body = rule_bodies(css, "#charimport-dialog fieldset > label")
    m = re.search(r"min-height:\s*(\d+)px", body)
    assert m and int(m.group(1)) >= 40
    # The checkbox inside keeps no 44px height of its own, or it would size
    # the row instead of the label.
    assert re.search(r"min-height:\s*0\b", rule_bodies(css, "#charimport-dialog fieldset > label > input"))
    # A fieldset is min-content wide by default: one long nowrap fit name
    # would widen its group past the dialog instead of ellipsising.
    assert re.search(r"min-width:\s*0\b", rule_bodies(css, "#charimport-dialog fieldset"))
