"""Mobile R4 T4: the corp inventory tracker and the contract monitor at
phone width.

Three lists become expand-on-tap rows:
- tracked items (partials/corp_inventory_items.html, D10 A: key 2 = quantity);
- the hangar scan (partials/corp_inventory_scan.html, D11 A: Track under the
  row, Show all past 10);
- contract thresholds (partials/corp_contract_items.html, D12 A: key 2 = count).

The add forms stack, and the type-search results become 40px rows.

Desktop must render as before (D21), so the cells phones need that desktop
doesn't have (Name, Status, Hangar, Match) are m-only copies, and the
existing controls (Remove, the Track inputs) sit in display:contents cells.

Contexts follow the shapes app/routes/corporations.py builds; every name
and id is invented."""
import functools
import re
import types
from html.parser import HTMLParser

import pytest

from app.routes import corporations as corps_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, phone_block, render_page, row_keys,
                           row_labelled, row_lead, rule_bodies, SITE_CSS)

_NS = types.SimpleNamespace
_section = functools.partial(css_section, release="R4")

_CORP = 98000001
_LOC = 1000000000001
_LOC_NAME = "Sample Azbel - Main"


# ── A small DOM tree, for checks below a cell's direct children ───────

class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "kids": [], "text": ""}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": {k: (v or "") for k, v in attrs}, "kids": [], "text": ""}
        self.stack[-1]["kids"].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1]["text"] += data


def _tree(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.root


def _walk(node):
    yield node
    for k in node["kids"]:
        yield from _walk(k)


def _cls(node):
    return node["attrs"].get("class", "").split()


def _find(node, pred):
    return [n for n in _walk(node) if pred(n)]


def _text(node):
    return re.sub(r"\s+", " ", node["text"] + " ".join(_text(k) for k in node["kids"])).strip()


def _row_nodes(html, cls, n):
    """The .m-row.<cls> rows as tree nodes; fails unless there are exactly n,
    so no check below can pass over zero rows."""
    rows = _find(_tree(html), lambda r: "m-row" in _cls(r) and cls in _cls(r))
    assert len(rows) == n, f"expected {n} .{cls} rows, found {len(rows)}"
    return rows


def _cells(html, n):
    """cells_rows(html), failing unless there are exactly n rows."""
    rows = cells_rows(html)
    assert len(rows) == n, f"expected {n} m-rows, found {len(rows)}"
    return rows


def _labels(row_node):
    return [k["attrs"]["data-m-label"] for k in row_node["kids"] if "data-m-label" in k["attrs"]]


def _cell(row_node, label):
    cells = [k for k in row_node["kids"] if k["attrs"].get("data-m-label") == label]
    assert len(cells) == 1, f"expected one {label!r} cell, found {len(cells)}"
    return cells[0]


def _style(node):
    return node["attrs"].get("style", "").replace(" ", "")


# ── Fixtures ──────────────────────────────────────────────────────────

def _thr(i, name, qty, state, flag="", low=10, crit=5):
    return _NS(id=i, type_id=34 + i, type_name=name, location_id=_LOC,
               location_name=_LOC_NAME, location_flag=flag, threshold_low=low,
               threshold_critical=crit, current_quantity=qty, alert_state=state)


def _tracked():
    """LOW, CRITICAL and OK; the LOW one sits in a non-default hangar."""
    return [
        _thr(1, "Sample Fuel Block", 1200, "low", flag="CorpSAG2", low=2000, crit=800),
        _thr(2, "Sample Isotopes", 400, "critical", low=3000, crit=1000),
        _thr(3, "Sample Drone II", 820, "ok", low=500, crit=200),
    ]


def _by_location(ts):
    return {_LOC: {"location_name": _LOC_NAME, "items": ts}}


def _render_items(ts=None):
    ts = _tracked() if ts is None else ts
    return render_page(corps_mod, "partials/corp_inventory_items.html",
                       f"/corporations/{_CORP}/inventory/check", corp_id=_CORP,
                       by_location=_by_location(ts), thresholds=ts)


def _render_inventory_page(ts=None):
    ts = _tracked() if ts is None else ts
    return render_page(corps_mod, "corp_inventory.html", f"/corporations/{_CORP}/inventory",
                       corp_id=_CORP, corp_name="Sample Holding", by_location=_by_location(ts),
                       thresholds=ts, hangar_labels=corps_mod.HANGAR_LABELS)


def _scan_items(n, tracked=(1, 4)):
    names = [f"Sample Item {i:02d}" for i in range(n)]
    return [{"location_id": _LOC, "type_id": 600 + i, "type_name": names[i],
             "flag": "CorpSAG1", "flag_label": "Hangar 1", "quantity": 1000 * (i + 1),
             "monitored": i in tracked} for i in range(n)]


def _render_scan(n=14, tracked=(1, 4)):
    return render_page(corps_mod, "partials/corp_inventory_scan.html",
                       f"/corporations/{_CORP}/inventory/scan", corp_id=_CORP,
                       location_name=_LOC_NAME, items=_scan_items(n, tracked))


def _contract(i, label, mtype, count, state, type_id=None, low=10, crit=5):
    return _NS(id=i, match_type=mtype, match_value=str(type_id or label), match_label=label,
               type_id=type_id, threshold_low=low, threshold_critical=crit,
               current_count=count, alert_state=state)


def _contracts():
    """An item match (OK), a title match (LOW) and a critical item match."""
    return [
        _contract(1, "Sample Drone II", "item", 14, "ok", type_id=2488),
        _contract(2, "doctrine", "title", 3, "low", low=6, crit=2),
        _contract(3, "Sample Fuel Block", "item", 0, "critical", type_id=4051, low=2, crit=1),
    ]


def _render_contract_items(ts=None):
    ts = _contracts() if ts is None else ts
    return render_page(corps_mod, "partials/corp_contract_items.html",
                       f"/corporations/{_CORP}/contracts/check", corp_id=_CORP, thresholds=ts)


def _render_contracts_page(ts=None):
    ts = _contracts() if ts is None else ts
    return render_page(corps_mod, "corp_contracts.html", f"/corporations/{_CORP}/contracts",
                       corp_id=_CORP, corp_name="Sample Holding", thresholds=ts)


def _is_img_lead(cell, size="24"):
    a = cell["attrs"]
    return cell["tag"] == "img" and a.get("width") == size and a.get("height") == size


def _assert_icon_pair(row):
    """The lead is a phone-only copy of the icon with no data-on-error. That
    handler sets an inline display:none, which the phone CSS overrides on a
    tagged cell (R1 contract caveat), so a broken icon would show a
    broken-image box. Desktop keeps its own icon, untagged and m-hide, with
    the handler."""
    for c in row["cells"]:
        if "data-m" in c["attrs"] or "data-m-label" in c["attrs"]:
            assert "data-on-error" not in c["attrs"], c["attrs"]
    leads = row_lead(row)
    assert len(leads) == 1
    lead = leads[0]
    a = lead["attrs"]
    assert _is_img_lead(lead)
    assert "m-only" in a.get("class", "").split()
    assert a.get("alt") == "" and "alt" in a
    assert a.get("loading") == "lazy"
    desk = [c for c in row["cells"] if c["tag"] == "img" and c is not lead]
    assert len(desk) == 1
    d = desk[0]["attrs"]
    assert "data-m" not in d and "data-m-label" not in d
    assert "m-hide" in d.get("class", "").split()
    assert d.get("data-on-error") == "hide"
    assert d.get("src") == a.get("src")


# ── 1. Tracked items (D10 A) ──────────────────────────────────────────

def test_tracked_items_are_expandable_rows():
    rows = assert_mrow(_render_items(), 3)
    assert len(rows) == 3
    assert all(r["attrs"].get("data-click") == "toggleMRow" for r in rows)


def test_tracked_item_lead_is_the_sized_icon_and_key_one_the_name():
    for row, t in zip(_cells(_render_items(), 3), _tracked()):
        lead = row_lead(row)
        assert len(lead) == 1 and _is_img_lead(lead[0]), lead
        k1 = row_keys(row)[0]
        assert k1["text"].startswith(t.type_name)
        # The desktop hangar sub-span stays in the name cell, phone-hidden.
        assert all("m-hide" in k.get("class", "").split() for k in k1["kids"]), k1["kids"]


def test_tracked_item_lead_is_a_phone_icon_without_the_hide_handler():
    for row in _cells(_render_items(), 3):
        _assert_icon_pair(row)


@pytest.mark.parametrize("idx, colour", [(0, "var(--warn)"), (1, "var(--danger)"), (2, "var(--text)")])
def test_tracked_item_key_two_is_the_quantity_coloured_by_alert_state(idx, colour):
    row = _cells(_render_items(), 3)[idx]
    keys = row_keys(row)
    assert len(keys) == 2
    assert keys[1]["text"] == str(_tracked()[idx].current_quantity)
    assert f"color:{colour}" in keys[1]["attrs"]["style"].replace(" ", "")


def test_tracked_item_unknown_quantity_is_a_dash():
    t = _thr(9, "Sample Unchecked", None, "ok")
    row = _cells(_render_items([t]), 1)[0]
    assert row_keys(row)[1]["text"] == "—"


def test_tracked_item_labels_come_in_order_with_name_first():
    rows = _row_nodes(_render_items(), "cinv-item", 3)
    assert _labels(rows[0]) == ["Name", "Status", "Thresholds", "Hangar", "Remove"]
    assert _labels(rows[1]) == ["Name", "Status", "Thresholds", "Remove"]
    assert _labels(rows[2]) == ["Name", "Status", "Thresholds", "Remove"]
    for row, t in zip(rows, _tracked()):
        name = _cell(row, "Name")
        assert "m-only" in _cls(name)
        assert _text(name) == t.type_name


def test_tracked_item_hangar_is_an_m_only_labelled_cell():
    rows = _row_nodes(_render_items(), "cinv-item", 3)
    hangar = _cell(rows[0], "Hangar")
    assert "m-only" in _cls(hangar)
    assert _text(hangar) == "Hangar 2"


@pytest.mark.parametrize("idx, word, cls", [(0, "LOW", "is-warn"), (1, "CRITICAL", "is-danger"), (2, "OK", None)])
def test_tracked_item_status_is_a_real_badge(idx, word, cls):
    """The m-only Status cell holds the same badge element as desktop, not
    an escaped copy of its markup."""
    row = _row_nodes(_render_items(), "cinv-item", 3)[idx]
    status = _cell(row, "Status")
    assert "m-only" in _cls(status)
    assert len(status["kids"]) == 1
    badge = status["kids"][0]
    assert "b-badge" in _cls(badge) and _text(badge) == word
    if cls:
        assert cls in _cls(badge)
    assert "&lt;" not in _render_items()
    # Desktop keeps its own badge: an untagged direct child of the row.
    desk = [k for k in row["kids"] if "b-badge" in _cls(k)]
    assert len(desk) == 1 and _text(desk[0]) == word and "m-only" not in _cls(desk[0])


def test_tracked_item_thresholds_is_the_existing_cell():
    row = _row_nodes(_render_items(), "cinv-item", 3)[0]
    cell = _cell(row, "Thresholds")
    assert "m-only" not in _cls(cell)
    assert _text(cell) == "L:2000 C:800"


def test_tracked_item_remove_is_the_existing_control_inside_a_labelled_cell():
    for row, t in zip(_row_nodes(_render_items(), "cinv-item", 3), _tracked()):
        cell = _cell(row, "Remove")
        # display:contents keeps the button a flex item of the desktop row.
        assert "display:contents" in _style(cell)
        assert len(cell["kids"]) == 1
        btn = cell["kids"][0]
        assert btn["tag"] == "button"
        assert "m-tap" not in _cls(btn), "m-tap's 40px would shrink the 44px button"
        assert btn["attrs"]["hx-post"] == f"/corporations/{_CORP}/inventory/threshold/{t.id}/delete"
        assert btn["attrs"]["hx-confirm"]
        assert btn["attrs"].get("aria-label") == f"Remove {t.type_name}"
        # One Remove control per row: no phone copy.
        assert len(_find(row, lambda n: "hx-post" in n["attrs"])) == 1


def test_tracked_item_labelled_cells_hold_one_value_element():
    for row in _cells(_render_items(), 3):
        assert_single_value_child(row)


def test_tracked_items_location_heading_stays_visible():
    """The location is the group heading (the mockup's panel subtitle) and
    there is no per-row Location line, so phones keep it."""
    heads = _find(_tree(_render_items()), lambda n: "cinv-loc" in _cls(n))
    assert len(heads) == 1
    cls = _cls(heads[0])
    assert "m-row" not in cls and "m-head" not in cls and "m-hide" not in cls
    assert _text(heads[0]) == _LOC_NAME


def test_inventory_page_renders_the_tracked_rows():
    assert len(assert_mrow(_render_inventory_page(), 3)) == 3


# ── 2. Hangar scan (D11 A) ────────────────────────────────────────────

def test_scan_rows_are_expandable_and_keep_their_scan_data():
    rows = assert_mrow(_render_scan(), 14)
    assert len(rows) == 14
    for r in rows:
        a = r["attrs"]
        assert a.get("data-click") == "toggleMRow"
        for k in ("data-scan-item", "data-type-id", "data-type-name", "data-location-id",
                  "data-location-name", "data-flag"):
            assert k in a, k


def test_scan_row_lead_icon_key_one_item_key_two_quantity():
    items = _scan_items(14)
    for row, item in zip(_cells(_render_scan(), 14), items):
        lead = row_lead(row)
        assert len(lead) == 1 and _is_img_lead(lead[0])
        k1, k2 = row_keys(row)
        assert k1["text"].startswith(item["type_name"])
        assert all("m-hide" in k.get("class", "").split() for k in k1["kids"])
        assert k2["text"] == str(item["quantity"])
        assert "color:var(--accent)" in k2["attrs"]["style"].replace(" ", "")


def test_scan_row_lead_is_a_phone_icon_without_the_hide_handler():
    for row in _cells(_render_scan(), 14):
        _assert_icon_pair(row)


def test_scan_row_labels_are_name_hangar_track():
    for row in _row_nodes(_render_scan(), "cinv-scan", 14):
        assert _labels(row) == ["Name", "Hangar", "Track"]
        assert "m-only" in _cls(_cell(row, "Name"))
        hangar = _cell(row, "Hangar")
        assert "m-only" in _cls(hangar) and _text(hangar) == "Hangar 1"


def test_scan_track_cell_holds_the_inputs_and_button_in_one_value_element():
    rows = _row_nodes(_render_scan(), "cinv-scan", 14)
    untracked = [r for i, r in enumerate(rows) if i not in (1, 4)]
    assert len(untracked) == 12
    for row in untracked:
        cell = _cell(row, "Track")
        assert "display:contents" in _style(cell)
        assert len(cell["kids"]) == 1
        group = cell["kids"][0]
        assert group["tag"] == "span" and "display:contents" in _style(group)
        kids = group["kids"]
        assert [k["tag"] for k in kids] == ["span", "input", "span", "input", "button"]
        # Phones name each box with a short m-only letter; desktop keeps the
        # hover title. Screen readers get an aria-label at both widths.
        for letter, k in (("L", kids[0]), ("C", kids[2])):
            assert "m-only" in _cls(k) and _text(k) == letter
            # The inputs' aria-labels name them; the letter isn't read twice.
            assert k["attrs"].get("aria-hidden") == "true"
        low, crit = kids[1], kids[3]
        assert "scan-low" in _cls(low) and "scan-critical" in _cls(crit)
        assert low["attrs"].get("aria-label") == "Low threshold"
        assert crit["attrs"].get("aria-label") == "Critical threshold"
        assert low["attrs"].get("title") == "Low threshold"
        assert crit["attrs"].get("title") == "Critical threshold"
        assert kids[4]["attrs"].get("data-click") == "addScanItem"
        # addScanItem reads row.querySelector('.scan-low'): one of each per row.
        assert len(_find(row, lambda n: "scan-low" in _cls(n))) == 1
        assert len(_find(row, lambda n: "scan-critical" in _cls(n))) == 1


def test_scan_track_cell_says_tracked_when_already_monitored():
    rows = _row_nodes(_render_scan(), "cinv-scan", 14)
    for i in (1, 4):
        cell = _cell(rows[i], "Track")
        assert _text(cell) == "Tracked"
        assert not _find(rows[i], lambda n: n["tag"] in ("input", "button"))


def test_scan_labelled_cells_hold_one_value_element():
    for row in _cells(_render_scan(), 14):
        assert_single_value_child(row)


def test_scan_shows_all_past_ten():
    c = clamps(_render_scan(14))
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.clamps) == 1
    # Only rows sit directly under .m-clamp: its nth-child(n+11) rule counts them.
    assert c.clamps[0]["children"] == 14
    assert len(c.showall) == 1
    btn = c.showall[0]
    assert btn["text"] == "Show all 14"
    assert btn["in_wrap"]
    a = btn["attrs"]
    assert "m-only" in a["class"].split()
    assert a.get("type") == "button"
    assert a.get("data-click") == "toggleExpanded"
    assert a.get("data-toggle-target") == ".m-clamp-wrap"


def test_scan_of_ten_has_no_show_all():
    c = clamps(_render_scan(10, tracked=()))
    assert c.clamps[0]["children"] == 10
    assert c.showall == []


def test_empty_scan_is_the_plain_message():
    html = _render_scan(0, tracked=())
    assert "m-row" not in html and "m-clamp" not in html
    assert "No items found" in html


def test_scan_box_unclamps_on_phones():
    box = _find(_tree(_render_inventory_page()), lambda n: n["attrs"].get("id") == "scan-results")
    assert len(box) == 1 and "m-unclamp" in _cls(box[0])
    # Desktop keeps its 400px scroll box.
    assert "max-height:400px" in _style(box[0])


# ── 3. Contract thresholds (D12 A) ────────────────────────────────────

def test_contract_rows_are_expandable_rows():
    rows = assert_mrow(_render_contract_items(), 3)
    assert len(rows) == 3
    assert all(r["attrs"].get("data-click") == "toggleMRow" for r in rows)


@pytest.mark.parametrize("idx, colour", [(0, "var(--text)"), (1, "var(--warn)"), (2, "var(--danger)")])
def test_contract_key_two_is_the_count_coloured_by_alert_state(idx, colour):
    row = _cells(_render_contract_items(), 3)[idx]
    k1, k2 = row_keys(row)
    t = _contracts()[idx]
    assert k1["text"].startswith(t.match_label)
    assert all("m-hide" in k.get("class", "").split() for k in k1["kids"])
    assert k2["text"] == str(t.current_count)
    assert f"color:{colour}" in k2["attrs"]["style"].replace(" ", "")


def test_contract_lead_is_the_icon_or_the_title_glyph():
    rows = _cells(_render_contract_items(), 3)
    assert _is_img_lead(row_lead(rows[0])[0])
    glyph = row_lead(rows[1])[0]
    assert glyph["tag"] == "span" and glyph["text"] == "T"
    assert _is_img_lead(row_lead(rows[2])[0])


def test_contract_item_lead_is_a_phone_icon_without_the_hide_handler():
    rows = _cells(_render_contract_items(), 3)
    for i in (0, 2):
        _assert_icon_pair(rows[i])
    # A title match keeps its single "T" glyph lead, with no desktop icon.
    assert not [c for c in rows[1]["cells"] if c["tag"] == "img"]
    assert all("data-on-error" not in c["attrs"] for c in rows[1]["cells"])


def test_contract_labels_come_in_order():
    rows = _row_nodes(_render_contract_items(), "cctr-item", 3)
    for row, t in zip(rows, _contracts()):
        assert _labels(row) == ["Name", "Match", "Status", "Thresholds", "Remove"]
        for label in ("Name", "Match", "Status"):
            assert "m-only" in _cls(_cell(row, label)), label
        assert _text(_cell(row, "Name")) == t.match_label
        assert _text(_cell(row, "Match")) == t.match_type


@pytest.mark.parametrize("idx, word", [(0, "OK"), (1, "LOW"), (2, "CRITICAL")])
def test_contract_status_is_a_real_badge(idx, word):
    row = _row_nodes(_render_contract_items(), "cctr-item", 3)[idx]
    status = _cell(row, "Status")
    assert len(status["kids"]) == 1
    assert "b-badge" in _cls(status["kids"][0]) and _text(status["kids"][0]) == word


def test_contract_unchecked_status_is_a_dash_badge():
    t = _contract(9, "Sample New", "title", None, "ok")
    row = _row_nodes(_render_contract_items([t]), "cctr-item", 1)[0]
    badge = _cell(row, "Status")["kids"][0]
    assert "b-badge" in _cls(badge) and _text(badge) == "—"


def test_contract_remove_is_the_existing_control_inside_a_labelled_cell():
    for row, t in zip(_row_nodes(_render_contract_items(), "cctr-item", 3), _contracts()):
        cell = _cell(row, "Remove")
        assert "display:contents" in _style(cell)
        assert len(cell["kids"]) == 1
        btn = cell["kids"][0]
        assert btn["tag"] == "button" and "m-tap" not in _cls(btn)
        assert btn["attrs"]["hx-post"] == f"/corporations/{_CORP}/contracts/threshold/{t.id}/delete"
        assert btn["attrs"].get("aria-label") == f"Remove {t.match_label}"
        assert len(_find(row, lambda n: "hx-post" in n["attrs"])) == 1


def test_contract_labelled_cells_hold_one_value_element():
    for row in _cells(_render_contract_items(), 3):
        assert_single_value_child(row)


def test_contracts_page_renders_the_threshold_rows():
    assert len(assert_mrow(_render_contracts_page(), 3)) == 3


# ── 4. Forms ──────────────────────────────────────────────────────────

def _by_id(html, el_id):
    found = _find(_tree(html), lambda n: n["attrs"].get("id") == el_id)
    assert len(found) == 1, el_id
    return found[0]


def _parent_of(root, node):
    for n in _walk(root):
        if any(k is node for k in n["kids"]):
            return n
    raise AssertionError("no parent")


def test_inventory_import_bar_stacks_so_the_select_goes_full_width():
    html = _render_inventory_page()
    root = _tree(html)
    sel = [n for n in _walk(root) if n["attrs"].get("id") == "scan-location"][0]
    bar = _parent_of(root, sel)
    assert "m-stack" in _cls(bar)


def test_inventory_add_item_row_stacks():
    root = _tree(_render_inventory_page())
    add = [n for n in _walk(root) if n["attrs"].get("data-click") == "addManualItem"][0]
    row = _parent_of(root, add)
    assert "m-stack" in _cls(row)
    ids = {n["attrs"].get("id") for n in _walk(row)}
    assert {"add-search", "add-location", "add-flag", "add-low", "add-critical"} <= ids


def test_contracts_add_threshold_row_stacks_and_keeps_its_match_toggle():
    root = _tree(_render_contracts_page())
    add = [n for n in _walk(root) if n["attrs"].get("data-click") == "addContractThreshold"][0]
    row = _parent_of(root, add)
    assert "m-stack" in _cls(row)
    kid_ids = [k["attrs"].get("id") for k in row["kids"]]
    assert "ct-title-field" in kid_ids and "ct-item-field" in kid_ids
    # The Match By toggle still shows and hides these with inline display.
    item_field = [k for k in row["kids"] if k["attrs"].get("id") == "ct-item-field"][0]
    assert "display:none" in _style(item_field)


@pytest.mark.parametrize("render, el_id", [
    (_render_inventory_page, "search-results"),
    (_render_contracts_page, "ct-search-results"),
])
def test_type_search_results_carry_the_phone_hook(render, el_id):
    assert "corp-type-results" in _cls(_by_id(render(), el_id))


# ── 5. CSS section ────────────────────────────────────────────────────

def _phone():
    phone, after = phone_block(_section("T4"))
    return phone


def _body(selector):
    return rule_bodies(_phone(), selector).replace(" ", "")


def test_section_is_one_phone_block_and_nothing_else():
    phone, after = phone_block(_section("T4"))
    assert phone.strip()
    assert after.strip() == "", f"rules after the phone block: {after.strip()[:80]!r}"
    assert "@media" not in phone


@pytest.mark.parametrize("row", ["cinv-item", "cctr-item"])
def test_remove_is_a_44px_square_on_phones(row):
    """The global phone button rule gives the 44px height; this widens it."""
    body = _body(f'.{row} > [data-m-label="Remove"] > .b-btn')
    assert "min-width:44px" in body
    assert "height" not in body


def test_type_search_rows_are_40px_on_phones():
    assert "min-height:40px" in _body(".corp-type-results > .b-row")


def test_track_group_lays_out_as_one_value_in_an_opened_row():
    """The inline display:contents would otherwise spill the two inputs and
    the button into the opened cell as separate items."""
    group = _body('.cinv-scan > [data-m-label="Track"] > span')
    assert "display:inline-flex!important" in group
    # The 40-44px button and inputs line up at one height.
    assert "align-items:stretch" in group


def test_track_inputs_are_wider_than_their_desktop_50px_on_phones():
    body = _body('.cinv-scan > [data-m-label="Track"] > span > input')
    m = re.search(r"width:([\d.]+)rem!important", body)
    assert m and float(m.group(1)) * 16 > 50


def test_track_letters_are_muted_11px_on_phones():
    """Polish B forces open-row descendants to font-size:inherit !important at
    (0,4,1); this rule's (0,4,2) and !important keep the letters at 11px."""
    body = _body('.m-row.cinv-scan > [data-m-label="Track"] > span > span.m-only')
    assert "font-size:11px!important" in body
    assert "color:var(--muted)" in body


def test_rows_keys_are_12px_on_phones():
    for sel in ('.cinv-item > [data-m="key"]', '.cinv-scan > [data-m="key"]',
                '.cctr-item > [data-m="key"]'):
        assert "font-size:12px!important" in _body(sel), sel


def test_title_glyph_lead_keeps_its_24px_width():
    assert "width:24px!important" in _body('.cctr-item > span[data-m="lead"]')


def test_long_location_heading_wraps_instead_of_widening_the_page():
    body = _body(".cinv-loc > .b-row-label")
    assert "min-width:0" in body and "overflow-wrap:anywhere" in body


def test_new_class_hooks_are_styled_only_in_this_sections_phone_block():
    css = re.sub(r"/\*.*?\*/", "", open(SITE_CSS, encoding="utf-8").read(), flags=re.S)
    phone = _phone()
    for cls in (".cinv-item", ".cinv-scan", ".cinv-scan-wrap", ".cctr-item", ".cinv-loc", ".corp-type-results"):
        n = len(re.findall(re.escape(cls) + r"(?![\w-])", phone))
        assert n, cls
        assert len(re.findall(re.escape(cls) + r"(?![\w-])", css)) == n, f"{cls} styled outside R4 T4"


def test_form_labels_and_contracts_hint_are_11px_on_phones():
    assert "font-size:11px!important" in _body(".cinv-form label")
    assert "font-size:11px!important" in _body(".cctr-hint")


class _Labels(HTMLParser):
    """Every <label>'s text, whether it sits inside a .cinv-form, and
    whether it carries the inline 9px."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.labels, self.cur = [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            inside = any("cinv-form" in (c or "").split() for c in self.stack)
            self.cur = [inside, "", "font-size:9px" in (a.get("style") or "").replace(" ", "")]
            self.labels.append(self.cur)
        if tag not in VOID:
            self.stack.append(a.get("class") or "")

    def handle_endtag(self, tag):
        if tag == "label":
            self.cur = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.cur is not None:
            self.cur[1] += data


@pytest.mark.parametrize("render, labels", [
    (_render_inventory_page, ["Structure", "Item", "Structure", "Hangar", "Low", "Critical"]),
    (_render_contracts_page, ["Match By", "Keyword", "Item Type", "Low", "Critical"]),
])
def test_every_form_label_sits_in_a_cinv_form(render, labels):
    """The phone rule reaches the 9px labels through .cinv-form."""
    p = _Labels()
    p.feed(render())
    form = [" ".join(t.split()) for inside, t, _ in p.labels if inside]
    assert form == labels
    small = [(inside, t) for inside, t, nine in p.labels if nine]
    assert len(small) == len(labels) and all(inside for inside, _ in small), \
        "every 9px label is inside a .cinv-form"
