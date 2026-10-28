"""Dogma attribute ID constants used by the fitting engine."""

# Ship base stats
ATTR_HP = 9
ATTR_SHIELD_HP = 263
ATTR_ARMOR_HP = 265
ATTR_POWER_OUTPUT = 11
ATTR_CPU_OUTPUT = 48
ATTR_CAPACITOR = 482
ATTR_CAP_RECHARGE = 55
ATTR_MAX_VELOCITY = 37
ATTR_MASS = 4
ATTR_INERTIA = 70
ATTR_SIG_RADIUS = 552
ATTR_CARGO_CAPACITY = 38
ATTR_DRONE_CAPACITY = 283
ATTR_DRONE_BANDWIDTH = 1271
ATTR_MAX_TARGET_RANGE = 76
ATTR_MAX_LOCKED_TARGETS = 192
ATTR_SCAN_RESOLUTION = 564
ATTR_CALIBRATION_OUTPUT = 1132
ATTR_RIG_SIZE = 1547

# Warp
ATTR_WARP_SPEED_MULTIPLIER = 600   # hull-class multiplier; modules apply post_percent here
ATTR_BASE_WARP_SPEED = 1281        # base warp speed (typically 1.0 AU/s on ships)
ATTR_WARP_CAP_NEED = 153           # capacitor cost per warp (GJ)

# Ship slot counts (NOTE: 12=low, 14=high — counterintuitive SDE naming)
ATTR_LOW_SLOTS = 12
ATTR_MED_SLOTS = 13
ATTR_HI_SLOTS = 14
ATTR_RIG_SLOTS = 1137
ATTR_TURRET_SLOTS = 102
ATTR_LAUNCHER_SLOTS = 101

# Subsystem slot modifier attributes (T3C subsystems — not in dogma effect pipeline)
ATTR_HI_SLOT_MODIFIER = 1374
ATTR_MED_SLOT_MODIFIER = 1375
ATTR_LOW_SLOT_MODIFIER = 1376
ATTR_TURRET_HARDPOINT_MODIFIER = 1368
ATTR_LAUNCHER_HARDPOINT_MODIFIER = 1369

# Module fitting requirements
ATTR_POWER = 30
ATTR_CPU = 50
ATTR_UPGRADE_COST = 1153

# Module activation
ATTR_CAPACITOR_NEED = 6    # cap per activation (GJ)
ATTR_DURATION = 73         # cycle time (ms)

# Drone attributes
ATTR_DRONE_BW_USED = 1272
ATTR_VOLUME = 161

# Shield recharge
ATTR_SHIELD_RECHARGE_RATE = 479  # shield recharge time (ms)

# Repair module output attributes
ATTR_ARMOR_DAMAGE_AMOUNT = 84    # armor HP per cycle
ATTR_SHIELD_BONUS = 68           # shield HP per cycle (shield boosters)

# Weapon / damage attributes
ATTR_DAMAGE_MULTIPLIER = 64    # turret/drone damage multiplier
ATTR_DAMAGE_MULTIPLIER_BONUS = 292   # skill-side per-level % bonus to the above (NOT 64)
EFFECT_DRONE_DMG_BONUS = 1730        # droneDmgBonus: on the drone size/specialization skills; 0 modifier rows
ATTR_RATE_OF_FIRE = 51         # turret/launcher ROF (ms) — "speed" attribute

# Character-level damage multiplier — BCU sets this via ItemModifier(charID).
# The DPS formula for missiles is: charge_damage * char_missileDmgMult / cycle_time.
ATTR_MISSILE_DAMAGE_MULTIPLIER = 212        # on character entity
ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS = 213  # on BCU (source attr)

# ── Overload / heat ──────────────────────────────────────────────────────
# Overheating is NOT a special case in the dogma data: every overload bonus
# is an ordinary effect whose modifiers target the module's OWN attributes
# (domain "itemID"), gated by effect category 5. A census of the CCP SDE
# (dogmaEffects.jsonl) found 49 modifier rows in category 5; all 49 have
# domain "itemID" and all 49 read a source attribute whose name begins with
# "overload". No category-5 row uses any other domain, and no "overload*"
# source attribute appears on an "itemID" row outside category 5.
#
# That equivalence is what OVERLOAD_SOURCE_ATTR_PREFIX exploits: selecting
# `domain == "itemID" AND source attribute name LIKE 'overload%'` picks out
# exactly the overload rows, without depending on effect_category being
# populated correctly in an already-imported database. See
# engine._get_overload_modifiers.
#
# This replaces the former hand-written OVERLOAD_ATTR_MAP, which covered 8
# of the 13 overload families and silently dropped overloadHardeningBonus
# (its target was recorded as None). The SDE rows reproduce the other 7
# families' targets exactly — the map's `is_reduction` flag was never read,
# because a reduction is simply a negative bonus under postPercent.
OVERLOAD_SOURCE_ATTR_PREFIX = "overload"

# Resistance-bonus attributes carried by hardeners. These are the SOURCE
# attributes the hardener's own effect reads to modify the ship's resonance
# (e.g. effect 5231 modifyActiveArmorResonancePostPercent: armorEmDamageResonance
# 267 modified by emDamageResistanceBonus 984, operator postPercent), and the
# TARGET attributes that overloadHardeningBonus (1208) boosts.
ATTR_EM_RESIST_BONUS = 984
ATTR_EXPL_RESIST_BONUS = 985
ATTR_KIN_RESIST_BONUS = 986
ATTR_THERM_RESIST_BONUS = 987

# ── Fighters ─────────────────────────────────────────────────────────────
# Ship side (SDE-confirmed 2026-10-23): a missing attribute means 0, so a hull
# with no tubes simply has none of these. Not skill-modified except the hangar.
ATTR_FIGHTER_CAPACITY = 2055            # fighter hangar, m3 (Fighter Hangar Management: +5%/lvl)
ATTR_FIGHTER_TUBES = 2216
ATTR_FIGHTER_LIGHT_SLOTS = 2217
ATTR_FIGHTER_SUPPORT_SLOTS = 2218
ATTR_FIGHTER_HEAVY_SLOTS = 2219
# Fighter side: which launch-slot class a squadron needs. Structure fighters
# (standup) can never launch from a ship, which has none of 2737-2739.
ATTR_FIGHTER_SQUADRON_MAX_SIZE = 2215   # fighters per squadron
ATTR_FIGHTER_IS_LIGHT = 2212
ATTR_FIGHTER_IS_SUPPORT = 2213
ATTR_FIGHTER_IS_HEAVY = 2214
ATTR_FIGHTER_IS_STANDUP_LIGHT = 2740
ATTR_FIGHTER_IS_STANDUP_SUPPORT = 2741
ATTR_FIGHTER_IS_STANDUP_HEAVY = 2742
# Primary attack (effect 6465) and the missile ability (effect 6431)
ATTR_FIGHTER_ATTACK_MULT = 2226
ATTR_FIGHTER_ATTACK_EM = 2227
ATTR_FIGHTER_ATTACK_THERMAL = 2228
ATTR_FIGHTER_ATTACK_KINETIC = 2229
ATTR_FIGHTER_ATTACK_EXPLOSIVE = 2230
ATTR_FIGHTER_ATTACK_DURATION = 2233     # ms
ATTR_FIGHTER_MISSILE_MULT = 2130
ATTR_FIGHTER_MISSILE_EM = 2131
ATTR_FIGHTER_MISSILE_THERMAL = 2132
ATTR_FIGHTER_MISSILE_KINETIC = 2133
ATTR_FIGHTER_MISSILE_EXPLOSIVE = 2134
ATTR_FIGHTER_MISSILE_DURATION = 2182    # ms
EFFECT_FIGHTER_ATTACK_MISSILE = 6465    # fighterAbilityAttackM, the sustained primary
EFFECT_FIGHTER_MISSILES = 6431          # fighterAbilityMissiles
EFFECT_FIGHTER_BOMB = 6485              # excluded from DPS
EFFECT_FIGHTER_KAMIKAZE = 6554          # excluded from DPS
SKILL_FIGHTER_HANGAR_MANAGEMENT = 24613

# Slots that never occupy a module rack: no CPU, powergrid, hardpoint or
# module-modifier pass, so they stay out of the fitted-module lists.
NON_RACK_SLOTS = ("drone", "cargo", "fighter")

# A ship has at most 8 slots per rack, so no module type can be fitted more
# than 8 times. The engine clamps a rack item's `quantity` to this: the
# per-copy loops (cross-module, booster, signature bloom) would otherwise run
# once per unit of an attacker-supplied number.
MAX_RACK_MODULE_COPIES = 8

# ── Module state (offline / online / active / overheated) ────────────────
# Effect 16 (online) is the one effect every fittable module carries; it is
# category 4 and is NOT an activation. A module can be switched to ACTIVE only
# if it has some other effect in an activation category: 1 (active), 2
# (target) or 3 (area). Category 5 is the overload effect (heat).
EFFECT_ONLINE = 16
ACTIVATION_EFFECT_CATS = frozenset({1, 2, 3})
EFFECT_CAT_OVERLOAD = 5

# Module states, ordered so `state >= STATE_ACTIVE` means "running".
STATE_OFFLINE = -1
STATE_ONLINE = 0
STATE_ACTIVE = 1
STATE_OVERHEATED = 2

# Modules whose natural resting state is ONLINE: pyfa's default state for a
# type carrying any of these effects (same effect ids, names as pyfa has them).
# Every other activatable module defaults to ACTIVE. 12126
# (microJumpPortalDriveCapital) is deliberately absent: pyfa defaults it to
# active.
DEFAULT_ONLINE_EFFECTS = frozenset({
    7012,   # moduleBonusAssaultDamageControl
    6719,   # (name not in the pyfa sample copy)
    4921,   # microJumpDrive
    6208,   # microJumpPortalDrive
    6484,   # emergencyHullEnergizer
    2857,   # cynosuralGeneration
    2152,   # jumpPortalGeneration
    3674,   # jumpPortalGenerationBO
    2858,   # cloneJumpAccepting
    980,    # cloakingWarpSafe
    5945,   # cloakingPrototype
    607,    # cloaking
    7142,   # massEntanglerEffect5
    118,    # electronicAttributeModifyOnline
    54,     # targetPassively
    47,     # cargoScan
    46,     # shipScan
    81,     # (name not in the pyfa sample copy)
    8109,   # targetSpectrumBreakerBonus
    8117,   # interdictionNullifierBonus
    8121,   # warpCoreStabilizerActive
    8364,   # industrialItemCompression
})
EFFECT_MJD = 4921
EFFECT_MJFG = 6208
EFFECT_MJFG_CAPITAL = 12126
ATTR_SIG_RADIUS_BONUS_PERCENT = 973
ATTR_MAX_GROUP_ACTIVE = 763     # 0 or absent = no limit on simultaneously active

# ── Reactive Armor Hardener ──────────────────────────────────────────────
# Effect 4928 (adaptiveArmorHardener) is the module's defining effect and
# carries NO modifierInfo — CCP models the redistribution in the game server,
# so the SDE cannot express it. Identify a reactive hardener by this effect
# rather than by name or group: it is what actually makes a module one.
EFFECT_ADAPTIVE_ARMOR_HARDENER = 4928
ATTR_RESISTANCE_SHIFT_AMOUNT = 1849   # resist points moved per cycle (6.0)

# ── Afterburners and microwarpdrives ─────────────────────────────────────
# Effects 6731 (moduleBonusAfterburner) and 6730 (moduleBonusMicrowarpdrive)
# carry NO modifierInfo in the SDE; the client and Pyfa hard-code them. Both
# add mass, scale maxVelocity by speedFactor * speedBoostFactor / mass, and an
# MWD additionally blooms the signature radius.
EFFECT_MWD = 6730
EFFECT_AFTERBURNER = 6731
ATTR_SPEED_FACTOR = 20          # percent thrust factor (skills and heat act on this)
ATTR_SPEED_BOOST_FACTOR = 567   # thrust, divided by ship mass
ATTR_MASS_ADDITION = 796        # kg added to the ship while the module runs
ATTR_SIG_RADIUS_BONUS = 554     # percent signature bloom (MWD only)

# ── Charges that modify their parent module ──────────────────────────────
# Most of this is ordinary dogma: a charge's effect carries modifiers with
# domain "otherID", meaning "the module I am loaded into". Two mechanics are
# NOT expressible in modifierInfo and are hand-coded (Pyfa hand-codes them too):
EFFECT_POWER_BOOSTER = 48             # capacitor booster module; effect 48 has 0 modifiers
ATTR_CAPACITOR_BONUS = 67             # GJ injected by a cap booster charge
ATTR_CHARGED_ARMOR_DAMAGE_MULTIPLIER = 1886   # ancillary repairer's paste multiplier (3.0)

# Spool-up (Triglavian entropic disintegrators)
ATTR_DMG_MULT_BONUS_PER_CYCLE = 2733
ATTR_DMG_MULT_BONUS_MAX = 2734
ATTR_OPTIMAL_RANGE = 54        # maxRange
ATTR_FALLOFF = 158
ATTR_TRACKING_SPEED = 160
ATTR_EM_DAMAGE = 114
ATTR_EXPLOSIVE_DAMAGE = 116
ATTR_KINETIC_DAMAGE = 117
ATTR_THERMAL_DAMAGE = 118

# Charge compatibility
ATTR_CHARGE_SIZE = 128
ATTR_CHARGE_GROUP_1 = 604
ATTR_CHARGE_GROUP_2 = 605
ATTR_CHARGE_GROUP_3 = 606
ATTR_CHARGE_GROUP_4 = 609
ATTR_CHARGE_GROUP_5 = 610
CHARGE_GROUP_ATTRS = [604, 605, 606, 609, 610]

# Sensor strength
ATTR_SCAN_GRAVIMETRIC = 211
ATTR_SCAN_LADAR = 209
ATTR_SCAN_MAGNETOMETRIC = 210
ATTR_SCAN_RADAR = 208

# Shield resists (damage resonance: 1 - resist)
ATTR_SHIELD_EM_RESONANCE = 271
ATTR_SHIELD_THERM_RESONANCE = 274
ATTR_SHIELD_KIN_RESONANCE = 273
ATTR_SHIELD_EXPL_RESONANCE = 272

# Armor resists
ATTR_ARMOR_EM_RESONANCE = 267
ATTR_ARMOR_THERM_RESONANCE = 270
ATTR_ARMOR_KIN_RESONANCE = 269
ATTR_ARMOR_EXPL_RESONANCE = 268

# Armor resonance attributes in (em, thermal, kinetic, explosive) order —
# the order the Reactive Armor Hardener phasing API uses throughout.
ARMOR_RESONANCE_ATTRS = (
    ATTR_ARMOR_EM_RESONANCE,
    ATTR_ARMOR_THERM_RESONANCE,
    ATTR_ARMOR_KIN_RESONANCE,
    ATTR_ARMOR_EXPL_RESONANCE,
)

# Damage-type keys in the same order, used by the resist_phasing request field.
DAMAGE_TYPE_KEYS = ("em", "thermal", "kinetic", "explosive")

# Hull resists — ships store hull/structure resonance in the generic
# damageResonance attrs (109-113), NOT the hull-specific 974-977.
# 974-977 are source attrs on hull-tanking modules (e.g. Damage Control).
ATTR_HULL_EM_RESONANCE = 113       # emDamageResonance
ATTR_HULL_THERM_RESONANCE = 110    # thermalDamageResonance
ATTR_HULL_KIN_RESONANCE = 109      # kineticDamageResonance
ATTR_HULL_EXPL_RESONANCE = 111     # explosiveDamageResonance

SHIP_STAT_ATTRS = {
    "hull_hp": ATTR_HP,
    "armor_hp": ATTR_ARMOR_HP,
    "shield_hp": ATTR_SHIELD_HP,
    "pg_output": ATTR_POWER_OUTPUT,
    "cpu_output": ATTR_CPU_OUTPUT,
    "capacitor": ATTR_CAPACITOR,
    "cap_recharge": ATTR_CAP_RECHARGE,
    "max_velocity": ATTR_MAX_VELOCITY,
    "mass": ATTR_MASS,
    "inertia": ATTR_INERTIA,
    "sig_radius": ATTR_SIG_RADIUS,
    "cargo_capacity": ATTR_CARGO_CAPACITY,
    "drone_capacity": ATTR_DRONE_CAPACITY,
    "drone_bandwidth": ATTR_DRONE_BANDWIDTH,
    "max_target_range": ATTR_MAX_TARGET_RANGE,
    "max_locked_targets": ATTR_MAX_LOCKED_TARGETS,
    "scan_resolution": ATTR_SCAN_RESOLUTION,
    "calibration_output": ATTR_CALIBRATION_OUTPUT,
    "hi_slots": ATTR_HI_SLOTS,
    "med_slots": ATTR_MED_SLOTS,
    "low_slots": ATTR_LOW_SLOTS,
    "rig_slots": ATTR_RIG_SLOTS,
    "turret_slots": ATTR_TURRET_SLOTS,
    "launcher_slots": ATTR_LAUNCHER_SLOTS,
    "warp_speed_multiplier": ATTR_WARP_SPEED_MULTIPLIER,
    "base_warp_speed": ATTR_BASE_WARP_SPEED,
}
