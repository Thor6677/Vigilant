"""app/skillfarm/prices.py — the short in-process price cache (T-073).

The point of this cache is that a page load never fires a fresh ESI call for
a price it already has. These tests never construct a real ESIClient or hit
the network — they monkeypatch the underlying app.esi.market functions and
count calls, so a "cache hit" is proven by the mock simply never being
invoked a second time.
"""
import asyncio

import pytest

from app.skillfarm import prices as farm_prices


@pytest.fixture(autouse=True)
def _clear_cache():
    farm_prices.clear_price_cache()
    yield
    farm_prices.clear_price_cache()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_hub_price_cache_hit_makes_no_second_call(monkeypatch):
    calls = []

    async def fake_sell(client, region_id, station_id, type_id):
        calls.append((region_id, station_id, type_id))
        return 123.0

    monkeypatch.setattr(farm_prices, "get_hub_sell_price", fake_sell)

    client = object()  # never touched on a cache hit
    first = _run(farm_prices.get_hub_price(client, "jita", 40520, "sell"))
    second = _run(farm_prices.get_hub_price(client, "jita", 40520, "sell"))

    assert first == 123.0 and second == 123.0
    assert len(calls) == 1, "second call should have been served from cache"


def test_hub_price_cache_is_keyed_by_type_and_source():
    """A different type_id or a different source (buy vs sell) must not
    collide with an existing cache entry."""
    from app.skillfarm import prices as fp
    import time as _time

    fp._cache[("hub", "jita", 1, "sell")] = (_time.monotonic(), 111.0)
    assert fp._cache_get(("hub", "jita", 1, "sell")) == 111.0
    assert fp._cache_get(("hub", "jita", 2, "sell")) is fp._MISS
    assert fp._cache_get(("hub", "jita", 1, "buy")) is fp._MISS


def test_plex_price_cache_hit_makes_no_second_call(monkeypatch):
    calls = []

    async def fake_region(client, region_id, type_id, order_type):
        calls.append((region_id, type_id, order_type))
        return 5_000_000.0

    monkeypatch.setattr(farm_prices, "get_region_price", fake_region)

    client = object()
    first = _run(farm_prices.get_plex_price(client, "sell"))
    second = _run(farm_prices.get_plex_price(client, "sell"))

    assert first == 5_000_000.0 and second == 5_000_000.0
    assert len(calls) == 1


def test_a_stale_entry_is_treated_as_a_miss():
    import time as _time
    from app.skillfarm import prices as fp

    # Backdate the cache entry past its TTL.
    fp._cache[("plex", "sell")] = (_time.monotonic() - fp.CACHE_TTL_SECONDS - 1, 999.0)
    assert fp._cache_get(("plex", "sell")) is fp._MISS


def test_an_unavailable_price_is_still_cached_but_with_a_shorter_ttl(monkeypatch):
    """ESI outage / no orders (price is None): don't hammer ESI every page
    load, but don't sit on "unavailable" for the full 15 minutes either."""
    calls = []

    async def fake_sell(client, region_id, station_id, type_id):
        calls.append(1)
        return None

    monkeypatch.setattr(farm_prices, "get_hub_sell_price", fake_sell)
    client = object()

    price = _run(farm_prices.get_hub_price(client, "jita", 40519, "sell"))
    assert price is None
    price_again = _run(farm_prices.get_hub_price(client, "jita", 40519, "sell"))
    assert price_again is None
    assert len(calls) == 1, "a None price must still be cached, not refetched immediately"

    # Confirm the MISS ttl is shorter than the real-price ttl (miss is retried sooner).
    assert farm_prices.MISS_TTL_SECONDS < farm_prices.CACHE_TTL_SECONDS


def test_unknown_hub_key_returns_none_without_calling_anything(monkeypatch):
    called = []
    monkeypatch.setattr(farm_prices, "get_hub_sell_price", lambda *a, **k: called.append(1))
    client = object()
    assert _run(farm_prices.get_hub_price(client, "nonexistent-hub", 1, "sell")) is None
    assert called == []
