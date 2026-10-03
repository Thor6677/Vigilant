"""Mobile R1 foundation (mobile design §4): the m-row helper and JS, the
tab dropdown macro, the hamburger menu groups, the search palette."""
import os
import re

import pytest

from tests._mobile import assert_mrow, mrows

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_ACTIONS_JS = os.path.join(_ROOT, "static", "js", "actions.js")


def _js() -> str:
    with open(_ACTIONS_JS, encoding="utf-8") as fh:
        return fh.read()


# ── assert_mrow helper ────────────────────────────────────────────────

GOOD = ('<div class="m-row" data-click="toggleMRow">'
        '<img data-m="lead" src="x"><span data-m="key">Name</span>'
        '<span data-m-label="Planet">Gas</span><span data-m="key">3h</span></div>')


def test_helper_accepts_a_valid_row():
    rows = assert_mrow(GOOD)
    assert len(rows) == 1 and len(rows[0]["children"]) == 4


def test_helper_ignores_grandchildren():
    html = ('<div class="m-row" data-click="toggleMRow"><span data-m="key">'
            '<b data-m="key">nested</b></span></div>')
    assert_mrow(html)


@pytest.mark.parametrize("bad, msg", [
    ('<div class="m-row" data-click="toggleMRow"><span>x</span></div>', "1–2 data-m=key"),
    ('<div class="m-row" data-click="toggleMRow"><i data-m="key"></i><i data-m="key"></i><i data-m="key"></i></div>', "1–2 data-m=key"),
    ('<div class="m-row" data-click="toggleMRow"><i data-m="key"></i><i data-m-label=" "></i></div>', "empty data-m-label"),
    ('<div class="m-row"><i data-m="key"></i></div>', "need data-click"),
    ('<a class="m-row m-row--link" data-click="toggleMRow"><i data-m="key"></i></a>', "link rows must not toggle"),
    ('<div class="m-row" data-click="toggleMRow"><i data-m="key" data-m-label="X"></i></div>', "can't be both"),
])
def test_helper_rejects_contract_violations(bad, msg):
    with pytest.raises(AssertionError, match=msg):
        assert_mrow(bad)


def test_helper_accepts_existing_expander_rows():
    html = ('<div class="m-row" data-click="toggleExpanded" data-toggle-target=".pi-row">'
            '<span data-m="key">a</span></div>')
    assert_mrow(html)


def test_mrows_finds_table_rows():
    html = ('<table class="m-table"><tbody><tr class="m-row" data-click="toggleMRow">'
            '<td data-m="key">a</td><td data-m-label="B">b</td></tr></tbody></table>')
    assert [r["tag"] for r in mrows(html)] == ["tr"]


# ── actions.js ────────────────────────────────────────────────────────

def test_actions_js_defines_mrow_handlers():
    js = _js()
    assert "window.toggleMRow" in js
    assert "window.mRowInit" in js
    assert "'(max-width: 640px)'" in js
    assert "htmx:afterSwap" in js


def test_toggle_mrow_ignores_taps_on_controls():
    js = _js()
    assert "a, button, input, select, textarea, label, summary" in js


def test_keydown_never_handles_link_rows_or_modified_keys():
    js = _js()
    region = js[js.index("addEventListener('keydown'"):]
    assert "classList.contains('m-row--link')" in region
    assert "e.metaKey" in region and "e.repeat" in region


def test_aria_is_resynced_after_any_row_click():
    js = _js()
    assert "function mSyncAria" in js
    assert "htmx:afterSettle" in js


# ── tab_nav / character_tabs ─────────────────────────────────────────

from jinja2 import Environment, FileSystemLoader

_TEMPLATES = os.path.join(_ROOT, "app", "templates")
_CHAR_TAB_PAGES = {
    "character_detail.html": "overview",
    "skills.html": "skills",
    "fittings.html": "fittings",
    "blueprints.html": "blueprints",
    "journal.html": "journal",
    "mining.html": "mining",
}


def _env():
    return Environment(loader=FileSystemLoader(_TEMPLATES))


def _render_character_tabs(active):
    tmpl = _env().from_string(
        '{% from "partials/_character_tabs.html" import character_tabs %}'
        '{{ character_tabs(cid, active) }}')
    return tmpl.render(cid=90000001, active=active)


@pytest.mark.parametrize("active, label", [
    ("overview", "Overview"), ("skills", "Skills"), ("fittings", "Fittings"),
    ("blueprints", "Blueprints"), ("journal", "Journal"), ("mining", "Mining")])
def test_character_tabs_mark_the_active_tab_in_both_forms(active, label):
    html = _render_character_tabs(active)
    strip, details = html.split('<details class="m-tabs">')
    assert strip.count('class="is-active"') == 1
    assert re.search(rf'class="is-active" aria-current="page">{label}<', strip)
    assert re.search(rf'<span class="m-tabs-current">{label}</span>', details)
    assert details.count('class="is-active"') == 1


def test_character_tabs_carry_all_links_and_extras():
    html = _render_character_tabs("overview")
    strip, details = html.split('<details class="m-tabs">')
    for part in (strip, details):
        for path in ("/character/90000001", "/character/90000001/skills",
                     "/character/90000001/fittings", "/character/90000001/blueprints",
                     "/character/90000001/journal", "/character/90000001/mining",
                     "/intel/entity/character/90000001",
                     "https://zkillboard.com/character/90000001/"):
            assert f'href="{path}"' in part, (path, part[:80])
    assert 'class="b-tab-extra"' in strip
    assert 'target="_blank" rel="noopener"' in details


@pytest.mark.parametrize("page", sorted(_CHAR_TAB_PAGES))
def test_character_pages_use_the_shared_macro(page):
    with open(os.path.join(_TEMPLATES, page), encoding="utf-8") as fh:
        src = fh.read()
    assert "character_tabs(" in src, page
    assert 'class="b-tab-strip"' not in src, page
    assert f"character_tabs(_cid, '{_CHAR_TAB_PAGES[page]}')" in src, page


# ── hamburger menu groups ────────────────────────────────────────────

import types

from app.nav import NAV_GROUPS, item_active, group_active


def _render_base(path="/industry/planetary", is_admin=True):
    env = _env()
    env.globals.update(nav_groups=NAV_GROUPS, nav_item_active=item_active,
                       nav_group_active=group_active)
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path=path),
        state=types.SimpleNamespace(csp_nonce="test-nonce"),
        session={"user_id": 1, "is_admin": is_admin,
                 "active_character_id": 90000001, "csrf_token": "t"},
    )
    return env.get_template("base.html").render(request=request, css_v="1", js_v="1")


def _mobile_menu(html):
    return html.split('id="mobile-menu"')[1].split('<div id="esi-banner"')[0]


def test_menu_groups_are_details_with_only_the_active_one_open():
    menu = _mobile_menu(_render_base("/industry/planetary"))
    groups = re.findall(r'<details class="b-mobile-group"( open)?>\s*<summary>([^<]+)</summary>', menu)
    with_items = [g["label"] for g in NAV_GROUPS if g["items"] and (not g["admin"])]
    labels = [label.strip() for _, label in groups]
    for label in with_items:
        assert label in labels, (label, labels)
    open_labels = [label.strip() for is_open, label in groups if is_open]
    assert open_labels == ["Industry"], open_labels


def test_menu_still_links_every_item():
    menu = _mobile_menu(_render_base("/dashboard"))
    for g in NAV_GROUPS:
        if g["admin"]:
            continue
        for item in g["items"]:
            assert f'href="{item["url"]}"' in menu, item["url"]


# ── search palette ───────────────────────────────────────────────────

def test_palette_is_centred_closable_and_hides_key_hints_on_touch():
    html = _render_base("/dashboard")
    assert re.search(r"#cmdk\s*\{[^}]*margin:\s*auto", html)
    assert 'class="cmdk-close" data-click="closePalette"' in html
    assert "window.closePalette" in html
    assert re.search(r"@media \(hover: none\), \(max-width: 640px\)\s*\{\s*#cmdk \.cmdk-hint\s*\{\s*display:\s*none", html)


def test_palette_enter_only_acts_from_the_input():
    html = _render_base("/dashboard")
    assert "if (e.target !== $('cmdk-input')) return;" in html
    assert ".cmdk-close:focus-visible" in html
    assert re.search(r"@media \(max-width: 640px\) \{ #cmdk \{ margin: 0\.75rem auto auto; \} \}", html)
    assert html.index("margin:auto") < html.index("margin: 0.75rem auto auto")


# ── audit script ─────────────────────────────────────────────────────

def test_mobile_audit_script_exists_and_restores_width():
    path = os.path.join(_ROOT, "scripts", "mobile-audit.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "360" in src and "scrollWidth" in src
    assert "h.style.width = prevWidth" in src
    assert "'OK'" in src
    assert "forced ? document.body.scrollWidth : h.scrollWidth" in src
    assert "verdict" in src
