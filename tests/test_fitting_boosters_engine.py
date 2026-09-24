"""Engine half of combat boosters (T-049): app/fitting/boosters.py.

Built on the SDE slice in tests/test_fitting_engine.py — the same hull,
turret, ammo, tracking computer and ancillary repairer — plus five boosters
shaped like real ones (effect IDs are the real ones where the SDE has them):

  Blue Pill   primary 4951 shield boost amount +30% (by skill Shield Operation)
              side    2737 shield capacity -30%      (ship attribute)
              side    2739 turret optimal range -30% (by skill Gunnery)
  Drop        primary 2847 turret tracking +37.5%
              side    8800 a fixture-only capacitor penalty, to prove a side
                      effect the label table does not know still gets a name
  Pyrolancea  primary 584  turret damage +3%
  Crucible    primary 2851 missile damage x1.06, through the character's
                      missileDamageMultiplier — the one ItemModifier/charID
                      row the engine models
  Exile       primary 5364 armor repair amount +20%
              side    2791 missile explosion radius +30%, whose SDE row
                      filters on Acceleration Control and is redirected to
                      Missile Launcher Operation (see boosters.py)

Exile shares Blue Pill's slot (boosterness 1) so the one-per-slot rule can be
shown. The turret, shield booster, repairer, launcher and missile carry the
skill requirements those filters look for.
"""
import math
import pathlib

import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEModuleSlot, SDEType,
    SDETypeDogmaAttribute, SDETypeEffect, SDETypeSkillReq,
)
from app.fitting.boosters import apply_booster_bonuses, get_booster_info
from app.fitting.constants import (
    ATTR_ARMOR_DAMAGE_AMOUNT, ATTR_CAPACITOR, ATTR_CAPACITOR_NEED,
    ATTR_CHARGE_GROUP_1, ATTR_CPU, ATTR_DAMAGE_MULTIPLIER, ATTR_DURATION,
    ATTR_KINETIC_DAMAGE, ATTR_MISSILE_DAMAGE_MULTIPLIER,
    ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS, ATTR_OPTIMAL_RANGE, ATTR_POWER,
    ATTR_RATE_OF_FIRE, ATTR_SHIELD_BONUS, ATTR_SHIELD_HP, ATTR_TRACKING_SPEED,
)
from app.fitting.engine import STACKING_CONSTANT, calculate_fitting_stats
from tests.test_fitting_engine import (
    AMMO, ANCILLARY_REP, ATTR_TRACKING_SPEED_BONUS, OP_POST_PERCENT, SHIP,
    TRACKING_COMPUTER, TURRET, _Slice, _attrs, _high, _low, _mid,
    _needs_update_with, _seed_rows,
)

OP_PRE_MUL = 0

# ── dogma attribute IDs the slice needs beyond constants.py ────────────────
ATTR_BOOSTERNESS = 1087
ATTR_CHANCE_1, ATTR_CHANCE_2, ATTR_CHANCE_4, ATTR_CHANCE_5 = 1089, 1090, 1092, 1093
ATTR_SHIELD_BOOST_MULTIPLIER = 548      # Blue Pill's bonus
ATTR_SHIELD_CAPACITY_PENALTY = 1143
ATTR_TURRET_OPTIMAL_PENALTY = 1144
ATTR_CAP_CAPACITY_PENALTY = 1150
ATTR_DAMAGE_MULTIPLIER_BONUS = 292      # Pyrolancea's bonus
ATTR_ARMOR_REP_BONUS = 895              # Exile's bonus
ATTR_MISSILE_AOE_CLOUD_PENALTY = 1149
ATTR_AOE_CLOUD_SIZE = 654

SKILL_GUNNERY = 3300
SKILL_MISSILE_LAUNCHER_OPERATION = 3319
SKILL_REPAIR_SYSTEMS = 3393
SKILL_SHIELD_OPERATION = 3416
SKILL_ACCELERATION_CONTROL = 3452

# ── fixture-local type IDs ──────────────────────────────────────────────────
BLUE_PILL = 9301
DROP = 9302
PYROLANCEA = 9303
CRUCIBLE = 9304
EXILE = 9305
SHIELD_BOOSTER = 9310
LAUNCHER = 9311
MISSILE = 9312
GROUP_BOOSTER = 303       # real
GROUP_MISSILE = 9505

# ── effect IDs (real unless noted) ──────────────────────────────────────────
EFF_SHIELD_BOOST_BOOSTER = 4951
EFF_SHIELD_CAPACITY_PENALTY = 2737
EFF_TURRET_OPTIMAL_PENALTY = 2739
EFF_TRACKING_BOOSTER = 2847
EFF_CAP_PENALTY_FIXTURE = 8800      # fixture-only, unknown to the label table
EFF_SURGICAL_STRIKE_BOOSTER = 584
EFF_MISSILE_DMG_CHAR = 2851
EFF_ARMOR_REP_BOOSTER = 5364
EFF_MISSILE_CLOUD_PENALTY = 2791

SHIELD_BOOST_AMOUNT = 100.0
SHIELD_BOOST_CYCLE_MS = 4000.0
LAUNCHER_ROF_MS = 5000.0
MISSILE_DAMAGE = 50.0
TURRET_OPTIMAL = 10_000.0


def _rows():
    rows = _seed_rows()

    rows += [
        SDEDogmaAttribute(attribute_id=a, attribute_name=n, display_name=d, stackable=s)
        for a, n, d, s in [
            (ATTR_BOOSTERNESS, "boosterness", None, True),
            (ATTR_CHANCE_1, "boosterEffectChance1", None, True),
            (ATTR_CHANCE_2, "boosterEffectChance2", None, True),
            (ATTR_CHANCE_4, "boosterEffectChance4", None, True),
            (ATTR_CHANCE_5, "boosterEffectChance5", None, True),
            (ATTR_SHIELD_BONUS, "shieldBonus", "Shield Bonus", False),
            (ATTR_SHIELD_HP, "shieldCapacity", "Shield Capacity", True),
            (ATTR_OPTIMAL_RANGE, "maxRange", "Optimal Range", False),
            (ATTR_CAPACITOR, "capacitorCapacity", "Capacitor Capacity", True),
            (ATTR_DAMAGE_MULTIPLIER, "damageMultiplier", "Damage Modifier", False),
            (ATTR_ARMOR_DAMAGE_AMOUNT, "armorDamageAmount", "Armor Hitpoints Repaired", False),
            (ATTR_AOE_CLOUD_SIZE, "aoeCloudSize", "Explosion Radius", False),
            (ATTR_MISSILE_DAMAGE_MULTIPLIER, "missileDamageMultiplier", None, False),
        ]
    ]

    rows += [
        SDEType(type_id=t, type_name=n, group_id=GROUP_BOOSTER, category_id=20)
        for t, n in [
            (BLUE_PILL, "Test Blue Pill Booster"), (DROP, "Test Drop Booster"),
            (PYROLANCEA, "Test Pyrolancea Dose"), (CRUCIBLE, "Test Crucible Booster"),
            (EXILE, "Test Exile Booster"),
        ]
    ]
    rows += [
        SDEType(type_id=SHIELD_BOOSTER, type_name="Test Shield Booster",
                group_id=9410, category_id=7),
        SDEType(type_id=LAUNCHER, type_name="Test Missile Launcher",
                group_id=9411, category_id=7),
        SDEType(type_id=MISSILE, type_name="Test Missile", group_id=GROUP_MISSILE,
                category_id=8),
    ]

    # What the boosters' filters look for.
    rows += [
        SDETypeSkillReq(type_id=TURRET, skill_type_id=SKILL_GUNNERY, required_level=1),
        SDETypeSkillReq(type_id=SHIELD_BOOSTER, skill_type_id=SKILL_SHIELD_OPERATION,
                        required_level=1),
        SDETypeSkillReq(type_id=ANCILLARY_REP, skill_type_id=SKILL_REPAIR_SYSTEMS,
                        required_level=1),
        SDETypeSkillReq(type_id=LAUNCHER, skill_type_id=SKILL_MISSILE_LAUNCHER_OPERATION,
                        required_level=1),
        SDETypeSkillReq(type_id=MISSILE, skill_type_id=SKILL_MISSILE_LAUNCHER_OPERATION,
                        required_level=1),
        # The turret gets an optimal range for the Blue Pill penalty to hit.
        SDETypeDogmaAttribute(type_id=TURRET, attribute_id=ATTR_OPTIMAL_RANGE,
                              value=TURRET_OPTIMAL),
    ]

    # ── modules ─────────────────────────────────────────────────────────────
    rows += _attrs(SHIELD_BOOSTER, {
        ATTR_SHIELD_BONUS: SHIELD_BOOST_AMOUNT,
        ATTR_DURATION: SHIELD_BOOST_CYCLE_MS,
        ATTR_CAPACITOR_NEED: 40.0,
        ATTR_CPU: 30.0,
        ATTR_POWER: 10.0,
    })
    rows += _attrs(LAUNCHER, {
        ATTR_RATE_OF_FIRE: LAUNCHER_ROF_MS,
        ATTR_CHARGE_GROUP_1: float(GROUP_MISSILE),
        ATTR_CPU: 30.0,
        ATTR_POWER: 5.0,
    })
    rows += _attrs(MISSILE, {ATTR_KINETIC_DAMAGE: MISSILE_DAMAGE, ATTR_AOE_CLOUD_SIZE: 100.0})
    rows += [SDEModuleSlot(type_id=LAUNCHER, slot_type="high", is_turret=False,
                           is_launcher=True)]

    # ── Blue Pill: slot 1 ───────────────────────────────────────────────────
    rows += _attrs(BLUE_PILL, {
        ATTR_BOOSTERNESS: 1.0,
        ATTR_SHIELD_BOOST_MULTIPLIER: 30.0,
        ATTR_SHIELD_CAPACITY_PENALTY: -30.0,
        ATTR_TURRET_OPTIMAL_PENALTY: -30.0,
        ATTR_CHANCE_1: 0.4,
        ATTR_CHANCE_4: 0.3,
    })
    rows += [
        SDEEffect(effect_id=EFF_SHIELD_BOOST_BOOSTER,
                  effect_name="shieldBoostAmplifierPassiveBooster"),
        SDEEffect(effect_id=EFF_SHIELD_CAPACITY_PENALTY,
                  effect_name="boosterShieldCapacityPenalty",
                  fitting_usage_chance_attribute_id=ATTR_CHANCE_1),
        SDEEffect(effect_id=EFF_TURRET_OPTIMAL_PENALTY,
                  effect_name="boosterTurretOptimalRangePenalty",
                  fitting_usage_chance_attribute_id=ATTR_CHANCE_4),
        SDETypeEffect(type_id=BLUE_PILL, effect_id=EFF_SHIELD_BOOST_BOOSTER),
        SDETypeEffect(type_id=BLUE_PILL, effect_id=EFF_SHIELD_CAPACITY_PENALTY),
        SDETypeEffect(type_id=BLUE_PILL, effect_id=EFF_TURRET_OPTIMAL_PENALTY),
        SDEModifier(effect_id=EFF_SHIELD_BOOST_BOOSTER,
                    func="LocationRequiredSkillModifier", domain="shipID",
                    modified_attribute_id=ATTR_SHIELD_BONUS,
                    modifying_attribute_id=ATTR_SHIELD_BOOST_MULTIPLIER,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_SHIELD_OPERATION),
        SDEModifier(effect_id=EFF_SHIELD_CAPACITY_PENALTY,
                    func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_SHIELD_HP,
                    modifying_attribute_id=ATTR_SHIELD_CAPACITY_PENALTY,
                    operator=OP_POST_PERCENT),
        SDEModifier(effect_id=EFF_TURRET_OPTIMAL_PENALTY,
                    func="LocationRequiredSkillModifier", domain="shipID",
                    modified_attribute_id=ATTR_OPTIMAL_RANGE,
                    modifying_attribute_id=ATTR_TURRET_OPTIMAL_PENALTY,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_GUNNERY),
    ]

    # ── Drop: slot 2 ────────────────────────────────────────────────────────
    rows += _attrs(DROP, {
        ATTR_BOOSTERNESS: 2.0,
        ATTR_TRACKING_SPEED_BONUS: 37.5,
        ATTR_CAP_CAPACITY_PENALTY: -25.0,
        ATTR_CHANCE_2: 0.4,
    })
    rows += [
        SDEEffect(effect_id=EFF_TRACKING_BOOSTER,
                  effect_name="trackingSpeedBonusPassiveRequiringGunneryTrackingSpeedBonus"),
        SDEEffect(effect_id=EFF_CAP_PENALTY_FIXTURE,
                  effect_name="boosterCapacitorPenaltyFixture",
                  fitting_usage_chance_attribute_id=ATTR_CHANCE_2),
        SDETypeEffect(type_id=DROP, effect_id=EFF_TRACKING_BOOSTER),
        SDETypeEffect(type_id=DROP, effect_id=EFF_CAP_PENALTY_FIXTURE),
        SDEModifier(effect_id=EFF_TRACKING_BOOSTER,
                    func="LocationRequiredSkillModifier", domain="shipID",
                    modified_attribute_id=ATTR_TRACKING_SPEED,
                    modifying_attribute_id=ATTR_TRACKING_SPEED_BONUS,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_GUNNERY),
        SDEModifier(effect_id=EFF_CAP_PENALTY_FIXTURE,
                    func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_CAPACITOR,
                    modifying_attribute_id=ATTR_CAP_CAPACITY_PENALTY,
                    operator=OP_POST_PERCENT),
    ]

    # ── Pyrolancea: slot 11 ─────────────────────────────────────────────────
    rows += _attrs(PYROLANCEA, {ATTR_BOOSTERNESS: 11.0, ATTR_DAMAGE_MULTIPLIER_BONUS: 3.0})
    rows += [
        SDEEffect(effect_id=EFF_SURGICAL_STRIKE_BOOSTER, effect_name="surgicalStrikeBooster"),
        SDETypeEffect(type_id=PYROLANCEA, effect_id=EFF_SURGICAL_STRIKE_BOOSTER),
        SDEModifier(effect_id=EFF_SURGICAL_STRIKE_BOOSTER,
                    func="LocationRequiredSkillModifier", domain="shipID",
                    modified_attribute_id=ATTR_DAMAGE_MULTIPLIER,
                    modifying_attribute_id=ATTR_DAMAGE_MULTIPLIER_BONUS,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_GUNNERY),
    ]

    # ── Crucible: slot 12 ───────────────────────────────────────────────────
    rows += _attrs(CRUCIBLE, {ATTR_BOOSTERNESS: 12.0, ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS: 1.06})
    rows += [
        SDEEffect(effect_id=EFF_MISSILE_DMG_CHAR, effect_name="missileDMGBonusPassive"),
        SDETypeEffect(type_id=CRUCIBLE, effect_id=EFF_MISSILE_DMG_CHAR),
        SDEModifier(effect_id=EFF_MISSILE_DMG_CHAR,
                    func="ItemModifier", domain="charID",
                    modified_attribute_id=ATTR_MISSILE_DAMAGE_MULTIPLIER,
                    modifying_attribute_id=ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS,
                    operator=OP_PRE_MUL),
    ]

    # ── Exile: slot 1, like Blue Pill ───────────────────────────────────────
    rows += _attrs(EXILE, {
        ATTR_BOOSTERNESS: 1.0,
        ATTR_ARMOR_REP_BONUS: 20.0,
        ATTR_MISSILE_AOE_CLOUD_PENALTY: 30.0,
        ATTR_CHANCE_5: 0.2,
    })
    rows += [
        SDEEffect(effect_id=EFF_ARMOR_REP_BOOSTER,
                  effect_name="armorAllRepairSystemsAmountBonusPassive"),
        SDEEffect(effect_id=EFF_MISSILE_CLOUD_PENALTY,
                  effect_name="boosterMissileExplosionCloudPenaltyFixed",
                  fitting_usage_chance_attribute_id=ATTR_CHANCE_5),
        SDETypeEffect(type_id=EXILE, effect_id=EFF_ARMOR_REP_BOOSTER),
        SDETypeEffect(type_id=EXILE, effect_id=EFF_MISSILE_CLOUD_PENALTY),
        SDEModifier(effect_id=EFF_ARMOR_REP_BOOSTER,
                    func="LocationRequiredSkillModifier", domain="shipID",
                    modified_attribute_id=ATTR_ARMOR_DAMAGE_AMOUNT,
                    modifying_attribute_id=ATTR_ARMOR_REP_BONUS,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_REPAIR_SYSTEMS),
        # As in the live SDE: the filter names Acceleration Control.
        SDEModifier(effect_id=EFF_MISSILE_CLOUD_PENALTY,
                    func="OwnerRequiredSkillModifier", domain="charID",
                    modified_attribute_id=ATTR_AOE_CLOUD_SIZE,
                    modifying_attribute_id=ATTR_MISSILE_AOE_CLOUD_PENALTY,
                    operator=OP_POST_PERCENT,
                    filter_type="skill", filter_value=SKILL_ACCELERATION_CONTROL),
    ]
    return rows


def _stats(items, **kwargs):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items, **kwargs)
    return _Slice().run(_calc, rows=_rows())


def _booster(type_id, *side_effects):
    return {"type_id": type_id, "side_effects": list(side_effects)}


SHIELD_BOOST_FIT = [_mid(SHIELD_BOOSTER)]
GUN_FIT = [_high(TURRET, charge_type_id=AMMO)]


# ════════════════════════════════════════════════════════════════════════════
# Applying boosters
# ════════════════════════════════════════════════════════════════════════════

def test_primary_effect_raises_the_shield_boost_amount():
    base = _stats(SHIELD_BOOST_FIT)["shield_rep_rate"]
    boosted = _stats(SHIELD_BOOST_FIT, boosters=[_booster(BLUE_PILL)])["shield_rep_rate"]
    assert base == pytest.approx(SHIELD_BOOST_AMOUNT / (SHIELD_BOOST_CYCLE_MS / 1000), abs=0.05)
    assert boosted == pytest.approx(base * 1.30, abs=0.05)


def test_side_effect_applies_only_when_switched_on():
    off = _stats(GUN_FIT, boosters=[_booster(BLUE_PILL)])
    on = _stats(GUN_FIT, boosters=[_booster(BLUE_PILL, EFF_SHIELD_CAPACITY_PENALTY)])

    assert on["shield_hp"] == pytest.approx(off["shield_hp"] * 0.70, abs=0.5)
    # The other side effect stays off: nothing was said about it.
    assert on["weapon_optimal_range"] == off["weapon_optimal_range"] == TURRET_OPTIMAL

    both = _stats(GUN_FIT, boosters=[
        _booster(BLUE_PILL, EFF_SHIELD_CAPACITY_PENALTY, EFF_TURRET_OPTIMAL_PENALTY),
    ])
    assert both["weapon_optimal_range"] == pytest.approx(TURRET_OPTIMAL * 0.70, abs=0.5)


def test_effect_ids_that_are_not_this_boosters_side_effects_are_ignored():
    """A primary effect listed as a side effect is not applied twice, and an
    effect from another booster — or from nowhere — does nothing."""
    plain = _stats(SHIELD_BOOST_FIT, boosters=[_booster(BLUE_PILL)])
    crafted = _stats(SHIELD_BOOST_FIT, boosters=[
        _booster(BLUE_PILL, EFF_SHIELD_BOOST_BOOSTER, EFF_TRACKING_BOOSTER, 4242424),
    ])
    assert crafted["shield_rep_rate"] == plain["shield_rep_rate"]
    assert crafted["shield_hp"] == plain["shield_hp"]


def test_non_boosters_and_a_second_booster_in_a_taken_slot_are_ignored():
    """The tracking computer carries a modifier the dispatcher could apply;
    named as a booster it must not be. Exile shares Blue Pill's slot, so
    with Blue Pill listed first Exile's repair bonus never lands."""
    fit = GUN_FIT + [_low(ANCILLARY_REP)]
    base = _stats(fit)
    stats = _stats(fit, boosters=[
        _booster(TRACKING_COMPUTER), _booster(BLUE_PILL), _booster(EXILE),
        "junk", {"type_id": "nope"},
    ])
    assert stats["weapon_tracking"] == base["weapon_tracking"]
    assert stats["armor_rep_rate"] == base["armor_rep_rate"]
    # Blue Pill itself did land (its optimal-range penalty is off, its
    # shield bonus has nothing to boost here, so look at the slot rule the
    # other way round: Exile first, then Blue Pill is the one dropped).
    swapped = _stats(fit, boosters=[
        _booster(EXILE), _booster(BLUE_PILL, EFF_TURRET_OPTIMAL_PENALTY),
    ])
    assert swapped["armor_rep_rate"] == pytest.approx(base["armor_rep_rate"] * 1.20, abs=0.05)
    assert swapped["weapon_optimal_range"] == TURRET_OPTIMAL


def test_booster_bonus_sits_outside_the_module_stacking_chain():
    """Two tracking computers penalise each other; the Drop booster's +37.5%
    multiplies the penalised product untouched, as dogma exempts implant-
    category sources and Pyfa's booster handlers never pass
    stackingPenalties (see engine._apply_character_modifiers)."""
    fit = GUN_FIT + [_mid(TRACKING_COMPUTER, quantity=2)]
    two_tc = _stats(fit)["weapon_tracking"]
    with_drop = _stats(fit, boosters=[_booster(DROP)])["weapon_tracking"]

    second_penalty = math.exp(-1 / STACKING_CONSTANT)
    expected_two_tc = 0.1 * 1.2 * (1 + 0.2 * second_penalty)
    assert two_tc == pytest.approx(expected_two_tc, abs=1e-4)
    assert with_drop == pytest.approx(expected_two_tc * 1.375, abs=1e-4)


def test_module_bonus_from_a_booster_reaches_weapon_dps():
    """Regression: from v1.4.0 to v1.4.2 the per-item attribute dicts were
    copied before skills, implants and hull bonuses were applied to the
    type-level map, so none of them reached DPS. A booster goes through the
    same path; its +3% has to show up."""
    base = _stats(GUN_FIT)["weapon_dps"]
    boosted = _stats(GUN_FIT, boosters=[_booster(PYROLANCEA)])["weapon_dps"]
    assert boosted == pytest.approx(base * 1.03, abs=0.05)


def test_character_missile_damage_multiplier_scales_missile_damage():
    fit = [_high(LAUNCHER, charge_type_id=MISSILE)]
    base = _stats(fit)["weapon_dps"]
    boosted = _stats(fit, boosters=[_booster(CRUCIBLE)])["weapon_dps"]
    assert base == pytest.approx(MISSILE_DAMAGE / (LAUNCHER_ROF_MS / 1000), abs=0.05)
    assert boosted == pytest.approx(base * 1.06, abs=0.05)


def test_explosion_radius_side_effect_is_redirected_to_missiles():
    """The SDE row for effect 2791 filters on Acceleration Control, which no
    charge requires; boosters.py redirects it to Missile Launcher Operation
    as Pyfa does. Checked on the maps directly: the engine has no stat for
    explosion radius."""
    async def _apply(db):
        ship_attrs: dict = {}
        modules = {LAUNCHER: {ATTR_RATE_OF_FIRE: LAUNCHER_ROF_MS}}
        charges = {MISSILE: {ATTR_AOE_CLOUD_SIZE: 100.0, ATTR_KINETIC_DAMAGE: MISSILE_DAMAGE}}
        await apply_booster_bonuses(
            db, ship_attrs, modules, charges,
            [_booster(EXILE, EFF_MISSILE_CLOUD_PENALTY)],
        )
        return charges[MISSILE][ATTR_AOE_CLOUD_SIZE], charges[MISSILE][ATTR_KINETIC_DAMAGE]

    radius, damage = _Slice().run(_apply, rows=_rows())
    assert radius == pytest.approx(130.0)
    assert damage == MISSILE_DAMAGE


# ════════════════════════════════════════════════════════════════════════════
# get_booster_info
# ════════════════════════════════════════════════════════════════════════════

def test_get_booster_info_shape():
    async def _lookup(db):
        return await get_booster_info(db, [BLUE_PILL, TRACKING_COMPUTER, 424242])

    info = _Slice().run(_lookup, rows=_rows())
    assert info == {
        BLUE_PILL: {
            "type_id": BLUE_PILL,
            "name": "Test Blue Pill Booster",
            "slot": 1,
            "side_effects": [
                {"effect_id": EFF_SHIELD_CAPACITY_PENALTY,
                 "label": "Shield capacity -30%", "chance": 0.4},
                {"effect_id": EFF_TURRET_OPTIMAL_PENALTY,
                 "label": "Turret optimal range -30%", "chance": 0.3},
            ],
        },
    }


def test_get_booster_info_names_an_unknown_side_effect_from_its_attribute():
    async def _lookup(db):
        return await get_booster_info(db, [DROP, PYROLANCEA])

    info = _Slice().run(_lookup, rows=_rows())
    assert info[DROP]["slot"] == 2
    assert info[DROP]["side_effects"] == [
        {"effect_id": EFF_CAP_PENALTY_FIXTURE, "label": "Capacitor Capacity -25%",
         "chance": 0.4},
    ]
    assert info[PYROLANCEA] == {
        "type_id": PYROLANCEA, "name": "Test Pyrolancea Dose", "slot": 11,
        "side_effects": [],
    }


# ════════════════════════════════════════════════════════════════════════════
# SDE: the side-effect marker column and its self-heal
# ════════════════════════════════════════════════════════════════════════════

def test_needs_update_reimports_when_no_effect_marks_a_side_effect():
    """Names and categories are fine — this is the post-v1.4.0 shape, not the
    legacy-YAML one — but fittingUsageChanceAttributeID was dropped."""
    assert _needs_update_with([
        (5231, "modifyActiveArmorResonancePostPercent", 1),
        (3015, "overloadHardeningBonus", 5),
        (2737, "boosterShieldCapacityPenalty", 0),
    ]) is True


def test_needs_update_is_quiet_once_a_side_effect_row_carries_its_marker():
    assert _needs_update_with([
        (5231, "modifyActiveArmorResonancePostPercent", 1),
        (2737, "boosterShieldCapacityPenalty", 0, 1089),
    ]) is False


def test_startup_migration_adds_the_marker_column():
    import app.main
    source = pathlib.Path(app.main.__file__).read_text(encoding="utf-8")
    assert "ALTER TABLE sde_effects ADD COLUMN fitting_usage_chance_attribute_id INTEGER" in source
