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


_LAZY = {
    "dashboard-recent-battles": "/dashboard/recent-battles",
    "dashboard-activity": "/dashboard/activity?window=24h",
    "dashboard-kill-pulse": "/dashboard/kill-pulse",
    "dashboard-combat-profile": "/dashboard/combat-profile",
}


def _open_tag(html, needle):
    """The full opening tag containing `needle` (attributes may span lines)."""
    at = html.index(needle)
    return html[html.rfind("<", 0, at):html.index(">", at) + 1]


def test_cards_default_autoloads_every_lazy_section():
    """Positive control for the deferral assertions below (mobile design
    §5.5). The four lazy sections always render deferred (data-dash-src),
    never with hx-trigger="load". In Cards with nothing collapsed or hidden,
    each one carries the data-dash-autoload marker that the page's init
    activates on DOMContentLoaded (desktop). That proves the marker's
    absence elsewhere is deliberate deferral, not the flags being off."""
    html = render_full("custom", killmails_enabled=True, battles_enabled=True)
    for el_id, src in _LAZY.items():
        tag = _open_tag(html, f'id="{el_id}"')
        assert f'data-dash-src="{src}"' in tag, el_id
        assert 'data-dash-autoload="1"' in tag, el_id
    for needle in _GAME_WIDE_LOAD_HX_GETS + (_COMBAT_PROFILE_HX_GET,):
        assert needle not in html, needle


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
    assert 'data-dash-autoload="1"' in _open_tag(html, 'id="dashboard-combat-profile"')
    assert "display:none" not in _open_tag(html, 'data-dash-body="combat_profile"')


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
    """ISS-087: real paused queues carry current_skill (their first entry),
    and so does the shared fixture's paused pilot. The golden cards hold
    because the card checks paused before current_skill."""
    cid = CHAR_PAUSED.character_id
    assert build_context("custom")["skill_map"][cid]["current_skill"] == "Navigation"
    card = extract_card(render_full("custom"), cid)
    assert "Paused (3 queued)" in card
    assert "Navigation" not in card


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


def test_remove_is_hidden_on_phones_in_cards_and_detailed():
    for mode in ("cards", "detailed"):
        html = render_full("custom", dash_mode=mode)
        forms = re.findall(r'<form method="POST" action="/auth/remove/\d+"([^>]*)>', html)
        assert len(forms) == len(CHARACTERS), mode
        assert all(f.startswith(' class="m-hide" data-confirm=') for f in forms), mode


# ── Mobile R1: heavy sections and titles (mobile design §5.5 / §5.6) ──────

import os

_PARTIALS = os.path.join(os.path.dirname(__file__), "..", "app", "templates", "partials")


def _partial_source(name):
    with open(os.path.join(_PARTIALS, name), encoding="utf-8") as fh:
        return fh.read()


def test_compact_and_collapsed_sections_get_no_autoload_marker():
    html = render_full("custom", dash_mode="compact", killmails_enabled=True, battles_enabled=True)
    for el_id in _LAZY:
        assert "data-dash-autoload" not in _open_tag(html, f'id="{el_id}"'), el_id
    html = render_full("custom", killmails_enabled=True,
                       prefs_patch={"collapsed_sections": ["activity"]})
    assert "data-dash-autoload" not in _open_tag(html, 'id="dashboard-activity"')
    assert 'data-dash-autoload="1"' in _open_tag(html, 'id="dashboard-kill-pulse"')


def test_phone_init_uses_a_per_device_open_set():
    html = render_full("custom", killmails_enabled=True, battles_enabled=True)
    assert "var DASH_PHONE_OPEN_KEY = 'vigilant.dash.phoneOpen';" in html
    assert ("var DASH_PHONE_COLLAPSED = ['wealth', 'battles', 'activity', "
            "'kill_pulse', 'combat_profile'];") in html
    assert "document.querySelectorAll('[data-dash-autoload]')" in html
    read = html.split("function dashPhoneOpenRead()")[1].split("function dashPhoneOpenWrite(")[0]
    write = html.split("function dashPhoneOpenWrite(")[1].split("\nfunction ")[0]
    assert "try {" in read and "catch (e)" in read
    assert "try {" in write and "catch (e)" in write
    toggle = html.split("function toggleDashSection()")[1].split("function hideDashSection()")[0]
    guard = "if (dashIsPhone() && DASH_PHONE_COLLAPSED.indexOf(key) !== -1)"
    assert guard in toggle
    phone = toggle.index(guard)
    assert toggle.index("dashPhoneOpenWrite(", phone) < toggle.index("return;", phone) \
        < toggle.index("persistSectionState()")


def _section_toggles(html):
    return re.findall(r'<button type="button" class="dash-sec-toggle b-btn m-tap"[^>]*'
                      r'data-section="([a-z_]+)"\s+title="([^"]*)" aria-expanded="(true|false)"[^>]*>([^<]*)</button>', html)


def test_section_toggles_render_title_and_aria_expanded_for_their_state():
    """Compact force-collapses combat_profile; a saved collapse closes
    wealth. Every toggle's glyph, title and aria-expanded agree."""
    html = render_full("custom", dash_mode="compact", killmails_enabled=True, battles_enabled=True,
                       prefs_patch={"collapsed_sections": ["wealth"]})
    toggles = {key: (title, aria, glyph) for key, title, aria, glyph in _section_toggles(html)}
    assert {"wealth", "battles", "activity", "kill_pulse", "combat_profile"} <= set(toggles)
    for key, (title, aria, glyph) in toggles.items():
        if key in ("wealth", "combat_profile"):
            assert (title.split(" ", 1)[0], aria, glyph) == ("Expand", "false", "▸"), key
        else:
            assert (title.split(" ", 1)[0], aria, glyph) == ("Collapse", "true", "▾"), key
    assert toggles["wealth"][0] == "Expand Wealth by Character"


def test_section_toggle_state_is_set_in_one_place_by_both_paths():
    """The phone init collapses heavy sections after load, and every toggle
    flips one: both must update the title and aria-expanded, not only the
    glyph."""
    html = render_full("custom", killmails_enabled=True, battles_enabled=True)
    helper = html.split("function dashSetSectionToggle(btn, expanded) {", 1)[1].split("\n}", 1)[0]
    assert "DASH_SECTION_LABELS[key]" in helper
    assert "btn.textContent = expanded ? '▾' : '▸';" in helper
    assert "btn.title = (expanded ? 'Collapse ' : 'Expand ') + label;" in helper
    assert "btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');" in helper
    toggle = html.split("function toggleDashSection()")[1].split("function hideDashSection()")[0]
    # Before the phone branch, which returns early.
    assert toggle.index("dashSetSectionToggle(this, willExpand);") \
        < toggle.index("if (dashIsPhone() && DASH_PHONE_COLLAPSED")
    init = html.split("function dashSectionsInit() {")[1].split("\n}", 1)[0]
    assert "if (btn) dashSetSectionToggle(btn, false);" in init
    for body in (toggle, init):
        assert ".textContent = " not in body


def test_each_lazy_section_title_appears_once():
    html = render_full("custom", killmails_enabled=True, battles_enabled=True)
    for title in ("Major Fleet Battles", "Activity", "Pilot Pulse", "Combat Profile"):
        assert html.count(f">{title}<") == 1, (title, html.count(f">{title}<"))
    assert "Major Fleet Battles in New Eden" not in html
    assert "Your Pilots Combat Profile" not in html
    for meta in ("New Eden &middot; last 7 days",
                 "30d &middot; all your characters &middot; stored killmails",
                 "your pilots &middot; 90d"):
        assert meta in html, meta


def test_lazy_partials_drop_their_title_lines_but_keep_data_meta():
    assert "Major Fleet Battles" not in _partial_source("dashboard_recent_battles.html")
    activity = _partial_source("dashboard_activity.html")
    assert "Activity · New Eden" not in activity
    assert "peak {{ fmt_pcu(peak_pcu) }} online" in activity
    assert "{% for w, label in [('1h','1H')" in activity  # window buttons stay
    pulse = _partial_source("dashboard_kill_pulse.html")
    assert "Pilot Pulse · {{ days }}d" not in pulse
    assert "all your characters · stored killmails" not in pulse
    profile = _partial_source("dashboard_combat_profile.html")
    assert "Your Pilots Combat Profile" not in profile
    assert ">Combat Profile<" not in profile
    assert ">Combat Radar<" in profile
    assert "{{ char_count }} char" in profile


# ── Mobile R1: tap targets (mobile design §5.7) ────────────────────────────

def test_section_buttons_use_the_tap_class_not_inline_sizing():
    html = render_full("custom")
    btns = re.findall(r'<button type="button" class="(dash-sec-(?:toggle|hide) b-btn m-tap)"[^>]*style="([^"]*)"', html)
    assert len(btns) >= 2
    for cls, style in btns:
        assert "padding" not in style and "font-size" not in style, (cls, style)
        assert "border:1px solid var(--border)" in style, (cls, style)


def test_section_button_desktop_size_lives_in_site_css():
    css = _css()
    assert re.search(r"\.b-btn\.dash-sec-toggle,\s*\.b-btn\.dash-sec-hide\s*\{\s*padding:\s*2px 6px;\s*font-size:\s*9px;\s*\}", css)
    phone = _media_bodies(css, "max-width: 640px")
    assert not re.search(r"\.b-btn\.dash-sec-toggle", phone)  # desktop rule, not phone-only
    assert re.search(r"\.dash-sec-toggle \+ \.dash-sec-hide\s*\{[^}]*margin-left:\s*4px", phone)
