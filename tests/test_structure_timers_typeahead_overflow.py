"""The Structure Timers search lists must not be clipped by their panel on desktop (ISS-147).

The System / Region / Owner and ACL result lists hang below their input
(position:absolute, max-height 200px) inside a .b-panel, which is
overflow:hidden. The two panels that hold them carry `tmr-ta-panel`, and a
desktop-only rule (min-width: 641px) lets them overflow, as for the Skill Plan
lists (ISS-114). Phones are untouched. A click-away / Escape handler closes
every list and cancels its pending search.

Static source pins: the template is read as text."""
import os
import re

from tests._mobile import SITE_CSS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_ROOT, "app", "templates", "structure_timers.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _css():
    return re.sub(r"/\*.*?\*/", "", _read(SITE_CSS), flags=re.S)


def test_both_search_panels_carry_the_hook():
    html = _read(_TEMPLATE)
    hooked = re.findall(r'<div class="b-panel tmr-ta-panel"[^>]*>', html)
    assert len(hooked) == 2
    # the panel that opens each result list is one of the hooked ones
    first_panel = html.index('class="b-panel tmr-ta-panel"')
    assert first_panel < html.index('id="system-results"') < html.index('class="b-panel tmr-ta-panel"', first_panel + 1)
    assert html.index('class="b-panel tmr-ta-panel"', first_panel + 1) < html.index('id="acl-results-')


def test_hook_overflows_only_on_desktop():
    css = _css()
    desktop = re.search(r"@media\s*\(min-width:\s*641px\)\s*\{\s*\.b-panel\.tmr-ta-panel\s*\{\s*overflow:\s*visible;\s*\}\s*\}", css)
    assert desktop, "desktop rule missing"
    assert len(re.findall(r"tmr-ta-panel", css)) == 1, "the hook must appear only inside the desktop block"


def test_click_away_and_escape_cover_every_list():
    js = _read(_TEMPLATE)
    assert "function closeResultLists(e)" in js
    assert "document.addEventListener('click', closeResultLists)" in js
    assert "e.key === 'Escape'" in js
    for name in ("'system'", "'region'", "'owner'", '[id^="acl-results-"]'):
        assert name in js


def test_late_search_responses_cannot_reopen_a_closed_list():
    js = _read(_TEMPLATE)
    assert "if (seq !== _searchSeq[field]) return;" in js
    assert "if (seq !== _aclSeq[groupId]) return;" in js
    # closing bumps the same counters the fetch callbacks compare against
    assert "_searchSeq[f] = (_searchSeq[f] || 0) + 1" in js
    assert "_aclSeq[gid] = (_aclSeq[gid] || 0) + 1" in js


def test_closing_cancels_a_search_even_before_its_list_is_open():
    js = _read(_TEMPLATE)
    body = js[js.index("function closeResultLists(e)"):js.index("document.addEventListener('click', closeResultLists)")]
    assert "display === 'none'" not in body, "an in-flight search must be cancelled while its list is still hidden"
