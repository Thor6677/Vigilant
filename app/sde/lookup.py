"""
Fast local lookups against the SDE tables.
Falls back gracefully if SDE isn't loaded yet.
"""
from collections import deque
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import (
    SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation,
    SDEBlueprintMaterial, SDETypeMaterial, SDECompressible, SDEBlueprintInfo,
    SDEGroup, SDETypeSkillReq, SDESkillInfo, SDECertificate, SDECertificateSkill,
    SDEShipMastery,
    SDEWormholeClass, SDEWormholeType, SDEMoon, SDEStar, SDEPlanet,
)

# Cached jump graph + cloning stations (loaded once, refreshed after 1h)
_graph_cache: dict | None = None
_graph_cache_ts: datetime | None = None
_GRAPH_CACHE_TTL = 3600  # seconds


async def type_name_to_id(db: AsyncSession, name: str) -> int | None:
    """Resolve item name to type_id. Case-insensitive."""
    result = await db.execute(
        select(SDEType.type_id).where(func.lower(SDEType.type_name) == name.lower())
    )
    row = result.scalar_one_or_none()
    return row


async def type_id_to_name(db: AsyncSession, type_id: int) -> str | None:
    result = await db.execute(select(SDEType.type_name).where(SDEType.type_id == type_id))
    return result.scalar_one_or_none()


async def type_ids_to_names(db: AsyncSession, type_ids: list[int]) -> dict[int, str]:
    """Bulk resolve type IDs to names. Returns {type_id: name}."""
    if not type_ids:
        return {}
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name).where(SDEType.type_id.in_(type_ids))
    )
    return {row.type_id: row.type_name for row in result.fetchall()}


async def search_types(db: AsyncSession, query: str, limit: int = 10) -> list[dict]:
    """Search item types by partial name."""
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name)
        .where(func.lower(SDEType.type_name).contains(query.lower()))
        .where(SDEType.published == True)
        .limit(limit)
    )
    return [{"type_id": r.type_id, "type_name": r.type_name} for r in result.fetchall()]


async def search_systems(db: AsyncSession, query: str, limit: int = 8) -> list[dict]:
    """Search solar systems by partial name for autocomplete."""
    result = await db.execute(
        select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
        .where(func.lower(SDESystem.system_name).contains(query.lower()))
        .order_by(func.length(SDESystem.system_name))
        .limit(limit)
    )
    return [
        {"system_id": r.system_id, "system_name": r.system_name,
         "security": round(r.security, 2) if r.security is not None else 0.0}
        for r in result.fetchall()
    ]


async def search_regions(db: AsyncSession, query: str, limit: int = 8) -> list[dict]:
    """Search regions by partial name for autocomplete."""
    result = await db.execute(
        select(SDERegion.region_id, SDERegion.region_name)
        .where(func.lower(SDERegion.region_name).contains(query.lower()))
        .order_by(func.length(SDERegion.region_name))
        .limit(limit)
    )
    return [{"region_id": r.region_id, "region_name": r.region_name} for r in result.fetchall()]


async def system_ids_to_names(db: AsyncSession, system_ids: list[int]) -> dict[int, str]:
    """Bulk resolve system IDs to names. Returns {system_id: name}."""
    if not system_ids:
        return {}
    result = await db.execute(
        select(SDESystem.system_id, SDESystem.system_name)
        .where(SDESystem.system_id.in_(system_ids))
    )
    return {row.system_id: row.system_name for row in result.fetchall()}


async def system_name_to_id(db: AsyncSession, name: str) -> int | None:
    result = await db.execute(
        select(SDESystem.system_id).where(func.lower(SDESystem.system_name) == name.lower())
    )
    return result.scalar_one_or_none()


async def system_info(db: AsyncSession, system_id: int) -> dict | None:
    result = await db.execute(select(SDESystem).where(SDESystem.system_id == system_id))
    sys = result.scalar_one_or_none()
    if not sys:
        return None
    info = {
        "system_id": sys.system_id,
        "system_name": sys.system_name,
        "security": round(sys.security, 2) if sys.security is not None else None,
    }
    if sys.constellation_id:
        cr = await db.execute(select(SDEConstellation).where(SDEConstellation.constellation_id == sys.constellation_id))
        const = cr.scalar_one_or_none()
        if const:
            info["constellation"] = const.constellation_name
    if sys.region_id:
        rr = await db.execute(select(SDERegion).where(SDERegion.region_id == sys.region_id))
        region = rr.scalar_one_or_none()
        if region:
            info["region"] = region.region_name
    return info


async def nearest_cloning_facilities(
    db: AsyncSession,
    start_system_id: int,
    max_jumps: int = 15,
    max_results: int = 5,
) -> list[dict]:
    """
    BFS across the full jump graph to find nearest NPC stations
    with cloning services. Returns list sorted by jump distance.
    """
    global _graph_cache, _graph_cache_ts
    now = datetime.now(timezone.utc)
    if _graph_cache is None or _graph_cache_ts is None or (now - _graph_cache_ts).total_seconds() > _GRAPH_CACHE_TTL:
        jumps_result = await db.execute(select(SDEJump.from_system_id, SDEJump.to_system_id))
        graph: dict[int, list[int]] = {}
        for row in jumps_result.fetchall():
            graph.setdefault(row.from_system_id, []).append(row.to_system_id)
        stations_result = await db.execute(
            select(SDEStation.station_id, SDEStation.station_name, SDEStation.system_id)
            .where(SDEStation.has_cloning == True)
        )
        cloning: dict[int, list[dict]] = {}
        for row in stations_result.fetchall():
            cloning.setdefault(row.system_id, []).append({
                "station_id": row.station_id,
                "station_name": row.station_name,
            })
        _graph_cache = {"graph": graph, "cloning": cloning}
        _graph_cache_ts = now

    graph = _graph_cache["graph"]
    cloning_by_system = _graph_cache["cloning"]

    # BFS
    visited = {start_system_id}
    queue = deque([(start_system_id, 0)])
    results = []

    while queue and len(results) < max_results:
        system_id, jumps = queue.popleft()
        if jumps > max_jumps:
            break
        if system_id in cloning_by_system:
            sys_result = await db.execute(
                select(SDESystem.system_name, SDESystem.security)
                .where(SDESystem.system_id == system_id)
            )
            sys_row = sys_result.fetchone()
            for station in cloning_by_system[system_id]:
                results.append({
                    "station_name": station["station_name"],
                    "system_name": sys_row.system_name if sys_row else str(system_id),
                    "security": round(sys_row.security, 2) if sys_row and sys_row.security else None,
                    "jumps": jumps,
                })
                if len(results) >= max_results:
                    break
        for neighbor in graph.get(system_id, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, jumps + 1))

    return sorted(results, key=lambda x: x["jumps"])


async def system_jump_distance(db: AsyncSession, origin_id: int, destination_id: int) -> int | None:
    """BFS jump distance between two systems. Returns 0 if same, int if reachable, None if unreachable."""
    if origin_id == destination_id:
        return 0
    jumps_result = await db.execute(select(SDEJump.from_system_id, SDEJump.to_system_id))
    graph: dict[int, list[int]] = {}
    for row in jumps_result.fetchall():
        graph.setdefault(row.from_system_id, []).append(row.to_system_id)
    visited = {origin_id}
    queue = deque([(origin_id, 0)])
    while queue:
        system_id, jumps = queue.popleft()
        for neighbor in graph.get(system_id, []):
            if neighbor == destination_id:
                return jumps + 1
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, jumps + 1))
    return None


async def jump_distances_from(db: AsyncSession, origin_id: int) -> dict[int, int]:
    """Full BFS from origin. Returns {system_id: jump_count} for all reachable systems."""
    jumps_result = await db.execute(select(SDEJump.from_system_id, SDEJump.to_system_id))
    graph: dict[int, list[int]] = {}
    for row in jumps_result.fetchall():
        graph.setdefault(row.from_system_id, []).append(row.to_system_id)
    distances: dict[int, int] = {origin_id: 0}
    queue = deque([origin_id])
    while queue:
        system_id = queue.popleft()
        for neighbor in graph.get(system_id, []):
            if neighbor not in distances:
                distances[neighbor] = distances[system_id] + 1
                queue.append(neighbor)
    return distances


async def stations_by_ids(db: AsyncSession, station_ids: list[int]) -> dict[int, dict]:
    """Bulk resolve NPC station IDs to {station_id: {system_id, station_name}}."""
    if not station_ids:
        return {}
    result = await db.execute(
        select(SDEStation.station_id, SDEStation.station_name, SDEStation.system_id)
        .where(SDEStation.station_id.in_(station_ids))
    )
    return {
        row.station_id: {"station_name": row.station_name, "system_id": row.system_id}
        for row in result.fetchall()
    }


async def get_blueprint_materials(db: AsyncSession, blueprint_type_id: int) -> list[dict]:
    """Return manufacturing materials for a blueprint type_id."""
    result = await db.execute(
        select(SDEBlueprintMaterial.material_type_id, SDEBlueprintMaterial.quantity)
        .where(SDEBlueprintMaterial.blueprint_type_id == blueprint_type_id)
        .where(SDEBlueprintMaterial.activity_id == 1)
    )
    rows = result.fetchall()
    if not rows:
        return []
    # Resolve material names
    material_ids = [r.material_type_id for r in rows]
    names = await type_ids_to_names(db, material_ids)
    return [
        {"type_id": r.material_type_id, "name": names.get(r.material_type_id, f"Type {r.material_type_id}"), "quantity": r.quantity}
        for r in rows
    ]


async def sde_is_loaded(db: AsyncSession) -> bool:
    """Check if SDE data has been imported."""
    result = await db.execute(select(func.count()).select_from(SDESystem))
    count = result.scalar()
    return (count or 0) > 0


async def find_blueprint_for_product(db: AsyncSession, product_type_id: int) -> int | None:
    """Find a blueprint type_id that manufactures the given product.
    Uses the EVE naming convention: product 'Foo' -> 'Foo Blueprint'.
    Returns None if no blueprint found or it has no manufacturing materials."""
    name = await type_id_to_name(db, product_type_id)
    if not name:
        return None
    bp_id = await type_name_to_id(db, name + " Blueprint")
    if not bp_id:
        return None
    # Verify it has manufacturing materials
    result = await db.execute(
        select(func.count()).select_from(SDEBlueprintMaterial)
        .where(SDEBlueprintMaterial.blueprint_type_id == bp_id)
        .where(SDEBlueprintMaterial.activity_id == 1)
    )
    count = result.scalar() or 0
    return bp_id if count > 0 else None


async def get_type_materials(db: AsyncSession, type_id: int) -> list[dict]:
    """Return reprocessing outputs for a type_id."""
    result = await db.execute(
        select(SDETypeMaterial.material_type_id, SDETypeMaterial.quantity)
        .where(SDETypeMaterial.type_id == type_id)
    )
    return [{"material_type_id": r.material_type_id, "quantity": r.quantity} for r in result.fetchall()]


async def get_compressed_ores(db: AsyncSession) -> list[dict]:
    """Return all compressed ore types with their volume and portion_size.
    Includes both 'Compressed X' and 'Batch Compressed X' variants."""
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name, SDEType.volume, SDEType.portion_size, SDEType.group_id)
        .where(SDEType.published == True)
        .where(SDEType.type_name.like("Compressed %"))
        .where(~SDEType.type_name.like("%Blueprint%"))
    )
    return [
        {"type_id": r.type_id, "name": r.type_name, "volume": r.volume or 0.15,
         "portion_size": r.portion_size or 1, "group_id": r.group_id}
        for r in result.fetchall()
    ]


async def get_ore_reprocessing_map(db: AsyncSession) -> dict:
    """Build a map of compressed ore type_id -> {name, volume, minerals: {mat_id: qty}}.
    Only includes ores that have reprocessing outputs (typeMaterials entries)."""
    ores = await get_compressed_ores(db)
    result = {}
    for ore in ores:
        mats = await get_type_materials(db, ore["type_id"])
        if not mats:
            continue
        result[ore["type_id"]] = {
            "name": ore["name"],
            "volume": ore["volume"],
            "portion_size": ore["portion_size"],
            "group_id": ore["group_id"],
            "minerals": {m["material_type_id"]: m["quantity"] for m in mats},
        }
    return result


async def get_blueprint_info(db: AsyncSession, blueprint_type_id: int) -> dict | None:
    """Get manufacturing time and product for a blueprint."""
    result = await db.execute(
        select(SDEBlueprintInfo).where(SDEBlueprintInfo.blueprint_type_id == blueprint_type_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return {
        "blueprint_type_id": row.blueprint_type_id,
        "product_type_id": row.product_type_id,
        "manufacturing_time": row.manufacturing_time,
        "product_quantity": row.product_quantity,
    }


async def get_blueprint_time(db: AsyncSession, blueprint_type_id: int) -> int | None:
    """Get base manufacturing time in seconds for a blueprint."""
    result = await db.execute(
        select(SDEBlueprintInfo.manufacturing_time).where(SDEBlueprintInfo.blueprint_type_id == blueprint_type_id)
    )
    return result.scalar_one_or_none()


async def get_type_volumes(db: AsyncSession, type_ids: list[int]) -> dict[int, float]:
    """Bulk fetch volumes from sde_types. Returns {type_id: volume}."""
    if not type_ids:
        return {}
    result = await db.execute(
        select(SDEType.type_id, SDEType.volume).where(SDEType.type_id.in_(type_ids))
    )
    return {row.type_id: row.volume or 0.0 for row in result.fetchall()}


async def get_type_group_ids(db: AsyncSession, type_ids: list[int]) -> dict[int, int | None]:
    """Bulk fetch group_ids from sde_types. Returns {type_id: group_id}."""
    if not type_ids:
        return {}
    result = await db.execute(
        select(SDEType.type_id, SDEType.group_id).where(SDEType.type_id.in_(type_ids))
    )
    return {row.type_id: row.group_id for row in result.fetchall()}


# ── Skill planning lookups ───────────────────────────────────────────────────

# EVE attribute ID -> human name mapping
ATTR_NAMES = {164: "charisma", 165: "intelligence", 166: "memory", 167: "perception", 168: "willpower"}


async def get_skill_requirements(db: AsyncSession, type_id: int) -> list[dict]:
    """Get skill requirements for an item/ship. Returns list of {skill_type_id, skill_name, required_level}."""
    result = await db.execute(
        select(SDETypeSkillReq.skill_type_id, SDETypeSkillReq.required_level)
        .where(SDETypeSkillReq.type_id == type_id)
    )
    rows = result.fetchall()
    if not rows:
        return []
    skill_ids = [r.skill_type_id for r in rows]
    names = await type_ids_to_names(db, skill_ids)
    return [
        {"skill_type_id": r.skill_type_id, "skill_name": names.get(r.skill_type_id, f"Skill {r.skill_type_id}"),
         "required_level": r.required_level}
        for r in rows
    ]


async def get_full_skill_tree(db: AsyncSession, type_id: int) -> list[dict]:
    """Recursively resolve all prerequisite skills for an item/ship.
    Returns a flat list of {skill_type_id, skill_name, required_level} including
    transitive prerequisites, deduplicated by highest required level."""
    needed: dict[int, int] = {}  # skill_type_id -> max required level

    async def _walk(tid: int, level: int | None = None):
        reqs = await db.execute(
            select(SDETypeSkillReq.skill_type_id, SDETypeSkillReq.required_level)
            .where(SDETypeSkillReq.type_id == tid)
        )
        for row in reqs.fetchall():
            req_level = row.required_level
            if level is not None:
                req_level = min(req_level, level)  # Don't inflate prereq levels
            current = needed.get(row.skill_type_id, 0)
            if req_level > current:
                needed[row.skill_type_id] = req_level
                # Recurse into this skill's own prerequisites
                await _walk(row.skill_type_id)

    await _walk(type_id)

    if not needed:
        return []
    names = await type_ids_to_names(db, list(needed.keys()))
    return [
        {"skill_type_id": sid, "skill_name": names.get(sid, f"Skill {sid}"),
         "required_level": lvl}
        for sid, lvl in sorted(needed.items(), key=lambda x: names.get(x[0], ""))
    ]


async def get_skill_info(db: AsyncSession, skill_type_id: int) -> dict | None:
    """Get skill metadata (primary/secondary attr, rank)."""
    result = await db.execute(
        select(SDESkillInfo).where(SDESkillInfo.type_id == skill_type_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return {
        "type_id": row.type_id,
        "primary_attr": row.primary_attr,
        "primary_attr_name": ATTR_NAMES.get(row.primary_attr, "unknown"),
        "secondary_attr": row.secondary_attr,
        "secondary_attr_name": ATTR_NAMES.get(row.secondary_attr, "unknown"),
        "rank": row.rank,
    }


async def get_skill_infos(db: AsyncSession, skill_type_ids: list[int]) -> dict[int, dict]:
    """Bulk fetch skill metadata. Returns {type_id: {primary_attr, secondary_attr, rank, ...}}."""
    if not skill_type_ids:
        return {}
    result = await db.execute(
        select(SDESkillInfo).where(SDESkillInfo.type_id.in_(skill_type_ids))
    )
    out = {}
    for row in result.scalars().all():
        out[row.type_id] = {
            "type_id": row.type_id,
            "primary_attr": row.primary_attr,
            "primary_attr_name": ATTR_NAMES.get(row.primary_attr, "unknown"),
            "secondary_attr": row.secondary_attr,
            "secondary_attr_name": ATTR_NAMES.get(row.secondary_attr, "unknown"),
            "rank": row.rank,
        }
    return out


async def get_ship_mastery(db: AsyncSession, ship_type_id: int) -> dict:
    """Get mastery data for a ship. Returns {level(0-4): [{certificate_id, name, skills: [...]}]}."""
    # Get mastery → certificate mappings
    mastery_result = await db.execute(
        select(SDEShipMastery.mastery_level, SDEShipMastery.certificate_id)
        .where(SDEShipMastery.ship_type_id == ship_type_id)
        .order_by(SDEShipMastery.mastery_level)
    )
    mastery_rows = mastery_result.fetchall()
    if not mastery_rows:
        return {}

    # Get all referenced certificate IDs
    cert_ids = list({r.certificate_id for r in mastery_rows})

    # Fetch certificate names
    cert_result = await db.execute(
        select(SDECertificate.certificate_id, SDECertificate.name)
        .where(SDECertificate.certificate_id.in_(cert_ids))
    )
    cert_names = {r.certificate_id: r.name for r in cert_result.fetchall()}

    # Fetch certificate skill requirements
    cs_result = await db.execute(
        select(SDECertificateSkill)
        .where(SDECertificateSkill.certificate_id.in_(cert_ids))
    )
    # cert_id -> list of skill entries
    cert_skills: dict[int, list] = {}
    all_skill_ids: set[int] = set()
    for cs in cs_result.scalars().all():
        cert_skills.setdefault(cs.certificate_id, []).append(cs)
        all_skill_ids.add(cs.skill_type_id)

    # Resolve skill names
    skill_names = await type_ids_to_names(db, list(all_skill_ids)) if all_skill_ids else {}

    # Mastery level names for certificate skill level lookup
    LEVEL_FIELDS = ["basic", "standard", "improved", "advanced", "elite"]

    # Build output per mastery level
    output: dict[int, list] = {}
    for row in mastery_rows:
        level = row.mastery_level
        cert_id = row.certificate_id
        level_field = LEVEL_FIELDS[level] if level < 5 else "elite"

        skills = []
        for cs in cert_skills.get(cert_id, []):
            req_level = getattr(cs, level_field, 0)
            if req_level and req_level > 0:
                skills.append({
                    "skill_type_id": cs.skill_type_id,
                    "skill_name": skill_names.get(cs.skill_type_id, f"Skill {cs.skill_type_id}"),
                    "required_level": req_level,
                })

        output.setdefault(level, []).append({
            "certificate_id": cert_id,
            "name": cert_names.get(cert_id, f"Certificate {cert_id}"),
            "skills": skills,
        })

    return output


async def get_mastery_skills(db: AsyncSession, ship_type_id: int, mastery_level: int) -> list[dict]:
    """Get all unique skills needed for a specific mastery level of a ship.
    Includes all skills from level 0 up to the requested level (cumulative).
    Returns [{skill_type_id, skill_name, required_level}] deduplicated by max level."""
    mastery_data = await get_ship_mastery(db, ship_type_id)
    if not mastery_data:
        return []

    needed: dict[int, int] = {}  # skill_type_id -> max required level
    for lvl in range(mastery_level + 1):
        for cert in mastery_data.get(lvl, []):
            for skill in cert.get("skills", []):
                sid = skill["skill_type_id"]
                req = skill["required_level"]
                if req > needed.get(sid, 0):
                    needed[sid] = req

    if not needed:
        return []
    names = await type_ids_to_names(db, list(needed.keys()))
    return sorted([
        {"skill_type_id": sid, "skill_name": names.get(sid, f"Skill {sid}"),
         "required_level": lvl}
        for sid, lvl in needed.items()
    ], key=lambda x: x["skill_name"])


async def get_skill_groups(db: AsyncSession) -> list[dict]:
    """Get all skill groups (categoryID=16). Returns [{group_id, group_name}]."""
    result = await db.execute(
        select(SDEGroup.group_id, SDEGroup.group_name)
        .where(SDEGroup.category_id == 16)
        .order_by(SDEGroup.group_name)
    )
    return [{"group_id": r.group_id, "group_name": r.group_name} for r in result.fetchall()]


async def get_skills_in_group(db: AsyncSession, group_id: int) -> list[dict]:
    """Get all skill types in a group. Returns [{type_id, type_name}]."""
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name)
        .where(SDEType.group_id == group_id)
        .where(SDEType.published == True)
        .order_by(SDEType.type_name)
    )
    return [{"type_id": r.type_id, "type_name": r.type_name} for r in result.fetchall()]


async def search_skills(db: AsyncSession, query: str, limit: int = 15) -> list[dict]:
    """Search skill types by partial name. Only returns items in skill category (category 16 groups)."""
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name)
        .join(SDEGroup, SDEType.group_id == SDEGroup.group_id)
        .where(SDEGroup.category_id == 16)
        .where(SDEType.published == True)
        .where(func.lower(SDEType.type_name).contains(query.lower()))
        .order_by(SDEType.type_name)
        .limit(limit)
    )
    return [{"type_id": r.type_id, "type_name": r.type_name} for r in result.fetchall()]


# ── Wormhole reference lookups ──────────────────────────────────────────────

# Cached wormhole class mappings (loaded once per process)
_wh_class_cache: dict[int, int] | None = None
_wh_class_cache_ts: datetime | None = None


async def _ensure_wh_class_cache(db: AsyncSession):
    """Load wormhole class mappings into memory."""
    global _wh_class_cache, _wh_class_cache_ts
    now = datetime.now(timezone.utc)
    if _wh_class_cache is not None and _wh_class_cache_ts and (now - _wh_class_cache_ts).total_seconds() < 3600:
        return
    result = await db.execute(select(SDEWormholeClass.location_id, SDEWormholeClass.wormhole_class_id))
    _wh_class_cache = {r.location_id: r.wormhole_class_id for r in result.fetchall()}
    _wh_class_cache_ts = now


async def get_system_wh_class(db: AsyncSession, system_id: int) -> int | None:
    """Determine wormhole class for a system.

    Checks system_id first, then constellation_id, then region_id against
    the mapLocationWormholeClasses table.
    """
    await _ensure_wh_class_cache(db)
    if not _wh_class_cache:
        return None
    # Direct system match
    if system_id in _wh_class_cache:
        return _wh_class_cache[system_id]
    # Constellation match
    sys_result = await db.execute(
        select(SDESystem.constellation_id, SDESystem.region_id)
        .where(SDESystem.system_id == system_id)
    )
    row = sys_result.fetchone()
    if not row:
        return None
    if row.constellation_id and row.constellation_id in _wh_class_cache:
        return _wh_class_cache[row.constellation_id]
    if row.region_id and row.region_id in _wh_class_cache:
        return _wh_class_cache[row.region_id]
    return None


async def get_wormhole_systems(
    db: AsyncSession,
    class_filter: int | None = None,
    effect_filter: str | None = None,
    static_filter: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    wh_data: dict | None = None,
) -> tuple[list[dict], int]:
    """Return filtered J-space systems with class, effect, and statics.

    Returns (list_of_systems, total_count).
    wh_data should be the loaded wormholes.json dict.
    """
    await _ensure_wh_class_cache(db)
    if not _wh_class_cache:
        return [], 0

    # Get all J-space systems (names starting with J and having 6-digit numbers)
    query = (
        select(SDESystem.system_id, SDESystem.system_name,
               SDESystem.constellation_id, SDESystem.region_id)
        .where(SDESystem.system_name.like("J%"))
        .where(func.length(SDESystem.system_name) == 7)
    )
    if search:
        query = query.where(func.lower(SDESystem.system_name).contains(search.lower()))
    query = query.order_by(SDESystem.system_name)

    result = await db.execute(query)
    all_systems = result.fetchall()

    system_effects = wh_data.get("system_effects", {}) if wh_data else {}
    system_statics = wh_data.get("system_statics", {}) if wh_data else {}

    filtered = []
    for sys in all_systems:
        # Determine class
        wh_class = _wh_class_cache.get(sys.system_id)
        if wh_class is None and sys.constellation_id:
            wh_class = _wh_class_cache.get(sys.constellation_id)
        if wh_class is None and sys.region_id:
            wh_class = _wh_class_cache.get(sys.region_id)
        if wh_class is None:
            continue  # Not a known WH system

        # Only include actual wormhole classes (1-6, 13)
        if wh_class not in (1, 2, 3, 4, 5, 6, 13):
            continue

        effect = system_effects.get(sys.system_name)
        statics = system_statics.get(sys.system_name, [])

        # Apply filters
        if class_filter is not None and wh_class != class_filter:
            continue
        if effect_filter and effect != effect_filter:
            continue
        if static_filter and static_filter not in statics:
            continue

        filtered.append({
            "system_id": sys.system_id,
            "system_name": sys.system_name,
            "wh_class": wh_class,
            "effect": effect,
            "statics": statics,
        })

    total = len(filtered)
    page = filtered[offset:offset + limit]
    return page, total


async def get_wormhole_system_detail(db: AsyncSession, system_name: str) -> dict | None:
    """Full detail for a single wormhole system."""
    result = await db.execute(
        select(SDESystem).where(func.lower(SDESystem.system_name) == system_name.lower())
    )
    sys = result.scalar_one_or_none()
    if not sys:
        return None

    wh_class = await get_system_wh_class(db, sys.system_id)

    info = {
        "system_id": sys.system_id,
        "system_name": sys.system_name,
        "wh_class": wh_class,
        "security": round(sys.security, 2) if sys.security is not None else None,
    }

    if sys.constellation_id:
        cr = await db.execute(
            select(SDEConstellation).where(SDEConstellation.constellation_id == sys.constellation_id)
        )
        const = cr.scalar_one_or_none()
        if const:
            info["constellation_id"] = const.constellation_id
            info["constellation"] = const.constellation_name

    if sys.region_id:
        rr = await db.execute(
            select(SDERegion).where(SDERegion.region_id == sys.region_id)
        )
        region = rr.scalar_one_or_none()
        if region:
            info["region_id"] = region.region_id
            info["region"] = region.region_name

    return info


async def get_system_celestials(db: AsyncSession, system_id: int) -> dict:
    """Get star, planets, and moon counts for a system."""
    # Star
    star_result = await db.execute(
        select(SDEStar).where(SDEStar.system_id == system_id)
    )
    star_row = star_result.scalar_one_or_none()
    star = None
    if star_row:
        star_type_name = None
        if star_row.type_id:
            star_type_name = await type_id_to_name(db, star_row.type_id)
        star = {
            "type_id": star_row.type_id,
            "type_name": star_type_name,
        }

    # Planets
    planet_result = await db.execute(
        select(SDEPlanet)
        .where(SDEPlanet.system_id == system_id)
        .order_by(SDEPlanet.planet_index)
    )
    planets_raw = planet_result.scalars().all()

    # Resolve planet type names
    planet_type_ids = list({p.planet_type_id for p in planets_raw if p.planet_type_id})
    planet_type_names = await type_ids_to_names(db, planet_type_ids) if planet_type_ids else {}

    # Moon counts per planet
    moon_result = await db.execute(
        select(SDEMoon.planet_id, func.count(SDEMoon.moon_id).label("moon_count"))
        .where(SDEMoon.system_id == system_id)
        .group_by(SDEMoon.planet_id)
    )
    moon_counts = {r.planet_id: r.moon_count for r in moon_result.fetchall()}

    # Fallback planet type names (SDE planet types are often unpublished)
    PLANET_TYPE_FALLBACK = {
        11: "Temperate", 12: "Ice", 13: "Gas",
        2014: "Oceanic", 2015: "Lava", 2016: "Barren",
        2017: "Storm", 2063: "Plasma", 30889: "Shattered",
    }

    planets = []
    for p in planets_raw:
        type_name = planet_type_names.get(p.planet_type_id)
        if not type_name or type_name == "Unknown":
            type_name = PLANET_TYPE_FALLBACK.get(p.planet_type_id, "Unknown")
        planets.append({
            "planet_id": p.planet_id,
            "planet_name": p.planet_name,
            "planet_index": p.planet_index,
            "type_id": p.planet_type_id,
            "type_name": type_name,
            "distance_au": p.distance_au,
            "moon_count": moon_counts.get(p.planet_id, 0),
        })

    return {"star": star, "planets": planets}


async def get_all_wormhole_types(db: AsyncSession) -> list[dict]:
    """Return all wormhole types with their dogma attributes."""
    result = await db.execute(
        select(SDEWormholeType).order_by(SDEWormholeType.type_name)
    )
    return [
        {
            "type_id": r.type_id,
            "type_name": r.type_name,
            "target_class": int(r.target_class) if r.target_class else None,
            "max_stable_mass": r.max_stable_mass,
            "max_stable_time": r.max_stable_time,
            "mass_regen": r.mass_regen,
            "max_jump_mass": r.max_jump_mass,
        }
        for r in result.scalars().all()
    ]


async def get_wormhole_type_by_name(db: AsyncSession, name: str) -> dict | None:
    """Lookup a single wormhole type by its short name (e.g., 'U574')."""
    # Wormhole type names in SDE are like "Wormhole U574" — search by suffix
    result = await db.execute(
        select(SDEWormholeType)
        .where(SDEWormholeType.type_name.like(f"%{name}%"))
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return {
        "type_id": row.type_id,
        "type_name": row.type_name,
        "target_class": int(row.target_class) if row.target_class else None,
        "max_stable_mass": row.max_stable_mass,
        "max_stable_time": row.max_stable_time,
        "mass_regen": row.mass_regen,
        "max_jump_mass": row.max_jump_mass,
    }
