"""T-076 Part A item 1: the account-group header regression.

Before the fix, the collapse chevron (a `.b-btn`, `flex: 1` by default —
design-system/css/components.css) had no override, so it grew to fill
`.b-section-head`'s free space (that class is `justify-content:
space-between`) — an empty full-width bar holding only the ▾ glyph, with the
account name and pilot count both shoved toward the far right. This proves
every button in the header macro is `flex:none` (never grows) and that the
rendered order is chevron, name, (collapsed summary), count — in both Cards
and Compact, which share one macro.
"""
import re

from tests._dashboard_fixture import render_full


def _header_block(html: str) -> str:
    start = html.index('data-click="toggleDashGroup"')
    # Back up to the b-section-head div itself (not some nested div that
    # happens to open closer to the marker, e.g. Cards' edit-mode-only
    # wrapper), forward to its balanced close.
    div_start = html.rfind('<div class="b-section-head"', 0, start)
    depth = 0
    for m in re.finditer(r"<div\b|</div\s*>", html[div_start:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return html[div_start:div_start + m.end()]
    raise AssertionError("unbalanced header div")


def test_no_button_in_the_header_is_free_to_grow():
    """Every `.b-btn` inside the header (chevron, and in Cards the
    move-up/down pair) must explicitly opt out of the class default's
    `flex: 1`, or it swallows the row's free space again."""
    for mode, show_edit in (("cards", True), ("compact", False)):
        html = render_full("custom", dash_mode=mode)
        header = _header_block(html)
        for m in re.finditer(r'<button[^>]*class="[^"]*b-btn[^"]*"[^>]*style="([^"]*)"', header):
            assert "flex:none" in m.group(1).replace(" ", ""), (
                f"a header button in {mode} mode can still grow: {m.group(0)}"
            )


def test_header_order_is_chevron_then_name_then_count_on_the_right():
    html = render_full("custom", dash_mode="cards")
    header = _header_block(html)
    chevron_idx = header.index("dash-group-toggle")
    name_idx = header.index("group-label")
    count_idx = header.rindex("b-muted-sm")
    assert chevron_idx < name_idx < count_idx


def test_count_sits_at_the_end_with_margin_left_auto():
    html = render_full("custom", dash_mode="cards")
    header = _header_block(html)
    m = re.search(r'<span class="b-muted-sm"[^>]*style="([^"]*)"', header)
    assert m is not None
    assert "margin-left:auto" in m.group(1).replace(" ", "")


def test_collapsed_summary_appears_inline_after_the_name_in_both_modes():
    for mode in ("cards", "compact"):
        html = render_full("custom", dash_mode=mode, prefs_patch={"collapsed_groups": ["Sample Corp"]})
        header = _header_block(html)
        name_idx = header.index("group-label")
        summary_idx = header.index("dash-group-summary")
        count_idx = header.rindex("b-muted-sm")
        assert name_idx < summary_idx < count_idx


def test_cards_header_still_carries_the_edit_mode_controls():
    html = render_full("custom", dash_mode="cards")
    header = _header_block(html)
    assert "group-move-up" in header
    assert "group-move-down" in header


def test_compact_header_has_no_edit_mode_controls():
    html = render_full("custom", dash_mode="compact")
    header = _header_block(html)
    assert "group-move-up" not in header
    assert "group-move-down" not in header
