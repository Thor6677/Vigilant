"""Mobile R3 T2: Build Finder on phones.

The ranked results become expand-on-tap rows: key 1 the product name as
plain text, key 2 the margin % (D5 A), in green or red as on desktop.
Opened, a row lists its full Name, Build / unit, Sell / unit, Margin and a
Market line holding the market-page link (D6 A). The desktop name cell
stays a link and stays untagged, so phones hide it and a tap on the row
can't navigate away by mistake. A missing invention skill, which desktop
explains only in the ⚠ hover title, is a visible Skills line. Lists past
10 rows stop at 10 with "Show all N".

The decryptor chips, the market-group tree's rows and arrows are 40px
tap targets, and the form's controls stack in one column at 360px. All of
that is CSS in the R3 T2 section, keyed on classes the page and the tree
fragment already carry; the tree is lazy-loaded, so its markup is not
restructured. Desktop renders as before (D21).

The results context is built the way the route builds it, with the
route's own ISK and % formatters. Names and ids are invented."""
import functools
import re
from html.parser import HTMLParser

from app.routes import industry as industry_mod
from tests._mobile import (VOID, SITE_CSS, assert_mrow, assert_single_value_child,
                           cells_rows, clamps, css_section, phone_block, render_page,
                           row_keys, row_labelled, row_lead, rule_bodies)

_section = functools.partial(css_section, release="R3")

_SKILL_ROW = 2      # the one row whose character lacks an invention skill
_NEGATIVE_ROW = 5   # a row that loses money
_LABELS = ["Name", "Build / unit", "Sell / unit", "Margin", "Market"]
_SKILL_LABELS = ["Name", "Build / unit", "Skills", "Sell / unit", "Margin", "Market"]


def _result(i, n):
    """One results row as the route builds it. The last row is unpriced
    (no sell value, so no margin), as unpriced items sort last."""
    cost = 1_250_000.0 + 37_500.0 * i
    unpriced = i == n - 1
    sell = None if unpriced else cost * (0.8 if i == _NEGATIVE_ROW else 1.4 - 0.02 * i)
    margin = None if sell is None else sell - cost
    pct = None if margin is None else margin / cost * 100
    inv = 210_000.0 if i == _SKILL_ROW else None
    return {
        "product_type_id": 700001 + i,
        "product_name": f"Sample Product {i:02d}",
        "cost_str": industry_mod._fmt_isk(cost),
        "sell_str": industry_mod._fmt_isk(sell),
        "margin_isk_str": industry_mod._fmt_isk(margin),
        "margin_pct_str": industry_mod._fmt_pct(pct),
        "margin_positive": margin is not None and margin > 0,
        "priced": not unpriced,
        "inv_str": industry_mod._fmt_isk(inv) if inv is not None else None,
        "skill_missing": i == _SKILL_ROW,
    }


def _render(n=14):
    rows = [_result(i, n) for i in range(n)]
    html = render_page(industry_mod, "partials/build_finder_results.html",
                       "/industry/build-finder/results",
                       rows=rows, total_n=n, shown=n, capped=False, me=10,
                       compute_ms=12.5, invention_active=True, tables_empty=False,
                       selected_character_id=0, encryption=4, science=4,
                       decryptor="none")
    return html, rows


class _Tree(HTMLParser):
    """Every start tag with its attrs and the attrs of its open ancestors."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a, [s[1] for s in self.stack]))
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _tree(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.tags


def _cls(a):
    return a.get("class", "").split()


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _paired(html, rows):
    """The rendered m-rows (cells_rows) beside the context rows they came
    from; fails unless there is one m-row per result."""
    parsed = cells_rows(html)
    assert len(parsed) == len(rows), f"{len(parsed)} m-rows for {len(rows)} results"
    return list(zip(parsed, rows))


def _colour(r):
    if not r["priced"]:
        return "var(--muted)"
    return "var(--success)" if r["margin_positive"] else "var(--danger)"


# ── Results rows ──────────────────────────────────────────────────────

def test_results_are_tap_to_open_rows():
    html, rows = _render()
    parsed = assert_mrow(html, min_rows=14)
    assert len(parsed) == len(rows) == 14
    for r in parsed:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in _cls(r["attrs"])
        assert {"b-table-row", "bf-row"} <= set(_cls(r["attrs"]))
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not row_lead(r), "no icons, so no lead"


def test_key_1_is_the_plain_product_name():
    html, rows = _render()
    for row, r in _paired(html, rows):
        k1 = row_keys(row)[0]
        assert k1["text"] == r["product_name"]
        assert "m-only" in _cls(k1["attrs"]), "desktop keeps its own name cell"
        assert k1["kids"] == [], "plain text: no link to catch the row's tap"


def test_key_2_is_the_margin_percent_coloured_as_on_desktop():
    html, rows = _render()
    for row, r in _paired(html, rows):
        k1, k2 = row_keys(row)
        assert k2["text"] == r["margin_pct_str"]
        style = k2["attrs"]["style"].replace(" ", "")
        assert f"color:{_colour(r)}" in style
        assert "font-weight:600" in style
        assert "m-only" not in _cls(k2["attrs"]), "it is the desktop Margin % cell"
    colours = {_colour(r) for r in rows}
    assert colours == {"var(--success)", "var(--danger)", "var(--muted)"}


def test_opened_row_lists_name_first_then_the_desktop_values():
    html, rows = _render()
    for n, (row, r) in enumerate(_paired(html, rows)):
        labelled = row_labelled(row)
        assert list(labelled) == (_SKILL_LABELS if n == _SKILL_ROW else _LABELS)
        assert labelled["Name"]["text"] == r["product_name"]
        assert "m-only" in _cls(labelled["Name"]["attrs"])
        assert labelled["Build / unit"]["text"].startswith(r["cost_str"])
        assert labelled["Sell / unit"]["text"] == r["sell_str"]
        assert labelled["Margin"]["text"] == r["margin_isk_str"]
    inv_row = cells_rows(html)[_SKILL_ROW]
    build = row_labelled(inv_row)["Build / unit"]
    assert build["text"] == f"{rows[_SKILL_ROW]['cost_str']} (+{rows[_SKILL_ROW]['inv_str']} inv)⚠"
    assert len(build["kids"]) == 1, "one wrapper holds the cost, the inv suffix and the ⚠"


def test_market_link_opens_inside_the_row_and_the_desktop_link_cell_is_untagged():
    html, rows = _render()
    for row, r in _paired(html, rows):
        href = f"/market/type/{r['product_type_id']}"
        market = row_labelled(row)["Market"]
        assert "m-only" in _cls(market["attrs"])
        assert len(market["kids"]) == 1 and market["kids"][0].get("href") == href
        assert market["text"] == "Open market page →"
        assert "m-tap" in _cls(market["kids"][0]), "a 40px target in the open row"
        desktop = _untagged(row)
        assert len(desktop) == 1, "only the desktop name cell is untagged"
        assert desktop[0]["kids"] == [{"href": href, "style": "color:var(--text);text-decoration:none;"}]
        assert desktop[0]["text"] == r["product_name"]
        assert desktop[0]["attrs"]["style"] == "flex:3;font-size:11px;"


def test_missing_skill_reason_is_visible_text_in_the_open_row():
    html, rows = _render()
    titles = re.findall(r'<span title="([^"]+)"[^>]*>&#9888;</span>', html)
    assert titles == ["character missing an invention skill (level 0 used)"]
    for n, (row, _) in enumerate(_paired(html, rows)):
        skills = row_labelled(row).get("Skills")
        if n != _SKILL_ROW:
            assert skills is None
            continue
        assert "m-only" in _cls(skills["attrs"])
        assert skills["text"] == titles[0]


def test_header_row_is_m_head():
    html, _ = _render()
    table_rows = [a for _, a, _ in _tree(html) if "b-table-row" in _cls(a)]
    heads = [a for a in table_rows if "m-head" in _cls(a)]
    assert len(heads) == 1
    assert table_rows[0] is heads[0], "the header comes first"
    for a in table_rows[1:]:
        assert "m-row" in _cls(a)


def test_show_all_past_ten_rows():
    html, _ = _render(14)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    assert [k["children"] for k in c.clamps] == [14]
    assert [b["text"] for b in c.showall] == ["Show all 14"]
    b = c.showall[0]
    assert b["in_wrap"]
    assert {"m-only", "m-showall"} <= set(_cls(b["attrs"]))
    assert b["attrs"].get("type") == "button"
    assert b["attrs"].get("data-click") == "toggleExpanded"
    assert b["attrs"].get("data-toggle-target") == ".m-clamp-wrap"
    for _, a, anc in _tree(html):
        if "m-showall" in _cls(a):
            assert {"m-clamp-wrap", "bf-clamp"} <= set(_cls(anc[-1])), (
                "the button is a direct child of its page-scoped wrap")
        if "m-row" in _cls(a):
            assert "m-clamp" in _cls(anc[-1]), "rows are direct children of the clamp"


def test_no_show_all_at_ten_rows():
    html, _ = _render(10)
    c = clamps(html)
    assert [k["children"] for k in c.clamps] == [10]
    assert c.showall == []


def test_rows_and_tagged_cells_are_never_hidden_inline():
    html, rows = _render()
    for r, _ in _paired(html, rows):
        for a in [r["attrs"]] + [c["attrs"] for c in r["cells"]
                                 if c["attrs"].keys() & {"data-m", "data-m-label"}]:
            assert "display:none" not in a.get("style", "").replace(" ", "")
            assert "hidden" not in a


def test_empty_results_render_no_rows():
    html = render_page(industry_mod, "partials/build_finder_results.html",
                       "/industry/build-finder/results", rows=[], total_n=0, shown=0,
                       capped=False, me=10, compute_ms=1.0, invention_active=False,
                       tables_empty=False)
    assert "No buildable items found" in html
    assert "m-row" not in html and "m-showall" not in html


# ── Tree fragment ─────────────────────────────────────────────────────

_NODES = [
    {"market_group_id": 501, "market_group_name": "Sample Ships", "has_children": True},
    {"market_group_id": 502, "market_group_name": "Sample Modules", "has_children": True},
    {"market_group_id": 503, "market_group_name": "Sample Charges", "has_children": False},
]
_SEARCH = [
    {"market_group_id": 511, "market_group_name": "Sample Frigates",
     "path": "Sample Ships > Sample Frigates"},
    {"market_group_id": 512, "market_group_name": "Sample Assault Frigates",
     "path": "Sample Ships > Sample Frigates > Sample Assault Frigates"},
]


def _tree_html(mode, parent=0):
    nodes = _SEARCH if mode == "search" else _NODES
    return render_page(industry_mod, "partials/build_finder_tree.html",
                       "/industry/build-finder/tree", nodes=nodes, parent=parent, mode=mode)


def test_tree_rows_carry_the_40px_hooks():
    """The phone CSS sizes the tree by these classes: every node's row is
    a .bft-row directly in its li.bft-node, starting with a 40px slot (the
    .bft-arrow button, or the .bft-arrow-empty spacer on a leaf so names
    line up) and then the .bft-select button. The expand arrows keep the
    lazy-load wiring the page JS relies on."""
    for mode, parent, count in (("tree", 0, 3), ("tree", 501, 3), ("search", 0, 2)):
        tags = _tree(_tree_html(mode, parent))
        rows = [(a, anc) for _, a, anc in tags if "bft-row" in _cls(a)]
        assert len(rows) == count
        for a, anc in rows:
            assert "bft-node" in _cls(anc[-1])
        slots = [(t, a, anc) for t, a, anc in tags
                 if {"bft-arrow", "bft-arrow-empty", "bft-select"} & set(_cls(a))]
        assert len(slots) == 2 * count
        for (t1, first, anc1), (t2, sel, anc2) in zip(slots[0::2], slots[1::2]):
            assert "bft-row" in _cls(anc1[-1]) and anc1[-1] is anc2[-1]
            assert {"bft-arrow", "bft-arrow-empty"} & set(_cls(first))
            assert t2 == "button" and "bft-select" in _cls(sel)
            assert sel.get("data-mg-id") and sel.get("data-mg-name")
            if "bft-arrow" in _cls(first):
                mg = first["data-mg-id"]
                assert t1 == "button"
                assert first["hx-get"] == f"/industry/build-finder/tree?parent={mg}"
                assert first["hx-target"] == f"#bft-kids-{mg}"
                assert first["hx-swap"] == "outerHTML"
        if mode == "tree":
            ids = {a.get("id") for _, a, _ in tags}
            assert {"bft-kids-501", "bft-kids-502"} <= ids
        assert "m-row" not in _tree_html(mode, parent), "a picker, not a data list"


# ── Page controls ─────────────────────────────────────────────────────

def _page():
    return render_page(industry_mod, "build_finder.html", "/industry/build-finder",
                       structures=industry_mod.STRUCTURES, rigs=industry_mod.RIGS,
                       sec_statuses=industry_mod.SEC_STATUS, cap=industry_mod.BUILD_FINDER_CAP,
                       decryptors=industry_mod.DECRYPTORS, css_v="1", js_v="1")


def test_page_controls_match_the_phone_selectors():
    """Each phone rule in the section names its element by a child
    combinator; these are the parents it relies on."""
    tags = _tree(_page())
    controls = [(a, anc) for _, a, anc in tags if "bf-controls" in _cls(a)]
    assert len(controls) == 2
    for a, anc in controls:
        assert anc[-1].get("id") == "bf-form"
    selects = [anc for _, a, anc in tags if "bf-select" in _cls(a)]
    assert len(selects) == 6
    for anc in selects:
        assert "bf-field" in _cls(anc[-1])
    me = [anc for _, a, anc in tags if "bf-me" in _cls(a)]
    assert len(me) == 1 and "bf-field" in _cls(me[0][-1])
    chips = [anc for _, a, anc in tags if "bf-chip" in _cls(a)]
    assert len(chips) == 1 + len(industry_mod.DECRYPTORS)
    for anc in chips:
        assert "bf-chip-row" in _cls(anc[-1])
    ids = {a.get("id") for _, a, _ in tags}
    assert {"bf-tree", "bf-market-group", "bf-selected-path", "bf-tree-search",
            "bf-results", "bf-form"} <= ids


def test_rank_button_spacer_label_is_desktop_only():
    """The empty label above Rank builds aligns it with the other fields on
    desktop; stacked on a phone it would be a blank line."""
    html = _page()
    m = re.search(r"<label([^>]*)>&nbsp;</label>\s*<button type=\"submit\" class=\"bf-go\">", html)
    assert m, "the spacer still sits right above the submit button"
    assert 'class="m-hide"' in m.group(1)


# ── CSS ───────────────────────────────────────────────────────────────

def _phone():
    body, desktop = phone_block(_section("T2"))
    return body, desktop


def test_decryptor_chips_are_40px_targets():
    body = rule_bodies(_phone()[0], ".bf-chip-row > .bf-chip")
    for decl in ("display: inline-flex", "align-items: center",
                 "min-height: 40px", "min-width: 40px", "font-size: 12px"):
        assert decl in body, decl


def test_tree_rows_and_arrows_are_40px():
    phone = _phone()[0]
    row = rule_bodies(phone, ".bft-node > .bft-row")
    assert "min-height: 40px" in row
    assert "align-items: stretch" in row, "both buttons fill the row's height"
    arrow = rule_bodies(phone, ".bft-row > .bft-arrow")
    assert "width: 40px" in arrow and "font-size: 12px" in arrow
    assert "width: 40px" in rule_bodies(phone, ".bft-row > .bft-arrow-empty"), (
        "leaf names line up with branch names")
    assert "font-size: 12px" in rule_bodies(phone, ".bft-row > .bft-select")


def test_controls_stack_without_fixed_widths():
    phone = _phone()[0]
    stack = rule_bodies(phone, "#bf-form > .bf-controls")
    assert "flex-direction: column" in stack and "align-items: stretch" in stack
    field = rule_bodies(phone, "#bf-form > .bf-controls > .bf-field")
    assert "min-width: 0 !important" in field, "beats the inline 220px and 240px"
    assert "flex: none !important" in field, "beats the inline flex shorthands"
    assert "min-width: 0" in rule_bodies(phone, ".bf-field > .bf-select"), "the page's 150px"
    assert "width: 100%" in rule_bodies(phone, ".bf-field > .bf-me"), "the page's 60px"


def test_result_keys_share_one_size_and_show_all_is_inset():
    phone = _phone()[0]
    assert "font-size: 12px !important" in rule_bodies(phone, '.bf-row > [data-m="key"]')
    body = rule_bodies(phone, ".bf-clamp > .m-showall")
    assert "width: calc(100% - 1.5rem)" in body
    assert "margin: 0.4rem 0.75rem 0.6rem" in body


def test_section_has_no_desktop_rules():
    """bf-row and bf-clamp act only on phones, and the section's desktop
    slot is empty, so desktop renders as before (D21)."""
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    phone, desktop = _phone()
    assert not desktop.strip()
    for cls in (".bf-row", ".bf-clamp", ".bf-chip", ".bft-", ".bf-controls", ".bf-field"):
        assert css.count(cls) == phone.count(cls) > 0, cls
