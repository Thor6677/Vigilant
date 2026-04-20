"""Fitting calculation engine — computes ship stats from dogma attributes.

Applies module passive effects via the modifier pipeline:
  1. Collect modifiers from all fitted modules' effects
  2. Group by target attribute
  3. Apply in operator order: preAssign → modAdd → modSub → postMul → postDiv → postPercent
  4. Stacking penalties on postMul/postPercent where attribute stackable=False

Stacking penalty formula (Pyfa-verified):
  S(n) = e^(-n^2 / 7.1289)   where n is 0-indexed position sorted by |effect - 1|

References: Pyfa eos/modifiedAttributeDict.py, eos/calc.py, docs/fitting-mechanics.md
"""

import math
from collections import defaultdict

from sqlalchemy import select, text

from app.fitting.cap_sim import simulate_cap
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import (
    SDETypeDogmaAttribute, SDEModuleSlot, SDEType,
    SDETypeEffect, SDEEffect, SDEModifier, SDEDogmaAttribute,
)
from app.db.sde_models import SDETypeSkillReq, SDETypeBonus

from app.fitting.constants import (
    ATTR_POWER, ATTR_CPU, ATTR_UPGRADE_COST, ATTR_VOLUME,
    ATTR_DRONE_BW_USED, SHIP_STAT_ATTRS,
    ATTR_MASS, ATTR_INERTIA,
    ATTR_CAPACITOR_NEED, ATTR_DURATION, ATTR_SHIELD_RECHARGE_RATE,
    ATTR_CPU_OUTPUT, ATTR_POWER_OUTPUT,
    ATTR_SHIELD_EM_RESONANCE, ATTR_SHIELD_THERM_RESONANCE,
    ATTR_SHIELD_KIN_RESONANCE, ATTR_SHIELD_EXPL_RESONANCE,
    ATTR_ARMOR_EM_RESONANCE, ATTR_ARMOR_THERM_RESONANCE,
    ATTR_ARMOR_KIN_RESONANCE, ATTR_ARMOR_EXPL_RESONANCE,
    ATTR_HULL_EM_RESONANCE, ATTR_HULL_THERM_RESONANCE,
    ATTR_HULL_KIN_RESONANCE, ATTR_HULL_EXPL_RESONANCE,
    ATTR_SHIELD_HP, ATTR_ARMOR_HP, ATTR_HP,
    ATTR_CAPACITOR, ATTR_CAP_RECHARGE,
    ATTR_DAMAGE_MULTIPLIER, ATTR_RATE_OF_FIRE,
    ATTR_EM_DAMAGE, ATTR_EXPLOSIVE_DAMAGE, ATTR_KINETIC_DAMAGE, ATTR_THERMAL_DAMAGE,
    OVERLOAD_ATTR_MAP,
    ATTR_DMG_MULT_BONUS_PER_CYCLE, ATTR_DMG_MULT_BONUS_MAX,
    ATTR_MISSILE_DAMAGE_MULTIPLIER, ATTR_MISSILE_DAMAGE_MULTIPLIER_BONUS,
    ATTR_ARMOR_DAMAGE_AMOUNT, ATTR_SHIELD_BONUS,
    ATTR_HI_SLOTS, ATTR_MED_SLOTS, ATTR_LOW_SLOTS,
    ATTR_TURRET_SLOTS, ATTR_LAUNCHER_SLOTS,
    ATTR_HI_SLOT_MODIFIER, ATTR_MED_SLOT_MODIFIER, ATTR_LOW_SLOT_MODIFIER,
    ATTR_TURRET_HARDPOINT_MODIFIER, ATTR_LAUNCHER_HARDPOINT_MODIFIER,
)

# Default skill level assumption (used as fallback when no character is
# selected — models the EVE-standard "All V" fitting target).
DEFAULT_SKILL_LEVEL = 5


async def _ship_scaling_skill_ids(db: AsyncSession, ship_type_id: int) -> list[int]:
    """Return unique skill IDs that scale this ship's per-level hull bonuses.

    Sourced from the SDE's typeBonus (traits) data, which CCP ships with each
    bonus explicitly tagged. Role bonuses (scaling_skill_id IS NULL) are
    skipped — they don't scale with any skill. Most ships have exactly one
    scaling skill (their racial class skill), but T3C hulls have several
    (one per subsystem slot).
    """
    result = await db.execute(
        select(SDETypeBonus.scaling_skill_id)
        .where(SDETypeBonus.type_id == ship_type_id)
        .where(SDETypeBonus.is_role_bonus == False)  # noqa: E712
        .where(SDETypeBonus.scaling_skill_id.isnot(None))
    )
    return list({row[0] for row in result.fetchall() if row[0]})


async def _subsystem_scaling_skill_ids(db: AsyncSession, sub_type_id: int) -> list[int]:
    """Return the skill IDs that scale a subsystem's per-level bonuses.

    For subsystems the scaling skill is the subsystem's own required skill
    (e.g. "Caldari Offensive Systems" for the Tengu's offensive subsystem).
    We use SDETypeSkillReq for this rather than SDETypeBonus — subsystems
    don't have their own traits rows, the bonuses are attached to dogma
    effects gated by the required skill.
    """
    result = await db.execute(
        select(SDETypeSkillReq.skill_type_id).where(SDETypeSkillReq.type_id == sub_type_id)
    )
    return [row[0] for row in result.fetchall()]


def _effective_skill_level(
    scaling_skill_ids: list[int],
    skill_levels: dict[int, int] | None,
) -> int:
    """Pick an effective level for a per-level bonus.

    - No character selected (skill_levels is None): assume All V.
    - No scaling skill IDs (shouldn't normally happen): assume All V too, so
      we don't silently zero-out a bonus we can't identify.
    - Otherwise: take the MIN of the character's levels across the scaling
      skills. Missing = 0, so an untrained class skill drops the bonus to
      zero (matches the in-game reality that you can't fly the ship at all).
    """
    if skill_levels is None:
        return DEFAULT_SKILL_LEVEL
    if not scaling_skill_ids:
        return DEFAULT_SKILL_LEVEL
    return min(skill_levels.get(sid, 0) for sid in scaling_skill_ids)


# CCP JSONL SDE operator IDs (0-indexed from "operation" field in modifierInfo)
OP_PRE_ASSIGN = -1   # Override base value
OP_PRE_MUL = 0       # Multiply before additions
OP_PRE_DIV = 1       # Divide before additions
OP_MOD_ADD = 2       # Flat add
OP_MOD_SUB = 3       # Flat subtract
OP_POST_MUL = 4      # Multiply after additions (stacking penalized if applicable)
OP_POST_DIV = 5      # Divide after additions
OP_POST_PERCENT = 6  # val = val * (1 + modifier/100) — most common
OP_POST_ASSIGN = 7   # Force/lock value

# Effect categories — determines when effects fire
EFFECT_CAT_PASSIVE = 0
EFFECT_CAT_ACTIVE = 1
EFFECT_CAT_ONLINE = 4
EFFECT_CAT_OVERLOAD = 5

# Passive-equivalent categories (fire when module is online)
PASSIVE_EFFECT_CATS = {EFFECT_CAT_PASSIVE, EFFECT_CAT_ONLINE}

# Stacking penalty constant: 2.67^2 = 7.1289
STACKING_CONSTANT = 7.1289


def stacking_penalty(n: int) -> float:
    """Stacking penalty multiplier for the nth module (0-indexed)."""
    return math.exp(-(n ** 2) / STACKING_CONSTANT)


def apply_stacking_penalties(modifiers: list[float]) -> float:
    """Apply stacking penalties to a list of multiplicative modifiers.

    Bonuses (>1) and penalties (<1) are sorted and penalized separately.
    Returns the combined product.
    """
    bonuses = sorted([m for m in modifiers if m > 1.0], key=lambda v: -abs(v - 1))
    penalties = sorted([m for m in modifiers if m < 1.0], key=lambda v: -abs(v - 1))

    result = 1.0
    for group in (bonuses, penalties):
        for i, mod in enumerate(group):
            penalty = stacking_penalty(i)
            result *= 1 + (mod - 1) * penalty
    return result


async def get_type_dogma_attrs(db: AsyncSession, type_id: int) -> dict[int, float]:
    """Get all dogma attributes for a type. Returns {attribute_id: value}."""
    result = await db.execute(
        select(SDETypeDogmaAttribute.attribute_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id == type_id)
    )
    return {row.attribute_id: row.value for row in result.fetchall()}


async def get_types_dogma_attrs(db: AsyncSession, type_ids: list[int]) -> dict[int, dict[int, float]]:
    """Bulk get dogma attributes for multiple types."""
    if not type_ids:
        return {}
    result = await db.execute(
        select(SDETypeDogmaAttribute.type_id, SDETypeDogmaAttribute.attribute_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id.in_(type_ids))
    )
    out: dict[int, dict[int, float]] = {}
    for row in result.fetchall():
        out.setdefault(row.type_id, {})[row.attribute_id] = row.value
    return out


async def _get_stackable_flags(db: AsyncSession, attribute_ids: set[int]) -> dict[int, bool]:
    """Get stackable flag for attributes. True = NOT stacking penalized."""
    if not attribute_ids:
        return {}
    result = await db.execute(
        select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.stackable)
        .where(SDEDogmaAttribute.attribute_id.in_(attribute_ids))
    )
    return {row.attribute_id: row.stackable for row in result.fetchall()}


async def _get_module_modifiers(
    db: AsyncSession, type_ids: list[int]
) -> dict[int, list[dict]]:
    """Get all passive ship-targeting modifiers for a list of module type IDs.

    Returns {type_id: [modifier_dicts]} where each modifier has:
      modified_attribute_id, modifying_attribute_id, operator, func,
      filter_type, filter_value
    """
    if not type_ids:
        return {}

    # Get passive effects for these types
    result = await db.execute(
        select(SDETypeEffect.type_id, SDETypeEffect.effect_id)
        .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
        .where(SDETypeEffect.type_id.in_(type_ids))
        .where(SDEEffect.effect_category.in_(PASSIVE_EFFECT_CATS))
    )
    type_effects: dict[int, list[int]] = defaultdict(list)
    all_effect_ids = set()
    for row in result.fetchall():
        type_effects[row.type_id].append(row.effect_id)
        all_effect_ids.add(row.effect_id)

    if not all_effect_ids:
        return {}

    # Get modifiers for those effects that target the ship domain
    result = await db.execute(
        select(SDEModifier)
        .where(SDEModifier.effect_id.in_(all_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "itemID"]))
    )
    effect_modifiers: dict[int, list] = defaultdict(list)
    for m in result.scalars().all():
        effect_modifiers[m.effect_id].append({
            "modified_attribute_id": m.modified_attribute_id,
            "modifying_attribute_id": m.modifying_attribute_id,
            "operator": m.operator,
            "func": m.func,
            "domain": m.domain,
            "filter_type": m.filter_type,
            "filter_value": m.filter_value,
        })

    # Map back to type_ids
    out: dict[int, list[dict]] = defaultdict(list)
    for tid, eff_ids in type_effects.items():
        for eff_id in eff_ids:
            out[tid].extend(effect_modifiers.get(eff_id, []))
    return out


def _apply_modifier(attrs: dict[int, float], target_attr: int, operator: int, value: float):
    """Apply a single modifier to an attribute dict. Handles damageMultiplier default."""
    if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
        attrs[target_attr] = 1.0
    current = attrs.get(target_attr, 0)
    if current == 0 and target_attr == ATTR_DAMAGE_MULTIPLIER:
        current = 1.0

    if operator == OP_MOD_ADD:
        attrs[target_attr] = current + value
    elif operator == OP_POST_PERCENT:
        attrs[target_attr] = current * (1 + value / 100)
    elif operator == OP_POST_MUL:
        attrs[target_attr] = current * value
    elif operator == OP_PRE_MUL:
        attrs[target_attr] = current * value


async def _apply_ship_hull_bonuses(
    db: AsyncSession,
    ship_type_id: int,
    ship_attrs: dict[int, float],
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_levels: dict[int, int] | None = None,
    scaling_skill_override: list[int] | None = None,
):
    """Apply ship hull bonuses to module and charge attributes.

    Handles three modifier function types:
    - LocationGroupModifier: matches modules by group ID → module_attrs_map
    - LocationRequiredSkillModifier: matches modules by skill req → module_attrs_map
    - OwnerRequiredSkillModifier: matches ALL items (modules, drones, charges)
      by skill req → module_attrs_map for modules/drones, charge_attrs_map for charges

    Uses modifierInfo as the authoritative source for targeting.  typeBonus.jsonl
    showinfo is display-only and unreliable for matching.  Per-level vs role
    detection uses attribute name patterns (shipBonus*/eliteBonus* = per-level).

    `skill_levels` is an optional {skill_id: active_level} dict for the
    selected character; if None the function falls back to All V.
    `scaling_skill_override` lets callers supply the per-level-scaling
    skills directly (used when re-entering this function for a subsystem,
    where the scaling skill is the subsystem's required skill rather than
    a ship traits row).
    """
    # Combine module + charge type IDs for skill-req lookup
    all_type_ids = list(module_attrs_map.keys())
    charge_type_ids = list(charge_attrs_map.keys())
    combined_type_ids = list(set(all_type_ids + charge_type_ids))
    if not combined_type_ids:
        return

    # Build group lookup for modules/drones
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id)
        .where(SDEType.type_id.in_(combined_type_ids))
    )
    group_ids: dict[int, int] = {row.type_id: row.group_id for row in result.fetchall()}

    # Build skill requirement lookup for modules, drones, AND charges
    skill_reqs: dict[int, set[int]] = defaultdict(set)
    result = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(combined_type_ids))
    )
    for row in result.fetchall():
        skill_reqs[row.type_id].add(row.skill_type_id)

    charge_tid_set = set(charge_type_ids)

    # Determine which source attributes are per-level vs role bonuses.
    per_level_attr_ids: set[int] = set()

    # Get ship's passive effects and their modifiers
    result = await db.execute(
        select(SDETypeEffect.effect_id)
        .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
        .where(SDETypeEffect.type_id == ship_type_id)
        .where(SDEEffect.effect_category.in_(PASSIVE_EFFECT_CATS))
    )
    ship_effect_ids = [row[0] for row in result.fetchall()]
    if not ship_effect_ids:
        return

    # Get source attribute names to identify per-level attrs
    result = await db.execute(
        select(SDEModifier.modifying_attribute_id)
        .where(SDEModifier.effect_id.in_(ship_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
    )
    src_attr_ids = list({row[0] for row in result.fetchall()})
    if src_attr_ids:
        name_result = await db.execute(
            select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.attribute_name)
            .where(SDEDogmaAttribute.attribute_id.in_(src_attr_ids))
        )
        for row in name_result.fetchall():
            name = row.attribute_name or ""
            # Per-level attrs: shipBonusCBC1, eliteBonusGunship2,
            # subsystemBonusCaldariOffensive, etc.
            # Role attrs contain "Role": shipBonusRole7, eliteBonusViolatorsRole1
            is_per_level_name = (
                (name.startswith("shipBonus") or name.startswith("eliteBonus")
                 or name.startswith("subsystemBonus"))
                and "Role" not in name
            )
            if is_per_level_name:
                per_level_attr_ids.add(row.attribute_id)

    result = await db.execute(
        select(SDEModifier)
        .where(SDEModifier.effect_id.in_(ship_effect_ids))
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
    )

    # Resolve the per-level scaling skill(s) once. For a ship this comes
    # from SDETypeBonus (traits). For a subsystem the caller overrides it
    # with the subsystem's required skill ID(s).
    if scaling_skill_override is not None:
        scaling_skills = scaling_skill_override
    else:
        scaling_skills = await _ship_scaling_skill_ids(db, ship_type_id)
    effective_skill_level = _effective_skill_level(scaling_skills, skill_levels)

    for mod in result.scalars().all():
        src_val = ship_attrs.get(mod.modifying_attribute_id)
        if src_val is None:
            continue

        # Determine matching type IDs based on func type
        matching_type_ids = set()
        matching_charge_ids = set()

        if mod.func == "LocationGroupModifier" and mod.filter_type == "group":
            for tid, gid in group_ids.items():
                if gid == mod.filter_value and tid not in charge_tid_set:
                    matching_type_ids.add(tid)

        elif mod.func == "LocationRequiredSkillModifier" and mod.filter_type == "skill":
            for tid, skills in skill_reqs.items():
                if mod.filter_value in skills and tid not in charge_tid_set:
                    matching_type_ids.add(tid)

        elif mod.func == "OwnerRequiredSkillModifier" and mod.filter_type == "skill":
            # Matches ALL items (modules, drones, charges) requiring the skill
            for tid, skills in skill_reqs.items():
                if mod.filter_value in skills:
                    if tid in charge_tid_set:
                        matching_charge_ids.add(tid)
                    else:
                        matching_type_ids.add(tid)

        if not matching_type_ids and not matching_charge_ids:
            continue

        # Per-level if source attribute is named shipBonus* or eliteBonus*
        is_per_level = mod.modifying_attribute_id in per_level_attr_ids
        effective_val = src_val * effective_skill_level if is_per_level else src_val
        target_attr = mod.modified_attribute_id

        # Apply to matching modules/drones
        for tid in matching_type_ids:
            if tid not in module_attrs_map:
                continue
            _apply_modifier(module_attrs_map[tid], target_attr, mod.operator, effective_val)

        # Apply to matching charges
        for tid in matching_charge_ids:
            if tid not in charge_attrs_map:
                continue
            _apply_modifier(charge_attrs_map[tid], target_attr, mod.operator, effective_val)



async def _apply_all_v_skill_bonuses(
    db: AsyncSession,
    module_attrs_map: dict[int, dict[int, float]],
    charge_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_levels: dict[int, int] | None = None,
):
    """Apply skill bonuses to module, drone, and charge attributes.

    Skills (category 16) have effects that modify items on the ship via:
    - LocationGroupModifier / LocationRequiredSkillModifier (domain=shipID)
      → targets modules/drones in module_attrs_map
    - OwnerRequiredSkillModifier (domain=charID)
      → targets drones in module_attrs_map AND charges in charge_attrs_map

    When `skill_levels` is None, applies the All-V assumption (× 5 for every
    skill). When a character's skill map is passed, each skill's bonus
    scales by that character's actual active level — an untrained skill
    contributes zero.  Skills are never stacking-penalized.
    """
    all_type_ids = list(module_attrs_map.keys())
    charge_type_ids = list(charge_attrs_map.keys())
    combined = list(set(all_type_ids + charge_type_ids))
    if not combined:
        return

    # Build group and skill-requirement lookups for all item types
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id)
        .where(SDEType.type_id.in_(combined))
    )
    group_ids: dict[int, int] = {row.type_id: row.group_id for row in result.fetchall()}

    skill_reqs: dict[int, set[int]] = defaultdict(set)
    result = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(combined))
    )
    for row in result.fetchall():
        skill_reqs[row.type_id].add(row.skill_type_id)

    charge_tid_set = set(charge_type_ids)

    # Find all skill modifiers that target items on the ship or owned by character
    result = await db.execute(
        select(SDEModifier.effect_id, SDEModifier.modifying_attribute_id,
               SDEModifier.modified_attribute_id, SDEModifier.operator,
               SDEModifier.func, SDEModifier.filter_type, SDEModifier.filter_value,
               SDETypeEffect.type_id)
        .join(SDETypeEffect, SDEModifier.effect_id == SDETypeEffect.effect_id)
        .join(SDEType, SDETypeEffect.type_id == SDEType.type_id)
        .where(SDEType.category_id == 16)  # Skills
        .where(SDEModifier.domain.in_(["shipID", "charID"]))
        .where(SDEModifier.func.in_([
            "LocationGroupModifier",
            "LocationRequiredSkillModifier",
            "OwnerRequiredSkillModifier",
        ]))
    )

    skill_modifiers = result.fetchall()
    if not skill_modifiers:
        return

    # Get skill bonus attributes for all relevant skills
    skill_type_ids = list({row.type_id for row in skill_modifiers})
    skill_attrs_map = await get_types_dogma_attrs(db, skill_type_ids)

    for row in skill_modifiers:
        skill_tid = row.type_id
        src_attr_id = row.modifying_attribute_id
        target_attr_id = row.modified_attribute_id
        operator = row.operator
        func = row.func
        filter_type = row.filter_type
        filter_value = row.filter_value

        # Get the skill's bonus attribute base value
        skill_attrs = skill_attrs_map.get(skill_tid, {})
        src_val = skill_attrs.get(src_attr_id)
        if src_val is None or src_val == 0:
            continue

        # Skill bonus scales by character's level in the skill providing
        # the bonus (row.type_id IS the skill's type_id via the join).
        lvl = DEFAULT_SKILL_LEVEL if skill_levels is None else skill_levels.get(skill_tid, 0)
        if lvl == 0:
            continue
        effective_val = src_val * lvl

        # Find matching items
        matching_modules = set()
        matching_charges = set()

        if func == "LocationGroupModifier" and filter_type == "group" and filter_value:
            for tid, gid in group_ids.items():
                if gid == filter_value and tid not in charge_tid_set:
                    matching_modules.add(tid)

        elif func == "LocationRequiredSkillModifier" and filter_type == "skill" and filter_value:
            for tid, skills in skill_reqs.items():
                if filter_value in skills and tid not in charge_tid_set:
                    matching_modules.add(tid)

        elif func == "OwnerRequiredSkillModifier" and filter_type == "skill" and filter_value:
            for tid, skills in skill_reqs.items():
                if filter_value in skills:
                    if tid in charge_tid_set:
                        matching_charges.add(tid)
                    else:
                        matching_modules.add(tid)

        # Apply to matching modules/drones
        for tid in matching_modules:
            if tid in module_attrs_map:
                _apply_modifier(module_attrs_map[tid], target_attr_id, operator, effective_val)

        # Apply to matching charges
        for tid in matching_charges:
            if tid in charge_attrs_map:
                _apply_modifier(charge_attrs_map[tid], target_attr_id, operator, effective_val)


async def get_ship_stats(db: AsyncSession, ship_type_id: int) -> dict:
    """Fetch all displayable base stats for a ship from dogma attributes."""
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    stats = {}
    for name, attr_id in SHIP_STAT_ATTRS.items():
        stats[name] = attrs.get(attr_id, 0)

    stats["align_time"] = _calc_align_time(stats.get("inertia", 0), stats.get("mass", 0))

    # Resists (convert resonance to resist percentage)
    stats["shield_em_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_EM_RESONANCE, 1.0))
    stats["shield_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_THERM_RESONANCE, 1.0))
    stats["shield_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_KIN_RESONANCE, 1.0))
    stats["shield_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_SHIELD_EXPL_RESONANCE, 1.0))
    stats["armor_em_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_EM_RESONANCE, 1.0))
    stats["armor_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_THERM_RESONANCE, 1.0))
    stats["armor_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_KIN_RESONANCE, 1.0))
    stats["armor_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_ARMOR_EXPL_RESONANCE, 1.0))
    stats["hull_em_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_EM_RESONANCE, 1.0))
    stats["hull_therm_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_THERM_RESONANCE, 1.0))
    stats["hull_kin_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_KIN_RESONANCE, 1.0))
    stats["hull_expl_resist"] = _resonance_to_resist(attrs.get(ATTR_HULL_EXPL_RESONANCE, 1.0))

    return stats


# Built-in damage profiles: (em, thermal, kinetic, explosive) fractions
DAMAGE_PROFILES = {
    "uniform": (0.25, 0.25, 0.25, 0.25),
    "em": (1.0, 0.0, 0.0, 0.0),
    "thermal": (0.0, 1.0, 0.0, 0.0),
    "kinetic": (0.0, 0.0, 1.0, 0.0),
    "explosive": (0.0, 0.0, 0.0, 1.0),
    "guristas": (0.0, 0.18, 0.82, 0.0),
    "serpentis": (0.0, 0.55, 0.45, 0.0),
    "angel": (0.0, 0.0, 0.08, 0.92),
    "blood": (0.50, 0.48, 0.0, 0.02),
    "sansha": (0.53, 0.47, 0.0, 0.0),
    "sleeper": (0.22, 0.36, 0.28, 0.14),
    "triglavian": (0.0, 0.65, 0.0, 0.35),
    "drifter": (0.0, 0.0, 0.0, 1.0),
}


async def calculate_fitting_stats(
    db: AsyncSession, ship_type_id: int, items: list[dict],
    damage_profile: str = "uniform",
    skill_levels: dict[int, int] | None = None,
) -> dict:
    """Calculate aggregate fitting stats for a ship + modules.

    Applies the dogma modifier pipeline:
    1. Get base ship attributes
    2. Collect all passive modifiers from fitted modules
    3. Apply modifiers with stacking penalties
    4. Compute derived stats (align time, resists)
    """
    # Get raw ship attributes — mass/capacity from invTypes if not in dogma
    ship_attrs = await get_type_dogma_attrs(db, ship_type_id)
    if ATTR_MASS not in ship_attrs or ship_attrs[ATTR_MASS] == 0:
        type_result = await db.execute(
            select(SDEType.mass).where(SDEType.type_id == ship_type_id)
        )
        type_mass = type_result.scalar_one_or_none()
        if type_mass:
            ship_attrs[ATTR_MASS] = type_mass

    # Collect all module type IDs (excluding drones and cargo)
    # Only online modules contribute to fitting (offline modules skip CPU/PG/effects)
    fitted_items = [i for i in items
                    if i.get("slot") not in ("drone", "cargo")
                    and i.get("online", True)]
    module_type_ids = list({item["type_id"] for item in fitted_items})
    all_type_ids = list({item["type_id"] for item in items})

    # Separate subsystem items — they need special handling (slot modifiers,
    # per-level scaling) and must be excluded from the normal modifier pipeline.
    subsystem_type_ids = set(
        i["type_id"] for i in fitted_items if i.get("slot") == "subsystem"
    )
    non_sub_module_type_ids = [t for t in module_type_ids if t not in subsystem_type_ids]

    # Get module attributes and modifiers (subsystems excluded from modifiers
    # — they're processed separately below)
    module_attrs_map = await get_types_dogma_attrs(db, all_type_ids) if all_type_ids else {}
    module_modifiers = await _get_module_modifiers(db, non_sub_module_type_ids) if non_sub_module_type_ids else {}

    # Build charge attrs map early so bonuses (ship hull, skills) can modify it.
    # Deep-copied so we can mutate charge damage values.
    charge_type_ids = list({
        item["charge_type_id"] for item in items
        if item.get("charge_type_id")
    })
    charge_attrs_map: dict[int, dict[int, float]] = {}
    if charge_type_ids:
        raw_charge = await get_types_dogma_attrs(db, charge_type_ids)
        charge_attrs_map = {tid: dict(attrs) for tid, attrs in raw_charge.items()}

    # ── Apply overload bonuses to overheated module attrs ───────────────────
    # Only apply once per type_id (all copies share the same attrs dict)
    overloaded_types = set()
    for item in items:
        if item.get("overheated") and item.get("online", True):
            tid = item["type_id"]
            if tid in overloaded_types or tid not in module_attrs_map:
                continue
            overloaded_types.add(tid)
            attrs = module_attrs_map[tid] = dict(module_attrs_map.get(tid, {}))
            raw_attrs = await get_type_dogma_attrs(db, tid)
            for ol_attr_id, (target_attr, is_reduction) in OVERLOAD_ATTR_MAP.items():
                ol_val = raw_attrs.get(ol_attr_id)
                if ol_val is None or ol_val == 0:
                    continue
                if target_attr is None:
                    continue
                current = attrs.get(target_attr)
                if current is None:
                    if target_attr == ATTR_DAMAGE_MULTIPLIER:
                        current = 1.0
                    else:
                        continue
                attrs[target_attr] = current * (1 + ol_val / 100)

    # ── Apply T3C subsystem bonuses ─────────────────────────────────────────
    # Subsystems provide:
    # 1. Slot/hardpoint modifiers (special attrs not in the dogma effect pipeline)
    # 2. ItemModifier bonuses to ship attributes (CPU, PG, cap, velocity, etc.)
    # 3. LocationGroupModifier/LocationRequiredSkillModifier bonuses to modules
    # 4. OwnerRequiredSkillModifier bonuses to charges (missile damage, etc.)
    #
    # Per-level attributes (subsystemBonus*) are scaled by skill level.
    # This section must run BEFORE skill bonuses so that MOD_ADD contributions
    # (CPU, PG, HP) are included in the skill percentage calculation.
    if subsystem_type_ids:
        # 1. Apply slot/hardpoint modifiers (attributes not in effect pipeline)
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            sub_attrs = module_attrs_map.get(item["type_id"], {})
            ship_attrs[ATTR_HI_SLOTS] = ship_attrs.get(ATTR_HI_SLOTS, 0) + sub_attrs.get(ATTR_HI_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_MED_SLOTS] = ship_attrs.get(ATTR_MED_SLOTS, 0) + sub_attrs.get(ATTR_MED_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_LOW_SLOTS] = ship_attrs.get(ATTR_LOW_SLOTS, 0) + sub_attrs.get(ATTR_LOW_SLOT_MODIFIER, 0)
            ship_attrs[ATTR_TURRET_SLOTS] = ship_attrs.get(ATTR_TURRET_SLOTS, 0) + sub_attrs.get(ATTR_TURRET_HARDPOINT_MODIFIER, 0)
            ship_attrs[ATTR_LAUNCHER_SLOTS] = ship_attrs.get(ATTR_LAUNCHER_SLOTS, 0) + sub_attrs.get(ATTR_LAUNCHER_HARDPOINT_MODIFIER, 0)

        # 2. Get subsystem modifiers and detect per-level source attributes
        sub_modifiers = await _get_module_modifiers(db, list(subsystem_type_ids))

        sub_src_attr_ids = set()
        for tid in subsystem_type_ids:
            for mod in sub_modifiers.get(tid, []):
                sub_src_attr_ids.add(mod["modifying_attribute_id"])

        sub_per_level_ids: set[int] = set()
        if sub_src_attr_ids:
            name_result = await db.execute(
                select(SDEDogmaAttribute.attribute_id, SDEDogmaAttribute.attribute_name)
                .where(SDEDogmaAttribute.attribute_id.in_(sub_src_attr_ids))
            )
            for row in name_result.fetchall():
                name = row.attribute_name or ""
                if name.startswith("subsystemBonus") and "Role" not in name:
                    sub_per_level_ids.add(row.attribute_id)

        # 3. Apply ItemModifier bonuses directly to ship_attrs
        #    (CPU, PG, drone BW, cap, velocity, agility, etc.)
        #
        #    Modifiers are accumulated by (target_attr, operator) first, then
        #    applied in dogma operator order: MOD_ADD before POST_MUL before
        #    POST_PERCENT.  This ensures correct results regardless of
        #    subsystem iteration order.
        #    Algorithm reference: pyfa eos/modifiedAttributeDict.py:308-416
        #    (__calculateValue — accumulates into operator-type buckets, applies
        #    in fixed order); theorycrafter FittingEngine.kt:3490-3582
        #    (iterates Operation.entries in enum declaration order).
        sub_ship_mods: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            tid = item["type_id"]
            sub_attrs = module_attrs_map.get(tid, {})
            # Per-subsystem scaling skill lookup (e.g. Caldari Offensive
            # Systems for the Tengu's offensive subsystem). Cached for the
            # duration of this subsystem's pass.
            sub_scaling_skills = await _subsystem_scaling_skill_ids(db, tid)
            sub_effective_level = _effective_skill_level(sub_scaling_skills, skill_levels)
            for mod in sub_modifiers.get(tid, []):
                if mod["func"] != "ItemModifier" or mod["domain"] != "shipID":
                    continue
                src_val = sub_attrs.get(mod["modifying_attribute_id"])
                if src_val is None:
                    continue
                if mod["modifying_attribute_id"] in sub_per_level_ids:
                    src_val *= sub_effective_level
                sub_ship_mods[mod["modified_attribute_id"]].append(
                    (mod["operator"], src_val)
                )

        # Apply accumulated modifiers in operator order per attribute
        for attr_id, mods in sub_ship_mods.items():
            current = ship_attrs.get(attr_id, 0)
            adds = [v for op, v in mods if op == OP_MOD_ADD]
            post_muls = [v for op, v in mods if op == OP_POST_MUL]
            post_pcts = [v for op, v in mods if op == OP_POST_PERCENT]
            pre_assigns = [v for op, v in mods if op == OP_PRE_ASSIGN]
            if pre_assigns:
                current = pre_assigns[-1]
            for a in adds:
                current += a
            for m in post_muls:
                current *= m
            for p in post_pcts:
                current *= (1 + p / 100)
            ship_attrs[attr_id] = current

    # ── Apply module-to-module bonuses (Bastion, Siege, etc.) ──────────────
    # Some modules (Bastion, Siege) have effects that modify OTHER modules
    # via LocationRequiredSkillModifier / LocationGroupModifier.
    # These are different from ship hull bonuses — the source attrs are on
    # the fitted module, not the ship.
    if non_sub_module_type_ids:
        # Get all cross-module modifiers from fitted module effects.
        # Includes OwnerRequiredSkillModifier (domain=charID) for damage mods
        # like DDA that target drones by skill requirement.
        mod_cross_result = await db.execute(
            select(SDETypeEffect.type_id, SDEModifier.modifying_attribute_id,
                   SDEModifier.modified_attribute_id, SDEModifier.operator,
                   SDEModifier.func, SDEModifier.filter_type, SDEModifier.filter_value)
            .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
            .join(SDEModifier, SDEModifier.effect_id == SDETypeEffect.effect_id)
            .where(SDETypeEffect.type_id.in_(non_sub_module_type_ids))
            .where(SDEEffect.effect_category.in_(PASSIVE_EFFECT_CATS))
            .where(SDEModifier.domain.in_(["shipID", "charID"]))
            .where(SDEModifier.func.in_([
                "LocationRequiredSkillModifier",
                "LocationGroupModifier",
                "OwnerRequiredSkillModifier",
            ]))
        )

        cross_mods = mod_cross_result.fetchall()
        if cross_mods:
            # Count copies of each module type in the fit
            module_copies: dict[int, int] = defaultdict(int)
            for item in fitted_items:
                module_copies[item["type_id"]] += item.get("quantity", 1)

            # Build skill/group lookups for all module types
            all_tid_list = list(module_attrs_map.keys())
            _skill_reqs: dict[int, set[int]] = defaultdict(set)
            sr_result = await db.execute(
                select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
                .where(SDETypeSkillReq.type_id.in_(all_tid_list))
            )
            for row in sr_result.fetchall():
                _skill_reqs[row.type_id].add(row.skill_type_id)

            _group_ids: dict[int, int] = {}
            gr_result = await db.execute(
                select(SDEType.type_id, SDEType.group_id)
                .where(SDEType.type_id.in_(all_tid_list))
            )
            _group_ids = {row.type_id: row.group_id for row in gr_result.fetchall()}

            # Get stackable flags for stacking penalty check
            cross_target_attrs = {cm[2] for cm in cross_mods}
            _stackable = await _get_stackable_flags(db, cross_target_attrs)

            # Collect cross-module multipliers grouped by (target_tid, target_attr, source_type).
            # Grouping by source type means copies of the same damage mod stack-penalize
            # each other, but different module types (e.g. Bastion vs Heat Sinks)
            # are independent groups whose products are multiplied together.
            # Key: (target_tid, target_attr, source_type_id) → list of multipliers
            cross_collectors: dict[tuple[int, int, int], list[float]] = defaultdict(list)

            for cm in cross_mods:
                src_type_id = cm[0]
                src_attrs = module_attrs_map.get(src_type_id, {})
                src_val = src_attrs.get(cm[1])
                if src_val is None:
                    continue

                # Find target modules/drones
                matching = set()
                if cm[4] == "LocationRequiredSkillModifier" and cm[5] == "skill":
                    for tid, skills in _skill_reqs.items():
                        if tid != src_type_id and cm[6] in skills:
                            matching.add(tid)
                elif cm[4] == "LocationGroupModifier" and cm[5] == "group":
                    for tid, gid in _group_ids.items():
                        if tid != src_type_id and gid == cm[6]:
                            matching.add(tid)
                elif cm[4] == "OwnerRequiredSkillModifier" and cm[5] == "skill":
                    for tid, skills in _skill_reqs.items():
                        if tid != src_type_id and cm[6] in skills:
                            matching.add(tid)

                if not matching:
                    continue

                target_attr = cm[2]
                operator = cm[3]
                copies = module_copies.get(src_type_id, 1)

                for tid in matching:
                    if tid not in module_attrs_map:
                        continue

                    if operator == OP_POST_MUL:
                        for _ in range(copies):
                            cross_collectors[(tid, target_attr, src_type_id)].append(src_val)
                    elif operator == OP_POST_PERCENT:
                        for _ in range(copies):
                            cross_collectors[(tid, target_attr, src_type_id)].append(1.0 + src_val / 100.0)
                    elif operator == OP_MOD_ADD:
                        attrs = module_attrs_map[tid]
                        attrs[target_attr] = attrs.get(target_attr, 0) + src_val * copies

            # Apply multipliers per group with stacking penalties, then combine.
            # Reorganize: (target_tid, target_attr) → list of per-group products.
            combined: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
            for (tid, target_attr, src_tid), multipliers in cross_collectors.items():
                is_stackable = _stackable.get(target_attr, True)
                if is_stackable or len(multipliers) <= 1:
                    group_product = 1.0
                    for m in multipliers:
                        group_product *= m
                else:
                    group_product = apply_stacking_penalties(multipliers)
                combined[(tid, target_attr)] *= group_product

            for (tid, target_attr), product in combined.items():
                attrs = module_attrs_map[tid]
                if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
                    attrs[target_attr] = 1.0
                current = attrs.get(target_attr, 0)
                if current == 0 and target_attr == ATTR_DAMAGE_MULTIPLIER:
                    current = 1.0
                attrs[target_attr] = current * product

    # ── Apply All-V fitting skills to ship attributes ─────────────────────
    # These skills use ItemModifier with domain=shipID to modify the ship
    # directly.  At All V the bonus is base_attr * 5 applied as postPercent.
    # CPU Management V / Power Grid Management V: +25% CPU/PG output
    ship_attrs[ATTR_CPU_OUTPUT] = ship_attrs.get(ATTR_CPU_OUTPUT, 0) * 1.25
    ship_attrs[ATTR_POWER_OUTPUT] = ship_attrs.get(ATTR_POWER_OUTPUT, 0) * 1.25
    # Shield Management V: +25% shield HP (attr 337 base=5, *5=25%)
    ship_attrs[ATTR_SHIELD_HP] = ship_attrs.get(ATTR_SHIELD_HP, 0) * 1.25
    # Hull Upgrades V: +25% armor HP (attr 335 base=5, *5=25%)
    ship_attrs[ATTR_ARMOR_HP] = ship_attrs.get(ATTR_ARMOR_HP, 0) * 1.25
    # Mechanics V: +25% hull HP (attr 327 base=5, *5=25%)
    ship_attrs[ATTR_HP] = ship_attrs.get(ATTR_HP, 0) * 1.25

    # ── Apply All-V weapon/support skill bonuses to module attributes ─────
    # Skills like Surgical Strike, Rapid Firing, etc. have modifiers that
    # target modules by group or required skill. At All V, the bonus is
    # skill_base_attr * 5, applied as postPercent.
    await _apply_all_v_skill_bonuses(
        db, module_attrs_map, charge_attrs_map, items, skill_levels=skill_levels,
    )

    # ── Apply ship hull bonuses to module/charge attributes ──────────────
    # Makes deep copies so we don't mutate cached SDE data
    module_attrs_map = {tid: dict(attrs) for tid, attrs in module_attrs_map.items()}
    await _apply_ship_hull_bonuses(
        db, ship_type_id, ship_attrs, module_attrs_map, charge_attrs_map, items,
        skill_levels=skill_levels,
    )

    # ── Apply subsystem bonuses to module/charge attributes ──────────────
    # Subsystem LocationGroupModifier/LocationRequiredSkillModifier/
    # OwnerRequiredSkillModifier bonuses work like ship hull bonuses but
    # originate from the fitted subsystem. Re-use _apply_ship_hull_bonuses
    # with each subsystem as the source (per-level detection extended to
    # cover subsystemBonus* attributes).
    if subsystem_type_ids:
        for item in fitted_items:
            if item.get("slot") != "subsystem":
                continue
            sub_attrs = module_attrs_map.get(item["type_id"], {})
            sub_scaling = await _subsystem_scaling_skill_ids(db, item["type_id"])
            await _apply_ship_hull_bonuses(
                db, item["type_id"], sub_attrs,
                module_attrs_map, charge_attrs_map, items,
                skill_levels=skill_levels,
                scaling_skill_override=sub_scaling,
            )

    # ── Collect character-level missile damage multiplier (BCU mechanism) ──
    # BCU sets character attr 212 (missileDamageMultiplier) via ItemModifier
    # with domain=charID.  Accumulate from all online fitted modules that have
    # this modifier, then apply as a global scale on missile charge damage.
    char_missile_dmg_mult = 1.0
    if module_type_ids:
        bcu_result = await db.execute(
            select(SDETypeEffect.type_id, SDEModifier.modifying_attribute_id,
                   SDEModifier.operator)
            .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
            .join(SDEModifier, SDEModifier.effect_id == SDETypeEffect.effect_id)
            .where(SDETypeEffect.type_id.in_(module_type_ids))
            .where(SDEEffect.effect_category.in_(PASSIVE_EFFECT_CATS))
            .where(SDEModifier.domain == "charID")
            .where(SDEModifier.func == "ItemModifier")
            .where(SDEModifier.modified_attribute_id == ATTR_MISSILE_DAMAGE_MULTIPLIER)
        )
        bcu_mods = bcu_result.fetchall()
        if bcu_mods:
            # Count copies and collect multipliers
            module_copies: dict[int, int] = defaultdict(int)
            for item in fitted_items:
                module_copies[item["type_id"]] += item.get("quantity", 1)
            bcu_multipliers: list[float] = []
            for row in bcu_mods:
                src_tid = row.type_id
                src_attr_id = row.modifying_attribute_id
                operator = row.operator
                src_val = module_attrs_map.get(src_tid, {}).get(src_attr_id)
                if src_val is None or src_val == 0:
                    continue
                copies = module_copies.get(src_tid, 0)
                for _ in range(copies):
                    if operator == OP_PRE_MUL:
                        bcu_multipliers.append(src_val)
                    elif operator == OP_POST_PERCENT:
                        bcu_multipliers.append(1.0 + src_val / 100.0)
            if bcu_multipliers:
                # BCU multipliers are stacking-penalized
                stackable_result = await _get_stackable_flags(
                    db, {ATTR_MISSILE_DAMAGE_MULTIPLIER}
                )
                is_stackable = stackable_result.get(ATTR_MISSILE_DAMAGE_MULTIPLIER, True)
                if is_stackable or len(bcu_multipliers) <= 1:
                    for m in bcu_multipliers:
                        char_missile_dmg_mult *= m
                else:
                    char_missile_dmg_mult *= apply_stacking_penalties(bcu_multipliers)

    # Fetch module slot info for turret/launcher counting
    slot_info = {}
    if module_type_ids:
        slot_result = await db.execute(
            select(SDEModuleSlot.type_id, SDEModuleSlot.is_turret, SDEModuleSlot.is_launcher)
            .where(SDEModuleSlot.type_id.in_(module_type_ids))
        )
        for row in slot_result.fetchall():
            slot_info[row.type_id] = {"is_turret": row.is_turret, "is_launcher": row.is_launcher}

    # Fetch drone volumes
    drone_items = [i for i in items if i.get("slot") == "drone"]
    drone_volumes = {}
    if drone_items:
        drone_type_ids = list({d["type_id"] for d in drone_items})
        vol_result = await db.execute(
            select(SDEType.type_id, SDEType.volume)
            .where(SDEType.type_id.in_(drone_type_ids))
        )
        drone_volumes = {row.type_id: row.volume or 0 for row in vol_result.fetchall()}

    # ── Step 1: Collect all attribute modifiers from fitted modules ────────
    # modifier_collectors[target_attr_id] = list of (operator, value) tuples
    mod_collectors: dict[int, list[tuple[int, float]]] = defaultdict(list)

    for item in fitted_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        mods = module_modifiers.get(tid, [])

        for mod in mods:
            # Only apply ship-domain modifiers (or item-domain for self-modifying)
            if mod["domain"] != "shipID":
                continue

            # Get the source attribute value from the module
            src_val = mod_attrs.get(mod["modifying_attribute_id"])
            if src_val is None:
                continue

            target_attr = mod["modified_attribute_id"]
            operator = mod["operator"]

            # Apply for each copy of the module
            for _ in range(qty):
                mod_collectors[target_attr].append((operator, src_val))

    # ── Step 2: Apply modifiers to ship attributes ────────────────────────
    # Get stackable flags for all modified attributes
    all_modified_attrs = set(mod_collectors.keys())
    stackable_flags = await _get_stackable_flags(db, all_modified_attrs)

    # Build modified ship attributes
    modified_attrs = dict(ship_attrs)

    for attr_id, modifiers in mod_collectors.items():
        base = modified_attrs.get(attr_id, 0)

        # Group by CCP operator
        pre_assigns = [v for op, v in modifiers if op == OP_PRE_ASSIGN]
        pre_muls = [v for op, v in modifiers if op == OP_PRE_MUL]
        pre_divs = [v for op, v in modifiers if op == OP_PRE_DIV]
        mod_adds = [v for op, v in modifiers if op == OP_MOD_ADD]
        mod_subs = [v for op, v in modifiers if op == OP_MOD_SUB]
        post_muls = [v for op, v in modifiers if op == OP_POST_MUL]
        post_divs = [v for op, v in modifiers if op == OP_POST_DIV]
        post_pcts = [v for op, v in modifiers if op == OP_POST_PERCENT]
        post_assigns = [v for op, v in modifiers if op == OP_POST_ASSIGN]

        # POST_ASSIGN: force/lock — skip all other modifiers
        if post_assigns:
            modified_attrs[attr_id] = post_assigns[-1]
            continue

        val = base

        # PRE_ASSIGN: override base
        if pre_assigns:
            val = pre_assigns[-1]

        # PRE_MUL / PRE_DIV
        for m in pre_muls:
            val *= m
        for d in pre_divs:
            if d != 0:
                val /= d

        # MOD_ADD / MOD_SUB: flat changes
        for a in mod_adds:
            val += a
        for s in mod_subs:
            val -= s

        # POST_MUL: multiplicative — stacking penalized if attribute not stackable
        is_stackable = stackable_flags.get(attr_id, True)
        if post_muls:
            if is_stackable:
                for m in post_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(post_muls)

        # POST_DIV
        for d in post_divs:
            if d != 0:
                val /= d

        # POST_PERCENT: val = val * (1 + modifier/100) — stacking penalized if needed
        if post_pcts:
            pct_muls = [1.0 + p / 100.0 for p in post_pcts]
            if is_stackable:
                for m in pct_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(pct_muls)

        modified_attrs[attr_id] = val

    # ── Step 3: Calculate resource usage (from module attributes) ─────────
    cpu_used = 0.0
    pg_used = 0.0
    calibration_used = 0.0
    turrets_used = 0
    launchers_used = 0
    drone_bw_used = 0.0
    drone_bay_used = 0.0

    for item in items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        slot = item.get("slot", "")

        if slot == "drone":
            drone_bw_used += mod_attrs.get(ATTR_DRONE_BW_USED, 0) * qty
            drone_bay_used += drone_volumes.get(tid, 0) * qty
            continue

        if slot == "cargo":
            continue

        # Offline modules don't consume CPU/PG (rigs always count calibration)
        if not item.get("online", True) and slot != "rig":
            continue

        cpu_used += mod_attrs.get(ATTR_CPU, 0) * qty
        pg_used += mod_attrs.get(ATTR_POWER, 0) * qty

        if slot == "rig":
            calibration_used += mod_attrs.get(ATTR_UPGRADE_COST, 0) * qty

        si = slot_info.get(tid, {})
        if si.get("is_turret"):
            turrets_used += qty
        if si.get("is_launcher"):
            launchers_used += qty

    # ── Step 4: Build stats from modified attributes ──────────────────────
    def mattr(attr_id, default=0):
        return modified_attrs.get(attr_id, default)

    # Recalculate derived stats from modified attributes
    align_time = _calc_align_time(mattr(ATTR_INERTIA), mattr(ATTR_MASS))

    # Recalculate resists from modified resonances
    shield_em_res = mattr(ATTR_SHIELD_EM_RESONANCE, 1.0)
    shield_therm_res = mattr(ATTR_SHIELD_THERM_RESONANCE, 1.0)
    shield_kin_res = mattr(ATTR_SHIELD_KIN_RESONANCE, 1.0)
    shield_expl_res = mattr(ATTR_SHIELD_EXPL_RESONANCE, 1.0)
    armor_em_res = mattr(ATTR_ARMOR_EM_RESONANCE, 1.0)
    armor_therm_res = mattr(ATTR_ARMOR_THERM_RESONANCE, 1.0)
    armor_kin_res = mattr(ATTR_ARMOR_KIN_RESONANCE, 1.0)
    armor_expl_res = mattr(ATTR_ARMOR_EXPL_RESONANCE, 1.0)
    hull_em_res = mattr(ATTR_HULL_EM_RESONANCE, 1.0)
    hull_therm_res = mattr(ATTR_HULL_THERM_RESONANCE, 1.0)
    hull_kin_res = mattr(ATTR_HULL_KIN_RESONANCE, 1.0)
    hull_expl_res = mattr(ATTR_HULL_EXPL_RESONANCE, 1.0)

    # EHP — uniform damage profile (25/25/25/25)
    shield_hp = mattr(ATTR_SHIELD_HP)
    armor_hp = mattr(ATTR_ARMOR_HP)
    hull_hp = mattr(ATTR_HP)
    dmg_prof = DAMAGE_PROFILES.get(damage_profile, DAMAGE_PROFILES["uniform"])
    shield_ehp = _calc_ehp(shield_hp, shield_em_res, shield_therm_res, shield_kin_res, shield_expl_res, dmg_prof)
    armor_ehp = _calc_ehp(armor_hp, armor_em_res, armor_therm_res, armor_kin_res, armor_expl_res, dmg_prof)
    hull_ehp = _calc_ehp(hull_hp, hull_em_res, hull_therm_res, hull_kin_res, hull_expl_res, dmg_prof)
    total_ehp = shield_ehp + armor_ehp + hull_ehp

    # ── Capacitor simulation (discrete-event) ──────────────────────────
    cap_capacity = mattr(ATTR_CAPACITOR)
    cap_recharge_ms = mattr(ATTR_CAP_RECHARGE)  # in ms

    # Build module drain list for the simulator
    cap_sim_modules: list[dict] = []
    for item in fitted_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        cap_need = mod_attrs.get(ATTR_CAPACITOR_NEED, 0)
        duration = mod_attrs.get(ATTR_DURATION, 0) or mod_attrs.get(ATTR_RATE_OF_FIRE, 0)
        if cap_need != 0 and duration > 0:
            si = slot_info.get(tid, {})
            cap_sim_modules.append({
                "cap_need": cap_need,
                "duration_ms": duration,
                "count": qty,
                "stagger": not si.get("is_turret", False),  # turrets fire together
            })

    cap_result = simulate_cap(cap_capacity, cap_recharge_ms, cap_sim_modules)
    cap_stable = cap_result["stable"]
    cap_stable_pct = cap_result["stable_pct"]
    cap_lasts_s = cap_result["time_to_empty_s"]
    peak_cap_recharge = cap_result["peak_recharge"]
    total_cap_drain = cap_result["total_drain"]

    # Shield recharge (passive tank)
    shield_recharge_ms = mattr(ATTR_SHIELD_RECHARGE_RATE, 0)
    shield_recharge_s = shield_recharge_ms / 1000 if shield_recharge_ms else 0
    peak_shield_recharge = _calc_peak_recharge(shield_hp, shield_recharge_s)

    # ── Active tank (rep/s) ──────────────────────────────────────────────
    armor_rep_rate = 0.0
    shield_rep_rate = 0.0
    for item in fitted_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        duration = mod_attrs.get(ATTR_DURATION, 0)
        if duration <= 0:
            continue
        cycle_s = duration / 1000.0
        # Armor repair modules have armorDamageAmount (attr 84)
        armor_rep = mod_attrs.get(ATTR_ARMOR_DAMAGE_AMOUNT, 0)
        if armor_rep > 0:
            armor_rep_rate += (armor_rep / cycle_s) * qty
        # Shield boost modules have shieldBonus (attr 68)
        shield_rep = mod_attrs.get(ATTR_SHIELD_BONUS, 0)
        if shield_rep > 0:
            shield_rep_rate += (shield_rep / cycle_s) * qty

    # ── Step 6: DPS calculation ──────────────────────────────────────────
    # charge_attrs_map was built early and modified by skill/hull bonus steps.
    weapon_dps = 0.0
    weapon_dps_max_spool = 0.0  # DPS at max spool (Triglavian)
    weapon_volley = 0.0
    drone_dps = 0.0
    spool_time_s = 0  # Time to reach max spool

    # Determine how many drones of each type are active (within bandwidth).
    # Sort by bandwidth cost ascending to maximize active drone count,
    # matching Pyfa's approach of capping by bandwidth.
    # Algorithm reference: pyfa eos/saveddata/drone.py:163-168 (amountActive
    # gates DPS); theorycrafter Mechanics.kt:1204-1236 (maxDroneGroupSize
    # caps at bandwidth/bwPerDrone).
    drone_bw_total_ship = mattr(SHIP_STAT_ATTRS["drone_bandwidth"])
    drone_active_counts: dict[int, int] = {}  # type_id → active count
    drone_entries = [(i["type_id"], i.get("quantity", 1)) for i in items if i.get("slot") == "drone"]
    # Sort by per-drone DPS descending so highest-DPS drones fill first
    # (matches typical player behavior — activate strongest drones).
    drone_bw_per = {}
    drone_dps_per: dict[int, float] = {}
    for tid, qty in drone_entries:
        if tid not in drone_bw_per:
            d_attrs = module_attrs_map.get(tid, {})
            drone_bw_per[tid] = d_attrs.get(ATTR_DRONE_BW_USED, 0)
            d_dmg = sum(d_attrs.get(a, 0) for a in (ATTR_EM_DAMAGE, ATTR_THERMAL_DAMAGE, ATTR_KINETIC_DAMAGE, ATTR_EXPLOSIVE_DAMAGE))
            d_mult = d_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
            d_cycle = d_attrs.get(ATTR_DURATION, 0) or d_attrs.get(ATTR_RATE_OF_FIRE, 0)
            drone_dps_per[tid] = (d_dmg * d_mult / (d_cycle / 1000)) if d_cycle > 0 and d_dmg > 0 else 0
    drone_entries.sort(key=lambda x: -drone_dps_per.get(x[0], 0))
    bw_remaining = drone_bw_total_ship
    for tid, qty in drone_entries:
        bw_each = drone_bw_per.get(tid, 0)
        if bw_each <= 0:
            drone_active_counts[tid] = drone_active_counts.get(tid, 0) + qty
            continue
        can_fit = int(bw_remaining / bw_each) if bw_each > 0 else qty
        active = min(qty, can_fit)
        drone_active_counts[tid] = drone_active_counts.get(tid, 0) + active
        bw_remaining -= active * bw_each

    for item in items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        slot = item.get("slot", "")

        if slot == "drone":
            # Drone DPS: only count drones within bandwidth
            active_qty = min(qty, drone_active_counts.get(tid, 0))
            if active_qty <= 0:
                continue
            # Decrement so subsequent entries of same type don't double-count
            drone_active_counts[tid] = drone_active_counts.get(tid, 0) - active_qty
            em = mod_attrs.get(ATTR_EM_DAMAGE, 0)
            therm = mod_attrs.get(ATTR_THERMAL_DAMAGE, 0)
            kin = mod_attrs.get(ATTR_KINETIC_DAMAGE, 0)
            expl = mod_attrs.get(ATTR_EXPLOSIVE_DAMAGE, 0)
            dmg_mult = mod_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
            cycle = mod_attrs.get(ATTR_DURATION, 0) or mod_attrs.get(ATTR_RATE_OF_FIRE, 0)
            if cycle > 0 and (em + therm + kin + expl) > 0:
                volley = (em + therm + kin + expl) * dmg_mult
                drone_dps += (volley / (cycle / 1000)) * active_qty
            continue

        if slot in ("cargo", "drone"):
            continue

        # Offline weapons don't fire
        if not item.get("online", True):
            continue

        # Turret/Launcher DPS: charge damage × module damageMultiplier / cycleTime
        charge_tid = item.get("charge_type_id")
        if not charge_tid:
            continue

        charge_attrs = charge_attrs_map.get(charge_tid, {})
        em = charge_attrs.get(ATTR_EM_DAMAGE, 0)
        therm = charge_attrs.get(ATTR_THERMAL_DAMAGE, 0)
        kin = charge_attrs.get(ATTR_KINETIC_DAMAGE, 0)
        expl = charge_attrs.get(ATTR_EXPLOSIVE_DAMAGE, 0)
        total_dmg = em + therm + kin + expl
        if total_dmg <= 0:
            continue

        dmg_mult = mod_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
        cycle = mod_attrs.get(ATTR_RATE_OF_FIRE, 0) or mod_attrs.get(ATTR_DURATION, 0)
        if cycle <= 0:
            continue

        # Apply character-level missile damage multiplier (from BCU) to launchers.
        # Launchers don't have their own damageMultiplier — all damage comes from
        # the charge.  BCU scales charge damage via the character's attr 212.
        si = slot_info.get(tid, {})
        if si.get("is_launcher") and char_missile_dmg_mult != 1.0:
            total_dmg *= char_missile_dmg_mult

        volley = total_dmg * dmg_mult
        weapon_volley += volley * qty
        weapon_dps += (volley / (cycle / 1000)) * qty

        # Spool-up: Triglavian entropic disintegrators ramp damage per cycle.
        # Spool is a separate multiplier on the volley (matching Pyfa/in-game):
        #   spool_volley = base_volley × (1 + spoolBoost)
        # where spoolBoost = damageMultiplierBonusMax (already modified by ship
        # hull bonuses like Babaroga's +100%).  The base volley already includes
        # all skill/ship/mod multipliers on damageMultiplier.
        spool_per_cycle = mod_attrs.get(ATTR_DMG_MULT_BONUS_PER_CYCLE, 0)
        spool_max = mod_attrs.get(ATTR_DMG_MULT_BONUS_MAX, 0)
        if spool_per_cycle > 0 and spool_max > 0:
            spool_volley = volley * (1 + spool_max)
            weapon_dps_max_spool += (spool_volley / (cycle / 1000)) * qty
            cycles_to_max = int(spool_max / spool_per_cycle)
            spool_time_s = max(spool_time_s, int(cycles_to_max * cycle / 1000))
        else:
            weapon_dps_max_spool += (volley / (cycle / 1000)) * qty

    total_dps = weapon_dps + drone_dps
    total_dps_max_spool = weapon_dps_max_spool + drone_dps

    return {
        "cpu_used": round(cpu_used, 1),
        "cpu_total": round(mattr(SHIP_STAT_ATTRS["cpu_output"]), 1),
        "pg_used": round(pg_used, 1),
        "pg_total": round(mattr(SHIP_STAT_ATTRS["pg_output"]), 1),
        "calibration_used": round(calibration_used),
        "calibration_total": round(mattr(SHIP_STAT_ATTRS["calibration_output"])),
        "turrets_used": turrets_used,
        "turrets_total": int(mattr(SHIP_STAT_ATTRS["turret_slots"])),
        "launchers_used": launchers_used,
        "launchers_total": int(mattr(SHIP_STAT_ATTRS["launcher_slots"])),
        "drone_bw_used": round(drone_bw_used, 1),
        "drone_bw_total": round(mattr(SHIP_STAT_ATTRS["drone_bandwidth"]), 1),
        "drone_bay_used": round(drone_bay_used, 1),
        "drone_bay_total": round(mattr(SHIP_STAT_ATTRS["drone_capacity"]), 1),
        "hull_hp": round(hull_hp),
        "armor_hp": round(armor_hp),
        "shield_hp": round(shield_hp),
        "shield_ehp": round(shield_ehp),
        "armor_ehp": round(armor_ehp),
        "hull_ehp": round(hull_ehp),
        "total_ehp": round(total_ehp),
        "max_velocity": round(mattr(SHIP_STAT_ATTRS["max_velocity"]), 1),
        "mass": round(mattr(SHIP_STAT_ATTRS["mass"])),
        "inertia": round(mattr(SHIP_STAT_ATTRS["inertia"]), 4),
        "align_time": round(align_time, 1),
        "warp_speed_au_s": round(
            mattr(SHIP_STAT_ATTRS["base_warp_speed"])
            * mattr(SHIP_STAT_ATTRS["warp_speed_multiplier"]),
            2,
        ),
        "capacitor": round(mattr(SHIP_STAT_ATTRS["capacitor"]), 1),
        "cap_recharge": round(mattr(SHIP_STAT_ATTRS["cap_recharge"]) / 1000, 1) if mattr(SHIP_STAT_ATTRS["cap_recharge"]) else 0,
        "max_target_range": round(mattr(SHIP_STAT_ATTRS["max_target_range"]) / 1000, 1),
        "max_locked_targets": int(mattr(SHIP_STAT_ATTRS["max_locked_targets"])),
        "scan_resolution": round(mattr(SHIP_STAT_ATTRS["scan_resolution"]), 1),
        "sig_radius": round(mattr(SHIP_STAT_ATTRS["sig_radius"]), 1),
        "lock_time": round(_calc_lock_time(
            mattr(SHIP_STAT_ATTRS["scan_resolution"]),
            mattr(SHIP_STAT_ATTRS["sig_radius"]),
        ), 1),
        "cargo_capacity": round(mattr(SHIP_STAT_ATTRS["cargo_capacity"]), 1),
        "hi_slots": int(mattr(SHIP_STAT_ATTRS["hi_slots"])),
        "med_slots": int(mattr(SHIP_STAT_ATTRS["med_slots"])),
        "low_slots": int(mattr(SHIP_STAT_ATTRS["low_slots"])),
        "rig_slots": int(mattr(SHIP_STAT_ATTRS["rig_slots"])),
        "shield_em_resist": _resonance_to_resist(shield_em_res),
        "shield_therm_resist": _resonance_to_resist(shield_therm_res),
        "shield_kin_resist": _resonance_to_resist(shield_kin_res),
        "shield_expl_resist": _resonance_to_resist(shield_expl_res),
        "armor_em_resist": _resonance_to_resist(armor_em_res),
        "armor_therm_resist": _resonance_to_resist(armor_therm_res),
        "armor_kin_resist": _resonance_to_resist(armor_kin_res),
        "armor_expl_resist": _resonance_to_resist(armor_expl_res),
        "hull_em_resist": _resonance_to_resist(hull_em_res),
        "hull_therm_resist": _resonance_to_resist(hull_therm_res),
        "hull_kin_resist": _resonance_to_resist(hull_kin_res),
        "hull_expl_resist": _resonance_to_resist(hull_expl_res),
        # Cap stability
        "cap_stable": cap_stable,
        "cap_stable_pct": round(cap_stable_pct, 1),
        "cap_drain_rate": round(total_cap_drain, 1),
        "peak_cap_recharge": round(peak_cap_recharge, 1),
        "cap_lasts_s": round(cap_lasts_s),
        # Shield passive recharge
        "peak_shield_recharge": round(peak_shield_recharge, 1),
        # EHP multiplier per layer (1 / avg_resonance) for HP→EHP conversion
        "shield_ehp_mult": round(shield_ehp / shield_hp, 2) if shield_hp > 0 else 1.0,
        "armor_ehp_mult": round(armor_ehp / armor_hp, 2) if armor_hp > 0 else 1.0,
        "hull_ehp_mult": round(hull_ehp / hull_hp, 2) if hull_hp > 0 else 1.0,
        # Active tank
        "armor_rep_rate": round(armor_rep_rate, 1),
        "shield_rep_rate": round(shield_rep_rate, 1),
        # DPS
        "weapon_dps": round(weapon_dps, 1),
        "drone_dps": round(drone_dps, 1),
        "total_dps": round(total_dps, 1),
        "weapon_volley": round(weapon_volley),
        "weapon_dps_max_spool": round(weapon_dps_max_spool, 1),
        "total_dps_max_spool": round(total_dps_max_spool, 1),
        "spool_time_s": spool_time_s,
    }


def _calc_align_time(inertia: float, mass: float) -> float:
    if not inertia or not mass:
        return 0
    return -math.log(0.25) * inertia * mass / 1_000_000


def _calc_lock_time(scan_resolution: float, target_sig_radius: float) -> float:
    """Time to lock a target with the given sig radius.

    Formula: 40000 / (scanResolution * asinh(sigRadius)^2), capped at 30 min.
    Uses own sig radius as default target (self-lock estimate).
    """
    if scan_resolution <= 0 or target_sig_radius <= 0:
        return 0
    asinh_sig = math.asinh(target_sig_radius)
    if asinh_sig <= 0:
        return 0
    lock_time = 40000.0 / (scan_resolution * asinh_sig ** 2)
    return min(lock_time, 1800.0)  # cap at 30 minutes


def _resonance_to_resist(resonance: float) -> float:
    """Convert damage resonance (0-1) to resist percentage (0-100)."""
    return round((1.0 - resonance) * 100, 1)


def _calc_ehp(
    hp: float, em_res: float, therm_res: float, kin_res: float, expl_res: float,
    damage_profile: tuple[float, float, float, float] = (0.25, 0.25, 0.25, 0.25),
) -> float:
    """Calculate EHP for one layer against a damage profile.

    Resonance is 0-1 where 0 = 100% resist, 1 = 0% resist.
    EHP = HP / weighted_avg(resonances, damage_profile)

    damage_profile is (em, therm, kin, expl) fractions summing to 1.0.
    """
    if hp <= 0:
        return 0
    em_w, th_w, ki_w, ex_w = damage_profile
    weighted_res = em_res * em_w + therm_res * th_w + kin_res * ki_w + expl_res * ex_w
    if weighted_res <= 0:
        return hp * 1000  # near-infinite EHP at 100% resist
    return hp / weighted_res


def _calc_peak_recharge(capacity: float, recharge_time_s: float) -> float:
    """Peak recharge rate at 25% capacity.

    Formula: 2.5 * capacity / recharge_time
    Source: Pyfa eos/saveddata/fit.py, docs/fitting-mechanics.md
    """
    if capacity <= 0 or recharge_time_s <= 0:
        return 0
    return 2.5 * capacity / recharge_time_s


def _find_cap_stable_pct(capacity: float, recharge_s: float, drain_rate: float) -> float:
    """Find the equilibrium cap percentage where recharge = drain.

    Cap recharge: dC/dt = (10 * C_max / tau) * (sqrt(C/C_max) - C/C_max)
    At equilibrium: recharge_rate = drain_rate
    Solve: (10 * C_max / tau) * (sqrt(p) - p) = drain_rate
    where p = C/C_max (fraction)

    Rearranging: sqrt(p) - p = drain_rate * tau / (10 * C_max)
    Let k = drain_rate * tau / (10 * C_max)
    sqrt(p) - p = k  →  sqrt(p) = k + p  →  p = (k + p)^2

    Binary search for p in [0, 0.25] where peak is at p=0.25.
    """
    if capacity <= 0 or recharge_s <= 0:
        return 100.0
    k = drain_rate * recharge_s / (10 * capacity)
    # Max possible k is 0.25 (at peak recharge). If k > 0.25, not stable.
    if k > 0.25:
        return 0.0

    # Binary search
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2
        p_sqrt = math.sqrt(mid)
        recharge_at_mid = p_sqrt - mid  # normalized recharge
        if recharge_at_mid > k:
            lo = mid  # can sustain at higher cap
        else:
            hi = mid
    return round(lo * 100, 1)
