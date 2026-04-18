"""Fitting calculation engine — computes ship stats from dogma attributes."""

import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDETypeDogmaAttribute, SDEModuleSlot, SDEType
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


async def get_type_dogma_attrs(db: AsyncSession, type_id: int) -> dict[int, float]:
    """Get all dogma attributes for a type. Returns {attribute_id: value}."""
    result = await db.execute(
        select(SDETypeDogmaAttribute.attribute_id, SDETypeDogmaAttribute.value)
        .where(SDETypeDogmaAttribute.type_id == type_id)
    )
    return {row.attribute_id: row.value for row in result.fetchall()}


async def get_types_dogma_attrs(db: AsyncSession, type_ids: list[int]) -> dict[int, dict[int, float]]:
    """Bulk get dogma attributes for multiple types. Returns {type_id: {attr_id: value}}."""
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


async def get_ship_stats(db: AsyncSession, ship_type_id: int) -> dict:
    """Fetch all displayable base stats for a ship from dogma attributes."""
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    stats = {}
    for name, attr_id in SHIP_STAT_ATTRS.items():
        val = attrs.get(attr_id, 0)
        stats[name] = val

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

    items: list of {type_id, slot, quantity}
    """
    ship_stats = await get_ship_stats(db, ship_type_id)

    # Collect all module type IDs
    module_type_ids = list({item["type_id"] for item in items})
    module_attrs_map = await get_types_dogma_attrs(db, module_type_ids) if module_type_ids else {}

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

    # Calculate resource usage
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

    return {
        "cpu_used": round(cpu_used, 1),
        "cpu_total": round(ship_stats.get("cpu_output", 0), 1),
        "pg_used": round(pg_used, 1),
        "pg_total": round(ship_stats.get("pg_output", 0), 1),
        "calibration_used": round(calibration_used),
        "calibration_total": round(ship_stats.get("calibration_output", 0)),
        "turrets_used": turrets_used,
        "turrets_total": int(ship_stats.get("turret_slots", 0)),
        "launchers_used": launchers_used,
        "launchers_total": int(ship_stats.get("launcher_slots", 0)),
        "drone_bw_used": round(drone_bw_used, 1),
        "drone_bw_total": round(ship_stats.get("drone_bandwidth", 0), 1),
        "drone_bay_used": round(drone_bay_used, 1),
        "drone_bay_total": round(ship_stats.get("drone_capacity", 0), 1),
        "hull_hp": round(ship_stats.get("hull_hp", 0)),
        "armor_hp": round(ship_stats.get("armor_hp", 0)),
        "shield_hp": round(ship_stats.get("shield_hp", 0)),
        "max_velocity": round(ship_stats.get("max_velocity", 0), 1),
        "mass": round(ship_stats.get("mass", 0)),
        "inertia": round(ship_stats.get("inertia", 0), 4),
        "align_time": round(ship_stats.get("align_time", 0), 1),
        "capacitor": round(ship_stats.get("capacitor", 0), 1),
        "cap_recharge": round(ship_stats.get("cap_recharge", 0) / 1000, 1) if ship_stats.get("cap_recharge") else 0,
        "max_target_range": round(ship_stats.get("max_target_range", 0) / 1000, 1),
        "max_locked_targets": int(ship_stats.get("max_locked_targets", 0)),
        "scan_resolution": round(ship_stats.get("scan_resolution", 0), 1),
        "sig_radius": round(ship_stats.get("sig_radius", 0), 1),
        "cargo_capacity": round(ship_stats.get("cargo_capacity", 0), 1),
        "hi_slots": int(ship_stats.get("hi_slots", 0)),
        "med_slots": int(ship_stats.get("med_slots", 0)),
        "low_slots": int(ship_stats.get("low_slots", 0)),
        "rig_slots": int(ship_stats.get("rig_slots", 0)),
        "shield_em_resist": ship_stats.get("shield_em_resist", 0),
        "shield_therm_resist": ship_stats.get("shield_therm_resist", 0),
        "shield_kin_resist": ship_stats.get("shield_kin_resist", 0),
        "shield_expl_resist": ship_stats.get("shield_expl_resist", 0),
        "armor_em_resist": ship_stats.get("armor_em_resist", 0),
        "armor_therm_resist": ship_stats.get("armor_therm_resist", 0),
        "armor_kin_resist": ship_stats.get("armor_kin_resist", 0),
        "armor_expl_resist": ship_stats.get("armor_expl_resist", 0),
        "hull_em_resist": ship_stats.get("hull_em_resist", 0),
        "hull_therm_resist": ship_stats.get("hull_therm_resist", 0),
        "hull_kin_resist": ship_stats.get("hull_kin_resist", 0),
        "hull_expl_resist": ship_stats.get("hull_expl_resist", 0),
    }


def _calc_align_time(inertia: float, mass: float) -> float:
    if not inertia or not mass:
        return 0
    return -math.log(0.25) * inertia * mass / 1_000_000


def _resonance_to_resist(resonance: float) -> float:
    """Convert damage resonance (0-1) to resist percentage (0-100)."""
    return round((1.0 - resonance) * 100, 1)
