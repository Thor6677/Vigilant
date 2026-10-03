import asyncio
import json
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, String, Text, DateTime, select, delete, func, text, bindparam
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
    "character_mail":    30,          # mail headers — ESI's own max-age; read live on each view
    # location — matches ESI's own 5s cache on this endpoint. Used to be
    # 60s, which was fine for the dashboard's own per-character sync (it
    # only re-fetches location every 60s regardless of this TTL — see
    # FIELD_CACHE_SECONDS["location"] in app/routes/dashboard.py — so this
    # entry was never actually deduping the dashboard's own calls, just
    # sitting well past its next scheduled fetch either way) but left the
    # live wormhole tracker (app/routes/wh_tracker.py) polling a system
    # that could be up to 60s stale. Kept as a real (if short) cache rather
    # than bypassed entirely: several tracker tabs open on the same
    # character still dedupe within the 5s window, and ESI's error limit
    # is shared across the whole app.
    "character_location": 5,
    "killmail":         86400,        # killmails are immutable — 24h
    "search":           300,          # search results — 5 min
    "corp_contracts":   300,          # corp contracts list — 5 min
    "contract_items":   14400,        # contract items (stable while outstanding) — 4h
    "intel_kills_resolve_entity": 86400,  # autocomplete name resolves — 24h
}


class ESICache(Base):
    __tablename__ = "esi_cache"

    key = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


def _cache_key(path: str, params: dict = None, principal: str = None) -> str:
    raw = path
    if params:
        raw += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    # Principal-scope authenticated responses so a value fetched with one
    # identity's token can never be served to a different caller. Public data
    # passes principal=None and stays globally shared (correct — it is not
    # authorization-gated). Without this, a corp-privileged response cached
    # under a role-holder's token is returned verbatim to any same-corp member
    # requesting the same path, before their own token (and ESI's per-token
    # 403) is ever consulted.
    if principal:
        raw = f"@{principal}|{raw}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32] + ":" + raw[:100]


def user_principal(user_id: int) -> str:
    """Principal for data cached per USER rather than per token: corp
    contracts (app/routes/corporations.py), served while any of the user's
    pilots in the corporation still holds the scope."""
    return f"user:{int(user_id)}"


def principal_key_pattern(principal: str, path_prefix: str) -> str:
    """LIKE pattern matching every key _cache_key builds for `principal` and
    a path starting with `path_prefix`, with or without params (ISS-077).

    A key is "<hash>:<raw[:100]>" with raw "@<principal>|<path>[?params]",
    so the pattern matches its readable tail. Both parts must be free of
    LIKE wildcards and fit well inside those 100 characters.
    """
    raw = f"@{principal}|{path_prefix}"
    if len(raw) > 90 or "%" in raw or "_" in raw:
        raise ValueError(f"can't match cache keys by {raw!r}")
    return f"%:{raw}%"


def _ttl_for_path(path: str) -> int:
    if "/universe/types/" in path:       return TTL["universe_type"]
    if "/universe/systems/" in path:     return TTL["universe_system"]
    if "/universe/stations/" in path:    return TTL["universe_station"]
    if "/universe/structures/" in path:  return TTL["universe_station"]
    if "/universe/constellations/" in path: return TTL["universe_const"]
    if "/universe/regions/" in path:     return TTL["universe_const"]
    if "/universe/names" in path:        return TTL["universe_names"]
    if "/intel_kills/resolve_entity" in path: return TTL["intel_kills_resolve_entity"]
    # Public character info: /characters/12345/ (no sub-path beyond the ID)
    import re as _re
    if _re.match(r'^/characters/\d+/?$', path):
        return TTL["character_public"]
    # Mail headers only. Bodies (/mail/{mail_id}/) never change and keep the
    # default; before this the list fell through to it too, 10x ESI's max-age.
    if _re.match(r'^/characters/\d+/mail/?$', path):
        return TTL["character_mail"]
    # Specific-subpath checks come BEFORE the generic /corporations/ and
    # /alliances/ catches — otherwise paths like /corporations/{id}/assets/
    # match the broad corp-info TTL (24h) before the assets-specific
    # 5-min TTL gets a chance. Affected before the reorder: corp assets,
    # corp wallet, corp industry jobs, corp blueprints, corp contracts —
    # all silently cached for 24 hours, breaking refresh and alerts.
    if "/contracts/" in path and "/items" in path: return TTL["contract_items"]
    if "/contracts/" in path:            return TTL["corp_contracts"]
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
    # Generic corp/alliance info catches — these are LAST so they only
    # match the bare /corporations/{id}/ and /alliances/{id}/ endpoints.
    if "/corporations/" in path:         return TTL["corporation"]
    if "/alliances/" in path:            return TTL["alliance"]
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


async def cache_get(db: AsyncSession | None, path: str, params: dict = None, principal: str = None):
    """Return cached data if present and not expired, else None.

    Always uses an isolated AsyncSessionLocal so concurrent cache operations
    can never poison the caller's session. The `db` argument is accepted for
    API compatibility but not used — previous behavior shared the caller's
    session, which cascaded SQLAlchemy errors during dashboard fan-outs.

    Pass `principal` for authorization-gated (authenticated) responses so the
    entry is namespaced to the requesting identity; leave it None for public
    data that is safe to share across all callers.
    """
    del db  # explicitly unused — kept for backwards-compat callsites
    key = _cache_key(path, params, principal)
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


async def cache_set(db: AsyncSession | None, path: str, data, params: dict = None, principal: str = None):
    """Store data in cache with the appropriate TTL.

    Always uses an isolated AsyncSessionLocal; see cache_get() for rationale.
    Cache writes are best-effort: any failure is swallowed so the caller's
    flow is never interrupted.

    Pass `principal` for authorization-gated (authenticated) responses; it must
    match the value used at read time. Leave it None for public data.
    """
    del db  # explicitly unused — kept for backwards-compat callsites
    key = _cache_key(path, params, principal)
    ttl = _ttl_for_path(path)
    from app.db.models import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as fresh_db:
            await _cache_set_impl(fresh_db, key, data, ttl)
    except Exception:
        pass  # cache writes must never break the caller


# ISS-073: the GC deletes in small transactions so SQLite's single write lock is
# never held for long (a long gap can leave a very large backlog of expired rows).
GC_BATCH_ROWS = 5000
GC_BATCH_PAUSE_SECONDS = 0.1
_GC_BATCH_SQL = (
    "DELETE FROM esi_cache WHERE rowid IN "
    "(SELECT rowid FROM esi_cache WHERE expires_at < :now LIMIT :n)"
)
_gc_running = False  # at most one GC at a time (scheduler task vs admin purge)

gc_log = logging.getLogger(__name__)


async def _between_gc_batches() -> None:
    """The pause between two batches (a seam the tests use to act mid-run)."""
    await asyncio.sleep(GC_BATCH_PAUSE_SECONDS)


@dataclass
class GCResult:
    removed: int = 0
    complete: bool = True   # False: stopped early (time cap, error or skipped)
    skipped: bool = False   # another GC was already running


async def cache_gc_run(now: datetime | None = None, max_seconds: float | None = None,
                       batch_rows: int | None = None) -> GCResult:
    """Delete expired cache rows in batches; never raises.

    One short transaction per batch with a pause between them. `now` is a naive
    UTC datetime (default: the current time). `max_seconds` caps the run so a
    caller can stay responsive; the rest is left for the next run. Rows already
    deleted by committed batches always count.
    """
    global _gc_running
    if _gc_running:
        return GCResult(0, False, True)
    _gc_running = True
    result = GCResult()
    try:
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        n = batch_rows or GC_BATCH_ROWS
        stmt = text(_GC_BATCH_SQL).bindparams(bindparam("now", type_=DateTime))
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        from app.db.models import AsyncSessionLocal
        try:
            while True:
                async with AsyncSessionLocal() as db:
                    res = await db.execute(stmt, {"now": now, "n": n})
                    await db.commit()
                    got = res.rowcount or 0
                result.removed += got
                if got < n:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    result.complete = False
                    break
                await _between_gc_batches()
        except Exception as e:
            result.complete = False
            gc_log.warning("ESI cache GC stopped after %d rows: %s", result.removed, e)
        return result
    finally:
        _gc_running = False


async def cache_gc(now: datetime | None = None, max_seconds: float | None = None) -> int:
    """Delete expired cache rows. Returns number of rows removed.

    Without this, expired rows accumulate forever — they're only ever cleaned
    on a read miss for the same key, which never happens for keys that are
    never read again.
    """
    return (await cache_gc_run(now, max_seconds)).removed


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
