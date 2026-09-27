"""T-070 step 1: the pilot-card markup used to be duplicated between the
grouped (`sort=custom`) and ungrouped loops in dashboard.html. This proves the
refactor into `partials/_pilot_card.html`'s `pilot_card` macro produced no
visual change, for every warning/staleness/reauth state the card renders.

The golden fixture (tests/fixtures/dashboard_pilot_cards_golden.json) was
captured from the pre-refactor template via the same harness
(tests/_dashboard_fixture.py) — see that module's docstring.
"""
import json
from pathlib import Path

from tests._dashboard_fixture import CHARACTERS, all_cards, render_content

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "dashboard_pilot_cards_golden.json"


def _golden() -> dict:
    with open(_GOLDEN_PATH) as f:
        return {int(k): v for k, v in json.load(f).items()}


def test_custom_sort_cards_match_pre_refactor_golden():
    cards = all_cards(render_content("custom"))
    golden = _golden()
    assert set(cards) == set(golden)
    for cid, html in cards.items():
        assert html == golden[cid], f"card for character {cid} changed:\n{html}\n!=\n{golden[cid]}"


def test_name_sort_cards_match_pre_refactor_golden():
    cards = all_cards(render_content("name"))
    golden = _golden()
    assert set(cards) == set(golden)
    for cid, html in cards.items():
        assert html == golden[cid], f"card for character {cid} changed:\n{html}\n!=\n{golden[cid]}"


def test_custom_and_name_sort_render_the_same_card_markup():
    """The two loops share one macro now — prove they still agree with each
    other, not just with the golden."""
    custom = all_cards(render_content("custom"))
    name = all_cards(render_content("name"))
    assert custom == name


def test_golden_fixture_covers_every_fixture_character():
    golden = _golden()
    assert set(golden) == {c.character_id for c in CHARACTERS}
