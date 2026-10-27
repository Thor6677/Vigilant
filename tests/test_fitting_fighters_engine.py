"""Fighter hangar, launch accounting and DPS in the fitting engine (ISS-136).

One item per squadron: ``{"slot": "fighter", "quantity": <squadron size>,
"online": <launched>}``. The engine counts a squadron only when it is online,
of a launchable class, and a tube plus a slot of its class are left (items are
walked in order); anything else is a reserve that costs hangar volume and
fires nothing. Fighter DPS is the primary attack (effect 6465) plus the
missile ability (effect 6431), both sustained, as pyfa counts them by default;
the engine keeps the two apart in its output.

Same approach as ``tests/test_fitting_engine.py``: a seeded SDE slice in a temp
SQLite file, real attribute and effect IDs where the mechanic depends on them
(fighter attrs 2212-2233 and 2130-2182, ship attrs 2055 and 2216-2219, effects
6465 and 6431, skill 24613), fixture-local type IDs and "Test ..." names. The
numbers are chosen so every expected value can be checked by hand.
"""
import math

from app.db.sde_models import (
    SDEDogmaAttribute, SDEEffect, SDEModifier, SDEType, SDETypeBonus,
    SDETypeEffect, SDETypeSkillReq,
)
from app.fitting.constants import (
    ATTR_FIGHTER_ATTACK_DURATION, ATTR_FIGHTER_ATTACK_EM, ATTR_FIGHTER_ATTACK_MULT,
    ATTR_FIGHTER_CAPACITY, ATTR_FIGHTER_IS_HEAVY,
    ATTR_FIGHTER_IS_LIGHT, ATTR_FIGHTER_IS_STANDUP_LIGHT, ATTR_FIGHTER_IS_SUPPORT,
    ATTR_FIGHTER_LIGHT_SLOTS, ATTR_FIGHTER_MISSILE_DURATION, ATTR_FIGHTER_MISSILE_EM,
    ATTR_FIGHTER_MISSILE_KINETIC, ATTR_FIGHTER_MISSILE_MULT,
    ATTR_FIGHTER_SQUADRON_MAX_SIZE, ATTR_FIGHTER_SUPPORT_SLOTS, ATTR_FIGHTER_TUBES,
    EFFECT_FIGHTER_ATTACK_MISSILE, EFFECT_FIGHTER_MISSILES, ATTR_HP, ATTR_MASS,
    SKILL_FIGHTER_HANGAR_MANAGEMENT,
)
from app.fitting.engine import calculate_fitting_stats
from tests.test_fitting_engine import (
    OP_POST_PERCENT, SHIP, _Slice, _attrs, _low, _seed_rows,
)

OP_POST_MUL = 4

# ── fixture-local IDs ───────────────────────────────────────────────────────
CARRIER, HULL_LVL, HULL_ROLE = 9101, 9102, 9103      # 4 tubes, 3 light, 2 support
PLAIN, SALVO_ATK, SUPERIORITY = 9201, 9202, 9203
SUPPORT_F, STANDUP, HEAVY_F = 9204, 9205, 9206
ATTACK_FX_ONLY, MISSILE_FX_ONLY = 9207, 9208   # attributes of BOTH blocks, one effect
SIZELESS = 9209                                  # a light attacker with no squadron size (2215)
DDA, FSU = 9301, 9302
SK_FIGHTERS, SK_INTERFACING, SK_SPEC, SK_HULL = 9601, 9602, 9603, 9604
GROUP_FIGHTER = 9700

# Source attributes the fixture's skills, hull and modules read from.
A_SKILL_5, A_SKILL_10, A_SKILL_2 = 9801, 9802, 9803
A_HULL_LVL, A_HULL_ROLE, A_DDA = 9811, 9812, 9821
A_FSU = 2337                                          # real: Fighter Support Unit cycle bonus

EFF_SK_FIGHTERS, EFF_SK_INTERFACING, EFF_SK_SPEC = 8101, 8102, 8103
EFF_HULL_LVL, EFF_HULL_ROLE, EFF_DDA, EFF_FSU = 8201, 8202, 8301, 8302
EFF_SK_HULL = 8203

ALL_V = {SK_FIGHTERS: 5, SK_INTERFACING: 5, SK_SPEC: 5}
NO_SKILLS = {}   # no skill trained: every skill and hull per-level bonus is 0

# All V skill multiplier on both attack blocks: Fighters 5%/lvl, Drone
# Interfacing 10%/lvl, a racial specialization 2%/lvl (postPercent, one after
# the other): 1.25 x 1.5 x 1.1 = 2.0625.


def _fighter_rows():
    rows = []
    # 2226 and 2130 (like 2233 and 2182) are not stackable in the real SDE, so
    # two damage amplifiers penalise each other.
    rows += [
        SDEDogmaAttribute(attribute_id=ATTR_FIGHTER_ATTACK_MULT,
                          attribute_name="fighterAbilityAttackMissileDamageMultiplier",
                          stackable=False),
        SDEDogmaAttribute(attribute_id=ATTR_FIGHTER_MISSILE_MULT,
                          attribute_name="fighterAbilityMissilesDamageMultiplier",
                          stackable=False),
        SDEDogmaAttribute(attribute_id=A_HULL_LVL, attribute_name="shipBonusTestG1",
                          stackable=True),
        SDEDogmaAttribute(attribute_id=A_HULL_ROLE, attribute_name="shipBonusRole4",
                          stackable=True),
    ]

    # ── hulls: 4 tubes, 3 light, 2 support, no heavy attr, 65000 m3 ─────────
    carrier_attrs = {
        ATTR_FIGHTER_TUBES: 4.0, ATTR_FIGHTER_LIGHT_SLOTS: 3.0,
        ATTR_FIGHTER_SUPPORT_SLOTS: 2.0, ATTR_FIGHTER_CAPACITY: 65000.0,
        ATTR_HP: 1000.0, ATTR_MASS: 1_000_000.0,
    }
    for tid, name, extra in (
        (CARRIER, "Test Carrier", {}),
        (HULL_LVL, "Test Carrier Per-Level", {A_HULL_LVL: 5.0}),   # +5% per level
        (HULL_ROLE, "Test Carrier Role", {A_HULL_ROLE: 100.0}),    # +100%, no skill
    ):
        rows.append(SDEType(type_id=tid, type_name=name, group_id=9710,
                            category_id=6, mass=1_000_000.0, volume=100_000.0))
        rows += _attrs(tid, {**carrier_attrs, **extra})

    # ── skills: each one raises BOTH attack blocks of fighters requiring it ──
    for skill, per_level, src, eff in (
        (SK_FIGHTERS, 5.0, A_SKILL_5, EFF_SK_FIGHTERS),
        (SK_INTERFACING, 10.0, A_SKILL_10, EFF_SK_INTERFACING),
        (SK_SPEC, 2.0, A_SKILL_2, EFF_SK_SPEC),
    ):
        rows.append(SDEType(type_id=skill, type_name=f"Test Skill {skill}",
                            group_id=9711, category_id=16))
        rows += _attrs(skill, {src: per_level})
        rows += [SDEEffect(effect_id=eff, effect_name=f"testSkill{skill}", effect_category=0),
                 SDETypeEffect(type_id=skill, effect_id=eff)]
        rows += [
            SDEModifier(effect_id=eff, func="OwnerRequiredSkillModifier", domain="charID",
                        modified_attribute_id=target, modifying_attribute_id=src,
                        operator=OP_POST_PERCENT, filter_type="skill",
                        filter_value=SK_FIGHTERS)
            for target in (ATTR_FIGHTER_ATTACK_MULT, ATTR_FIGHTER_MISSILE_MULT)
        ]

    # ── hull bonuses to fighter damage (reads the ship's own attribute) ─────
    for hull, eff, src in ((HULL_LVL, EFF_HULL_LVL, A_HULL_LVL),
                           (HULL_ROLE, EFF_HULL_ROLE, A_HULL_ROLE)):
        rows += [SDEEffect(effect_id=eff, effect_name=f"testHull{hull}", effect_category=0),
                 SDETypeEffect(type_id=hull, effect_id=eff)]
        rows += [SDEModifier(effect_id=eff, func="OwnerRequiredSkillModifier",
                             domain="charID", modified_attribute_id=target,
                             modifying_attribute_id=src, operator=OP_POST_PERCENT,
                             filter_type="skill", filter_value=SK_FIGHTERS)
                 for target in (ATTR_FIGHTER_ATTACK_MULT, ATTR_FIGHTER_MISSILE_MULT)]
    rows.append(SDETypeBonus(type_id=HULL_LVL, bonus_value=5.0, is_role_bonus=False,
                             scaling_skill_id=SK_HULL))
    # SK_HULL feeds skillLevel (280) into the hull's bonus: that link is what
    # makes it per level, and SK_HULL's level is the one it multiplies by.
    rows += [SDEType(type_id=SK_HULL, type_name="Test Hull Class Skill",
                     group_id=9711, category_id=16),
             SDEEffect(effect_id=EFF_SK_HULL, effect_name="testHullClassSkill",
                       effect_category=0),
             SDETypeEffect(type_id=SK_HULL, effect_id=EFF_SK_HULL),
             SDEModifier(effect_id=EFF_SK_HULL, func="ItemModifier", domain="shipID",
                         modified_attribute_id=A_HULL_LVL, modifying_attribute_id=280,
                         operator=OP_POST_PERCENT)]
    rows.append(SDETypeBonus(type_id=HULL_ROLE, bonus_value=100.0, is_role_bonus=True))

    # ── modules: a damage amplifier (+10% on 2226) and a support unit ───────
    rows += [SDEType(type_id=DDA, type_name="Test Damage Amplifier", group_id=9712, category_id=7),
             SDEType(type_id=FSU, type_name="Test Support Unit", group_id=9713, category_id=7)]
    rows += _attrs(DDA, {A_DDA: 10.0})
    rows += _attrs(FSU, {A_FSU: 0.95})
    rows += [SDEEffect(effect_id=EFF_DDA, effect_name="testDda", effect_category=0),
             SDETypeEffect(type_id=DDA, effect_id=EFF_DDA),
             SDEEffect(effect_id=EFF_FSU, effect_name="testFsu", effect_category=0),
             SDETypeEffect(type_id=FSU, effect_id=EFF_FSU)]
    rows += [SDEModifier(effect_id=EFF_DDA, func="OwnerRequiredSkillModifier",
                         domain="charID", modified_attribute_id=target,
                         modifying_attribute_id=A_DDA, operator=OP_POST_PERCENT,
                         filter_type="skill", filter_value=SK_FIGHTERS)
             for target in (ATTR_FIGHTER_ATTACK_MULT, ATTR_FIGHTER_MISSILE_MULT)]
    rows += [SDEModifier(effect_id=EFF_FSU, func="OwnerRequiredSkillModifier",
                         domain="charID", modified_attribute_id=target,
                         modifying_attribute_id=A_FSU, operator=OP_POST_MUL,
                         filter_type="skill", filter_value=SK_FIGHTERS)
             for target in (ATTR_FIGHTER_ATTACK_DURATION, ATTR_FIGHTER_MISSILE_DURATION)]

    # ── fighters ─────────────────────────────────────────────────────────────
    attack = {ATTR_FIGHTER_ATTACK_MULT: 1.0, ATTR_FIGHTER_ATTACK_EM: 108.0,
              ATTR_FIGHTER_ATTACK_DURATION: 5000.0}
    salvo = {ATTR_FIGHTER_MISSILE_MULT: 1.0, ATTR_FIGHTER_MISSILE_KINETIC: 196.5,
             ATTR_FIGHTER_MISSILE_DURATION: 14000.0}
    spec = [
        # (id, name, volume, attrs, effects)
        (PLAIN, "Test Plain Attacker", 1000.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 6, **attack},
         [EFFECT_FIGHTER_ATTACK_MISSILE]),
        (SALVO_ATK, "Test Salvo Attacker", 1000.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 6, **attack, **salvo},
         [EFFECT_FIGHTER_ATTACK_MISSILE, EFFECT_FIGHTER_MISSILES]),
        (SUPERIORITY, "Test Superiority", 800.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 12,
          ATTR_FIGHTER_MISSILE_MULT: 1.0, ATTR_FIGHTER_MISSILE_EM: 42.0,
          ATTR_FIGHTER_MISSILE_DURATION: 3500.0},
         [EFFECT_FIGHTER_MISSILES]),
        # Both attribute blocks, one effect each: the attributes alone must
        # not make an ability count.
        (ATTACK_FX_ONLY, "Test Attack Effect Only", 1000.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 6, **attack, **salvo},
         [EFFECT_FIGHTER_ATTACK_MISSILE]),
        (MISSILE_FX_ONLY, "Test Missile Effect Only", 1000.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 6, **attack, **salvo},
         [EFFECT_FIGHTER_MISSILES]),
        (SIZELESS, "Test Sizeless Fighter", 1000.0,
         {ATTR_FIGHTER_IS_LIGHT: 1, **attack}, [EFFECT_FIGHTER_ATTACK_MISSILE]),
        (SUPPORT_F, "Test Support Fighter", 3000.0,
         {ATTR_FIGHTER_IS_SUPPORT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 3}, []),
        (STANDUP, "Test Standup Fighter", 1600.0,
         {ATTR_FIGHTER_IS_STANDUP_LIGHT: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 9, **attack},
         [EFFECT_FIGHTER_ATTACK_MISSILE]),
        (HEAVY_F, "Test Heavy Fighter", 1800.0,
         {ATTR_FIGHTER_IS_HEAVY: 1, ATTR_FIGHTER_SQUADRON_MAX_SIZE: 6, **attack},
         [EFFECT_FIGHTER_ATTACK_MISSILE]),
    ]
    for tid, name, vol, attrs, effects in spec:
        rows.append(SDEType(type_id=tid, type_name=name, group_id=GROUP_FIGHTER,
                            category_id=87, volume=vol))
        rows += _attrs(tid, attrs)
        rows += [SDETypeEffect(type_id=tid, effect_id=e) for e in effects]
        # Every fighter requires the Fighters-like skill, which is what the skill
        # rows, the hull bonuses and the damage amplifier all filter on.
        rows.append(SDETypeSkillReq(type_id=tid, skill_type_id=SK_FIGHTERS,
                                    required_level=1))
    return rows


def _fit(items, ship=CARRIER, **kwargs):
    async def _calc(db):
        return await calculate_fitting_stats(db, ship, items, **kwargs)
    return _Slice().run(_calc, rows=_seed_rows() + _fighter_rows())


def _sq(type_id, qty=6, name=None, **over):
    """One squadron, launched unless `online=False` is passed."""
    item = {"type_id": type_id, "type_name": name or f"Test {type_id}",
            "slot": "fighter", "quantity": qty}
    item.update(over)
    return item


# ════════════════════════════════════════════════════════════════════════════
# T2a: hangar, launch accounting, version
# ════════════════════════════════════════════════════════════════════════════

def test_hangar_total_carries_fighter_hangar_management():
    """Base 65000 m3, +5% per level (skill 24613, attr 2340 = 5.0).
    No character selected = All V: 65000 x (1 + 5 x 5 / 100) = 65000 x 1.25 = 81250.
    Level 3: 65000 x 1.15 = 74750. Untrained: 65000."""
    assert _fit([])["fighter_hangar_total"] == 81250.0
    assert _fit([], skill_levels={SKILL_FIGHTER_HANGAR_MANAGEMENT: 3})["fighter_hangar_total"] == 74750.0
    assert _fit([], skill_levels=NO_SKILLS)["fighter_hangar_total"] == 65000.0


def test_hull_without_tubes_has_no_fighter_capacity():
    stats = _fit([], ship=SHIP)
    assert stats["fighter_tubes_total"] == 0
    assert stats["fighter_hangar_total"] == 0.0


def test_reserve_squadron_counts_toward_the_hangar_but_not_the_tubes():
    """Launched 6 x 1000 = 6000 m3, reserve 6 x 1000 = 6000: 12000 m3 in the bay,
    one tube used."""
    stats = _fit([_sq(PLAIN), _sq(PLAIN, online=False)])
    assert stats["fighter_hangar_used"] == 12000.0
    assert stats["fighter_tubes_used"] == 1
    assert stats["fighter_light_used"] == 1
    assert stats["warnings"] == []


def test_reserve_does_not_take_a_tube_from_a_later_squadron():
    """Four reserves ahead of a launched squadron leave all four tubes free."""
    stats = _fit([_sq(PLAIN, online=False)] * 4 + [_sq(PLAIN)])
    assert stats["fighter_tubes_used"] == 1


def test_fifth_launched_squadron_on_four_tubes_is_not_counted():
    """3 light + 2 support fits the class limits (3 and 2) but not the 4 tubes:
    the fifth, in order, is the second support squadron."""
    items = [_sq(PLAIN), _sq(PLAIN), _sq(PLAIN), _sq(SUPPORT_F, 3), _sq(SUPPORT_F, 3, name="Test Last")]
    stats = _fit(items)
    assert (stats["fighter_tubes_used"], stats["fighter_tubes_total"]) == (4, 4)
    assert stats["fighter_support_used"] == 1
    assert 'Fighter squadron "Test Last" not counted: no free launch tube' in stats["warnings"]


def test_fourth_light_squadron_on_three_light_slots_is_not_counted():
    items = [_sq(PLAIN)] * 3 + [_sq(PLAIN, name="Test Fourth")]
    stats = _fit(items)
    assert (stats["fighter_light_used"], stats["fighter_light_total"]) == (3, 3)
    assert stats["fighter_tubes_used"] == 3        # the refused one took no tube
    assert 'Fighter squadron "Test Fourth" not counted: no free light squadron slot' in stats["warnings"]
    # ...and it adds no damage: three squadrons, not four (see the DPS tests).
    assert stats["fighter_dps"] == _fit(items[:3])["fighter_dps"]


def test_class_the_hull_has_no_slots_for_is_not_counted():
    """The fixture hull has no heavy slot attribute at all (= 0)."""
    stats = _fit([_sq(HEAVY_F, name="Test Heavy")])
    assert (stats["fighter_heavy_used"], stats["fighter_heavy_total"]) == (0, 0)
    assert stats["fighter_dps"] == 0.0
    assert 'Fighter squadron "Test Heavy" not counted: this ship cannot launch heavy fighters' in stats["warnings"]


def test_structure_fighter_is_never_launched_from_a_ship():
    stats = _fit([_sq(STANDUP, 9, name="Test Standup")])
    assert stats["fighter_tubes_used"] == 0
    assert stats["fighter_dps"] == 0.0
    assert ('Fighter squadron "Test Standup" not counted: '
            'structure fighters cannot launch from a ship') in stats["warnings"]
    assert stats["fighter_hangar_used"] == 14400.0   # still in the bay: 9 x 1600


def test_hull_with_no_tubes_launches_nothing_but_keeps_the_fighters_in_the_bay():
    stats = _fit([_sq(PLAIN, name="Test Stranded")], ship=SHIP)
    assert stats["fighter_tubes_used"] == 0
    assert stats["fighter_dps"] == 0.0
    assert stats["fighter_hangar_used"] == 6000.0
    assert 'Fighter squadron "Test Stranded" not counted: this ship has no fighter tubes' in stats["warnings"]


def test_squadron_above_its_maximum_is_clamped_with_a_warning():
    """Attr 2215 = 6. Eight is counted as six: 6 x 1000 = 6000 m3, and the DPS
    is that of six (6 x 108 / 5 = 129.6 with no skills)."""
    stats = _fit([_sq(PLAIN, 8, name="Test Oversized")], skill_levels=NO_SKILLS)
    assert stats["fighter_hangar_used"] == 6000.0
    assert stats["fighter_primary_dps"] == 129.6
    assert 'Fighter squadron "Test Oversized" has 8 fighters; counted as 6, the squadron maximum' \
        in stats["warnings"]


def test_hostile_quantities_neither_raise_nor_inflate_damage():
    """The stats route takes raw JSON, where Infinity, NaN and 1e999 parse. A type
    with no size on record is capped at 12 (the largest real squadron), so even
    10**400 or 1e300 counts as 12: 12 x 108 / 5 = 259.2 with no skills."""
    for bad in (math.inf, -math.inf, math.nan, 10 ** 400, 1e300, 1e999, "7", True):
        stats = _fit([_sq(SIZELESS, bad), _sq(PLAIN, bad)], skill_levels=NO_SKILLS)
        assert stats["fighter_primary_dps"] <= 259.2 + 129.6 + 1e-6, bad
    assert _fit([_sq(SIZELESS, 10 ** 400)], skill_levels=NO_SKILLS)["fighter_primary_dps"] == 259.2
    assert _fit([_sq(SIZELESS, math.inf)], skill_levels=NO_SKILLS)["fighter_primary_dps"] == 0.0


def test_hangar_over_capacity_warns():
    """Untrained hangar = 65000 m3. Eleven reserve squadrons of 6 x 1000 m3 =
    11 x 6000 = 66000 m3 is over it."""
    items = [_sq(PLAIN, online=False)] * 11
    stats = _fit(items, skill_levels=NO_SKILLS)
    assert stats["fighter_hangar_used"] == 66000.0
    assert any("Fighter hangar over capacity" in w for w in stats["warnings"])


def test_fighter_info_is_reported_per_type_for_the_page():
    info = _fit([_sq(PLAIN), _sq(SUPPORT_F, 3)])["fighter_info"]
    assert info[str(PLAIN)] == {"class": "light", "max_size": 6, "volume": 1000.0, "primary": "attack",
                              "has_attack": True, "has_missiles": False}
    assert info[str(SUPPORT_F)]["class"] == "support"


def test_a_fit_without_fighters_has_zero_fighter_figures():
    stats = _fit([_low(DDA)])
    assert stats["fighter_dps"] == 0.0 and stats["effective_fighter_dps"] == 0.0
    assert stats["fighter_tubes_used"] == 0 and stats["fighter_hangar_used"] == 0.0
    assert stats["fighter_info"] == {}


# ════════════════════════════════════════════════════════════════════════════
# T2b: fighter DPS (hand-computed reference values)
# ════════════════════════════════════════════════════════════════════════════

def test_base_squadron_with_no_bonuses():
    """n x damage x multiplier / cycle seconds = 6 x 108 x 1.0 / 5 = 129.6."""
    stats = _fit([_sq(PLAIN)], skill_levels=NO_SKILLS)
    assert stats["fighter_primary_dps"] == 129.6
    assert stats["fighter_salvo_dps"] == 0.0
    assert stats["fighter_dps"] == 129.6
    # Fighter DPS is part of every total.
    assert stats["total_dps"] == stats["total_dps_max_spool"] == 129.6
    assert stats["effective_total_dps"] == stats["effective_total_dps_max_spool"] == 129.6


def test_all_v_skills_give_the_reference_primary_figure():
    """Fighters 5 x 5 = +25%, Drone Interfacing 10 x 5 = +50%, specialization
    2 x 5 = +10%: 1.25 x 1.5 x 1.1 = 2.0625.
    6 x 108 x 2.0625 / 5 = 1336.5 / 5 = 267.3 (one All V squadron, primary only)."""
    stats = _fit([_sq(PLAIN)], skill_levels=ALL_V)
    assert stats["fighter_primary_dps"] == 267.3
    # Same with no character selected (All V is the default).
    assert _fit([_sq(PLAIN)])["fighter_primary_dps"] == 267.3


def test_a_skill_the_character_lacks_gives_no_bonus():
    """Only Fighters trained: 6 x 108 x 1.25 / 5 = 162.0. Level 3: x1.15 = 149.04."""
    assert _fit([_sq(PLAIN)], skill_levels={SK_FIGHTERS: 5})["fighter_dps"] == 162.0
    assert _fit([_sq(PLAIN)], skill_levels={SK_FIGHTERS: 3})["fighter_dps"] == 149.0
    assert _fit([_sq(PLAIN)], skill_levels={SK_INTERFACING: 5})["fighter_dps"] == 194.4  # x1.5


def test_three_all_v_squadrons_add_up_and_one_type_twice_is_not_merged():
    """Three squadrons of one type: 3 x 267.3 = 801.9 (the plan's carrier figure)."""
    assert _fit([_sq(PLAIN)] * 3)["fighter_dps"] == 801.9


def test_reserve_and_refused_squadrons_add_no_damage():
    assert _fit([_sq(PLAIN, online=False)])["fighter_dps"] == 0.0


def test_per_level_hull_bonus_scales_with_the_hulls_skill():
    """Hull +5%/level, scaling skill SK_HULL. Fighters skill left untrained, so the
    only bonus is the hull's. Base 129.6.
    Level 5: x1.25 = 162.0. Level 3: x1.15 = 149.04. Untrained hull skill: x1."""
    items = [_sq(PLAIN)]
    assert _fit(items, ship=HULL_LVL, skill_levels={SK_HULL: 5})["fighter_dps"] == 162.0
    assert _fit(items, ship=HULL_LVL, skill_levels={SK_HULL: 3})["fighter_dps"] == 149.0
    assert _fit(items, ship=HULL_LVL, skill_levels=NO_SKILLS)["fighter_dps"] == 129.6


def test_role_hull_bonus_does_not_scale_with_skills():
    """+100% role bonus doubles the damage whatever is trained: 129.6 x 2 = 259.2."""
    assert _fit([_sq(PLAIN)], ship=HULL_ROLE, skill_levels=NO_SKILLS)["fighter_dps"] == 259.2


def test_damage_amplifiers_are_stacking_penalised_on_the_multiplier():
    """Attr 2226 is not stackable. One amplifier (+10%): 129.6 x 1.1 = 142.56 -> 142.6.
    Two: the second counts at exp(-1 / 7.1289) = 0.86912 of its bonus, so
    1.1 x (1 + 0.1 x 0.86912) = 1.1 x 1.086912 = 1.1956032 and
    129.6 x 1.1956032 = 154.95 -> 155.0 (unpenalised 1.21 would be 156.8)."""
    one = _fit([_sq(PLAIN), _low(DDA)], skill_levels=NO_SKILLS)
    two = _fit([_sq(PLAIN), _low(DDA, quantity=2)], skill_levels=NO_SKILLS)
    assert one["fighter_dps"] == 142.6
    assert two["fighter_dps"] == 155.0


def test_offline_damage_amplifier_gives_no_bonus():
    assert _fit([_sq(PLAIN), _low(DDA, online=False)], skill_levels=NO_SKILLS)["fighter_dps"] == 129.6


def test_support_unit_shortens_the_cycle():
    """x0.95 on the 5000 ms cycle = 4750 ms: 6 x 108 / 4.75 = 136.42 -> 136.4."""
    stats = _fit([_sq(PLAIN), _low(FSU)], skill_levels=NO_SKILLS)
    assert stats["fighter_dps"] == 136.4


def test_attack_fighter_default_adds_the_missile_salvo_to_the_primary():
    """pyfa default: the salvo counts as if it never ran out.
    Primary 267.3 (see above). Salvo: 6 x 196.5 x 2.0625 / 14 = 2431.6875 / 14
    = 173.69 -> 173.7. Together 267.3 + 173.69 = 440.99 -> 441.0."""
    stats = _fit([_sq(SALVO_ATK)], skill_levels=ALL_V)
    assert stats["fighter_primary_dps"] == 267.3
    assert stats["fighter_salvo_dps"] == 173.7
    assert stats["fighter_dps"] == 441.0
    assert stats["total_dps"] == 441.0


def test_type_with_only_the_attack_effect_has_no_salvo():
    """The salvo needs effect 6431 on the type, not just the 2130-2182 attributes.
    This type has both blocks: only the primary counts, 267.3 (no 173.7 salvo)."""
    stats = _fit([_sq(ATTACK_FX_ONLY)], skill_levels=ALL_V)
    assert stats["fighter_salvo_dps"] == 0.0
    assert stats["fighter_dps"] == 267.3


def test_type_with_only_the_missile_effect_ignores_the_attack_attributes():
    """Mirror: effect 6431 only, but the 2226-2233 attributes are present. Only
    the missile block counts: 6 x 196.5 x 2.0625 / 14 = 173.7 (not 441.0).
    It has no attack effect, so it is a superiority-style type and the missile
    damage sits in the primary bucket."""
    stats = _fit([_sq(MISSILE_FX_ONLY)], skill_levels=ALL_V)
    assert stats["fighter_dps"] == 173.7
    assert stats["fighter_primary_dps"] == 173.7
    assert stats["fighter_salvo_dps"] == 0.0


def test_superiority_fighter_missiles_are_its_primary_not_a_salvo():
    """12 x 42 x 2.0625 / 3.5 = 1039.5 / 3.5 = 297.0. Its only weapon, so it
    is the sustained primary and a future salvo toggle cannot remove it."""
    stats = _fit([_sq(SUPERIORITY, 12)], skill_levels=ALL_V)
    assert stats["fighter_primary_dps"] == 297.0
    assert stats["fighter_salvo_dps"] == 0.0
    assert stats["fighter_dps"] == 297.0


def test_salvo_attacker_with_hull_amplifier_and_support_unit_golden():
    """Attack fighter with a salvo on a +5%/level hull (All V: +25%), Fighters at
    V (+25%), one amplifier (+10%), one support unit (cycles x0.95). Both
    damage multipliers: 1.25 x 1.25 x 1.1 = 1.71875.
    Primary: 6 x 108 x 1.71875 / 4.75 s = 1113.75 / 4.75 = 234.47 -> 234.5.
    Salvo:   6 x 196.5 x 1.71875 / 13.3 s = 2026.40625 / 13.3 = 152.36 -> 152.4.
    Total 386.84 -> 386.8."""
    stats = _fit([_sq(SALVO_ATK), _low(DDA), _low(FSU)], ship=HULL_LVL,
                 skill_levels={SK_FIGHTERS: 5, SK_HULL: 5})
    assert stats["fighter_primary_dps"] == 234.5
    assert stats["fighter_salvo_dps"] == 152.4
    assert stats["fighter_dps"] == 386.8


def test_repeated_reference_to_one_squadron_dict_is_counted_per_entry():
    """Four references to ONE dict on three light slots: the fourth is refused,
    DPS is that of three squadrons (3 x 267.3 = 801.9), not four."""
    sq = _sq(PLAIN)
    stats = _fit([sq, sq, sq, sq])
    assert stats["fighter_launched"] == [True, True, True, False]
    assert stats["fighter_light_used"] == 3
    assert stats["fighter_dps"] == 801.9


def test_launch_results_are_reported_in_request_order():
    """One bool per fighter item, other items skipped over."""
    items = [_low(DDA), _sq(PLAIN, online=False), _sq(PLAIN), _sq(STANDUP, 9)]
    stats = _fit(items)
    assert stats["fighter_squadrons"] == 3
    assert stats["fighter_launched"] == [False, True, False]
    assert _fit([_low(DDA)])["fighter_launched"] == []


def test_effective_dps_is_weighted_per_damage_type_and_per_ability():
    """Target resists (EM, thermal, kinetic, explosive) = (0.45, 0.55, 0.00, 0.00).
    Primary is all EM: 267.3 x (1 - 0.45) = 147.015 -> 147.0. Salvo is all kinetic:
    173.69 x (1 - 0) = 173.7. Sum 320.707 -> 320.7. Raw stays 441.0."""
    stats = _fit([_sq(SALVO_ATK)], skill_levels=ALL_V, target_resist_profile="sansha")
    assert stats["fighter_dps"] == 441.0
    assert stats["effective_fighter_primary_dps"] == 147.0
    assert stats["effective_fighter_salvo_dps"] == 173.7
    assert stats["effective_fighter_dps"] == 320.7
    assert stats["effective_total_dps"] == 320.7
    assert stats["effective_total_dps_max_spool"] == 320.7


def test_uniform_target_makes_effective_equal_raw():
    stats = _fit([_sq(SALVO_ATK)], skill_levels=ALL_V)
    assert stats["effective_fighter_dps"] == stats["fighter_dps"] == 441.0
