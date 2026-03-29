import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.esi.client import ESIClient

logger = logging.getLogger(__name__)

# In-memory cache of structure IDs that returned 401/403.
# Maps structure_id -> timestamp of the first 401.
# Cached entries are skipped for 24 hours to avoid wasting ESI calls.
_structure_401_cache: dict[int, datetime] = {}
_STRUCTURE_401_TTL = timedelta(hours=24)


async def get_station(client: ESIClient, station_id: int) -> dict:
    return await client.get_public(f"/universe/stations/{station_id}/")


async def get_structure(client: ESIClient, structure_id: int) -> dict:
    now = datetime.now(timezone.utc)

    # Skip structures that recently returned 401/403
    cached_at = _structure_401_cache.get(structure_id)
    if cached_at and (now - cached_at) < _STRUCTURE_401_TTL:
        return {"name": "Unknown Structure"}

    try:
        result = await client.get(f"/universe/structures/{structure_id}/")
        # Successful — clear any stale 401 entry
        _structure_401_cache.pop(structure_id, None)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            _structure_401_cache[structure_id] = now
            logger.debug("Structure %s returned %s — cached for 24h", structure_id, e.response.status_code)
            return {"name": "Unknown Structure"}
        raise


async def get_type(client: ESIClient, type_id: int) -> dict:
    return await client.get_public(f"/universe/types/{type_id}/")
