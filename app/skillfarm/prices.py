"""Price lookups for the skill-farm page (T-073), with a short in-process
cache so a page load never fires a fresh ESI call for a price it already has
-- the render loop calls these once per (pilot, type) *before* this cache,
and there are only ever three distinct type/source lookups needed per
render (Skill Extractor, Large Skill Injector, PLEX), so caching by
(kind, key, type_id, source) also naturally shares one fetch across every
pilot row on the same page load.

This sits on top of (not instead of) app/esi/client.py's own DB-backed ESI
cache: that one survives restarts but still means a real coroutine call
through the client on every miss; this one short-circuits before the client
is touched at all, which is what the "cache hit makes no ESI call" test
checks (it mocks the client and asserts it's called at most once).
"""
from __future__ import annotations

import time

from app.esi.client import ESIClient
from app.esi.market import (
    APPRAISAL_HUBS,
    get_hub_buy_price,
    get_hub_sell_price,
    get_region_price,
)
from app.skillfarm.constants import PLEX_REGION_ID, PLEX_TYPE_ID

# A real price is good for 15 minutes. An unavailable one (ESI hiccup, no
# orders) is retried sooner rather than silently sitting on "unavailable"
# for the same 15 minutes -- an outage shouldn't look identical to "this
# market genuinely has no orders" for a quarter of an hour.
CACHE_TTL_SECONDS = 900
MISS_TTL_SECONDS = 60

_MISS = object()
_cache: dict[tuple, tuple[float, float | None]] = {}


def clear_price_cache() -> None:
    """Test-only escape hatch -- the cache is module-level and otherwise
    bleeds between tests (and between real page loads across different
    price sources, which is exactly the point in production)."""
    _cache.clear()


def _cache_get(key: tuple):
    hit = _cache.get(key)
    if hit is None:
        return _MISS
    fetched_at, price = hit
    ttl = CACHE_TTL_SECONDS if price is not None else MISS_TTL_SECONDS
    if time.monotonic() - fetched_at > ttl:
        return _MISS
    return price


def _cache_set(key: tuple, price: float | None) -> None:
    _cache[key] = (time.monotonic(), price)


async def get_hub_price(client: ESIClient, hub_key: str, type_id: int, source: str) -> float | None:
    """Cached lowest-sell / highest-buy price for `type_id` at `hub_key`
    (an app.esi.market.APPRAISAL_HUBS key). `source` is "sell" or "buy"."""
    key = ("hub", hub_key, type_id, source)
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    hub = APPRAISAL_HUBS.get(hub_key)
    if not hub:
        return None
    if source == "buy":
        price = await get_hub_buy_price(client, hub["region_id"], hub["station_id"], type_id)
    else:
        price = await get_hub_sell_price(client, hub["region_id"], hub["station_id"], type_id)
    _cache_set(key, price)
    return price


async def get_plex_price(client: ESIClient, source: str) -> float | None:
    """Cached PLEX price from the Global PLEX Market region -- not any
    single station (see app/skillfarm/constants.py). `source` is "sell" or
    "buy"."""
    key = ("plex", source)
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    price = await get_region_price(client, PLEX_REGION_ID, PLEX_TYPE_ID, source)
    _cache_set(key, price)
    return price
