"""Module active state in the stats engine (ISS-156).

A module has four states: offline, online, active, overheated. Which one a
module is in comes from `resolve_states` (tests/test_fitting_module_state.py
covers that on its own); this file checks what the ENGINE does with the answer:

* a category-1 (active) effect row applies only while that copy is running;
  categories 0 and 4 apply from online;
* a module that rests at online by default (cynosural field, cloak, micro jump
  drive, signature suppressor, Assault Damage Control) must not change the
  fit until the user switches it on, and a module that is on by default
  (hardener, propulsion, turret) must keep working with no `active` key;
* cap use, active tank, weapon fire and a reactive hardener's resists follow
  the same "running" test, and heat only counts at OVERHEATED.

Type ids, names and the fixture-local source attributes are invented; the
effect, attribute and operator ids are the real ones. The shared hull is the
one from tests/test_fitting_engine.py (velocity 200, signature 100, cap 1000).
"""
import pytest

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEModuleSlot, SDEType,
    SDETypeDogmaAttribute, SDETypeEffect,
)
from app.fitting.constants import (
    ATTR_ARMOR_EM_RESONANCE, ATTR_CAPACITOR_NEED, ATTR_DAMAGE_MULTIPLIER,
    ATTR_DURATION, ATTR_EM_RESIST_BONUS, ATTR_MAX_VELOCITY, ATTR_SIG_RADIUS,
    ATTR_SPEED_FACTOR, ATTR_MISSILE_DAMAGE_MULTIPLIER,
)
from app.fitting.engine import calculate_fitting_stats

from tests.test_fitting_engine import (
    AFTERBURNER, AMMO, ANCILLARY_REP, HARDENER, MWD, RAH, SHIP, TURRET,
    OP_POST_PERCENT, _Slice, _attrs, _high, _low, _mid, _seed_rows,
)

# Operators (real ids).
OP_PRE_MUL = 0
OP_POST_MUL = 4
OP_POST_ASSIGN = 7

# Real attribute ids.
ATTR_MAX_VELOCITY_MODIFIER = 306        # maxVelocityModifier, a cloak's speed multiplier
ATTR_SIG_BLOOM_PERCENT = 973            # an MJD's signature bloom, percent
ATTR_MAX_GROUP_ACTIVE = 763
ATTR_SCAN_RES = 564

# Invented types.
CYNO = 9701
CLOAK = 9702
SUPPRESSOR = 9703
MJD = 9704
MJFG = 9705
MJFG_CAPITAL = 9706
ASSAULT_DC = 9707
SIEGE = 9708
BCU = 9709
LAUNCHER_T = 9710
MISSILE_T = 9711
PLAIN_FOOTER = 9712          # a module with no activation effect at all
GROUP_CYNO = 9720
GROUP_LAUNCHER = 9721
GROUP_MISSILE = 9722

# Real effect ids (names in the comments), plus the activation effects of the
# seeded turret / repairer / hardener from tests/test_fitting_engine.py.
EFF_ONLINE = 16
EFF_CYNO = 2857            # cynosuralGeneration, category 1
EFF_CLOAK = 607            # cloaking, category 1
EFF_SUPPRESSOR_PASSIVE = 8108   # category 4: the always-on half
EFF_SUPPRESSOR_ACTIVE = 8109    # targetSpectrumBreakerBonus, category 1
EFF_MJD = 4921             # microJumpDrive, category 1, no modifier rows
EFF_MJFG = 6208            # microJumpPortalDrive, category 1, no modifier rows
EFF_MJFG_CAPITAL = 12126   # microJumpPortalDriveCapital, category 1
EFF_ASSAULT_DC = 7012      # moduleBonusAssaultDamageControl, category 1
EFF_ASSAULT_DC_PASSIVE = 7100   # fixture: the passive half that reads the resists
EFF_SIEGE = 6582           # siege-style activation, category 1
EFF_BCU = 660              # missileDMGBonus
EFF_LAUNCH = 9             # missileLaunching, category 2

# Fixture-local source attributes (not in the real SDE under these ids).
ATTR_SUPPRESS_PASSIVE = 9801     # 0.9
ATTR_SUPPRESS_ACTIVE = 9802      # 0.3
ATTR_DC_ACTIVE_BONUS = 9803      # -50: what the Assault DC assigns when active
ATTR_SIEGE_BONUS = 9804          # +50 percent turret damage
ATTR_BCU_BONUS = 9805            # 1.06

MJD_BLOOM = 150.0                # percent: x2.5


def _effect(effect_id, name, category):
    return SDEEffect(effect_id=effect_id, effect_name=name, effect_category=category)


def _link(type_id, *effect_ids):
    return [SDETypeEffect(type_id=type_id, effect_id=e) for e in effect_ids]


def _rows(group_limit=None):
    """The shared engine seed plus the modules this file exercises."""
    rows = _seed_rows()
    rows += [
        SDEDogmaAttribute(attribute_id=ATTR_MAX_VELOCITY_MODIFIER,
                          attribute_name="maxVelocityModifier", stackable=False),
        SDEDogmaAttribute(attribute_id=ATTR_DAMAGE_MULTIPLIER,
                          attribute_name="damageMultiplier", stackable=True),
        SDEDogmaAttribute(attribute_id=ATTR_MISSILE_DAMAGE_MULTIPLIER,
                          attribute_name="missileDamageMultiplier", stackable=True),
    ]

    # ── cynosural field generator: effect 2857 zeroes speed (speedFactor -100)
    rows += [SDEType(type_id=CYNO, type_name="Test Cynosural Field", group_id=GROUP_CYNO,
                     category_id=7)]
    rows += _attrs(CYNO, {ATTR_SPEED_FACTOR: -100.0})
    rows += [
        _effect(EFF_ONLINE, "online", 4), _effect(EFF_CYNO, "cynosuralGeneration", 1),
        *_link(CYNO, EFF_ONLINE, EFF_CYNO),
        SDEModifier(effect_id=EFF_CYNO, func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_MAX_VELOCITY,
                    modifying_attribute_id=ATTR_SPEED_FACTOR,
                    operator=OP_POST_PERCENT),
    ]
    if group_limit is not None:
        rows += [SDETypeDogmaAttribute(type_id=CYNO, attribute_id=ATTR_MAX_GROUP_ACTIVE,
                                       value=float(group_limit))]

    # ── cloaking device: effect 607, speed x maxVelocityModifier (0.25)
    rows += [SDEType(type_id=CLOAK, type_name="Test Cloaking Device", group_id=9723,
                     category_id=7)]
    rows += _attrs(CLOAK, {ATTR_MAX_VELOCITY_MODIFIER: 0.25})
    rows += [
        _effect(EFF_CLOAK, "cloaking", 1), *_link(CLOAK, EFF_ONLINE, EFF_CLOAK),
        SDEModifier(effect_id=EFF_CLOAK, func="ItemModifier", domain="shipID",
                    modified_attribute_id=ATTR_MAX_VELOCITY,
                    modifying_attribute_id=ATTR_MAX_VELOCITY_MODIFIER,
                    operator=OP_POST_MUL),
    ]

    # ── signature suppressor: a category-4 half (x0.9, always) and the
    #    activation half 8109 (category 1, x0.3)
    rows += [SDEType(type_id=SUPPRESSOR, type_name="Test Signature Suppressor",
                     group_id=9724, category_id=7)]
    rows += _attrs(SUPPRESSOR, {ATTR_SUPPRESS_PASSIVE: 0.9, ATTR_SUPPRESS_ACTIVE: 0.3})
    rows += [
        _effect(EFF_SUPPRESSOR_PASSIVE, "signatureSuppressorPassive", 4),
        _effect(EFF_SUPPRESSOR_ACTIVE, "targetSpectrumBreakerBonus", 1),
        *_link(SUPPRESSOR, EFF_ONLINE, EFF_SUPPRESSOR_PASSIVE, EFF_SUPPRESSOR_ACTIVE),
        SDEModifier(effect_id=EFF_SUPPRESSOR_PASSIVE, func="ItemModifier",
                    domain="shipID", modified_attribute_id=ATTR_SIG_RADIUS,
                    modifying_attribute_id=ATTR_SUPPRESS_PASSIVE, operator=OP_POST_MUL),
        SDEModifier(effect_id=EFF_SUPPRESSOR_ACTIVE, func="ItemModifier",
                    domain="shipID", modified_attribute_id=ATTR_SIG_RADIUS,
                    modifying_attribute_id=ATTR_SUPPRESS_ACTIVE, operator=OP_POST_MUL),
    ]

    # ── micro jump drives: no modifier rows; the engine adds the bloom
    for group_id, (type_id, name, effect_id, ename) in enumerate((
        (MJD, "Test Micro Jump Drive", EFF_MJD, "microJumpDrive"),
        (MJFG, "Test Micro Jump Field Generator", EFF_MJFG, "microJumpPortalDrive"),
        (MJFG_CAPITAL, "Test Capital Micro Jump Field Generator", EFF_MJFG_CAPITAL,
         "microJumpPortalDriveCapital"),
    ), start=9725):
        rows += [SDEType(type_id=type_id, type_name=name, group_id=group_id,
                         category_id=7)]
        rows += _attrs(type_id, {
            ATTR_SIG_BLOOM_PERCENT: MJD_BLOOM,
            ATTR_CAPACITOR_NEED: 800.0, ATTR_DURATION: 20_000.0,
        })
        rows += [_effect(effect_id, ename, 1), *_link(type_id, EFF_ONLINE, effect_id)]

    # ── Assault Damage Control: activation assigns the module's own resist
    #    bonus (postAssign onto itself); the passive half hands it to the hull
    rows += [SDEType(type_id=ASSAULT_DC, type_name="Test Assault Damage Control",
                     group_id=9730, category_id=7)]
    rows += _attrs(ASSAULT_DC, {ATTR_EM_RESIST_BONUS: 0.0, ATTR_DC_ACTIVE_BONUS: -50.0})
    rows += [
        _effect(EFF_ASSAULT_DC, "moduleBonusAssaultDamageControl", 1),
        _effect(EFF_ASSAULT_DC_PASSIVE, "testDamageControlPassive", 0),
        *_link(ASSAULT_DC, EFF_ONLINE, EFF_ASSAULT_DC, EFF_ASSAULT_DC_PASSIVE),
        SDEModifier(effect_id=EFF_ASSAULT_DC, func="ItemModifier", domain="itemID",
                    modified_attribute_id=ATTR_EM_RESIST_BONUS,
                    modifying_attribute_id=ATTR_DC_ACTIVE_BONUS,
                    operator=OP_POST_ASSIGN),
        SDEModifier(effect_id=EFF_ASSAULT_DC_PASSIVE, func="ItemModifier",
                    domain="shipID", modified_attribute_id=ATTR_ARMOR_EM_RESONANCE,
                    modifying_attribute_id=ATTR_EM_RESIST_BONUS,
                    operator=OP_POST_PERCENT),
    ]

    # ── siege-style module: a category-1 row that boosts turrets by group
    rows += [SDEType(type_id=SIEGE, type_name="Test Siege Module", group_id=9731,
                     category_id=7)]
    rows += _attrs(SIEGE, {ATTR_SIEGE_BONUS: 50.0})
    rows += [
        _effect(EFF_SIEGE, "siegeModeEffect6582", 1), *_link(SIEGE, EFF_ONLINE, EFF_SIEGE),
        SDEModifier(effect_id=EFF_SIEGE, func="LocationGroupModifier", domain="shipID",
                    modified_attribute_id=ATTR_DAMAGE_MULTIPLIER,
                    modifying_attribute_id=ATTR_SIEGE_BONUS,
                    operator=OP_POST_PERCENT, filter_type="group", filter_value=9403),
    ]

    # ── a ballistic-control-style module: charID missile damage multiplier
    rows += [
        SDEType(type_id=BCU, type_name="Test Ballistic Control", group_id=9732,
                category_id=7),
        SDEType(type_id=LAUNCHER_T, type_name="Test Missile Bay", group_id=GROUP_LAUNCHER,
                category_id=7),
        SDEType(type_id=MISSILE_T, type_name="Test Missile Round", group_id=GROUP_MISSILE,
                category_id=8),
    ]
    rows += _attrs(BCU, {ATTR_BCU_BONUS: 1.06})
    rows += _attrs(LAUNCHER_T, {51: 5000.0, 604: float(GROUP_MISSILE)})   # rate of fire, charge group
    rows += _attrs(MISSILE_T, {117: 50.0})   # kineticDamage
    rows += [
        _effect(EFF_BCU, "missileDMGBonus", 1), *_link(BCU, EFF_ONLINE, EFF_BCU),
        SDEModifier(effect_id=EFF_BCU, func="ItemModifier", domain="charID",
                    modified_attribute_id=ATTR_MISSILE_DAMAGE_MULTIPLIER,
                    modifying_attribute_id=ATTR_BCU_BONUS, operator=OP_PRE_MUL),
        _effect(EFF_LAUNCH, "missileLaunching", 2), *_link(LAUNCHER_T, EFF_LAUNCH),
        SDEModuleSlot(type_id=LAUNCHER_T, slot_type="high", is_turret=False,
                      is_launcher=True),
    ]

    # ── a module with no activation effect at all
    rows += [SDEType(type_id=PLAIN_FOOTER, type_name="Test Plain Module", group_id=9733,
                     category_id=7)]
    rows += _link(PLAIN_FOOTER, EFF_ONLINE)
    return rows


def _stats(items, group_limit=None, **kwargs):
    async def _calc(db):
        return await calculate_fitting_stats(db, SHIP, items, **kwargs)
    return _Slice().run(_calc, rows=_rows(group_limit))


BASE_SPEED = 200.0
BASE_SIG = 100.0


# ════════════════════════════════════════════════════════════════════════════
# Resting at online: the fit is unchanged until the user switches it on
# ════════════════════════════════════════════════════════════════════════════

def test_cynosural_field_keeps_full_speed_by_default_and_zeroes_it_when_active():
    """Effect 2857 is category 1, and pyfa rests a cyno at online. A Raven
    with one fitted must not read 0 m/s."""
    assert _stats([_high(CYNO)])["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.05)
    assert _stats([_high(CYNO, active=False)])["max_velocity"] == pytest.approx(
        BASE_SPEED, abs=0.05)
    assert _stats([_high(CYNO, active=True)])["max_velocity"] == pytest.approx(0.0, abs=0.05)


def test_cloak_cuts_speed_only_when_active():
    """Effect 607, maxVelocityModifier 0.25: 200 -> 50 while cloaked."""
    assert _stats([_mid(CLOAK)])["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.05)
    assert _stats([_mid(CLOAK, active=True)])["max_velocity"] == pytest.approx(50.0, abs=0.05)


def test_signature_suppressor_active_half_waits_for_activation():
    """8108 (category 4, x0.9) applies from online; 8109 (category 1, x0.3)
    only once active, and the two stack with a penalty:
    100 * 0.3 * (1 + (0.9 - 1) * 0.8691) = 27.4."""
    rested = _stats([_mid(SUPPRESSOR)])
    assert rested["sig_radius"] == pytest.approx(90.0, abs=0.05)
    active = _stats([_mid(SUPPRESSOR, active=True)])
    assert active["sig_radius"] == pytest.approx(27.4, abs=0.05)


def test_assault_damage_control_assigns_its_resists_only_when_active():
    """The module's own activation row (postAssign, operator 7) sets the resist
    bonus its passive effect hands to the hull: 0% resting, 50% active."""
    assert _stats([_low(ASSAULT_DC)])["armor_em_resist"] == pytest.approx(0.0, abs=0.05)
    assert _stats([_low(ASSAULT_DC, active=True)])["armor_em_resist"] == pytest.approx(
        50.0, abs=0.05)


def test_micro_jump_drive_rests_without_drain_and_blooms_unpenalised_when_active():
    """4921 rests at online: no cap drain, no bloom. Active it uses cap and
    blooms the signature by 150% (x2.5); two of them multiply (6.25), as the
    drive's bloom is not stacking-penalised."""
    rested = _stats([_mid(MJD)])
    assert rested["cap_drain_rate"] == 0
    assert rested["cap_stable"] is True
    assert rested["sig_radius"] == pytest.approx(BASE_SIG, abs=0.05)

    one = _stats([_mid(MJD, active=True)])
    assert one["sig_radius"] == pytest.approx(250.0, abs=0.05)
    assert one["cap_drain_rate"] > 0
    assert one["cap_stable"] is False

    two = _stats([_mid(MJD, active=True), _mid(MJD, active=True)])
    assert two["sig_radius"] == pytest.approx(625.0, abs=0.05)


def test_rack_module_quantity_is_clamped_to_the_slot_count():
    """A request can carry any `quantity` for a rack module; the per-copy
    loops must not run once per unit. 8 is the most slots one rack has, so a
    huge number counts as 8 (1.0 + 1.5 compounds 8 times) and returns."""
    huge = _mid(MJD, active=True)
    huge["quantity"] = 10 ** 9
    eight = _mid(MJD, active=True)
    eight["quantity"] = 8
    junk = _mid(MJD, active=True)
    junk["quantity"] = "many"
    assert (_stats([huge])["sig_radius"]
            == pytest.approx(_stats([eight])["sig_radius"], rel=1e-9))
    assert _stats([junk])["sig_radius"] == pytest.approx(250.0, abs=0.05)


def test_micro_jump_field_generator_bloom_is_stacking_penalised():
    """6208 goes through the module collectors: two blooms of x2.5 become
    2.5 * (1 + 1.5 * 0.8691) = 5.759, not 6.25."""
    rested = _stats([_mid(MJFG)])
    assert rested["sig_radius"] == pytest.approx(BASE_SIG, abs=0.05)
    two = _stats([_mid(MJFG, active=True), _mid(MJFG, active=True)])
    assert two["sig_radius"] == pytest.approx(575.9, abs=0.1)
    assert two["sig_radius"] < 625.0


def test_capital_micro_jump_field_generator_is_active_by_default():
    """12126 is deliberately NOT a rests-at-online effect: pyfa runs it."""
    stats = _stats([_mid(MJFG_CAPITAL)])
    assert stats["sig_radius"] == pytest.approx(250.0, abs=0.05)
    assert stats["module_states"] == [1]
    held = _stats([_mid(MJFG_CAPITAL, active=False)])
    assert held["sig_radius"] == pytest.approx(BASE_SIG, abs=0.05)


# ════════════════════════════════════════════════════════════════════════════
# Running by default: existing fits keep working with no `active` key
# ════════════════════════════════════════════════════════════════════════════

def test_hardener_and_turret_apply_by_default():
    hardener = _stats([_low(HARDENER)])
    assert hardener["armor_em_resist"] == pytest.approx(30.0, abs=0.05)
    assert hardener["module_states"] == [1]
    turret = _stats([_high(TURRET, charge_type_id=AMMO)])
    assert turret["weapon_dps"] == pytest.approx(10.0, abs=0.05)
    assert turret["module_states"] == [1]


def test_hardener_held_at_online_gives_no_resist():
    stats = _stats([_low(HARDENER, active=False)])
    assert stats["armor_em_resist"] == pytest.approx(0.0, abs=0.05)
    assert stats["module_states"] == [0]


def test_reactive_hardener_held_at_online_adds_no_resonance_but_is_still_fitted():
    running = _stats([_low(RAH)])
    held = _stats([_low(RAH, active=False)])
    assert running["armor_em_resist"] == pytest.approx(15.0, abs=0.05)
    assert held["armor_em_resist"] == pytest.approx(0.0, abs=0.05)
    assert held["rah_fitted"] is True


def test_turret_held_at_online_deals_no_damage_but_keeps_its_hardpoint():
    stats = _stats([_high(TURRET, charge_type_id=AMMO, active=False)])
    assert stats["weapon_dps"] == 0
    assert stats["weapon_volley"] == 0
    assert stats["turrets_used"] == 1
    assert stats["cpu_used"] > 0   # still online, still drawing power


def test_repairer_held_at_online_repairs_nothing_and_uses_no_cap():
    running = _stats([_low(ANCILLARY_REP)])
    held = _stats([_low(ANCILLARY_REP, active=False)])
    assert running["armor_rep_rate"] > 0
    assert running["cap_drain_rate"] > 0
    assert held["armor_rep_rate"] == 0
    assert held["cap_drain_rate"] == 0
    assert held["cpu_used"] == running["cpu_used"]


def test_a_module_with_no_activation_effect_cannot_be_run_or_held():
    for item in (_mid(PLAIN_FOOTER), _mid(PLAIN_FOOTER, active=True)):
        assert _stats([item])["module_states"] == [0]


# ════════════════════════════════════════════════════════════════════════════
# Cross-module sources (siege, guidance computers) and the character multiplier
# ════════════════════════════════════════════════════════════════════════════

def test_siege_style_module_boosts_turrets_only_while_running():
    gun = _high(TURRET, charge_type_id=AMMO)
    default = _stats([gun, _high(SIEGE)])
    assert default["weapon_dps"] == pytest.approx(15.0, abs=0.05)   # damage x1.5
    held = _stats([gun, _high(SIEGE, active=False)])
    assert held["weapon_dps"] == pytest.approx(10.0, abs=0.05)


def test_cross_module_gate_is_decided_per_copy():
    """Two sieges, one held: only the running copy contributes."""
    gun = _high(TURRET, charge_type_id=AMMO)
    one_of_two = _stats([gun, _high(SIEGE), _high(SIEGE, active=False)])
    one = _stats([gun, _high(SIEGE)])
    assert one_of_two["weapon_dps"] == pytest.approx(one["weapon_dps"], abs=0.05)


def test_character_missile_multiplier_needs_a_running_source():
    bay = _high(LAUNCHER_T, charge_type_id=MISSILE_T)
    default = _stats([bay, _low(BCU)])
    held = _stats([bay, _low(BCU, active=False)])
    bare = _stats([bay])
    assert bare["weapon_dps"] == pytest.approx(10.0, abs=0.05)
    assert default["weapon_dps"] == pytest.approx(10.6, abs=0.05)
    assert held["weapon_dps"] == pytest.approx(10.0, abs=0.05)


# ════════════════════════════════════════════════════════════════════════════
# Heat, offline, and the response
# ════════════════════════════════════════════════════════════════════════════

def test_heat_applies_only_at_overheated():
    gun = dict(charge_type_id=AMMO)
    cool = _stats([_high(TURRET, **gun)])
    hot = _stats([_high(TURRET, overheated=True, **gun)])
    assert hot["module_states"] == [2]
    assert hot["weapon_dps"] > cool["weapon_dps"]


def test_overheated_implies_active_even_when_active_is_false():
    gun = dict(charge_type_id=AMMO)
    stats = _stats([_high(TURRET, overheated=True, active=False, **gun)])
    assert stats["module_states"] == [2]
    assert stats["weapon_dps"] > 10.0


def test_overheated_flag_on_a_module_that_cannot_overheat_changes_nothing():
    """The cyno has no category-5 effect: heat is ignored and it stays at
    its default (online), so speed is unchanged."""
    stats = _stats([_high(CYNO, overheated=True)])
    assert stats["module_states"] == [0]
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.05)


def test_offline_excludes_everything_even_with_active_true():
    off = dict(online=False, active=True)
    stats = _stats([
        _high(CYNO, **off), _mid(CLOAK, **off), _mid(MJD, **off),
        _low(HARDENER, **off), _low(ASSAULT_DC, **off),
        _high(TURRET, charge_type_id=AMMO, **off),
    ])
    assert stats["module_states"] == [-1] * 6
    assert stats["max_velocity"] == pytest.approx(BASE_SPEED, abs=0.05)
    assert stats["sig_radius"] == pytest.approx(BASE_SIG, abs=0.05)
    assert stats["armor_em_resist"] == pytest.approx(0.0, abs=0.05)
    assert stats["weapon_dps"] == 0
    assert stats["cap_drain_rate"] == 0


def test_module_states_covers_every_item_and_marks_drones_none():
    stats = _stats([
        _low(HARDENER), {"type_id": 9999, "slot": "drone", "quantity": 2},
        _high(CYNO),
    ])
    assert stats["module_states"] == [1, None, 0]


def test_input_items_are_not_mutated():
    item = _high(CYNO)
    before = dict(item)
    _stats([item])
    assert item == before   # the private _state lives on a copy


# ════════════════════════════════════════════════════════════════════════════
# Group limit
# ════════════════════════════════════════════════════════════════════════════

def test_group_limit_rests_the_extras_and_warns():
    stats = _stats([_high(CYNO, active=True), _high(CYNO, active=True)], group_limit=1)
    assert stats["module_states"] == [1, 0]
    assert any("at most 1" in w.lower() for w in stats["warnings"])


def test_max_group_active_zero_means_no_limit():
    stats = _stats([_high(CYNO, active=True), _high(CYNO, active=True)], group_limit=0)
    assert stats["module_states"] == [1, 1]
    assert not any("at most" in w.lower() for w in stats["warnings"])


def test_explicit_choice_keeps_its_group_slot_over_a_default():
    """Both propulsion modules default to active; the one the user chose wins
    the single slot whatever its position (see test_fitting_propulsion)."""
    async def _calc(db):
        return await calculate_fitting_stats(
            db, SHIP, [_mid(AFTERBURNER), _mid(MWD, active=True)])
    rows = _seed_rows() + [
        SDETypeDogmaAttribute(type_id=t, attribute_id=ATTR_MAX_GROUP_ACTIVE, value=1.0)
        for t in (AFTERBURNER, MWD)
    ]
    stats = _Slice().run(_calc, rows=rows)
    assert stats["module_states"] == [0, 1]
