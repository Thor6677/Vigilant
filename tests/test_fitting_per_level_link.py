"""A hull or subsystem bonus scales with skill level only when a skill feeds
skillLevel into it (ISS-150).

The engine used to decide by attribute NAME (shipBonus*/eliteBonus*/
subsystemBonus*, minus "Role"), which scaled role bonuses by level and applied
real per-level bonuses (mining, industrial, freighter) flat. The SDE states it
outright: a skill's effect carries a modifier whose source is skillLevel (280)
and whose target is the bonus attribute, on shipID. No such row means a flat
bonus; the level is the LINKED skill's, whatever else the hull lists.

Built on the SDE slice in tests/test_fitting_engine.py plus invented hulls,
subsystems, skills and attributes (9950s / 9960s / 9970s / 9990s). Hull and
subsystem bonuses are postPercent on max velocity (base 2000) and capacitor
(base 1000), and on a turret's tracking (base 0.1) where a module is targeted.
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeBonus,
    SDETypeDogmaAttribute, SDETypeEffect, SDETypeSkillReq,
)
from app.fitting.constants import (
    ATTR_CAPACITOR, ATTR_HP, ATTR_MASS, ATTR_MAX_VELOCITY, ATTR_TRACKING_SPEED,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    AMMO, OP_POST_PERCENT, TURRET, _Slice, _high, _seed_rows,
)

ATTR_SKILL_LEVEL = 280                     # skillLevel

SK_CLASS, SK_OTHER, SK_REQ = 9951, 9952, 9953       # category 16
HULL, HULL_MODULES = 9960, 9961
SUB = 9970

# Bonus attributes. Names are deliberately misleading in both directions.
A_ROLE_NAMED_FLAT = 9981      # shipBonusTestFlat1-style name, no skillLevel row
A_LOOKS_LIKE_ROLE = 9982      # name contains "Role" but a skill feeds it
A_ODD_NAME = 9983             # miningBargeBonusTest1: no shipBonus prefix, linked
A_OTHER_LINKED = 9984         # linked to SK_OTHER
A_ITEM_DOMAIN = 9985          # skillLevel row on domain itemID (not a hull link)
A_TWO_LINKS = 9986            # linked to both SK_CLASS and SK_OTHER
A_SUB_LINKED = 9987           # subsystemBonusTestCore1: LocationGroupModifier link
A_SUB_FLAT = 9988             # subsystemBonusTestRole1-style: no link
A_MODULE_LINKED = 9989        # hull bonus aimed at turrets, linked
A_MODULE_FLAT = 9990          # hull bonus aimed at turrets, no link

BASE_CAP, BASE_SPEED, BASE_TRACKING = 1000.0, 2000.0, 0.1
GROUP_SUBSYSTEM_SKILL = 9980


def _effect(type_id, effect_id, rows):
    return [SDEEffect(effect_id=effect_id, effect_name=f"testEffect{effect_id}",
                      effect_category=0),
            SDETypeEffect(type_id=type_id, effect_id=effect_id)] + rows


def _ship_self(source, target):
    return SDEModifier(effect_id=0, func="ItemModifier", domain="shipID",
                       modified_attribute_id=target, modifying_attribute_id=source,
                       operator=OP_POST_PERCENT)


def _with_effect(type_id, effect_id, mod):
    mod.effect_id = effect_id
    return _effect(type_id, effect_id, [mod])


def _skill_link(skill, effect_id, attr, func="ItemModifier", domain="shipID"):
    """A skill whose effect feeds skillLevel into `attr`."""
    mod = SDEModifier(effect_id=effect_id, func=func, domain=domain,
                      modified_attribute_id=attr,
                      modifying_attribute_id=ATTR_SKILL_LEVEL,
                      operator=OP_POST_PERCENT)
    if func == "LocationGroupModifier":
        mod.filter_type, mod.filter_value = "group", 0
    return _effect(skill, effect_id, [mod])


def _hull(hull, name, bonuses):
    """`bonuses` is [(attr, value, target_attr)]: ship-self postPercent traits."""
    rows = [SDEType(type_id=hull, type_name=name, group_id=9670, category_id=6,
                    mass=1_000_000.0, volume=100_000.0)]
    for base in ((ATTR_HP, 1000.0), (ATTR_MASS, 1_000_000.0),
                 (ATTR_CAPACITOR, BASE_CAP), (ATTR_MAX_VELOCITY, BASE_SPEED)):
        rows.append(SDETypeDogmaAttribute(type_id=hull, attribute_id=base[0], value=base[1]))
    for i, (attr, value, target) in enumerate(bonuses):
        rows.append(SDETypeDogmaAttribute(type_id=hull, attribute_id=attr, value=value))
        rows += _with_effect(hull, 8950 + hull % 10 * 10 + i, _ship_self(attr, target))
    return rows


def _rows(extra=()):
    rows = _seed_rows()
    rows += [SDEDogmaAttribute(attribute_id=a, attribute_name=n, stackable=True)
             for a, n in ((A_ROLE_NAMED_FLAT, "shipBonusTestFlat1"),
                          (A_LOOKS_LIKE_ROLE, "eliteBonusTestRole2"),
                          (A_ODD_NAME, "miningBargeBonusTest1"),
                          (A_OTHER_LINKED, "industrialBonusTest1"),
                          (A_ITEM_DOMAIN, "shipBonusTestItemDomain1"),
                          (A_TWO_LINKS, "shipBonusTestTwoLinks1"),
                          (A_SUB_LINKED, "subsystemBonusTestCore1"),
                          (A_SUB_FLAT, "subsystemBonusTestRole1"),
                          (A_MODULE_LINKED, "shipBonusTestGunnery1"),
                          (A_MODULE_FLAT, "shipBonusTestRole3"))]
    rows += [SDEType(type_id=s, type_name=f"Test Skill {s}", group_id=9660,
                     category_id=16) for s in (SK_CLASS, SK_OTHER, SK_REQ)]
    rows += _skill_link(SK_CLASS, 8901, A_LOOKS_LIKE_ROLE)
    rows += _skill_link(SK_CLASS, 8902, A_ODD_NAME)
    rows += _skill_link(SK_OTHER, 8903, A_OTHER_LINKED)
    rows += _skill_link(SK_CLASS, 8904, A_ITEM_DOMAIN, domain="itemID")
    rows += _skill_link(SK_CLASS, 8905, A_TWO_LINKS)
    rows += _skill_link(SK_OTHER, 8906, A_TWO_LINKS)
    rows += _skill_link(SK_CLASS, 8907, A_SUB_LINKED, func="LocationGroupModifier")
    rows += _skill_link(SK_CLASS, 8908, A_MODULE_LINKED)
    rows += list(extra)
    return rows


def _stats(hull, skill_levels, items=(), extra=()):
    async def _calc(db):
        return await calculate_fitting_stats(db, hull, list(items),
                                             skill_levels=skill_levels)
    return _Slice().run(_calc, rows=_rows(extra))


def _speed(extra_bonuses, skill_levels):
    hull_rows = _hull(HULL, "Test Hull", extra_bonuses)
    return _stats(HULL, skill_levels, extra=hull_rows)["max_velocity"]


# (i) ---------------------------------------------------------------------

def test_a_shipbonus_named_attribute_with_no_link_is_flat():
    """+10% at its source value at every level (it used to be 10 x level)."""
    for levels in ({SK_CLASS: 5}, {SK_CLASS: 1}, {}, None):
        speed = _speed([(A_ROLE_NAMED_FLAT, 10.0, ATTR_MAX_VELOCITY)], levels)
        assert speed == pytest.approx(BASE_SPEED * 1.10, abs=0.5)


def test_an_attribute_with_role_in_its_name_scales_if_a_skill_feeds_it():
    """The name is not consulted: the skillLevel row is. 10 x 3 = +30%."""
    speed = _speed([(A_LOOKS_LIKE_ROLE, 10.0, ATTR_MAX_VELOCITY)], {SK_CLASS: 3})
    assert speed == pytest.approx(BASE_SPEED * 1.30, abs=0.5)


# (ii) --------------------------------------------------------------------

def test_a_per_level_attribute_without_a_shipbonus_name_scales_by_level():
    """miningBargeBonus-style name: 5 x 3 = +15% (it used to be flat +5%)."""
    speed = _speed([(A_ODD_NAME, 5.0, ATTR_MAX_VELOCITY)], {SK_CLASS: 3})
    assert speed == pytest.approx(BASE_SPEED * 1.15, abs=0.5)


def test_a_skill_level_row_on_the_itemid_domain_is_not_a_hull_link():
    """A skill's own per-level attribute (domain itemID) says nothing about a
    hull bonus that happens to share the attribute: it stays flat."""
    speed = _speed([(A_ITEM_DOMAIN, 10.0, ATTR_MAX_VELOCITY)], {SK_CLASS: 5})
    assert speed == pytest.approx(BASE_SPEED * 1.10, abs=0.5)


# (iii) -------------------------------------------------------------------

def test_the_level_is_the_linked_skills_not_the_hulls_other_skills():
    """The hull's traits name SK_CLASS, but A_OTHER_LINKED is fed by SK_OTHER
    (level 2): +20% on velocity. The cap bonus follows SK_CLASS (level 5)."""
    rows = _hull(HULL, "Test Hull", [
        (A_ODD_NAME, 5.0, ATTR_CAPACITOR),
        (A_OTHER_LINKED, 10.0, ATTR_MAX_VELOCITY),
    ])
    rows.append(SDETypeBonus(type_id=HULL, bonus_value=5.0, is_role_bonus=False,
                             scaling_skill_id=SK_CLASS))
    stats = _stats(HULL, {SK_CLASS: 5, SK_OTHER: 2}, extra=rows)
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.20, abs=0.5)
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.25, abs=0.5)


# (iv) / (v) --------------------------------------------------------------

def test_no_character_is_all_v():
    speed = _speed([(A_ODD_NAME, 5.0, ATTR_MAX_VELOCITY)], None)
    assert speed == pytest.approx(BASE_SPEED * 1.25, abs=0.5)


def test_an_untrained_linked_skill_gives_no_bonus():
    for levels in ({}, {SK_CLASS: 0}, {SK_OTHER: 5}):
        speed = _speed([(A_ODD_NAME, 5.0, ATTR_MAX_VELOCITY)], levels)
        assert speed == pytest.approx(BASE_SPEED, abs=0.5)


# (d) module-targeting rows follow the same per-attribute decision ----------

def test_a_bonus_aimed_at_modules_is_decided_per_source_attribute_too():
    """Two hull bonuses to turret tracking (postPercent, group of TURRET): the
    linked one is 10 x 3 = +30%, the unlinked one a flat +10%."""
    rows = _hull(HULL_MODULES, "Test Gunnery Hull", [])
    for i, attr in enumerate((A_MODULE_LINKED, A_MODULE_FLAT)):
        rows.append(SDETypeDogmaAttribute(type_id=HULL_MODULES, attribute_id=attr,
                                          value=10.0))
        mod = SDEModifier(effect_id=0, func="LocationGroupModifier", domain="shipID",
                          modified_attribute_id=ATTR_TRACKING_SPEED,
                          modifying_attribute_id=attr, operator=OP_POST_PERCENT,
                          filter_type="group", filter_value=9403)   # TURRET's group
        rows += _with_effect(HULL_MODULES, 8990 + i, mod)
    levels = {SK_CLASS: 3}
    stats = _stats(HULL_MODULES, levels, items=[_high(TURRET, charge_type_id=AMMO)], extra=rows)
    # base 0.1 x 1.30 x 1.10 (tracking is not stackable in this slice)
    assert stats["weapon_tracking"] == pytest.approx(BASE_TRACKING * 1.30 * 1.10, abs=1e-4)


# (vi) subsystems ---------------------------------------------------------

def _subsystem_rows():
    """A subsystem whose per-level bonus is fed by SK_CLASS through a
    LocationGroupModifier link, but which REQUIRES SK_REQ: the link, not the
    requirement, sets the level. A second, unlinked bonus is flat."""
    rows = [SDEType(type_id=SUB, type_name="Test Subsystem", group_id=954,
                    category_id=32),
            SDETypeSkillReq(type_id=SUB, skill_type_id=SK_REQ, required_level=1)]
    rows += _attr_vals(SUB, {A_SUB_LINKED: 5.0, A_SUB_FLAT: 7.0})
    rows += _with_effect(SUB, 8970, _ship_self(A_SUB_LINKED, ATTR_MAX_VELOCITY))
    rows += _with_effect(SUB, 8971, _ship_self(A_SUB_FLAT, ATTR_CAPACITOR))
    return rows


def _attr_vals(type_id, mapping):
    return [SDETypeDogmaAttribute(type_id=type_id, attribute_id=a, value=v)
            for a, v in mapping.items()]


def _sub_stats(skill_levels):
    items = [{"type_id": SUB, "slot": "subsystem", "quantity": 1}]
    hull = _hull(HULL, "Test Subsystem Hull", [])
    return _stats(HULL, skill_levels, items=items, extra=hull + _subsystem_rows())


def test_a_subsystem_bonus_scales_by_its_linked_skill():
    """5 x 3 = +15% on velocity from SK_CLASS (3), not from the required SK_REQ
    (5); the unlinked bonus is a flat +7% on capacitor."""
    stats = _sub_stats({SK_CLASS: 3, SK_REQ: 5})
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.15, abs=0.5)
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.07, abs=0.5)


def test_a_subsystem_bonus_defaults_to_all_v_and_zeroes_when_untrained():
    stats = _sub_stats(None)
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED * 1.25, abs=0.5)
    stats = _sub_stats({SK_REQ: 5})
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.5)
    assert stats["capacitor"] == pytest.approx(BASE_CAP * 1.07, abs=0.5)


# (vii) -------------------------------------------------------------------

def test_an_attribute_fed_by_two_skills_takes_the_lower_level():
    """The SDE links no hull attribute to two skills today; if one ever is,
    the result is the MIN across them, so it is deterministic: 10 x 2 = +20%."""
    speed = _speed([(A_TWO_LINKS, 10.0, ATTR_MAX_VELOCITY)], {SK_CLASS: 5, SK_OTHER: 2})
    assert speed == pytest.approx(BASE_SPEED * 1.20, abs=0.5)
    speed = _speed([(A_TWO_LINKS, 10.0, ATTR_MAX_VELOCITY)], {SK_CLASS: 4})
    assert speed == pytest.approx(BASE_SPEED, abs=0.5)
