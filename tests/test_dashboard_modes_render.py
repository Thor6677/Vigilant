"""T-070 render tests for modes, collapsible groups and collapsible/hideable
lower sections. Uses the same deterministic harness as the pilot-card
refactor proof (tests/_dashboard_fixture.py) — no database, no real
`datetime.now()` dependency.
"""
import re

from tests._dashboard_fixture import CHAR_PAUSED, CHARACTERS, build_context, extract_card, render_full

_GAME_WIDE_LOAD_HX_GETS = (
    'hx-get="/dashboard/recent-battles"',
    'hx-get="/dashboard/activity?window=24h"',
    'hx-get="/dashboard/kill-pulse"',
)
_COMBAT_PROFILE_HX_GET = 'hx-get="/dashboard/combat-profile"'


def test_default_mode_is_cards():
    html = render_full("custom")
    assert "dash-compact-row" not in html
    assert 'class="b-card"' in html


def test_cards_default_loads_every_game_wide_section():
    """Positive control for the "no hx-get" assertions below: with the
    feature flags on and nothing collapsed/hidden, Cards fires all four
    lazy loads on `load` — proves the *absence* of hx-get elsewhere is
    deliberate deferral, not just the flags being off."""
    html = render_full("custom", killmails_enabled=True, battles_enabled=True)
    for needle in _GAME_WIDE_LOAD_HX_GETS:
        assert needle in html, f"expected {needle} in default Cards render"
    assert _COMBAT_PROFILE_HX_GET in html


def test_compact_renders_one_row_per_pilot():
    html = render_full("name", dash_mode="compact")
    rows = re.findall(r'class="dash-compact-row"', html)
    assert len(rows) == len(CHARACTERS)
    for c in CHARACTERS:
        assert f'data-char-id="{c.character_id}"' in html
        assert f'href="/character/{c.character_id}"' in html


def test_compact_fires_no_hx_get_for_game_wide_sections():
    html = render_full("custom", dash_mode="compact", killmails_enabled=True, battles_enabled=True)
    for needle in _GAME_WIDE_LOAD_HX_GETS:
        assert needle not in html, f"compact mode must not fire {needle}"
    # Deferred, not dropped: the URL is still there for "Show" to wire up.
    assert 'data-dash-src="/dashboard/recent-battles"' in html
    assert 'data-dash-src="/dashboard/activity?window=24h"' in html
    assert 'data-dash-src="/dashboard/kill-pulse"' in html
    assert "game-wide section" in html


def test_compact_starts_with_pilot_sections_collapsed():
    html = render_full("custom", dash_mode="compact", killmails_enabled=True)
    # combat_profile is deferred (not game-wide, but starts collapsed in Compact)
    assert _COMBAT_PROFILE_HX_GET not in html
    assert 'data-dash-src="/dashboard/combat-profile"' in html


def test_cards_mode_never_forces_pilot_sections_collapsed():
    html = render_full("custom", dash_mode="cards", killmails_enabled=True)
    assert _COMBAT_PROFILE_HX_GET in html


def test_a_collapsed_section_has_no_load_trigger_hx_get():
    html = render_full("custom", killmails_enabled=True,
                        prefs_patch={"collapsed_sections": ["combat_profile"]})
    assert _COMBAT_PROFILE_HX_GET not in html
    assert 'data-dash-src="/dashboard/combat-profile"' in html


def test_a_hidden_section_has_no_load_trigger_hx_get():
    html = render_full("custom", killmails_enabled=True,
                        prefs_patch={"hidden_sections": ["combat_profile"]})
    assert _COMBAT_PROFILE_HX_GET not in html
    assert 'data-dash-src="/dashboard/combat-profile"' in html
    # The wrapper itself starts hidden.
    wrap_start = html.index('data-dash-section="combat_profile"')
    div_start = html.rfind("<div", 0, wrap_start)
    div_end = html.index(">", wrap_start)
    assert "display:none;" in html[div_start:div_end]


def test_hidden_section_appears_in_the_footer_with_a_show_link():
    html = render_full("custom", prefs_patch={"hidden_sections": ["wealth"]})
    assert 'data-dash-hidden-item="wealth"' in html
    assert 'data-click="showDashSection" data-section="wealth"' in html
    assert 'id="dash-hidden-footer"' in html
    # The footer itself isn't display:none when something is hidden.
    footer_start = html.index('id="dash-hidden-footer"')
    div_start = html.rfind("<div", 0, footer_start)
    div_end = html.index(">", footer_start)
    assert "display:none;" not in html[div_start:div_end]


def test_collapsed_group_renders_its_summary_line():
    html = render_full("custom", prefs_patch={"collapsed_groups": ["Sample Corp"]})
    assert 'class="dash-group-summary"' in html
    # Visible (no display:none) for the collapsed group, and the summary
    # line reports the right pilot count for this fixture's single group.
    m = re.search(r'<span class="dash-group-summary"[^>]*>(.*?)</span>', html, re.S)
    assert m is not None
    summary_tag = html[m.start():m.start() + 400]
    assert "display:none;" not in summary_tag.split(">", 1)[0]
    assert f"{len(CHARACTERS)} pilots" in re.sub(r"\s+", " ", m.group(1))


def test_expanded_group_hides_its_summary_line():
    html = render_full("custom")  # nothing collapsed by default
    m = re.search(r'<span class="dash-group-summary"[^>]*>', html)
    assert m is not None
    assert "display:none;" in m.group(0)


def test_compact_mode_hides_edit_controls():
    html = render_full("custom", dash_mode="compact")
    assert 'id="edit-mode-btn"' not in html


def test_cards_mode_keeps_edit_controls():
    html = render_full("custom", dash_mode="cards")
    assert 'id="edit-mode-btn"' in html


def test_mode_toggle_present_with_cards_as_default_selection():
    html = render_full("custom")
    assert 'data-mode="compact"' in html
    assert 'data-mode="cards"' in html


def test_compact_default_seeds_empty_persisted_section_arrays():
    """Regression guard: Compact's own collapse defaults (combat_profile /
    recent_kills start collapsed) must never leak into what gets persisted.
    The page seeds its JS state straight from dash_prefs, not from a DOM
    scan — so with nothing actually saved, both arrays render empty even
    though bodies are visually collapsed on this same render."""
    html = render_full("custom", dash_mode="compact", killmails_enabled=True)
    assert "var dashCollapsedSections = [].slice();" in html
    assert "var dashHiddenSections = [].slice();" in html


def test_attention_strip_mount_point_present_in_every_mode():
    for mode in ("cards", "compact"):
        html = render_full("custom", dash_mode=mode)
        assert 'id="dash-attention"' in html
        assert 'hx-get="/dashboard/attention"' in html
        assert 'data-htmx-no-error="1"' in html


def test_paused_card_says_paused_even_when_the_queue_has_a_next_skill():
    """ISS-087: real paused queues carry current_skill (their first entry).
    The shared fixture's paused pilot doesn't, so patch it here rather than
    in the fixture, which would move the golden cards."""
    cid = CHAR_PAUSED.character_id
    skill_map = dict(build_context("custom")["skill_map"])
    skill_map[cid] = dict(skill_map[cid], current_skill="Gunnery", current_level=5)
    card = extract_card(render_full("custom", skill_map=skill_map), cid)
    assert "Paused (3 queued)" in card
    assert "Gunnery" not in card


# ── Mobile R1: phone toolbar (mobile design §5.1) ──────────────────────────

from tests.test_mobile_css import _css, _media_bodies

_SORTS = [("Grouped", "custom"), ("Name", "name"), ("Corp", "corp"),
          ("Training", "training"), ("Queue End", "queue")]
_VIEWS = [("Compact", "compact"), ("Cards", "cards"), ("Detailed", "detailed"), ("Table", "table")]


def _options(html, handler):
    m = re.search(r'<select data-change="' + handler + r'"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"no <select> bound to {handler}"
    return re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', m.group(1))


def test_phone_sort_select_lists_every_sort_url():
    opts = _options(render_full("name"), "dashSortSelect")
    assert [(label, value) for value, _sel, label in opts] == [
        (label, f"/dashboard?sort={value}") for label, value in _SORTS]
    assert [value for value, sel, _label in opts if sel] == ["/dashboard?sort=name"]


def test_phone_view_select_lists_every_mode():
    opts = _options(render_full("custom", dash_mode="table"), "dashViewSelect")
    assert [(label, value) for value, _sel, label in opts] == _VIEWS
    assert [value for value, sel, _label in opts if sel] == ["table"]


def test_phone_select_handlers_share_set_dash_mode():
    html = render_full("custom")
    assert "function setDashMode() { dashSetMode(this.dataset.mode); }" in html
    assert "function dashViewSelect() { dashSetMode(this.value); }" in html
    assert "function dashSortSelect() {" in html
    assert "url.indexOf('/dashboard?sort=') === 0" in html
    assert html.count('class="m-only dash-phone-toolbar"') == 1


def test_desktop_sort_and_view_controls_hide_on_phones():
    html = render_full("custom")
    assert re.findall(r'<a href="/dashboard\?sort=(\w+)" class="b-btn m-hide"', html) == [v for _l, v in _SORTS]
    assert len(re.findall(r'<button type="button" class="b-btn m-hide" data-click="setDashMode"', html)) == 4


def test_secondary_toolbar_controls_stay_on_phones_only_when_they_apply():
    cards = render_full("custom")
    assert 'class="dash-toolbar"' in cards
    assert cards.count('id="edit-mode-btn"') == 1  # never duplicated: JS finds it by id
    assert 'class="dash-toolbar m-hide"' in render_full("custom", dash_mode="compact")
    farm = render_full("name", farm_info={"ready_now": 2})
    assert 'class="dash-toolbar"' in farm
    assert farm.count('href="/tools/skill-farm"') == 1


def test_toolbar_wraps_on_phones():
    phone = _media_bodies(_css(), "max-width: 640px")
    assert re.search(r"\.dash-toolbar\s*\{[^}]*flex-wrap:\s*wrap\s*!important", phone)


def test_tag_filter_hides_edit_and_the_toolbar_row_on_phones():
    html = render_full("custom", active_tag_filter=["x"])
    assert 'class="dash-toolbar m-hide"' in html
    assert 'id="edit-mode-btn"' not in html


def test_phone_selects_reset_to_rendered_selection_on_pageshow():
    html = render_full("custom")
    assert "pageshow" in html
    assert ".dash-phone-toolbar select" in html


# ── Task 9 review: Table view sorts by its own columns on phones ───────────

from app.dashboard import prefs as prefs_mod


def _table_mode(**prefs):
    return render_full("custom", dash_mode="table", TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS,
                       table_rows=[], prefs_patch=prefs or None)


def _reverse_button(html):
    m = re.search(r'<button [^>]*data-click="dashTableSortReverse"[^>]*>([^<]*)</button>', html)
    assert m, "no reverse-sort button"
    return m


def test_table_mode_phone_sort_lists_the_visible_columns():
    html = _table_mode(table_columns=["pilot", "account", "wallet"],
                       table_sort={"key": "wallet", "dir": "desc"})
    opts = _options(html, "dashTableSortSelect")
    assert [(v, label) for v, _sel, label in opts] == [("pilot", "Pilot"), ("account", "Account"), ("wallet", "Wallet")]
    assert [v for v, sel, _label in opts if sel] == ["wallet"]
    # Option values are exactly the header keys a desktop click sorts by.
    assert re.findall(r'<th data-click="sortDashTable" data-col="([^"]+)"', html) == [v for v, _s, _l in opts]
    btn = _reverse_button(html)
    assert 'aria-label="Reverse sort"' in btn.group(0) and "m-tap" in btn.group(0)
    assert "disabled" not in btn.group(0)
    assert btn.group(1) == "▼"
    assert '<select data-change="dashSortSelect"' not in html  # `?sort=` only breaks ties here
    assert html.count('class="m-only dash-phone-toolbar"') == 1


def test_table_mode_phone_sort_names_a_hidden_sort_column():
    html = _table_mode(table_columns=["account", "wallet"], table_sort={"key": "pilot", "dir": "asc"})
    assert '<option value="" disabled selected>Pilot</option>' in html
    assert [v for v, sel, _label in _options(html, "dashTableSortSelect") if sel] == []
    btn = _reverse_button(html)
    assert " disabled" in btn.group(0) and btn.group(1) == "▲"


def test_other_modes_keep_the_url_sort_select():
    for mode in ("cards", "compact", "detailed"):
        html = render_full("name", dash_mode=mode)
        assert [v for v, sel, _label in _options(html, "dashSortSelect") if sel] == ["/dashboard?sort=name"], mode
        assert '<select data-change="dashTableSortSelect"' not in html, mode
        assert not re.search(r'<button [^>]*data-click="dashTableSortReverse"', html), mode


def test_phone_table_sort_runs_the_header_click_path():
    html = _table_mode()
    for fn in ("function dashTableSortSelect() {", "function dashTableSortReverse() {"):
        body = html.split(fn, 1)[1].split("\n}", 1)[0]
        assert "sortDashTable.call(th)" in body, fn
    sort_body = html.split("function sortDashTable() {", 1)[1].split("\n}", 1)[0]
    assert "_dashSyncPhoneTableSort();" in sort_body
    pageshow = html.split("window.addEventListener('pageshow'", 1)[1].split("\n});", 1)[0]
    assert "_dashSyncPhoneTableSort();" in pageshow
