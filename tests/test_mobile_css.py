"""Mobile design §4 / §8.1: contract checks on static/css/site.css.

These read the stylesheet as text. They pin the rules every phone layout in
the app depends on, so a later edit can't silently drop one."""
import os
import re

_SITE_CSS = os.path.join(os.path.dirname(__file__), "..", "static", "css", "site.css")
PHONE = "max-width: 640px"


def _css() -> str:
    with open(_SITE_CSS, encoding="utf-8") as fh:
        return re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)


def _media_bodies(css: str, query: str) -> str:
    """Concatenated bodies of every @media block whose prelude contains `query`."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if query not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth and i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        out.append(css[m.end():i - 1])
    return "\n".join(out)


def _phone() -> str:
    return _media_bodies(_css(), PHONE)


def test_phone_inputs_are_16px_important():
    assert re.search(r"input,\s*select,\s*textarea\s*\{[^}]*font-size:\s*16px\s*!important", _phone())


def test_no_invalid_max_with_inherit_anywhere():
    assert not re.search(r"max\([^)]*inherit", _css())


def test_panel_safety_net_scrolls_instead_of_clipping():
    phone = _phone()
    assert re.search(r"\.b-panel\s*\{[^}]*overflow-x:\s*auto", phone)
    assert re.search(r"\.b-panel\.is-brackets\s*\{[^}]*overflow:\s*visible", phone)


def test_full_width_stat_override_removed_and_grid_added():
    assert not re.search(r"\.b-stat\s*\{\s*flex:\s*1 1 100%", _css())
    assert re.search(r"\.b-stats\s*\{[^}]*grid-template-columns:\s*repeat\(2", _phone())


def test_mrow_places_every_item_explicitly():
    phone = _phone()
    assert re.search(r'\.m-row > \[data-m="lead"\]\s*\{[^}]*grid-column:\s*1', phone)
    assert re.search(r'\.m-row > \[data-m="key"\]\s*\{[^}]*grid-column:\s*2', phone)
    assert re.search(r'\.m-row > \[data-m="key"\] ~ \[data-m="key"\]\s*\{[^}]*grid-column:\s*3', phone)
    assert re.search(r"\.m-row:not\(\.m-row--link\)::after\s*\{[^}]*grid-column:\s*4", phone)
    assert re.search(r"\.m-row > \[data-m-label\]\s*\{[^}]*grid-column:\s*1 / -1", phone)


def test_mrow_open_states_cover_both_expanders():
    phone = _phone()
    for sel in (r"\.m-row\.is-open:not\(\.m-row--link\) > \[data-m-label\]",
                r"\.m-row\.is-expanded:not\(\.m-row--link\) > \[data-m-label\]",
                r"\.is-expanded > \.m-row:not\(\.m-row--link\) > \[data-m-label\]"):
        assert re.search(sel, phone), sel


def test_open_cells_group_value_pieces_right():
    phone = _phone()
    assert re.search(r"\.is-expanded > \.m-row:not\(\.m-row--link\) > \[data-m-label\]\s*\{[^}]*justify-content:\s*flex-end[^}]*flex-wrap:\s*wrap", phone)
    assert re.search(r"\.m-row > \[data-m-label\]::before\s*\{[^}]*margin-right:\s*auto", phone)


def test_open_states_exclude_link_rows():
    phone = _phone()
    checked = 0
    for m in re.finditer(r"([^{}]+)\{", phone):
        for sel in m.group(1).split(","):
            if ".m-row" in sel and re.search(r"\.is-(?:open|expanded)\b", sel):
                assert ":not(.m-row--link)" in sel, sel.strip()
                checked += 1
    # Not vacuous: the chevron and labelled-cell rules give 3 selectors each.
    assert checked >= 6, checked
    assert ".m-row.is-open:not(.m-row--link)" in phone
    assert ".is-expanded > .m-row:not(.m-row--link) > [data-m-label]" in phone


def test_m_hide_is_last_phone_utility():
    phone = _phone()
    i = phone.rindex(".m-hide")
    assert i > phone.index(".m-tap") and i > phone.index(".m-pair")


def test_utilities_exist():
    css = _css()
    phone = _phone()
    desktop = _media_bodies(css, "min-width: 641px")
    assert re.search(r"\.m-only[^{]*\{[^}]*display:\s*none\s*!important", desktop)
    assert re.search(r"details\.m-tabs[^{]*\{[^}]*display:\s*none\s*!important", desktop)
    for cls in (r"\.m-hide", r"\.m-tabs-desktop", r"\.m-tap", r"\.m-unclamp",
                r"\.m-clamp-wrap", r"\.m-stack", r"\.m-pair", r"\.m-head",
                r"table\.m-table"):
        assert re.search(cls, phone), cls


def test_menu_groups_are_styled():
    assert re.search(r"\.b-mobile-menu \.b-mobile-group > summary", _css())


# ── Polish A ─────────────────────────────────────────────────────────
# Menu, tab dropdown and glyph rules. Most menu rules aren't media-scoped
# (the menu only shows below 1000px), so these read the whole sheet.

def _rules(css: str):
    """(selectors, body) for every innermost rule; @media preludes drop out
    because a selector run can't contain a brace."""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield [s.strip() for s in m.group(1).split(",")], m.group(2)


def _rule(css: str, selector: str) -> str:
    bodies = [body for sels, body in _rules(css) if selector in sels]
    assert bodies, f"no rule for {selector}"
    return "\n".join(bodies)


def _has_decl(body: str, decl: str) -> bool:
    """`decl` ("prop: value") as a whole declaration, not a prefix of a longer one."""
    prop, value = (x.strip() for x in decl.split(":", 1))
    return bool(re.search(rf"(?:^|[;{{\s]){re.escape(prop)}:\s*{re.escape(value)}\s*(?:;|$)", body))


def test_b_tab_extra_rules_are_pinned():
    css = _css()
    box = _rule(css, ".b-tab-strip .b-tab-extra")
    assert _has_decl(box, "margin-left: auto")
    link = _rule(css, ".b-tab-strip .b-tab-extra a")
    for decl in ("font-size: 9px", "padding: 0", "border-bottom: none"):
        assert _has_decl(link, decl), decl


_GLYPH_RULES = [
    (".b-mobile-menu .b-mobile-group > summary::after", "▸"),
    (".b-mobile-menu .b-mobile-group[open] > summary::after", "▾"),
    (".m-row:not(.m-row--link)::after", "▸"),
    (".m-row.is-open:not(.m-row--link)::after", "▾"),
    (".m-row.is-expanded:not(.m-row--link)::after", "▾"),
    (".is-expanded > .m-row:not(.m-row--link)::after", "▾"),
    ("details.m-tabs > summary::after", "▾"),
    ("details.m-tabs[open] > summary::after", "▴"),
]


def test_decorative_glyphs_have_empty_alt_text():
    """Screen readers announce generated content; `content: "X" / ""` gives
    it empty alt text. The plain declaration first is the fallback for
    engines that drop the alt-text form."""
    css = _css()
    for selector, glyph in _GLYPH_RULES:
        body = _rule(css, selector)
        assert f'content: "{glyph}"; content: "{glyph}" / "";' in body, selector
    # No bare chevron left in the menu, m-row or tab dropdown rules.
    for sels, body in _rules(css):
        if any(k in s for s in sels for k in (".b-mobile-group", ".m-row", "m-tabs")):
            for g in re.findall(r'content:\s*"([▸▾▴])"\s*;', body):
                assert f'content: "{g}" / ""' in body, sels


def test_tab_separator_is_a_gap_below_the_last_tab():
    sep = _rule(_phone(), "details.m-tabs .m-tabs-sep")
    assert _has_decl(sep, "height: 6px")
    assert _has_decl(sep, "border-bottom: 1px solid var(--border)")
    # A top border would sit against the last tab's bottom border: one 2px line.
    assert "border-top" not in sep


def test_tab_dropdown_rules_target_the_list_by_class():
    css = _css()
    assert "nav.m-tabs-list" not in css
    assert _has_decl(_rule(_phone(), "details.m-tabs .m-tabs-list"), "border-top: 1px solid var(--border)")


def test_menu_focus_rings_are_not_clipped():
    css = _css()
    # The menu scrolls (overflow-y:auto), so an outside ring is clipped.
    summary = _rule(css, ".b-mobile-menu .b-mobile-group > summary:focus-visible")
    assert _has_decl(summary, "outline-offset: -2px")
    # Links and Logout use the underline treatment instead of a ring.
    underline = _rule(css, ".b-mobile-menu a:focus-visible")
    assert _has_decl(underline, "outline: none")
    assert ".b-mobile-menu .b-mobile-logout:focus-visible" in [
        s for sels, body in _rules(css) if body == underline for s in sels]


def test_dead_menu_separator_rule_is_gone():
    assert ".b-mobile-sep" not in _css()


def test_mobile_logout_lines_up_with_menu_links():
    css = _css()
    link = _rule(css, ".b-mobile-menu a")
    logout = _rule(css, ".b-mobile-menu .b-mobile-logout")
    for decl in ("display: block", "padding: 0.75rem 1.5rem", "font-size: 13px",
                 "letter-spacing: 0.14em", "text-transform: uppercase"):
        assert _has_decl(link, decl) and _has_decl(logout, decl), decl
    # The UA button styles that would otherwise misalign it.
    for decl in ("width: 100%", "background: none", "border: none", "font-family: inherit",
                 "line-height: inherit", "text-align: left"):
        assert _has_decl(logout, decl), decl


def test_hamburger_focus_ring_stays_on_screen():
    """Closing the menu puts focus on the Menu button, which fills the bar's
    height; an outside ring would lose its top edge off-screen."""
    assert _has_decl(_rule(_css(), ".b-hamburger:focus-visible"), "outline-offset: -2px")
