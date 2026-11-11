"""On-demand cached market price history (Phase 4 Task 1).

Storage design (the "192GB lesson"): NO bulk ingest. History rows accrue only
for the (region, type) pairs a user actually looks at. ESI
`/markets/{region_id}/history/` returns ~400 daily rows in one call; we upsert
them into `market_history` and stamp `market_history_meta.fetched_at`. A stamp
younger than `HISTORY_TTL` serves rows straight from the DB — no network. A
stale/absent stamp triggers exactly one refetch.

The public entry point is `get_history(region_id, type_id, db)`. Each caller
passes its own request-scoped session; the service never fans out across a
shared session, so there are no asyncio.gather write hazards to worry about.

Failed fetches are remembered in memory per (region, type) for `FAILURE_TTL`
(90 s): within that window a stale/absent stamp does not trigger another ESI
call — stored rows (possibly none) are served and the meta stamp is left as
it was. A success clears the stamp; `clear_failure` clears it early (the hub
comparison's retry button). The stamps are per process and lost on restart,
which only costs one retry.

`get_history(..., fetch=False)` never calls ESI: it serves stored rows,
fresh or stale (possibly none), and remembers nothing. The hub comparison
uses it for the hubs after one whose fetch has just failed in the same
request, so an outage costs one slow attempt per request without marking
hubs that were never tried.

Fetches are single-flight per (region, type), the idiom of
`app.market.orders`: an `asyncio.Lock` per key, held across the fetch, the
upsert and its commit. On a cold item page the price chart's `history.json`
and the hub comparison both want The Forge's history at once; the second
caller waits, then re-checks freshness (and a just-remembered failure) on ITS
OWN session before fetching, so it reads what the first stored instead of
fetching again. Each caller still passes its own session; only the lock is
shared.
"""
from __future__ import annotations

import asyncio
from datetime import date as date_cls, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketHistory, MarketHistoryMeta
from app.esi.client import ESIClient

# The Forge — Jita's region. Task 1 is single-region; the schema stores
# region_id so later tasks can add a hub switcher without a migration.
DEFAULT_REGION_ID = 10000002

# Daily history is republished by CCP once per day, so a 24h freshness window
# means at most one wasted refetch per type per day.
HISTORY_TTL = timedelta(hours=24)

# After a failed fetch, no new attempt for this long (see module docstring).
FAILURE_TTL = timedelta(seconds=90)

# {(region_id, type_id): when the last fetch failed}
_failed: dict[tuple[int, int], datetime] = {}
# {(region_id, type_id): the lock a fetch for that key holds}
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _now() -> datetime:
    """Clock seam — monkeypatched in tests to exercise TTL expiry."""
    return datetime.now(timezone.utc)


def _get_lock(key: tuple[int, int]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _remember_failure(key: tuple[int, int]) -> None:
    """Stamp a failed fetch, dropping stamps that have already expired so
    the dict holds only failures still inside FAILURE_TTL."""
    now = _now()
    for k, stamp in list(_failed.items()):
        if now - stamp >= FAILURE_TTL:
            del _failed[k]
    _failed[key] = now


def recently_failed(region_id: int, type_id: int) -> bool:
    """True when a fetch for this key failed less than FAILURE_TTL ago."""
    stamp = _failed.get((region_id, type_id))
    return stamp is not None and (_now() - stamp) < FAILURE_TTL


def clear_failure(region_id: int, type_id: int) -> None:
    """Forget a remembered failure, so the next `get_history` may fetch."""
    _failed.pop((region_id, type_id), None)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat stored stamps as UTC so
    comparisons against an aware `_now()` don't raise (mirrors cache.py)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_date(value) -> date_cls | None:
    """ESI history `date` is an ISO 'YYYY-MM-DD' string."""
    if isinstance(value, date_cls):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


async def _fetch_history_esi(region_id: int, type_id: int) -> list[dict]:
    """Fetch raw daily history from ESI. Bypasses the client DB cache — the
    `market_history` table IS our cache, so double-caching would only bloat
    `esi_cache` with rows we already persist. Monkeypatched in tests.

    Returns the raw list of {date, average, highest, lowest, volume,
    order_count} dicts, or [] if ESI returns a non-list.
    """
    client = ESIClient("", cache_enabled=False)
    data = await client.get_public(
        f"/markets/{region_id}/history/", params={"type_id": type_id}
    )
    return data if isinstance(data, list) else []


async def _read_rows(db: AsyncSession, region_id: int, type_id: int) -> list[MarketHistory]:
    result = await db.execute(
        select(MarketHistory)
        .where(
            MarketHistory.region_id == region_id,
            MarketHistory.type_id == type_id,
        )
        .order_by(MarketHistory.date)
    )
    return list(result.scalars().all())


async def is_fresh(db: AsyncSession, region_id: int, type_id: int) -> bool:
    """True when stored history is younger than HISTORY_TTL, i.e.
    `get_history` would answer from the database without the network."""
    row = (await db.execute(
        select(MarketHistoryMeta).where(
            MarketHistoryMeta.region_id == region_id,
            MarketHistoryMeta.type_id == type_id,
        )
    )).scalar_one_or_none()
    if row is None:
        return False
    fetched = _aware(row.fetched_at)
    return fetched is not None and (_now() - fetched) < HISTORY_TTL


async def fetched_at(db: AsyncSession, region_id: int, type_id: int) -> datetime | None:
    """When history for this (region, type) was last fetched successfully, or
    None if it never has been. `get_history` stamps the meta row even when ESI
    returns zero rows, so None means "could not load", not "never traded"."""
    stamp = (await db.execute(
        select(MarketHistoryMeta.fetched_at).where(
            MarketHistoryMeta.region_id == region_id,
            MarketHistoryMeta.type_id == type_id,
        )
    )).scalar_one_or_none()
    return _aware(stamp)


async def _upsert(db: AsyncSession, region_id: int, type_id: int, raw: list[dict]) -> None:
    """Idempotent upsert of history rows + a fresh meta stamp.

    Upsert (INSERT OR REPLACE on the composite PK) means re-fetching the same
    type never duplicates rows — the second write overwrites the first. The
    meta stamp is written even when `raw` is empty, so a type with no market
    history isn't refetched on every page view.
    """
    values = []
    for r in raw:
        d = _parse_date(r.get("date"))
        if d is None:
            continue
        values.append({
            "region_id": region_id,
            "type_id": type_id,
            "date": d,
            "average": r.get("average"),
            "highest": r.get("highest"),
            "lowest": r.get("lowest"),
            "volume": r.get("volume"),
            "order_count": r.get("order_count"),
        })

    if values:
        stmt = sqlite_insert(MarketHistory).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["region_id", "type_id", "date"],
            set_={
                "average": stmt.excluded.average,
                "highest": stmt.excluded.highest,
                "lowest": stmt.excluded.lowest,
                "volume": stmt.excluded.volume,
                "order_count": stmt.excluded.order_count,
            },
        )
        await db.execute(stmt)

    meta_stmt = sqlite_insert(MarketHistoryMeta).values(
        region_id=region_id, type_id=type_id, fetched_at=_now().replace(tzinfo=None),
    ).on_conflict_do_update(
        index_elements=["region_id", "type_id"],
        set_={"fetched_at": _now().replace(tzinfo=None)},
    )
    await db.execute(meta_stmt)
    await db.commit()


async def get_history(
    region_id: int, type_id: int, db: AsyncSession, *, fetch: bool = True
) -> list[MarketHistory]:
    """Return daily history rows for (region, type), cache-first.

    Fresh meta (< 24h) → rows are read straight from the DB, no network.
    Stale/missing meta → one ESI fetch (single-flight per key, see the
    module docstring), upsert, re-read. If the fetch raises
    (ESI down), we fall back to whatever rows are already stored
    (stale-on-error) rather than surfacing the error to the page, and remember
    the failure: for FAILURE_TTL the stored rows are served without a fetch.
    `fetch=False` serves the stored rows without a fetch and without
    remembering anything (see the module docstring).

    Rows are ordered by date ascending.
    """
    if await is_fresh(db, region_id, type_id):
        return await _read_rows(db, region_id, type_id)
    if not fetch or recently_failed(region_id, type_id):
        return await _read_rows(db, region_id, type_id)

    key = (region_id, type_id)
    async with _get_lock(key):
        # Re-check after acquiring the lock, on this caller's own session:
        # another caller may have stored fresh rows meanwhile, or failed (in
        # which case don't repeat its attempt).
        if await is_fresh(db, region_id, type_id) or recently_failed(region_id, type_id):
            return await _read_rows(db, region_id, type_id)
        try:
            raw = await _fetch_history_esi(region_id, type_id)
        except Exception:
            # Stale-on-error: never fail the page over an ESI hiccup — serve
            # what we have (possibly empty) and leave the meta stamp stale so
            # a view after FAILURE_TTL retries.
            _remember_failure(key)
            return await _read_rows(db, region_id, type_id)

        _failed.pop(key, None)
        await _upsert(db, region_id, type_id, raw)
    return await _read_rows(db, region_id, type_id)
