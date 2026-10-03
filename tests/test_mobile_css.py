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
    for m in re.finditer(r"([^{}]*\.(?:is-open|is-expanded)[^{}]*)\{", phone):
        for sel in m.group(1).split(","):
            if ".m-row" in sel and ("is-open" in sel or "is-expanded" in sel) and ".m-row" in sel.split("is-")[-1] + ".m-row" and "m-tabs" not in sel and "m-clamp" not in sel:
                assert ":not(.m-row--link)" in sel, sel.strip()
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
