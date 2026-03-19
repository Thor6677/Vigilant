"""
Fast local lookups against the SDE tables.
Falls back gracefully if SDE isn't loaded yet.
"""
from collections import deque
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDEType, SDESystem, SDEJump, SDEStation, SDERegion, SDEConstellation, SDEBlueprintMaterial


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
