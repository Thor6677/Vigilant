import json
import hashlib
from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, String, Text, DateTime, select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Base

# TTLs in seconds per ESI endpoint category
TTL = {
    "universe_type":    86400 * 365,  # item names/info — permanent
    "universe_system":  86400 * 365,  # system info — permanent
    "universe_station": 86400,        # station info — 24h
    "universe_const":   86400 * 365,  # constellation/region — permanent
    "universe_names":   86400 * 30,   # name resolution — 30 days
    "character_public":  3600,         # public char info (corp/alliance) — 1h
    "corporation":      86400,        # corp info (name, ticker) — 24h
    "alliance":         86400,        # alliance info (name) — 24h
    "route":            600,          # route calc — 10 min
    "market_orders":    300,          # market orders — 5 min
    "market_prices":    300,          # global prices — 5 min
    "character_assets": 300,          # assets — 5 min
    "character_jobs":   300,          # industry jobs — 5 min
    "character_clones": 300,          # clones — 5 min
    "character_wallet": 120,          # wallet — 2 min
    "character_location": 60,         # location — 60 sec
    "killmail":         86400,        # killmails are immutable — 24h
    "search":           300,          # search results — 5 min
    "corp_contracts":   300,          # corp contracts list — 5 min
    "contract_items":   14400,        # contract items (stable while outstanding) — 4h
}


class ESICache(Base):
    __tablename__ = "esi_cache"

    key = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


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
    # Public character info: /characters/12345/ (no sub-path beyond the ID)
    import re as _re
    if _re.match(r'^/characters/\d+/?$', path):
        return TTL["character_public"]
    if "/contracts/" in path and "/items" in path: return TTL["contract_items"]
    if "/contracts/" in path:            return TTL["corp_contracts"]
    if "/corporations/" in path:         return TTL["corporation"]
    if "/alliances/" in path:            return TTL["alliance"]
    if "/killmails/" in path:            return TTL["killmail"]
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


async def _cache_get_impl(db: AsyncSession, key: str):
    result = await db.execute(select(ESICache).where(ESICache.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        await db.execute(delete(ESICache).where(ESICache.key == key))
        return None
    return json.loads(row.data)


async def cache_get(db: AsyncSession | None, path: str, params: dict = None):
    """Return cached data if present and not expired, else None.

    Always uses an isolated AsyncSessionLocal so concurrent cache operations
    can never poison the caller's session. The `db` argument is accepted for
    API compatibility but not used — previous behavior shared the caller's
    session, which cascaded SQLAlchemy errors during dashboard fan-outs.
    """
    del db  # explicitly unused — kept for backwards-compat callsites
    key = _cache_key(path, params)
    from app.db.models import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as fresh_db:
            result = await _cache_get_impl(fresh_db, key)
            await fresh_db.commit()  # commit any expired-row delete
            return result
    except Exception:
        return None  # cache lookup must never break the caller


async def _cache_set_impl(db: AsyncSession, key: str, data, ttl: int):
    """Atomic upsert via SQLite's INSERT OR REPLACE.

    Previous SELECT-then-INSERT pattern raced two concurrent writers: both
    missed the row, both INSERTed, the second commit failed on the PK
    collision and was silently swallowed by the outer cache_set try/except,
    losing that write entirely. INSERT OR REPLACE is single-statement atomic.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    payload = json.dumps(data, default=str)
    stmt = sqlite_insert(ESICache).values(
        key=key, data=payload, expires_at=expires_at,
    ).on_conflict_do_update(
        index_elements=[ESICache.key],
        set_={"data": payload, "expires_at": expires_at},
    )
    await db.execute(stmt)
    await db.commit()


async def cache_set(db: AsyncSession | None, path: str, data, params: dict = None):
    """Store data in cache with the appropriate TTL.

    Always uses an isolated AsyncSessionLocal; see cache_get() for rationale.
    Cache writes are best-effort: any failure is swallowed so the caller's
    flow is never interrupted.
    """
    del db  # explicitly unused — kept for backwards-compat callsites
    key = _cache_key(path, params)
    ttl = _ttl_for_path(path)
    from app.db.models import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as fresh_db:
            await _cache_set_impl(fresh_db, key, data, ttl)
    except Exception:
        pass  # cache writes must never break the caller


async def cache_gc() -> int:
    """Delete expired cache rows. Returns number of rows removed.

    Without this, expired rows accumulate forever — they're only ever cleaned
    on a read miss for the same key, which never happens for keys that are
    never read again.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    from app.db.models import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(delete(ESICache).where(ESICache.expires_at < now))
            await db.commit()
            return result.rowcount or 0
    except Exception:
        return 0


_CACHE_STATS_MEMO: dict = {"at": None, "val": None}
_CACHE_STATS_TTL = timedelta(minutes=10)


async def cache_stats(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    memoed_at = _CACHE_STATS_MEMO["at"]
    if memoed_at and (now - memoed_at) < _CACHE_STATS_TTL:
        return _CACHE_STATS_MEMO["val"]

    total = (await db.execute(select(func.count(ESICache.key)))).scalar() or 0
    active = (await db.execute(
        select(func.count(ESICache.key)).where(ESICache.expires_at > now.replace(tzinfo=None))
    )).scalar() or 0
    val = {"total_entries": total, "active_entries": active, "expired_entries": total - active}
    _CACHE_STATS_MEMO["at"] = now
    _CACHE_STATS_MEMO["val"] = val
    return val
