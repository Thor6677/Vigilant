"""A module's location-scoped rows must not reach the hull's own attributes (ISS-149).

The ship pass selected module modifier rows by domain == "shipID" alone. A
LocationRequiredSkillModifier / LocationGroupModifier / LocationModifier row on
shipID means "the items fitted on this ship" (a Missile Guidance Enhancer
boosting missile velocity), not the ship, but the pass applied it to the hull
whenever the attribute id coincided. Missile velocity and ship speed are both
attribute 37 (maxVelocity), so a Raven with Missile Guidance Enhancers got a
faster hull. pyfa applies those effects to the loaded charge only; only
ItemModifier rows on shipID change the ship.

The SDE slice is the one in tests/test_fitting_engine.py plus invented module
types. Names and ids are invented.
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute,
    SDETypeEffect,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    ATTR_MAX_VELOCITY, OP_POST_PERCENT, SHIP, _Slice, _mid, _seed_rows,
)

BARE_SPEED = 200.0                      # SHIP's maxVelocity in the seed slice
ATTR_VELOCITY_BONUS = 9840              # a module's own "+% velocity" attribute
SKILL_GUIDANCE = 9841
GROUP_TARGET = 9842
EFFECT_ID = 8801
MODULE = 9801


def _rows(func, domain, filter_type, filter_value):
    rows = _seed_rows()
    rows += [
        SDEType(type_id=MODULE, type_name="Test Guidance Module", group_id=9800,
                category_id=7),
        SDETypeDogmaAttribute(type_id=MODULE, attribute_id=ATTR_VELOCITY_BONUS, value=10.0),
        SDEEffect(effect_id=EFFECT_ID, effect_name="testGuidanceBonus", effect_category=0),
        SDETypeEffect(type_id=MODULE, effect_id=EFFECT_ID),
        SDEModifier(effect_id=EFFECT_ID, func=func, domain=domain,
                    modified_attribute_id=ATTR_MAX_VELOCITY,
                    modifying_attribute_id=ATTR_VELOCITY_BONUS,
                    operator=OP_POST_PERCENT, filter_type=filter_type,
                    filter_value=filter_value),
    ]
    return rows


def _speed(items, **row_args):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items)
    return _Slice().run(_calc, rows=_rows(**row_args))["max_velocity"]


@pytest.mark.parametrize("func, filter_type, filter_value", [
    ("LocationRequiredSkillModifier", "skill", SKILL_GUIDANCE),
    ("LocationGroupModifier", "group", GROUP_TARGET),
    ("LocationModifier", None, None),
])
def test_location_rows_leave_the_hull_alone(func, filter_type, filter_value):
    got = _speed([_mid(MODULE)], func=func, domain="shipID",
                 filter_type=filter_type, filter_value=filter_value)
    assert got == pytest.approx(BARE_SPEED, abs=0.05)


def test_copies_of_the_module_do_not_move_the_hull_either():
    got = _speed([_mid(MODULE, quantity=3)], func="LocationRequiredSkillModifier",
                 domain="shipID", filter_type="skill", filter_value=SKILL_GUIDANCE)
    assert got == pytest.approx(BARE_SPEED, abs=0.05)


def test_an_item_modifier_on_the_ship_still_applies():
    # The same module, but a row that really targets the ship: +10% of 200.
    got = _speed([_mid(MODULE)], func="ItemModifier", domain="shipID",
                 filter_type=None, filter_value=None)
    assert got == pytest.approx(BARE_SPEED * 1.10, abs=0.05)
