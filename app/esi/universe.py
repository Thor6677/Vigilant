import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.esi.client import ESIClient

logger = logging.getLogger(__name__)

# In-memory cache of structure IDs that returned 401/403.
# Maps structure_id -> timestamp of the first 401.
# Cached entries are skipped for 24 hours to avoid wasting ESI calls.
_structure_401_cache: dict[int, datetime] = {}
_STRUCTURE_401_TTL = timedelta(hours=2)


async def get_station(client: ESIClient, station_id: int) -> dict:
    return await client.get_public(f"/universe/stations/{station_id}/")


async def get_structure(client: ESIClient, structure_id: int, db=None) -> dict:
    now = datetime.now(timezone.utc)

    # Check DB cache first (populated by corp structure fetches)
    if db:
        from sqlalchemy import select
        from app.db.models import StructureNameCache
        cached = await db.execute(
            select(StructureNameCache).where(StructureNameCache.structure_id == structure_id)
        )
        cached_entry = cached.scalar_one_or_none()
        if cached_entry:
            result = {"name": cached_entry.name}
            if cached_entry.solar_system_id:
                result["solar_system_id"] = cached_entry.solar_system_id
            return result

    # Skip structures that recently returned 401/403
    cached_at = _structure_401_cache.get(structure_id)
    if cached_at and (now - cached_at) < _STRUCTURE_401_TTL:
        return {"name": "Unknown Structure"}

    try:
        result = await client.get(f"/universe/structures/{structure_id}/")
        # Successful — clear any stale 401 entry and cache the name
        _structure_401_cache.pop(structure_id, None)
        if db and result.get("name"):
            await cache_structure_name(db, structure_id, result["name"], result.get("solar_system_id"))
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            _structure_401_cache[structure_id] = now
            logger.debug("Structure %s returned %s — cached for 24h", structure_id, e.response.status_code)
            return {"name": "Unknown Structure"}
        raise


async def cache_structure_name(db, structure_id: int, name: str, solar_system_id: int = None):
    """Persist a structure name to the DB cache."""
    from sqlalchemy import select
    from app.db.models import StructureNameCache
    existing = await db.execute(
        select(StructureNameCache).where(StructureNameCache.structure_id == structure_id)
    )
    entry = existing.scalar_one_or_none()
    if entry:
        entry.name = name
        if solar_system_id:
            entry.solar_system_id = solar_system_id
        entry.updated_at = datetime.now(timezone.utc)
    else:
        db.add(StructureNameCache(
            structure_id=structure_id, name=name,
            solar_system_id=solar_system_id,
        ))
    await db.commit()


async def cache_corp_structures(db, structures: list):
    """Bulk-cache structure names from a corp structures API response."""
    for s in structures:
        sid = s.get("structure_id")
        name = s.get("name")
        if sid and name:
            await cache_structure_name(db, sid, name, s.get("system_id"))


async def get_cached_structure(db, structure_id: int) -> dict | None:
    """Look up a structure from the DB cache. Returns dict with name and solar_system_id, or None."""
    from sqlalchemy import select
    from app.db.models import StructureNameCache
    result = await db.execute(
        select(StructureNameCache).where(StructureNameCache.structure_id == structure_id)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        return None
    return {"name": entry.name, "solar_system_id": entry.solar_system_id}


async def get_cached_structure_name(db, structure_id: int) -> str | None:
    """Look up a structure name from the DB cache. Returns None if not cached."""
    cached = await get_cached_structure(db, structure_id)
    return cached["name"] if cached else None


async def get_type(client: ESIClient, type_id: int) -> dict:
    return await client.get_public(f"/universe/types/{type_id}/")
