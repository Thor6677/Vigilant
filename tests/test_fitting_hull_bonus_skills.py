"""Each per-level hull bonus scales with ITS OWN class skill (ISS-142).

A hull with several class skills (a carrier with Amarr Carrier for one set of
bonuses and Caldari Carrier for another) used to scale every per-level bonus by
the MIN of the character's levels across all of them. The dogma link from a
bonus attribute to its skill is the skill's own ItemModifier/shipID effect
reading skillLevel (attribute 280) into that bonus attribute. The engine
resolves that link (ISS-150: it is also what decides whether an attribute is
per level at all; a bonus with no link is flat, and there is no MIN fallback).

Built on the SDE slice in tests/test_fitting_engine.py plus invented hulls,
skills and attributes (9650s / 9850s / 8850s), with the hull traits as
ship-self postPercent bonuses to capacitor (base 1000) and max velocity
(base 2000), neither of which a fitting skill touches.
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeBonus,
    SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import (
    ATTR_CAPACITOR, ATTR_HP, ATTR_MASS, ATTR_MAX_VELOCITY,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import OP_POST_PERCENT, _Slice, _seed_rows

ATTR_SKILL_LEVEL = 280                     # skillLevel

SK_A, SK_B, SK_OTHER = 9651, 9652, 9653    # category 16
HULL_TWO, HULL_MIXED, HULL_FOREIGN = 9850, 9851, 9852

A_BONUS_A, A_BONUS_B, A_BONUS_C, A_BONUS_D = 9861, 9862, 9863, 9864

BASE_CAP, BASE_SPEED = 1000.0, 2000.0


def _hull_effect(hull, effect_id, source, target):
    """A hull trait: `source` (the hull's own attribute) as postPercent on `target`."""
    return [
        SDEEffect(effect_id=effect_id, effect_name=f"testHullBonus{effect_id}",
                  effect_category=0),
        SDETypeEffect(type_id=hull, effect_id=effect_id),
        SDEModifier(effect_id=effect_id, func="ItemModifier", domain="shipID",
                    modified_attribute_id=target, modifying_attribute_id=source,
                    operator=OP_POST_PERCENT),
    ]


def _skill(skill, effect_id, bonus_attr=None):
    """A class skill. With `bonus_attr`, its effect feeds skillLevel into that
    hull attribute (the real SDE's link from a hull bonus to its skill)."""
    rows = [SDEType(type_id=skill, type_name=f"Test Class Skill {skill}",
                    group_id=9660, category_id=16)]
    if bonus_attr is not None:
        rows += [
            SDEEffect(effect_id=effect_id, effect_name=f"testClassSkill{skill}",
                      effect_category=0),
            SDETypeEffect(type_id=skill, effect_id=effect_id),
            SDEModifier(effect_id=effect_id, func="ItemModifier", domain="shipID",
                        modified_attribute_id=bonus_attr,
                        modifying_attribute_id=ATTR_SKILL_LEVEL,
                        operator=OP_POST_PERCENT),
        ]
    return rows


def _hull(hull, name, bonuses):
    """`bonuses` is [(attr, value, trait_skill, target_attr)]."""
    rows = [SDEType(type_id=hull, type_name=name, group_id=9670, category_id=6,
                    mass=1_000_000.0, volume=100_000.0)]
    for attr, value, _skill_id, _target in bonuses:
        rows.append(SDETypeDogmaAttribute(type_id=hull, attribute_id=attr, value=value))
    for base in ((ATTR_HP, 1000.0), (ATTR_MASS, 1_000_000.0),
                 (ATTR_CAPACITOR, BASE_CAP), (ATTR_MAX_VELOCITY, BASE_SPEED)):
        rows.append(SDETypeDogmaAttribute(type_id=hull, attribute_id=base[0], value=base[1]))
    for i, (attr, value, skill_id, target) in enumerate(bonuses):
        rows.append(SDETypeBonus(type_id=hull, bonus_value=value, is_role_bonus=False,
                                 scaling_skill_id=skill_id))
        rows += _hull_effect(hull, 8850 + hull % 10 * 10 + i, attr, target)
    return rows


def _rows():
    rows = _seed_rows()
    rows += [SDEDogmaAttribute(attribute_id=a, attribute_name=n, stackable=True)
             for a, n in ((A_BONUS_A, "shipBonusTestA1"), (A_BONUS_B, "shipBonusTestB1"),
                          (A_BONUS_C, "shipBonusTestC1"), (A_BONUS_D, "shipBonusTestD1"))]
    # SK_A and SK_B each feed their own attribute; SK_OTHER feeds D on HULL_FOREIGN.
    rows += _skill(SK_A, 8801, A_BONUS_A)
    rows += _skill(SK_B, 8802, A_BONUS_B)
    rows += _skill(SK_OTHER, 8803, A_BONUS_D)
    # Two bonuses, one per class skill: A +5%/level to capacitor, B +10%/level to velocity.
    rows += _hull(HULL_TWO, "Test Two-Class Hull", [
        (A_BONUS_A, 5.0, SK_A, ATTR_CAPACITOR),
        (A_BONUS_B, 10.0, SK_B, ATTR_MAX_VELOCITY),
    ])
    # A resolves to SK_A, C has no skillLevel row at all (a flat bonus).
    rows += _hull(HULL_MIXED, "Test Mixed Hull", [
        (A_BONUS_A, 5.0, SK_A, ATTR_CAPACITOR),
        (A_BONUS_C, 10.0, SK_B, ATTR_MAX_VELOCITY),
    ])
    # D resolves to SK_OTHER, which is not one of this hull's trait skills
    # (the traits are no longer consulted).
    rows += _hull(HULL_FOREIGN, "Test Foreign-Skill Hull", [
        (A_BONUS_A, 5.0, SK_A, ATTR_CAPACITOR),
        (A_BONUS_D, 10.0, SK_A, ATTR_MAX_VELOCITY),
    ])
    return rows


def _stats(hull, skill_levels):
    async def _calc(db):
        return await calculate_fitting_stats(db, hull, [], skill_levels=skill_levels)
    return _Slice().run(_calc, rows=_rows())


def test_each_bonus_scales_with_its_own_skill():
    """A 5 x 5 = +25% on capacitor (1250 of 1000), B 10 x 1 = +10% on max velocity.
    The MIN (1) would give +5% on capacitor, 1050."""
    stats = _stats(HULL_TWO, {SK_A: 5, SK_B: 1})
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.25, abs=0.5)
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.10, abs=0.5)


def test_no_character_is_all_v_for_every_bonus():
    stats = _stats(HULL_TWO, None)
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.25, abs=0.5)
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.50, abs=0.5)


def test_an_untrained_skill_zeroes_only_its_own_bonus():
    for levels in ({SK_A: 0, SK_B: 5}, {SK_B: 5}):
        stats = _stats(HULL_TWO, levels)
        assert stats["capacitor"] == pytest.approx(BASE_CAP, abs=0.5)
        assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.50, abs=0.5)


def test_a_bonus_with_no_skill_link_is_flat():
    """C has no skillLevel row, so it is a role-style bonus: +10% on velocity at
    its source value whatever the hull's skills are (it used to take the MIN of
    5 and 2 and give +20%). A still uses SK_A."""
    for levels in ({SK_A: 5, SK_B: 2}, {SK_A: 5, SK_B: 0}, None):
        stats = _stats(HULL_MIXED, levels)
        assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.10, abs=0.5)
    stats = _stats(HULL_MIXED, {SK_A: 5, SK_B: 2})
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.25, abs=0.5)


def test_a_linked_skill_outside_the_hulls_trait_skills_still_sets_the_level():
    """D links to SK_OTHER, which the hull's traits do not list. The SDE link
    decides, not the traits: SK_OTHER at 5 gives +50% on velocity (the hull's
    SK_A at 3 only scales capacitor, +15%)."""
    stats = _stats(HULL_FOREIGN, {SK_A: 3, SK_OTHER: 5})
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.15, abs=0.5)
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.50, abs=0.5)
    stats = _stats(HULL_FOREIGN, {SK_A: 3})          # SK_OTHER untrained
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.5)
