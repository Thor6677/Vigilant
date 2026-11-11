"""Hub order-book (Phase 4 Task 2).

Unlike `app.market.history` (persisted, 24h TTL, one row per day), the order
book is pure market noise that changes minute-to-minute — nothing here is
worth a table. It's a module-level dict cache keyed by (region_id, type_id),
same TTL-cache idiom as `app.intel.evescout` (fetch-once, single-flight via a
per-key `asyncio.Lock`, stale-on-error fallback), just keyed instead of a
single global slot.

We deliberately bypass the ESI client's own DB-backed cache
(`cache_enabled=False`) — this module's dict IS the cache, so routing through
`app.db.cache` as well would just double-cache the same rows (mirrors the
reasoning in `history._fetch_history_esi`).

Single ESI call per (region, type): `order_type=all` returns both sides in one
response; we split on `is_buy_order` rather than firing two requests.

Failed fetches are remembered per key for `FAILURE_TTL` (90 s): within that
window `get_orders` does not call ESI again for the key and serves the stale
entry (or [] when there is none). Without this, a page that asks for several
regions on every input change (the hub comparison) re-attempted every failing
fetch — each with the client's own retries — on each change while ESI was
down. A successful fetch clears the stamp; `clear_failure` clears it early
(the hub comparison's retry button).

The dict is bounded: each insert drops entries older than `CACHE_MAX_AGE`
(one hour), except books for the type being inserted — the hub comparison
fetches one type across five regions in one request and may be serving one
of them stale-on-error while another succeeds. With ESI wholly down nothing is
inserted, so nothing is dropped and stale books stay available.

Known limitation: `get_public` returns page 1 only (ESI orders aren't
price-sorted, and X-Pages isn't exposed here). For the small set of
hyper-liquid types with >1000 open orders in a region, the true best
price could theoretically sit on a later page. `get_hub_sell_price`
(app/esi/market.py) makes the same single-page assumption; matching it here
rather than building pagination is a deliberate scope call for this task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.esi.client import ESIClient

# The Forge — same hub as app.market.history.DEFAULT_REGION_ID.
DEFAULT_REGION_ID = 10000002

# Order books move fast (players requote constantly) but ESI itself only
# republishes region orders roughly every few minutes server-side, so 5
# minutes keeps us well clear of hammering ESI without feeling stale.
ORDER_BOOK_TTL = timedelta(minutes=5)

# After a failed fetch, no new attempt for this long (see module docstring).
FAILURE_TTL = timedelta(seconds=90)

# Entries older than this are dropped whenever a new book is inserted.
CACHE_MAX_AGE = timedelta(hours=1)

SELL_DEPTH = 15
BUY_DEPTH = 15

# Player (Upwell) structure IDs; below this is an NPC station. Same threshold
# as app.routes.industry_jobs.STATION_ID_CEILING — defined locally so this
# module doesn't reach into an unrelated route module for a constant.
STATION_ID_CEILING = 10 ** 12


def _now() -> datetime:
    """Clock seam — monkeypatched in tests to exercise TTL expiry."""
    return datetime.now(timezone.utc)


# ── Module-level cache ────────────────────────────────────────────────────────
# {(region_id, type_id): (fetched_at, raw_orders)}
_cache: dict[tuple[int, int], tuple[datetime, list[dict]]] = {}
_locks: dict[tuple[int, int], asyncio.Lock] = {}
# {(region_id, type_id): when the last fetch failed}
_failed: dict[tuple[int, int], datetime] = {}


def _get_lock(key: tuple[int, int]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def recently_failed(region_id: int, type_id: int) -> bool:
    """True when a fetch for this key failed less than FAILURE_TTL ago."""
    stamp = _failed.get((region_id, type_id))
    return stamp is not None and (_now() - stamp) < FAILURE_TTL


def clear_failure(region_id: int, type_id: int) -> None:
    """Forget a remembered failure, so the next `get_orders` fetches."""
    _failed.pop((region_id, type_id), None)


def _store(key: tuple[int, int], data: list[dict]) -> None:
    """Insert a fresh book, dropping entries older than CACHE_MAX_AGE (but
    not other regions' books for the same type) and expired failure stamps."""
    now = _now()
    for k, (stamp, _rows) in list(_cache.items()):
        if k[1] != key[1] and now - stamp > CACHE_MAX_AGE:
            del _cache[k]
    for k, stamp in list(_failed.items()):
        if now - stamp >= FAILURE_TTL:
            del _failed[k]
    _cache[key] = (now, data)


async def _fetch_orders_esi(region_id: int, type_id: int) -> list[dict]:
    """Fetch raw region orders (both sides) for one type. Monkeypatched in
    tests — the network is never hit there."""
    client = ESIClient("", cache_enabled=False)
    data = await client.get_public(
        f"/markets/{region_id}/orders/",
        params={"type_id": type_id, "order_type": "all"},
    )
    return data if isinstance(data, list) else []


async def get_orders(region_id: int, type_id: int) -> list[dict]:
    """Return raw ESI orders for (region, type), 5-min TTL cached.

    Fresh cache entry → served with no network. Stale/missing → single-flight
    refetch (concurrent callers for the same key collapse onto one ESI
    request). On fetch error, stale-on-error: keep serving the last-known-good
    entry if one exists, else []. The failure is remembered for FAILURE_TTL,
    during which this key is served the same way without calling ESI.
    """
    key = (region_id, type_id)
    cached = _cache.get(key)
    if cached is not None and (_now() - cached[0]) < ORDER_BOOK_TTL:
        return cached[1]
    if recently_failed(region_id, type_id):
        return cached[1] if cached is not None else []

    async with _get_lock(key):
        # Re-check after acquiring the lock — another waiter may have
        # refreshed, or failed (in which case don't repeat its attempt).
        cached = _cache.get(key)
        if cached is not None and (_now() - cached[0]) < ORDER_BOOK_TTL:
            return cached[1]
        if recently_failed(region_id, type_id):
            return cached[1] if cached is not None else []
        try:
            data = await _fetch_orders_esi(region_id, type_id)
        except Exception:
            _failed[key] = _now()
            return cached[1] if cached is not None else []
        _failed.pop(key, None)
        _store(key, data)
        return data


def fetched_at(region_id: int, type_id: int) -> datetime | None:
    """When this (region, type) was last fetched successfully, or None if it
    never has been. `get_orders` returns [] both for "no orders" and for "the
    fetch failed with nothing cached"; this tells the two apart. After a
    stale-on-error serve this is still the time of the last SUCCESSFUL fetch,
    so it can be older than ORDER_BOOK_TTL."""
    cached = _cache.get((region_id, type_id))
    return cached[0] if cached is not None else None


def build_order_book(orders: list[dict]) -> dict:
    """Pure transform: raw ESI orders → the depth-capped book + spread stats.

    No I/O, no location-name resolution (that needs a DB lookup the caller
    owns) — rows carry `location_id` only. Kept side-effect-free so the spread
    math and depth-capping are unit-testable without touching ESI or the DB.

    Spread definition (documented once, here): spread = best_sell - best_buy;
    spread_pct = spread / best_sell * 100 — i.e. the spread as a percentage of
    the ask (sell) price, the standard bid-ask-spread-as-%-of-ask convention.
    Example: best sell 100, best buy 90 → spread 10, spread_pct 10.0.
    """
    sells = sorted(
        (o for o in orders if not o.get("is_buy_order")),
        key=lambda o: o["price"],
    )[:SELL_DEPTH]
    buys = sorted(
        (o for o in orders if o.get("is_buy_order")),
        key=lambda o: -o["price"],
    )[:BUY_DEPTH]

    def _row(o: dict) -> dict:
        return {
            "price": o.get("price"),
            "volume_remain": o.get("volume_remain"),
            "location_id": o.get("location_id"),
        }

    sell_rows = [_row(o) for o in sells]
    buy_rows = [_row(o) for o in buys]

    best_sell = sell_rows[0]["price"] if sell_rows else None
    best_buy = buy_rows[0]["price"] if buy_rows else None

    spread = None
    spread_pct = None
    if best_sell is not None and best_buy is not None and best_sell:
        spread = best_sell - best_buy
        spread_pct = spread / best_sell * 100

    return {
        "sell_orders": sell_rows,
        "buy_orders": buy_rows,
        "best_sell": best_sell,
        "best_buy": best_buy,
        "spread": spread,
        "spread_pct": spread_pct,
    }


def location_ids_in_book(book: dict) -> set[int]:
    """IDs that need a name — only the rows actually displayed (≤30), never
    the full raw order list, so the follow-up SDE lookup stays cheap."""
    return {
        row["location_id"]
        for row in (*book["sell_orders"], *book["buy_orders"])
        if row.get("location_id") is not None
    }


def location_name(location_id: int | None, station_names: dict[int, str]) -> str:
    """Resolve a location_id to a display name.

    NPC stations (< 1e12): looked up via the caller-supplied SDE station-name
    map (cheap, local). Player (Upwell) structures (>= 1e12): we deliberately
    do NOT call the auth'd `/universe/structures/{id}/` endpoint from this
    public-ish, unauthenticated-per-character view — just label it generically
    with the raw ID for reference.
    """
    if location_id is None:
        return "Unknown"
    if location_id >= STATION_ID_CEILING:
        return f"Player structure ({location_id})"
    return station_names.get(location_id, f"Station {location_id}")
