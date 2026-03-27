"""
Fast local lookups against the SDE tables.
Falls back gracefully if SDE isn't loaded yet.
"""
from collections import deque
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEBlueprintMaterial, SDETypeMaterial, SDECompressible, SDEBlueprintInfo


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
    # Load full jump graph into memory (one-time per call, ~30K edges)
    jumps_result = await db.execute(select(SDEJump.from_system_id, SDEJump.to_system_id))
    graph: dict[int, list[int]] = {}
    for row in jumps_result.fetchall():
        graph.setdefault(row.from_system_id, []).append(row.to_system_id)

    # Load cloning stations indexed by system_id
    stations_result = await db.execute(
        select(SDEStation.station_id, SDEStation.station_name, SDEStation.system_id)
        .where(SDEStation.has_cloning == True)
    )
    cloning_by_system: dict[int, list[dict]] = {}
    for row in stations_result.fetchall():
        cloning_by_system.setdefault(row.system_id, []).append({
            "station_id": row.station_id,
            "station_name": row.station_name,
        })

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
