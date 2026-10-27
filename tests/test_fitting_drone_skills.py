"""ISS-157: drone size and specialization skills must raise drone damage.

Light/Medium/Heavy Drone Operation, Sentry Drone Interfacing and the racial
specializations carry effect 1730 (droneDmgBonus) and a per-level bonus in
attribute 292 (damageMultiplierBonus). They have no modifier rows, so the
engine applies them itself. The engine used to read attribute 64
(damageMultiplier) instead of 292 and silently skipped every one of them.

All type ids are invented (97xx); the dogma attribute and effect ids are real.
"""
import pytest

import app.fitting.engine as E
from app.db.sde_models import (
    SDEEffect, SDEModifier, SDEType, SDETypeDogmaAttribute, SDETypeEffect,
    SDETypeSkillReq,
)
from app.fitting.constants import (
    ATTR_DAMAGE_MULTIPLIER, ATTR_DAMAGE_MULTIPLIER_BONUS, ATTR_EM_DAMAGE,
    ATTR_RATE_OF_FIRE, EFFECT_DRONE_DMG_BONUS,
)
from tests.test_fitting_engine import OP_POST_PERCENT, SHIP, _Slice, _seed_rows

CATEGORY_SKILL = 16
CATEGORY_DRONE = 18

SKILL_DRONES = 9761
SKILL_INTERFACING = 9762   # modelled in the SDE, like Drone Interfacing
SKILL_SIZE = 9763          # 5%/level, effect 1730 only
SKILL_SPEC = 9764          # 2%/level, effect 1730 only
SKILL_NO_EFFECT = 9765     # carries attr 292 but not effect 1730
DRONE_T2 = 9771            # requires DRONES, SIZE, SPEC
DRONE_T1 = 9772            # requires DRONES, SIZE
DRONE_PLAIN = 9773         # requires DRONES, NO_EFFECT
EFF_INTERFACING = 8761


def _skill(type_id, name, bonus, effect_id, decoy=True):
    rows = [
        SDEType(type_id=type_id, type_name=name, group_id=9760, category_id=CATEGORY_SKILL),
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=ATTR_DAMAGE_MULTIPLIER_BONUS, value=bonus),
    ]
    if effect_id is not None:
        rows.append(SDETypeEffect(type_id=type_id, effect_id=effect_id))
    if decoy:
        # A value under damageMultiplier itself: reading 64 instead of 292
        # would turn +5%/level into +250%/level and fail loudly.
        rows.append(SDETypeDogmaAttribute(type_id=type_id, attribute_id=ATTR_DAMAGE_MULTIPLIER, value=50.0))
    return rows


def _drone(type_id, name, skills):
    return [
        SDEType(type_id=type_id, type_name=name, group_id=9770, category_id=CATEGORY_DRONE),
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=ATTR_EM_DAMAGE, value=100.0),
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=ATTR_DAMAGE_MULTIPLIER, value=1.0),
        SDETypeDogmaAttribute(type_id=type_id, attribute_id=ATTR_RATE_OF_FIRE, value=1000.0),
    ] + [SDETypeSkillReq(type_id=type_id, skill_type_id=s, required_level=1) for s in skills]


def _rows():
    rows = _seed_rows()
    rows += [
        SDEEffect(effect_id=EFFECT_DRONE_DMG_BONUS, effect_name="droneDmgBonus", effect_category=0),
        SDEEffect(effect_id=EFF_INTERFACING, effect_name="testDroneInterfacing", effect_category=0),
        SDEModifier(effect_id=EFF_INTERFACING, func="OwnerRequiredSkillModifier", domain="charID",
                    modified_attribute_id=ATTR_DAMAGE_MULTIPLIER,
                    modifying_attribute_id=ATTR_DAMAGE_MULTIPLIER_BONUS,
                    operator=OP_POST_PERCENT, filter_type="skill", filter_value=SKILL_DRONES),
        SDEType(type_id=SKILL_DRONES, type_name="Test Drones", group_id=9760, category_id=CATEGORY_SKILL),
    ]
    rows += _skill(SKILL_INTERFACING, "Test Drone Interfacing", 10.0, EFF_INTERFACING, decoy=False)
    rows += _skill(SKILL_SIZE, "Test Medium Drone Operation", 5.0, EFFECT_DRONE_DMG_BONUS)
    rows += _skill(SKILL_SPEC, "Test Racial Drone Specialization", 2.0, EFFECT_DRONE_DMG_BONUS)
    rows += _skill(SKILL_NO_EFFECT, "Test Unrelated Skill", 5.0, None)
    rows += _drone(DRONE_T2, "Test Drone II", (SKILL_DRONES, SKILL_SIZE, SKILL_SPEC))
    rows += _drone(DRONE_T1, "Test Drone I", (SKILL_DRONES, SKILL_SIZE))
    rows += _drone(DRONE_PLAIN, "Test Plain Drone", (SKILL_DRONES, SKILL_NO_EFFECT))
    return rows


def _drone_dps(type_id, n=1, **kw):
    async def _calc(db):
        return await E.calculate_fitting_stats(
            db, SHIP, [{"type_id": type_id, "slot": "drone", "quantity": n}], **kw)
    return _Slice().run(_calc, rows=_rows())["drone_dps"]


def test_size_and_specialization_skills_raise_drone_damage_at_all_v():
    # 100 EM / 1 s, x1.50 interfacing x1.25 size x1.10 spec
    assert _drone_dps(DRONE_T2) == pytest.approx(100 * 1.5 * 1.25 * 1.10, abs=0.1)


def test_a_drone_without_the_specialization_gets_only_the_size_skill():
    assert _drone_dps(DRONE_T1, n=2) == pytest.approx(2 * 100 * 1.5 * 1.25, abs=0.1)


def test_each_skill_scales_by_the_characters_own_level():
    lv = {SKILL_DRONES: 5, SKILL_INTERFACING: 5, SKILL_SIZE: 3, SKILL_SPEC: 0}
    assert _drone_dps(DRONE_T2, skill_levels=lv) == pytest.approx(100 * 1.5 * 1.15, abs=0.1)


def test_no_skills_leaves_the_base_damage():
    assert _drone_dps(DRONE_T2, skill_levels={}) == pytest.approx(100.0, abs=0.1)


def test_a_skill_without_effect_1730_is_not_applied():
    # Attribute 292 alone is not enough: the gap skills are found by effect.
    assert _drone_dps(DRONE_PLAIN) == pytest.approx(100 * 1.5, abs=0.1)
