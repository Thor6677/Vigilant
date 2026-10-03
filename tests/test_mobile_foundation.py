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
