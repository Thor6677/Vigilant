"""Flat adds land before percentage bonuses on the ship's own attributes.

ISS-043. Dogma applies every modifier to an attribute in operator order —
preAssign, preMul, preDiv, modAdd, modSub, postMul, postDiv, postPercent,
postAssign — whatever the source. A Large Shield Extender II's +2600
(modAdd) therefore lands before Shield Management's +25% (postPercent):
(5500 + 2600) x 1.25 = 10125 on a Drake, not 5500 x 1.25 + 2600 = 9475.

The engine used to apply skill, implant, booster, hull and subsystem
percentages straight to the ship's attributes before the fitted modules'
rows were collected, so every flat add came after them. Now those sources
are collected and applied in the same operator-ordered pass as the modules,
unpenalized — see calculate_fitting_stats() Step 2.

Built on the SDE slice in tests/test_fitting_engine.py plus a shield
extender, an armor plate, a cap battery, a cap implant, a booster with a
capacitor side effect, and a per-level hull trait on shield HP (as on the
Crow, Flycatcher or Vulture). Each test fails on the old ordering.
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute,
    SDETypeEffect,
)
from app.fitting.constants import (
    ATTR_ARMOR_HP, ATTR_CAPACITOR, ATTR_CPU, ATTR_POWER, ATTR_SHIELD_HP,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    OP_MOD_ADD, OP_POST_PERCENT, SHIP, SHIP_ARMOR_HP, _Slice, _attrs, _low,
    _mid, _seed_rows,
)

SHIP_SHIELD_HP = 1200.0     # as seeded by tests/test_fitting_engine.py
SHIP_CAPACITOR = 1000.0

SKILL_SHIELD_MANAGEMENT = 3419
SKILL_HULL_UPGRADES = 3394
CATEGORY_IMPLANT = 20
GROUP_BOOSTER = 303
ATTR_BOOSTERNESS = 1087
ATTR_CHANCE_2 = 1090
ATTR_CAP_CAPACITY_PENALTY = 1150

# fixture-local
SHIELD_EXTENDER = 9701
ARMOR_PLATE = 9702
CAP_BATTERY = 9703
CAP_IMPLANT = 9704
CAP_BOOSTER = 9705
ATTR_SHIELD_ADD = 9710
ATTR_ARMOR_ADD = 9711
ATTR_CAP_ADD = 9712
ATTR_CAP_IMPLANT_BONUS = 9713
ATTR_HULL_SHIELD_BONUS = 9714      # named shipBonus*, so per level
EFF_SHIELD_EXTENDER = 8701
EFF_ARMOR_PLATE = 8702
EFF_CAP_BATTERY = 8703
EFF_CAP_IMPLANT = 8704
EFF_CAP_SIDE_EFFECT = 8705
EFF_HULL_SHIELD = 8706

EXTENDER_HP = 2600.0
PLATE_HP = 1600.0
BATTERY_GJ = 1000.0
IMPLANT_CAP_PCT = 10.0
BOOSTER_CAP_PCT = -30.0
HULL_SHIELD_PCT_PER_LEVEL = 5.0
FITTING_SKILL_V = 1.25


def _ship_self(type_id, effect_id, name, target, source, operator, category=0):
    return [
        SDEEffect(effect_id=effect_id, effect_name=name, effect_category=category),
        SDETypeEffect(type_id=type_id, effect_id=effect_id),
        SDEModifier(effect_id=effect_id, func="ItemModifier", domain="shipID",
                    modified_attribute_id=target, modifying_attribute_id=source,
                    operator=operator),
    ]


def _rows(hull_trait=False):
    rows = _seed_rows()
    rows += [
        SDEDogmaAttribute(attribute_id=ATTR_HULL_SHIELD_BONUS,
                          attribute_name="shipBonusTestShield1", stackable=True),
        SDEType(type_id=SHIELD_EXTENDER, type_name="Test Large Shield Extender",
                group_id=9420, category_id=7),
        SDEType(type_id=ARMOR_PLATE, type_name="Test 1600mm Plate",
                group_id=9421, category_id=7),
        SDEType(type_id=CAP_BATTERY, type_name="Test Large Cap Battery",
                group_id=9422, category_id=7),
        SDEType(type_id=CAP_IMPLANT, type_name="Test Cap Capacity Implant",
                group_id=301, category_id=CATEGORY_IMPLANT),
        SDEType(type_id=CAP_BOOSTER, type_name="Test Booster With Cap Penalty",
                group_id=GROUP_BOOSTER, category_id=CATEGORY_IMPLANT),
    ]
    rows += _attrs(SHIELD_EXTENDER, {ATTR_SHIELD_ADD: EXTENDER_HP, ATTR_CPU: 30.0, ATTR_POWER: 50.0})
    rows += _attrs(ARMOR_PLATE, {ATTR_ARMOR_ADD: PLATE_HP, ATTR_CPU: 10.0, ATTR_POWER: 200.0})
    rows += _attrs(CAP_BATTERY, {ATTR_CAP_ADD: BATTERY_GJ, ATTR_CPU: 20.0, ATTR_POWER: 100.0})
    rows += _attrs(CAP_IMPLANT, {ATTR_CAP_IMPLANT_BONUS: IMPLANT_CAP_PCT})
    rows += _attrs(CAP_BOOSTER, {
        ATTR_BOOSTERNESS: 1.0,
        ATTR_CAP_CAPACITY_PENALTY: BOOSTER_CAP_PCT,
        ATTR_CHANCE_2: 0.4,
    })
    rows += _ship_self(SHIELD_EXTENDER, EFF_SHIELD_EXTENDER, "shieldCapacityBonus",
                       ATTR_SHIELD_HP, ATTR_SHIELD_ADD, OP_MOD_ADD)
    rows += _ship_self(ARMOR_PLATE, EFF_ARMOR_PLATE, "armorHPBonusAdd",
                       ATTR_ARMOR_HP, ATTR_ARMOR_ADD, OP_MOD_ADD)
    rows += _ship_self(CAP_BATTERY, EFF_CAP_BATTERY, "capacitorCapacityBonus",
                       ATTR_CAPACITOR, ATTR_CAP_ADD, OP_MOD_ADD)
    rows += _ship_self(CAP_IMPLANT, EFF_CAP_IMPLANT, "capacitorCapacityImplant",
                       ATTR_CAPACITOR, ATTR_CAP_IMPLANT_BONUS, OP_POST_PERCENT)
    rows += _ship_self(CAP_BOOSTER, EFF_CAP_SIDE_EFFECT, "boosterCapacitorCapacityPenalty",
                       ATTR_CAPACITOR, ATTR_CAP_CAPACITY_PENALTY, OP_POST_PERCENT)
    # Mark the booster's effect as a side effect, so it only applies on request.
    for row in rows:
        if isinstance(row, SDEEffect) and row.effect_id == EFF_CAP_SIDE_EFFECT:
            row.fitting_usage_chance_attribute_id = ATTR_CHANCE_2
    if hull_trait:
        rows += _attrs(SHIP, {ATTR_HULL_SHIELD_BONUS: HULL_SHIELD_PCT_PER_LEVEL})
        rows += _ship_self(SHIP, EFF_HULL_SHIELD, "shipShieldCapacityBonus",
                           ATTR_SHIELD_HP, ATTR_HULL_SHIELD_BONUS, OP_POST_PERCENT)
    return rows


def _stats(items, hull_trait=False, **kwargs):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items, **kwargs)
    return _Slice().run(_calc, rows=_rows(hull_trait=hull_trait))


def test_shield_extender_adds_before_shield_management():
    stats = _stats([_mid(SHIELD_EXTENDER)], skill_levels={SKILL_SHIELD_MANAGEMENT: 5})
    assert stats["shield_hp"] == pytest.approx((SHIP_SHIELD_HP + EXTENDER_HP) * FITTING_SKILL_V, abs=0.5)


def test_armor_plate_adds_before_hull_upgrades():
    stats = _stats([_low(ARMOR_PLATE)], skill_levels={SKILL_HULL_UPGRADES: 5})
    assert stats["armor_hp"] == pytest.approx((SHIP_ARMOR_HP + PLATE_HP) * FITTING_SKILL_V, abs=0.5)


def test_cap_battery_adds_before_booster_side_effect_and_implant():
    booster = [{"type_id": CAP_BOOSTER, "side_effects": [EFF_CAP_SIDE_EFFECT]}]
    with_booster = _stats([_mid(CAP_BATTERY)], skill_levels={}, boosters=booster)
    assert with_booster["capacitor"] == pytest.approx(
        (SHIP_CAPACITOR + BATTERY_GJ) * (1 + BOOSTER_CAP_PCT / 100), abs=0.5)

    with_implant = _stats([_mid(CAP_BATTERY)], skill_levels={}, implants=[CAP_IMPLANT])
    assert with_implant["capacitor"] == pytest.approx(
        (SHIP_CAPACITOR + BATTERY_GJ) * (1 + IMPLANT_CAP_PCT / 100), abs=0.5)


def test_hull_shield_trait_multiplies_after_the_extender():
    """A per-level hull bonus (5% shield HP per level, All V) and Shield
    Management V both scale the extended pool, not the bare hull."""
    stats = _stats([_mid(SHIELD_EXTENDER)], hull_trait=True,
                   skill_levels={SKILL_SHIELD_MANAGEMENT: 5})
    expected = (SHIP_SHIELD_HP + EXTENDER_HP) * FITTING_SKILL_V * (1 + 5 * HULL_SHIELD_PCT_PER_LEVEL / 100)
    assert stats["shield_hp"] == pytest.approx(expected, abs=0.5)


def test_module_percentages_still_stack_with_penalties_after_the_add():
    """Two extenders' adds sum, then the skill's +25% applies once — the
    unpenalized sources never enter the modules' stacking chain and never
    change the sum of flat adds."""
    stats = _stats([_mid(SHIELD_EXTENDER, quantity=2)],
                   skill_levels={SKILL_SHIELD_MANAGEMENT: 5})
    assert stats["shield_hp"] == pytest.approx((SHIP_SHIELD_HP + 2 * EXTENDER_HP) * FITTING_SKILL_V, abs=0.5)
