"""Skills that raise the hull's own attributes are read from the SDE (ISS-152).

Navigation (+5% speed per level), Evasive Maneuvering, Capacitor Management,
Long Range Targeting, Signature Analysis and the like carry an ItemModifier on
shipID. The engine applied only a hardcoded list of six skills (CPU, power
grid, shield, armor, hull HP, fighter hangar), so a Raven with all skills at V
read 113 m/s where pyfa and EVE Workbench show 141, and likewise for align
time, capacitor, recharge, target range and scan resolution.

Each bonus is the skill's source attribute times the character's level in that
skill (All V when no character is selected), a postPercent row that is never
stacking-penalised. A row whose source is the skill-level attribute carries a
hull skill's per-level trait and is left to the hull-bonus code.

Names and ids are invented; the six legacy skills come from the base slice.
"""
import pytest

from app.db.sde_models import SDEType
from app.fitting.constants import ATTR_CAPACITOR, ATTR_CAP_RECHARGE
from app.fitting.engine import ATTR_SKILL_LEVEL, calculate_fitting_stats
from tests.test_fitting_engine import (
    ATTR_MAX_VELOCITY, SHIP, SHIP_SELF_SKILLS, _Slice, _seed_rows, _ship_self_skill_rows,
)

BARE_SPEED = 200.0
BARE_CAPACITOR = 1000.0

NAV = 9501          # invented "Navigation": +5% speed per level
CAPMGMT = 9502      # invented "Capacitor Management": +5% capacitor per level
RECHARGE = 9503     # invented "Capacitor Systems Operation": -5% recharge per level
ATTR_NAV_SRC = 9511
ATTR_CAP_SRC = 9512
ATTR_RECHARGE_SRC = 9513
UNKNOWN_ATTR = 9999  # an attribute the hull does not have


def _skills(extra=()):
    return [
        (NAV, 7101, ATTR_MAX_VELOCITY, ATTR_NAV_SRC),
        (CAPMGMT, 7102, ATTR_CAPACITOR, ATTR_CAP_SRC),
        *extra,
    ]


def _stats(levels, skills=None, rows_extra=(), recharge_value=-5.0):
    rows = _seed_rows()
    rows += _ship_self_skill_rows(skills or _skills())
    rows += list(rows_extra)

    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, [], skill_levels=levels)
    return _Slice().run(_calc, rows=rows)


def test_a_ship_self_skill_scales_by_the_all_v_default():
    got = _stats(None)
    assert got["max_velocity"] == pytest.approx(BARE_SPEED * 1.25, abs=0.05)
    assert got["capacitor"] == pytest.approx(BARE_CAPACITOR * 1.25, abs=0.05)


def test_each_skill_scales_by_its_own_level():
    got = _stats({NAV: 3, CAPMGMT: 5})
    assert got["max_velocity"] == pytest.approx(BARE_SPEED * 1.15, abs=0.05)
    assert got["capacitor"] == pytest.approx(BARE_CAPACITOR * 1.25, abs=0.05)


def test_an_untrained_skill_contributes_nothing():
    got = _stats({CAPMGMT: 5})
    assert got["max_velocity"] == pytest.approx(BARE_SPEED, abs=0.05)
    assert got["capacitor"] == pytest.approx(BARE_CAPACITOR * 1.25, abs=0.05)


def test_the_six_legacy_skills_still_apply_at_all_v():
    # CPU Management and the rest come from the base slice's SDE rows now.
    assert len(SHIP_SELF_SKILLS) == 6
    bare = _stats({})
    all_v = _stats(None)
    assert all_v["cpu_total"] == pytest.approx(bare["cpu_total"] * 1.25, abs=0.05)
    assert all_v["shield_hp"] == pytest.approx(bare["shield_hp"] * 1.25, abs=0.5)


def test_a_skill_level_scaling_row_is_left_to_the_hull_code():
    # A hull skill's trait carrier reads attribute 280 (skillLevel) as its
    # source; applying it here would add skill-level percent to the hull.
    carrier = _ship_self_skill_rows([(9504, 7104, ATTR_CAPACITOR, ATTR_SKILL_LEVEL)], value=50.0)
    got = _stats(None, skills=_skills(), rows_extra=carrier)
    assert got["capacitor"] == pytest.approx(BARE_CAPACITOR * 1.25, abs=0.05)


def test_an_attribute_the_hull_lacks_is_skipped():
    other = _ship_self_skill_rows([(9505, 7105, UNKNOWN_ATTR, 9514)])
    got = _stats(None, skills=_skills(), rows_extra=other)
    assert got["max_velocity"] == pytest.approx(BARE_SPEED * 1.25, abs=0.05)
    assert UNKNOWN_ATTR not in got.get("ship_attrs", {})


def test_an_unpublished_skill_is_ignored():
    rows = _ship_self_skill_rows([(9506, 7106, ATTR_MAX_VELOCITY, 9515)], value=100.0)
    for r in rows:
        if isinstance(r, SDEType):
            r.published = False
    got = _stats(None, skills=_skills(), rows_extra=rows)
    assert got["max_velocity"] == pytest.approx(BARE_SPEED * 1.25, abs=0.05)


def test_a_negative_bonus_lowers_the_attribute():
    # Capacitor Systems Operation: -5% recharge per level, -25% at V.
    skills = _skills([(RECHARGE, 7103, ATTR_CAP_RECHARGE, ATTR_RECHARGE_SRC)])
    rows = _seed_rows()
    rows += _ship_self_skill_rows(skills)
    for r in rows:   # flip the invented recharge skill's source value to -5
        if getattr(r, "type_id", None) == RECHARGE and getattr(r, "attribute_id", None) == ATTR_RECHARGE_SRC:
            r.value = -5.0

    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, [])
    base = _Slice().run(_calc, rows=_seed_rows())
    got = _Slice().run(_calc, rows=rows)
    assert got["cap_recharge"] == pytest.approx(base["cap_recharge"] * 0.75, abs=0.05)
