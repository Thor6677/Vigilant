"""Type-level bonuses must reach the per-item figures.

From v1.4.0 to v1.4.2 calculate_fitting_stats() copied each fitted item's
attribute dict from module_attrs_map BEFORE skills, implants, hull and
subsystem bonuses were applied to that map, so every one of those bonuses
changed a dict nothing read any more. Weapon DPS, rep rates, tracking and
optimal range showed none of them: a turret with Surgical Strike V and one
with the skill untrained gave the same DPS. Charges were the exception,
because they are read from their type-level map directly.

Built on the SDE slice in tests/test_fitting_engine.py plus a damage skill
(3% per level on turrets, like Surgical Strike), a repair skill (5% per
level on armor repairers), a damage implant (+10% on turrets) and a
tracking implant (+10% on turrets). Each test compares the figure with and
without the bonus; on the broken ordering every pair comes out equal.
"""
import pytest

from app.db.sde_models import (
    SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
    SDETypeSkillReq,
)
from app.fitting.constants import (
    ATTR_ARMOR_DAMAGE_AMOUNT, ATTR_DAMAGE_MULTIPLIER, ATTR_TRACKING_SPEED,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    AMMO, ANCILLARY_REP, OP_POST_PERCENT, SHIP, TURRET, _Slice, _high, _low,
    _seed_rows,
)

CATEGORY_SKILL = 16
CATEGORY_IMPLANT = 20
SKILL_GUNNERY = 3300
SKILL_REPAIR_SYSTEMS = 3393

ATTR_DAMAGE_MULTIPLIER_BONUS = 292      # damageMultiplierBonus
ATTR_ARMOR_REP_BONUS = 895              # armorDamageAmountBonus
ATTR_TRACKING_BONUS = 767               # trackingSpeedBonus

DAMAGE_SKILL = 9601
REPAIR_SKILL = 9602
DAMAGE_IMPLANT = 9603
TRACKING_IMPLANT = 9604

EFF_DAMAGE_SKILL = 8601
EFF_REPAIR_SKILL = 8602
EFF_DAMAGE_IMPLANT = 8603
EFF_TRACKING_IMPLANT = 8604


def _bonus_source(type_id, name, category, effect_id, source_attr, value,
                  target_attr, skill):
    """A skill or implant whose single effect boosts `target_attr` on every
    module requiring `skill`, by its `source_attr` value (per level for a
    skill)."""
    return [
        SDEType(type_id=type_id, type_name=name, group_id=9600, category_id=category),
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=source_attr, value=value),
        SDEEffect(effect_id=effect_id, effect_name=name, effect_category=0),
        SDETypeEffect(type_id=type_id, effect_id=effect_id),
        SDEModifier(effect_id=effect_id, func="LocationRequiredSkillModifier",
                    domain="shipID", modified_attribute_id=target_attr,
                    modifying_attribute_id=source_attr, operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=skill),
    ]


def _rows():
    rows = _seed_rows()
    rows += [
        SDETypeSkillReq(type_id=TURRET, skill_type_id=SKILL_GUNNERY, required_level=1),
        SDETypeSkillReq(type_id=ANCILLARY_REP, skill_type_id=SKILL_REPAIR_SYSTEMS,
                        required_level=1),
    ]
    rows += _bonus_source(DAMAGE_SKILL, "Test Surgical Strike", CATEGORY_SKILL,
                          EFF_DAMAGE_SKILL, ATTR_DAMAGE_MULTIPLIER_BONUS, 3.0,
                          ATTR_DAMAGE_MULTIPLIER, SKILL_GUNNERY)
    rows += _bonus_source(REPAIR_SKILL, "Test Repair Systems", CATEGORY_SKILL,
                          EFF_REPAIR_SKILL, ATTR_ARMOR_REP_BONUS, 5.0,
                          ATTR_ARMOR_DAMAGE_AMOUNT, SKILL_REPAIR_SYSTEMS)
    rows += _bonus_source(DAMAGE_IMPLANT, "Test Damage Implant", CATEGORY_IMPLANT,
                          EFF_DAMAGE_IMPLANT, ATTR_DAMAGE_MULTIPLIER_BONUS, 10.0,
                          ATTR_DAMAGE_MULTIPLIER, SKILL_GUNNERY)
    rows += _bonus_source(TRACKING_IMPLANT, "Test Tracking Implant", CATEGORY_IMPLANT,
                          EFF_TRACKING_IMPLANT, ATTR_TRACKING_BONUS, 10.0,
                          ATTR_TRACKING_SPEED, SKILL_GUNNERY)
    return rows


def _stats(items, **kwargs):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items, **kwargs)
    return _Slice().run(_calc, rows=_rows())


GUN_FIT = [_high(TURRET, charge_type_id=AMMO)]
REP_FIT = [_low(ANCILLARY_REP)]
UNTRAINED = {DAMAGE_SKILL: 0, REPAIR_SKILL: 0}


def test_turret_damage_skill_changes_weapon_dps():
    untrained = _stats(GUN_FIT, skill_levels=UNTRAINED)["weapon_dps"]
    trained = _stats(GUN_FIT, skill_levels={**UNTRAINED, DAMAGE_SKILL: 5})["weapon_dps"]
    assert untrained > 0
    assert trained == pytest.approx(untrained * 1.15, abs=0.05)


def test_damage_implant_changes_weapon_dps():
    base = _stats(GUN_FIT, skill_levels=UNTRAINED)["weapon_dps"]
    plugged = _stats(GUN_FIT, skill_levels=UNTRAINED,
                     implants=[DAMAGE_IMPLANT])["weapon_dps"]
    assert plugged == pytest.approx(base * 1.10, abs=0.05)


def test_repair_skill_changes_armor_rep_rate():
    untrained = _stats(REP_FIT, skill_levels=UNTRAINED)["armor_rep_rate"]
    trained = _stats(REP_FIT, skill_levels={**UNTRAINED, REPAIR_SKILL: 5})["armor_rep_rate"]
    assert untrained > 0
    assert trained == pytest.approx(untrained * 1.25, abs=0.05)


def test_tracking_implant_changes_weapon_tracking():
    base = _stats(GUN_FIT, skill_levels=UNTRAINED)["weapon_tracking"]
    plugged = _stats(GUN_FIT, skill_levels=UNTRAINED,
                     implants=[TRACKING_IMPLANT])["weapon_tracking"]
    assert base > 0
    assert plugged == pytest.approx(base * 1.10, abs=1e-4)
