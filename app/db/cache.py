import json
import hashlib
from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, String, Text, DateTime, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Base

# TTLs in seconds per ESI endpoint category
TTL = {
    "universe_type":    86400 * 365,  # item names/info — permanent
    "universe_system":  86400 * 365,  # system info — permanent
    "universe_station": 86400,        # station info — 24h
    "universe_const":   86400 * 365,  # constellation/region — permanent
    "universe_names":   86400 * 30,   # name resolution — 30 days
    "corporation":      3600,         # corp info — 1h
    "alliance":         3600,         # alliance info — 1h
    "route":            600,          # route calc — 10 min
    "market_orders":    300,          # market orders — 5 min
    "market_prices":    300,          # global prices — 5 min
    "character_assets": 300,          # assets — 5 min
    "character_jobs":   300,          # industry jobs — 5 min
    "character_clones": 300,          # clones — 5 min
    "character_wallet": 120,          # wallet — 2 min
    "character_location": 60,         # location — 60 sec
    "search":           300,          # search results — 5 min
}


class ESICache(Base):
    __tablename__ = "esi_cache"

    key = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)


def _cache_key(path: str, params: dict = None) -> str:
    raw = path
    if params:
        raw += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hashlib.sha256(raw.encode()).hexdigest()[:32] + ":" + raw[:100]


def _ttl_for_path(path: str) -> int:
    if "/universe/types/" in path:       return TTL["universe_type"]
    if "/universe/systems/" in path:     return TTL["universe_system"]
    if "/universe/stations/" in path:    return TTL["universe_station"]
    if "/universe/structures/" in path:  return TTL["universe_station"]
    if "/universe/constellations/" in path: return TTL["universe_const"]
    if "/universe/regions/" in path:     return TTL["universe_const"]
    if "/universe/names" in path:        return TTL["universe_names"]
    if "/corporations/" in path:         return TTL["corporation"]
    if "/alliances/" in path:            return TTL["alliance"]
    if "/route/" in path:                return TTL["route"]
    if "/markets/" in path and "/orders" in path: return TTL["market_orders"]
    if "/markets/prices" in path:        return TTL["market_prices"]
    if "/assets/" in path:               return TTL["character_assets"]
    if "/industry/jobs" in path:         return TTL["character_jobs"]
    if "/clones/" in path:               return TTL["character_clones"]
    if "/wallet" in path:                return TTL["character_wallet"]
    if "/location/" in path:             return TTL["character_location"]
    if "/search/" in path:               return TTL["search"]
    return 300  # default 5 min


async def cache_get(db: AsyncSession, path: str, params: dict = None):
    """Return cached data if present and not expired, else None."""
    key = _cache_key(path, params)
    result = await db.execute(select(ESICache).where(ESICache.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        await db.execute(delete(ESICache).where(ESICache.key == key))
        await db.commit()
        return None
    return json.loads(row.data)


async def cache_set(db: AsyncSession, path: str, data, params: dict = None):
    """Store data in cache with appropriate TTL."""
    key = _cache_key(path, params)
    ttl = _ttl_for_path(path)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    result = await db.execute(select(ESICache).where(ESICache.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.data = json.dumps(data, default=str)
        row.expires_at = expires_at
    else:
        db.add(ESICache(
            key=key,
            data=json.dumps(data, default=str),
            expires_at=expires_at,
        ))
    await db.commit()


async def cache_invalidate(db: AsyncSession, pattern: str):
    """Invalidate all cache entries whose key contains pattern."""
    result = await db.execute(select(ESICache))
    rows = result.scalars().all()
    for row in rows:
        if pattern in row.key:
            await db.delete(row)
    await db.commit()


async def cache_stats(db: AsyncSession) -> dict:
    """Return cache statistics."""
    result = await db.execute(select(ESICache))
    rows = result.scalars().all()
    now = datetime.now(timezone.utc)
    active = sum(
        1 for r in rows
        if (r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)) > now
    )
    return {"total_entries": len(rows), "active_entries": active, "expired_entries": len(rows) - active}
