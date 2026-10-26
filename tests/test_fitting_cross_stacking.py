"""Cross-module bonuses stack by (attribute, operator), not by source type.

Cross-module multipliers (a damage amplifier boosting drones, a gyrostabilizer
boosting turrets) used to be stacking-penalised per SOURCE TYPE. A tech 2 and a
faction version of the same module are different types sharing one effect, so
each landed in a group of one and neither was penalised against the other. Two
modules that modify the same attribute with the same operator now penalise each
other whichever type they come from; a different operator stays a separate
chain.

The SDE slice is the one in tests/test_fitting_engine.py plus invented drone
and module types. Attribute 64 (damageMultiplier) is not declared by that
slice, and an undeclared attribute reads as stackable, which would skip every
penalty here, so it is declared non-stackable below.
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute,
    SDETypeEffect, SDETypeSkillReq,
)
from app.fitting.constants import (
    ATTR_DAMAGE_MULTIPLIER, ATTR_EM_DAMAGE, ATTR_RATE_OF_FIRE,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    AMMO, OP_POST_PERCENT, SHIP, TURRET, _Slice, _high, _low, _seed_rows,
)

OP_POST_MUL = 4
CATEGORY_DRONE = 18
SKILL_DRONES = 9436
GROUP_TURRET = 9403                     # the seed slice's turret group

DRONE = 9701
DDA_T2 = 9702
DDA_NAVY = 9703
GYRO_T2 = 9704
GYRO_FACTION = 9705
PERCENT_MOD = 9706

EFF_DDA = 8701
EFF_GYRO = 8702
EFF_PERCENT_MOD = 8703

ATTR_DDA_BONUS = 9831
ATTR_PERCENT_BONUS = 9832
GROUP_DDA = 9710
GROUP_GYRO = 9711
GROUP_PERCENT_MOD = 9712

# Stacking-penalty weights by position (1st, 2nd, 3rd, 4th strongest bonus);
# test_penalty_table_matches_the_engine checks them against the engine.
PEN = [1.0, 0.86912, 0.570583, 0.282955]


def _module(type_id, name, group, attrs):
    return [SDEType(type_id=type_id, type_name=name, group_id=group, category_id=7)] + [
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=a, value=v)
        for a, v in attrs.items()
    ]


def _effect(effect_id, name, type_ids, func, domain, target, source, operator,
            filter_type, filter_value):
    return [
        SDEEffect(effect_id=effect_id, effect_name=name, effect_category=0),
        *[SDETypeEffect(type_id=t, effect_id=effect_id) for t in type_ids],
        SDEModifier(effect_id=effect_id, func=func, domain=domain,
                    modified_attribute_id=target, modifying_attribute_id=source,
                    operator=operator, filter_type=filter_type,
                    filter_value=filter_value),
    ]


def _rows():
    rows = _seed_rows()
    rows += [
        SDEDogmaAttribute(attribute_id=ATTR_DAMAGE_MULTIPLIER,
                          attribute_name="damageMultiplier", stackable=False),
        SDEDogmaAttribute(attribute_id=ATTR_RATE_OF_FIRE,
                          attribute_name="speed", stackable=False),
    ]
    # A drone with no bandwidth cost, so every copy is active:
    # 100 EM x 1.0 / 1 s = 100 DPS before any amplifier.
    rows += [
        SDEType(type_id=DRONE, type_name="Test Drone", group_id=9700,
                category_id=CATEGORY_DRONE),
        SDETypeDogmaAttribute(type_id=DRONE, attribute_id=ATTR_EM_DAMAGE, value=100.0),
        SDETypeDogmaAttribute(type_id=DRONE, attribute_id=ATTR_DAMAGE_MULTIPLIER, value=1.0),
        SDETypeDogmaAttribute(type_id=DRONE, attribute_id=ATTR_RATE_OF_FIRE, value=1000.0),
        SDETypeSkillReq(type_id=DRONE, skill_type_id=SKILL_DRONES, required_level=1),
    ]
    # Two drone damage amplifiers: different types, one effect, operator 6.
    rows += _module(DDA_T2, "Test Drone Amplifier T2", GROUP_DDA, {ATTR_DDA_BONUS: 20.0})
    rows += _module(DDA_NAVY, "Test Drone Amplifier Navy", GROUP_DDA, {ATTR_DDA_BONUS: 25.0})
    rows += _effect(EFF_DDA, "testDroneAmp", [DDA_T2, DDA_NAVY],
                    "OwnerRequiredSkillModifier", "charID", ATTR_DAMAGE_MULTIPLIER,
                    ATTR_DDA_BONUS, OP_POST_PERCENT, "skill", SKILL_DRONES)
    # Two turret damage mods: different types, one effect, operator 4.
    rows += _module(GYRO_T2, "Test Gyro T2", GROUP_GYRO, {ATTR_DAMAGE_MULTIPLIER: 1.10})
    rows += _module(GYRO_FACTION, "Test Gyro Faction", GROUP_GYRO,
                    {ATTR_DAMAGE_MULTIPLIER: 1.12})
    rows += _effect(EFF_GYRO, "testGyro", [GYRO_T2, GYRO_FACTION],
                    "LocationGroupModifier", "shipID", ATTR_DAMAGE_MULTIPLIER,
                    ATTR_DAMAGE_MULTIPLIER, OP_POST_MUL, "group", GROUP_TURRET)
    # A turret mod using the other operator (postPercent, +15%).
    rows += _module(PERCENT_MOD, "Test Percent Mod", GROUP_PERCENT_MOD,
                    {ATTR_PERCENT_BONUS: 15.0})
    rows += _effect(EFF_PERCENT_MOD, "testPercentMod", [PERCENT_MOD],
                    "LocationGroupModifier", "shipID", ATTR_DAMAGE_MULTIPLIER,
                    ATTR_PERCENT_BONUS, OP_POST_PERCENT, "group", GROUP_TURRET)
    return rows


def _stats(items):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items)
    return _Slice().run(_calc, rows=_rows())


def _drones(n=1):
    return {"type_id": DRONE, "slot": "drone", "quantity": n}


def _guns(*mods):
    return [_high(TURRET, charge_type_id=AMMO, quantity=4), *mods]


def test_penalty_table_matches_the_engine():
    """Guard the constants used in the expected values below."""
    from app.fitting.engine import apply_stacking_penalties
    assert apply_stacking_penalties([1.2, 1.25]) == pytest.approx(
        1.25 * (1 + 0.20 * PEN[1]), abs=1e-4)


def test_drone_amplifiers_of_two_types_penalise_each_other():
    # Navy (+25%) is strongest and unpenalised; T2 (+20%) takes the 2nd step.
    # 100 x 1.25 x (1 + 0.20 x 0.869120) = 146.7, not 150.0 (no penalty).
    mixed = _stats([_drones(), _low(DDA_T2), _low(DDA_NAVY)])["drone_dps"]
    assert mixed == pytest.approx(100 * 1.25 * (1 + 0.20 * PEN[1]), abs=0.1)
    assert mixed == pytest.approx(146.7, abs=0.1)


def test_three_drone_amplifiers_of_two_types_use_three_penalty_steps():
    # 25%, 20%, 20% in descending order take steps 1, 2, 3:
    # 100 x 1.25 x (1 + 0.2 x 0.86912) x (1 + 0.2 x 0.570583) = 163.5
    # (separate groups would give 176.1).
    fit = [_drones(), _low(DDA_T2, quantity=2), _low(DDA_NAVY)]
    got = _stats(fit)["drone_dps"]
    assert got == pytest.approx(
        100 * 1.25 * (1 + 0.20 * PEN[1]) * (1 + 0.20 * PEN[2]), abs=0.1)
    assert got == pytest.approx(163.5, abs=0.1)


def test_turret_damage_mods_of_two_types_penalise_each_other():
    # Four turrets: 4 x 20 x 2.0 = 160 volley, 40 DPS at 4 s.
    base = _stats(_guns())
    assert base["weapon_dps"] == pytest.approx(40.0, abs=0.05)
    assert base["weapon_volley"] == pytest.approx(160.0, abs=0.5)

    # Faction 1.12 first, then T2 1.10 at step 2 and T2 1.10 at step 3:
    # 1.12 x 1.086912 x 1.057058 = 1.28679 -> 51.5 DPS, 205.9 volley
    # (separate groups would give 53.6 / 214.3).
    mult = 1.12 * (1 + 0.10 * PEN[1]) * (1 + 0.10 * PEN[2])
    got = _stats(_guns(_low(GYRO_T2, quantity=2), _low(GYRO_FACTION)))
    assert got["weapon_dps"] == pytest.approx(40 * mult, abs=0.1)
    assert got["weapon_volley"] == pytest.approx(160 * mult, abs=0.5)
    assert got["weapon_dps"] == pytest.approx(51.5, abs=0.1)
    assert got["weapon_volley"] == pytest.approx(205.9, abs=0.5)


def test_one_source_type_still_penalises_its_own_copies():
    # Two of one type: 1.10 x (1 + 0.10 x 0.86912) = 1.195603.
    got = _stats(_guns(_low(GYRO_T2, quantity=2)))["weapon_dps"]
    assert got == pytest.approx(40 * 1.10 * (1 + 0.10 * PEN[1]), abs=0.05)
    assert got == pytest.approx(47.8, abs=0.05)


def test_a_different_operator_is_a_separate_chain():
    # Two T2 gyros (postMul chain, 1.195603) and one +15% postPercent module
    # (its own chain of one): 40 x 1.195603 x 1.15 = 55.0. Pooling both
    # operators into one chain per attribute would give 52.9.
    got = _stats(_guns(_low(GYRO_T2, quantity=2), _low(PERCENT_MOD)))["weapon_dps"]
    assert got == pytest.approx(40 * 1.10 * (1 + 0.10 * PEN[1]) * 1.15, abs=0.1)
    assert got == pytest.approx(55.0, abs=0.1)
