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
