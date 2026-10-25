"""T-082: the battle loot page on phones follows the R1-R6 row contract.

Wreck rows are expandable m-rows (key 1 = ship plus victim pilot, key 2 =
dropped value); the group totals are link m-rows. The page holds no phone rule
in site.css: its small phone block lives in the template's own <style>. The
fixtures come from test_battle_loot_route (invented ids and names)."""
import re

from tests._mobile import assert_mrow, cells_rows, assert_single_value_child, row_keys, row_labelled
from tests.test_battle_loot_route import STAMP, SYS_A, W, _get, _page, env  # noqa: F401


def test_wreck_and_group_rows_meet_the_mrow_contract(env):
    html = _page().text
    rows = assert_mrow(html, min_rows=3)
    wreck = [r for r in rows if r["attrs"].get("data-click") == "toggleMRow"]
    groups = [r for r in rows if "m-row--link" in r["attrs"].get("class", "").split()]
    assert len(wreck) == 2 and len(groups) == 2
    for r in cells_rows(html):
        assert_single_value_child(r)


def test_a_wreck_row_shows_ship_and_pilot_on_one_line_and_detail_when_open(env):
    html = _page().text
    wreck = next(r for r in cells_rows(html) if r["attrs"].get("data-click") == "toggleMRow")
    assert len(row_keys(wreck)) == 2
    key1 = next(c for c in wreck["cells"] if c["attrs"].get("data-m") == "key")
    assert "Test Frigate" in key1["text"] and "Pilot One" in key1["text"]
    assert set(row_labelled(wreck)) >= {"Pilot", "Top drops", "Destroyed"}


def test_the_detail_container_is_a_sibling_not_a_row_child(env):
    html = _page().text
    assert re.search(r'</div>\s*<div id="bl-d-1" class="bl-detail"></div>', html)


def test_page_has_no_inline_handlers_and_no_script_outside_content(env):
    html = _get(f"/intel/loot/related/{SYS_A}/{STAMP}").text
    assert not re.search(r"\son(click|change|submit|input|load)\s*=", html)
    page_src = open("app/templates/battle_loot.html").read()
    assert "<script" not in page_src                     # htmx attributes only


def test_the_price_status_line_wraps_on_phones():
    src = open("app/templates/battle_loot.html").read()
    assert re.search(r"\.bl-status\s*\{[^}]*overflow-wrap:\s*anywhere", src)
    assert "@media (max-width: 640px)" in src
