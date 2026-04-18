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
from app.fitting.constants import (
    ATTR_POWER, ATTR_CPU, ATTR_UPGRADE_COST, ATTR_VOLUME,
    ATTR_DRONE_BW_USED, SHIP_STAT_ATTRS,
    ATTR_MASS, ATTR_INERTIA,
    ATTR_SHIELD_EM_RESONANCE, ATTR_SHIELD_THERM_RESONANCE,
    ATTR_SHIELD_KIN_RESONANCE, ATTR_SHIELD_EXPL_RESONANCE,
    ATTR_ARMOR_EM_RESONANCE, ATTR_ARMOR_THERM_RESONANCE,
    ATTR_ARMOR_KIN_RESONANCE, ATTR_ARMOR_EXPL_RESONANCE,
    ATTR_HULL_EM_RESONANCE, ATTR_HULL_THERM_RESONANCE,
    ATTR_HULL_KIN_RESONANCE, ATTR_HULL_EXPL_RESONANCE,
)

# CCP dogma operator IDs (from modifierInfo)
OP_PRE_ASSIGN = 1
OP_PRE_MUL = 2
OP_PRE_DIV = 3
OP_MOD_ADD = 4
OP_MOD_SUB = 5
OP_POST_MUL = 6
OP_POST_DIV = 7
OP_POST_PERCENT = 8  # value = value * (1 + modifier/100)
OP_POST_ASSIGN = 9

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
    # Get raw ship attributes
    ship_attrs = await get_type_dogma_attrs(db, ship_type_id)

    # Collect all module type IDs (excluding drones and cargo)
    fitted_items = [i for i in items if i.get("slot") not in ("drone", "cargo")]
    module_type_ids = list({item["type_id"] for item in fitted_items})
    all_type_ids = list({item["type_id"] for item in items})

    # Get module attributes and modifiers
    module_attrs_map = await get_types_dogma_attrs(db, all_type_ids) if all_type_ids else {}
    module_modifiers = await _get_module_modifiers(db, module_type_ids) if module_type_ids else {}

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

        # Group by operator
        pre_assigns = [v for op, v in modifiers if op == OP_PRE_ASSIGN]
        mod_adds = [v for op, v in modifiers if op == OP_MOD_ADD]
        mod_subs = [v for op, v in modifiers if op == OP_MOD_SUB]
        post_muls = [v for op, v in modifiers if op == OP_POST_MUL]
        post_divs = [v for op, v in modifiers if op == OP_POST_DIV]
        post_pcts = [v for op, v in modifiers if op == OP_POST_PERCENT]
        pre_muls = [v for op, v in modifiers if op == OP_PRE_MUL]
        post_assigns = [v for op, v in modifiers if op == OP_POST_ASSIGN]

        val = base

        # preAssign: override base
        if pre_assigns:
            val = pre_assigns[-1]

        # preMul
        for m in pre_muls:
            val *= m

        # modAdd / modSub
        for a in mod_adds:
            val += a
        for s in mod_subs:
            val -= s

        # postMul — apply stacking penalties if attribute is not stackable
        is_stackable = stackable_flags.get(attr_id, True)
        if post_muls:
            if is_stackable:
                for m in post_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(post_muls)

        # postDiv
        for d in post_divs:
            if d != 0:
                val /= d

        # postPercent — convert to multiplier, apply stacking if needed
        if post_pcts:
            pct_muls = [1.0 + p / 100.0 for p in post_pcts]
            if is_stackable:
                for m in pct_muls:
                    val *= m
            else:
                val *= apply_stacking_penalties(pct_muls)

        # postAssign: final override
        if post_assigns:
            val = post_assigns[-1]

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
    shield_em_resist = _resonance_to_resist(mattr(ATTR_SHIELD_EM_RESONANCE, 1.0))
    shield_therm_resist = _resonance_to_resist(mattr(ATTR_SHIELD_THERM_RESONANCE, 1.0))
    shield_kin_resist = _resonance_to_resist(mattr(ATTR_SHIELD_KIN_RESONANCE, 1.0))
    shield_expl_resist = _resonance_to_resist(mattr(ATTR_SHIELD_EXPL_RESONANCE, 1.0))
    armor_em_resist = _resonance_to_resist(mattr(ATTR_ARMOR_EM_RESONANCE, 1.0))
    armor_therm_resist = _resonance_to_resist(mattr(ATTR_ARMOR_THERM_RESONANCE, 1.0))
    armor_kin_resist = _resonance_to_resist(mattr(ATTR_ARMOR_KIN_RESONANCE, 1.0))
    armor_expl_resist = _resonance_to_resist(mattr(ATTR_ARMOR_EXPL_RESONANCE, 1.0))
    hull_em_resist = _resonance_to_resist(mattr(ATTR_HULL_EM_RESONANCE, 1.0))
    hull_therm_resist = _resonance_to_resist(mattr(ATTR_HULL_THERM_RESONANCE, 1.0))
    hull_kin_resist = _resonance_to_resist(mattr(ATTR_HULL_KIN_RESONANCE, 1.0))
    hull_expl_resist = _resonance_to_resist(mattr(ATTR_HULL_EXPL_RESONANCE, 1.0))

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
        "hull_hp": round(mattr(SHIP_STAT_ATTRS["hull_hp"])),
        "armor_hp": round(mattr(SHIP_STAT_ATTRS["armor_hp"])),
        "shield_hp": round(mattr(SHIP_STAT_ATTRS["shield_hp"])),
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
        "shield_em_resist": shield_em_resist,
        "shield_therm_resist": shield_therm_resist,
        "shield_kin_resist": shield_kin_resist,
        "shield_expl_resist": shield_expl_resist,
        "armor_em_resist": armor_em_resist,
        "armor_therm_resist": armor_therm_resist,
        "armor_kin_resist": armor_kin_resist,
        "armor_expl_resist": armor_expl_resist,
        "hull_em_resist": hull_em_resist,
        "hull_therm_resist": hull_therm_resist,
        "hull_kin_resist": hull_kin_resist,
        "hull_expl_resist": hull_expl_resist,
    }


def _calc_align_time(inertia: float, mass: float) -> float:
    if not inertia or not mass:
        return 0
    return -math.log(0.25) * inertia * mass / 1_000_000


def _resonance_to_resist(resonance: float) -> float:
    """Convert damage resonance (0-1) to resist percentage (0-100)."""
    return round((1.0 - resonance) * 100, 1)
