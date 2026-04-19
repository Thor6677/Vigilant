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
ATTR_RATE_OF_FIRE = 51         # turret/launcher ROF (ms) — "speed" attribute

# Character-level damage multiplier — BCU sets this via ItemModifier(charID).
# The DPS formula for missiles is: charge_damage * char_missileDmgMult / cycle_time.
ATTR_MISSILE_DAMAGE_MULTIPLIER = 212        # on character entity
ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS = 213  # on BCU (source attr)

# Overload bonus attributes (on modules, applied when overheated)
# Mapping: overload attr ID → (target attr to modify, is_reduction)
# is_reduction=True means the bonus mechanically reduces the target (ROF/duration)
OVERLOAD_ATTR_MAP = {
    1210: (ATTR_DAMAGE_MULTIPLIER, False),   # overloadDamageModifier → +% damage
    1205: (ATTR_RATE_OF_FIRE, True),          # overloadRofBonus → reduce cycle time
    1223: (20, False),                        # overloadSpeedFactorBonus → +% speed
    1208: (None, False),                      # overloadHardeningBonus → resist (special)
    1230: (84, False),                        # overloadArmorDamageAmount → +% armor rep
    1231: (68, False),                        # overloadShieldBonus → +% shield boost
    1206: (ATTR_DURATION, True),              # overloadSelfDurationBonus → reduce cycle
    1222: (54, False),                        # overloadRangeBonus → +% optimal range
}

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
ATTR_CHARGE_GROUP_4 = 607
ATTR_CHARGE_GROUP_5 = 610
CHARGE_GROUP_ATTRS = [604, 605, 606, 607, 610]

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
}
