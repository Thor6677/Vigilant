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
)

# Default skill level assumption
DEFAULT_SKILL_LEVEL = 5

# Keyword-to-attribute mapping for typeBonus.jsonl bonusText parsing.
# Maps keywords found in English bonus text to (attribute_id, is_multiplicative).
# Multiplicative bonuses use postPercent (val * (1 + bonus/100)).
# Source: docs/fitting-mechanics.md, verified against Pyfa behavior.
BONUS_KEYWORD_ATTRS = {
    "damage": [ATTR_DAMAGE_MULTIPLIER],
    "rate of fire": [ATTR_RATE_OF_FIRE],
    "optimal range": [54],           # maxRange
    "falloff": [158],                # falloff
    "tracking speed": [160],         # trackingSpeed
    "hitpoints": [9, 265, 263],      # hull, armor, shield — context-dependent
    "max velocity": [37],            # maxVelocity
    "amount": [84],                  # armorDamageAmount (repair)
    "shield boost amount": [68],     # shieldBonus
    "mining yield": [194],           # miningAmount
    "drone operation range": [458],  # droneControlRange
}

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


async def _apply_ship_hull_bonuses(
    db: AsyncSession,
    ship_type_id: int,
    ship_attrs: dict[int, float],
    module_attrs_map: dict[int, dict[int, float]],
    items: list[dict],
    skill_level: int = DEFAULT_SKILL_LEVEL,
):
    """Apply ship hull bonuses to module attributes (modifies module_attrs_map in-place).

    Uses two data sources:
    1. SDETypeBonus (from typeBonus.jsonl) — parsed ship traits with keyword matching
    2. SDEModifier (from modifierInfo) — fallback for effects with structured modifiers

    The typeBonus approach covers per-level and role bonuses that modifierInfo misses.
    """
    all_type_ids = list(module_attrs_map.keys())
    if not all_type_ids:
        return

    # Build skill requirement and group lookups for modules
    skill_reqs: dict[int, set[int]] = defaultdict(set)
    result = await db.execute(
        select(SDETypeSkillReq.type_id, SDETypeSkillReq.skill_type_id)
        .where(SDETypeSkillReq.type_id.in_(all_type_ids))
    )
    for row in result.fetchall():
        skill_reqs[row.type_id].add(row.skill_type_id)

    group_ids: dict[int, int] = {}
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id)
        .where(SDEType.type_id.in_(all_type_ids))
    )
    group_ids = {row.type_id: row.group_id for row in result.fetchall()}

    # ── Source 1: typeBonus.jsonl parsed bonuses ──────────────────────────
    result = await db.execute(
        select(SDETypeBonus)
        .where(SDETypeBonus.type_id == ship_type_id)
    )
    type_bonuses = result.scalars().all()

    for tb in type_bonuses:
        if not tb.bonus_keyword or not tb.target_type_id:
            continue

        # Determine effective bonus value
        if tb.is_role_bonus:
            effective_val = tb.bonus_value
        else:
            effective_val = tb.bonus_value * skill_level

        # Find matching modules — target_type_id is a skill type_id
        matching_type_ids = set()
        for tid, skills in skill_reqs.items():
            if tb.target_type_id in skills:
                matching_type_ids.add(tid)

        if not matching_type_ids:
            continue

        # Map keyword to target attributes
        target_attrs = _resolve_bonus_keyword(tb.bonus_keyword)
        if not target_attrs:
            continue

        # Apply bonus as postPercent to each matching module
        for tid in matching_type_ids:
            if tid not in module_attrs_map:
                continue
            attrs = module_attrs_map[tid]
            for target_attr in target_attrs:
                if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
                    attrs[target_attr] = 1.0
                current = attrs.get(target_attr, 0)
                if current == 0 and target_attr in (ATTR_DAMAGE_MULTIPLIER,):
                    current = 1.0
                attrs[target_attr] = current * (1 + effective_val / 100)

    # ── Source 2: modifierInfo-based ship modifiers (role bonuses etc.) ───
    result = await db.execute(
        select(SDETypeEffect.effect_id)
        .join(SDEEffect, SDETypeEffect.effect_id == SDEEffect.effect_id)
        .where(SDETypeEffect.type_id == ship_type_id)
        .where(SDEEffect.effect_category.in_(PASSIVE_EFFECT_CATS))
    )
    ship_effect_ids = [row[0] for row in result.fetchall()]
    if not ship_effect_ids:
        return

    result = await db.execute(
        select(SDEModifier)
        .where(SDEModifier.effect_id.in_(ship_effect_ids))
        .where(SDEModifier.domain == "shipID")
    )

    for mod in result.scalars().all():
        src_val = ship_attrs.get(mod.modifying_attribute_id)
        if src_val is None:
            continue

        matching_type_ids = set()
        is_per_level = False

        if mod.func == "LocationRequiredSkillModifier" and mod.filter_type == "skill":
            is_per_level = True
            for tid, skills in skill_reqs.items():
                if mod.filter_value in skills:
                    matching_type_ids.add(tid)
        elif mod.func == "LocationGroupModifier" and mod.filter_type == "group":
            for tid, gid in group_ids.items():
                if gid == mod.filter_value:
                    matching_type_ids.add(tid)

        if not matching_type_ids:
            continue

        effective_val = src_val * skill_level if is_per_level else src_val
        target_attr = mod.modified_attribute_id

        for tid in matching_type_ids:
            if tid not in module_attrs_map:
                continue
            attrs = module_attrs_map[tid]
            if target_attr == ATTR_DAMAGE_MULTIPLIER and target_attr not in attrs:
                attrs[target_attr] = 1.0
            current = attrs.get(target_attr, 0)

            if mod.operator == OP_MOD_ADD:
                attrs[target_attr] = current + effective_val
            elif mod.operator == OP_POST_PERCENT:
                attrs[target_attr] = current * (1 + effective_val / 100)
            elif mod.operator == OP_POST_MUL:
                attrs[target_attr] = current * effective_val


def _resolve_bonus_keyword(keyword: str) -> list[int]:
    """Map a typeBonus keyword string to target attribute IDs."""
    kw = keyword.lower()
    # Check each known pattern — longest match first
    for pattern, attr_ids in sorted(BONUS_KEYWORD_ATTRS.items(), key=lambda x: -len(x[0])):
        if pattern in kw:
            return attr_ids
    return []


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


async def calculate_fitting_stats(
    db: AsyncSession, ship_type_id: int, items: list[dict]
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
    fitted_items = [i for i in items if i.get("slot") not in ("drone", "cargo")]
    module_type_ids = list({item["type_id"] for item in fitted_items})
    all_type_ids = list({item["type_id"] for item in items})

    # Get module attributes and modifiers
    module_attrs_map = await get_types_dogma_attrs(db, all_type_ids) if all_type_ids else {}
    module_modifiers = await _get_module_modifiers(db, module_type_ids) if module_type_ids else {}

    # ── Apply All-V fitting skills to ship attributes ─────────────────────
    # CPU Management V: +25% cpuOutput, PG Management V: +25% powerOutput
    ship_attrs[ATTR_CPU_OUTPUT] = ship_attrs.get(ATTR_CPU_OUTPUT, 0) * 1.25
    ship_attrs[ATTR_POWER_OUTPUT] = ship_attrs.get(ATTR_POWER_OUTPUT, 0) * 1.25

    # ── Apply ship hull bonuses to module attributes ──────────────────────
    # Makes deep copies so we don't mutate cached SDE data
    module_attrs_map = {tid: dict(attrs) for tid, attrs in module_attrs_map.items()}
    await _apply_ship_hull_bonuses(
        db, ship_type_id, ship_attrs, module_attrs_map, items
    )

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
    shield_ehp = _calc_ehp(shield_hp, shield_em_res, shield_therm_res, shield_kin_res, shield_expl_res)
    armor_ehp = _calc_ehp(armor_hp, armor_em_res, armor_therm_res, armor_kin_res, armor_expl_res)
    hull_ehp = _calc_ehp(hull_hp, hull_em_res, hull_therm_res, hull_kin_res, hull_expl_res)
    total_ehp = shield_ehp + armor_ehp + hull_ehp

    # Cap stability — peak recharge vs total module drain
    cap_capacity = mattr(ATTR_CAPACITOR)
    cap_recharge_ms = mattr(ATTR_CAP_RECHARGE)  # in ms
    cap_recharge_s = cap_recharge_ms / 1000 if cap_recharge_ms else 0
    peak_cap_recharge = _calc_peak_recharge(cap_capacity, cap_recharge_s)

    # Sum cap drain from all active (non-drone, non-cargo) modules
    total_cap_drain = 0.0
    for item in fitted_items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        cap_need = mod_attrs.get(ATTR_CAPACITOR_NEED, 0)
        duration = mod_attrs.get(ATTR_DURATION, 0)
        if cap_need > 0 and duration > 0:
            total_cap_drain += (cap_need / (duration / 1000)) * qty

    cap_stable = peak_cap_recharge >= total_cap_drain
    if cap_stable and total_cap_drain > 0:
        # Find equilibrium percentage where recharge = drain
        cap_stable_pct = _find_cap_stable_pct(cap_capacity, cap_recharge_s, total_cap_drain)
    elif total_cap_drain == 0:
        cap_stable_pct = 100.0
    else:
        cap_stable_pct = 0.0

    # Estimate time until cap empty when unstable
    cap_lasts_s = 0.0
    if not cap_stable and total_cap_drain > 0:
        # Simple estimate: cap / (drain - avg_recharge)
        avg_recharge = cap_capacity / cap_recharge_s if cap_recharge_s else 0
        net_drain = total_cap_drain - avg_recharge
        if net_drain > 0:
            cap_lasts_s = cap_capacity / net_drain

    # Shield recharge (passive tank)
    shield_recharge_ms = mattr(ATTR_SHIELD_RECHARGE_RATE, 0)
    shield_recharge_s = shield_recharge_ms / 1000 if shield_recharge_ms else 0
    peak_shield_recharge = _calc_peak_recharge(shield_hp, shield_recharge_s)

    # ── Step 6: DPS calculation ──────────────────────────────────────────
    # Collect charge attrs for modules that have charges loaded
    charge_type_ids = list({
        item["charge_type_id"] for item in items
        if item.get("charge_type_id")
    })
    charge_attrs_map = await get_types_dogma_attrs(db, charge_type_ids) if charge_type_ids else {}

    weapon_dps = 0.0
    weapon_volley = 0.0
    drone_dps = 0.0

    for item in items:
        tid = item["type_id"]
        qty = item.get("quantity", 1)
        mod_attrs = module_attrs_map.get(tid, {})
        slot = item.get("slot", "")

        if slot == "drone":
            # Drone DPS: drone's own damage × damageMultiplier / cycleTime
            em = mod_attrs.get(ATTR_EM_DAMAGE, 0)
            therm = mod_attrs.get(ATTR_THERMAL_DAMAGE, 0)
            kin = mod_attrs.get(ATTR_KINETIC_DAMAGE, 0)
            expl = mod_attrs.get(ATTR_EXPLOSIVE_DAMAGE, 0)
            dmg_mult = mod_attrs.get(ATTR_DAMAGE_MULTIPLIER, 1)
            cycle = mod_attrs.get(ATTR_DURATION, 0) or mod_attrs.get(ATTR_RATE_OF_FIRE, 0)
            if cycle > 0 and (em + therm + kin + expl) > 0:
                volley = (em + therm + kin + expl) * dmg_mult
                drone_dps += (volley / (cycle / 1000)) * qty
            continue

        if slot in ("cargo", "drone"):
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

        volley = total_dmg * dmg_mult
        weapon_volley += volley * qty
        weapon_dps += (volley / (cycle / 1000)) * qty

    total_dps = weapon_dps + drone_dps

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
        "capacitor": round(mattr(SHIP_STAT_ATTRS["capacitor"]), 1),
        "cap_recharge": round(mattr(SHIP_STAT_ATTRS["cap_recharge"]) / 1000, 1) if mattr(SHIP_STAT_ATTRS["cap_recharge"]) else 0,
        "max_target_range": round(mattr(SHIP_STAT_ATTRS["max_target_range"]) / 1000, 1),
        "max_locked_targets": int(mattr(SHIP_STAT_ATTRS["max_locked_targets"])),
        "scan_resolution": round(mattr(SHIP_STAT_ATTRS["scan_resolution"]), 1),
        "sig_radius": round(mattr(SHIP_STAT_ATTRS["sig_radius"]), 1),
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
        # DPS
        "weapon_dps": round(weapon_dps, 1),
        "drone_dps": round(drone_dps, 1),
        "total_dps": round(total_dps, 1),
        "weapon_volley": round(weapon_volley),
    }


def _calc_align_time(inertia: float, mass: float) -> float:
    if not inertia or not mass:
        return 0
    return -math.log(0.25) * inertia * mass / 1_000_000


def _resonance_to_resist(resonance: float) -> float:
    """Convert damage resonance (0-1) to resist percentage (0-100)."""
    return round((1.0 - resonance) * 100, 1)


def _calc_ehp(hp: float, em_res: float, therm_res: float, kin_res: float, expl_res: float) -> float:
    """Calculate EHP for one layer assuming uniform damage (25/25/25/25).

    Resonance is 0-1 where 0 = 100% resist, 1 = 0% resist.
    EHP = HP / avg(resonances)
    """
    if hp <= 0:
        return 0
    avg_res = (em_res + therm_res + kin_res + expl_res) / 4
    if avg_res <= 0:
        return hp * 1000  # near-infinite EHP at 100% resist
    return hp / avg_res


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
