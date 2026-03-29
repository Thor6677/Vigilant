import asyncio
from app.esi.client import ESIClient
from app.db.models import AsyncSessionLocal


async def get_market_prices(client: ESIClient) -> list:
    """Get global average and adjusted prices for all items."""
    return await client.get_public("/markets/prices/")


async def get_character_orders(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/orders/")


# ── Trade hub sell price fetching ─────────────────────────────────────────────

APPRAISAL_HUBS = {
    "jita":    {"label": "Jita 4-4",     "region_id": 10000002, "station_id": 60003760},
    "amarr":   {"label": "Amarr VIII",   "region_id": 10000043, "station_id": 60008494},
    "dodixie": {"label": "Dodixie IX",   "region_id": 10000032, "station_id": 60011866},
    "hek":     {"label": "Hek VIII",     "region_id": 10000042, "station_id": 60005686},
    "rens":    {"label": "Rens VI",      "region_id": 10000030, "station_id": 60004588},
}


async def get_hub_sell_price(
    client: ESIClient, region_id: int, station_id: int, type_id: int,
) -> float | None:
    """Get the lowest sell price for a type at a specific trade hub station."""
    try:
        orders = await client.get_public(
            f"/markets/{region_id}/orders/",
            params={"type_id": type_id, "order_type": "sell"},
        )
        if not isinstance(orders, list):
            return None
        station_orders = [o for o in orders if o.get("location_id") == station_id]
        if not station_orders:
            return None
        return min(o["price"] for o in station_orders)
    except Exception:
        return None


async def get_hub_prices_batch(
    client: ESIClient,
    hub_key: str,
    type_ids: list[int],
    max_concurrent: int = 10,
) -> dict[int, float | None]:
    """Fetch sell prices for multiple items concurrently with a semaphore."""
    hub = APPRAISAL_HUBS.get(hub_key)
    if not hub:
        return {}

    region_id = hub["region_id"]
    station_id = hub["station_id"]
    sem = asyncio.Semaphore(max_concurrent)
    results: dict[int, float | None] = {}

    async def fetch_one(tid: int):
        async with sem:
            # Each concurrent call gets its own DB session to avoid SQLAlchemy contention
            async with AsyncSessionLocal() as session:
                c = ESIClient("", db=session)
                results[tid] = await get_hub_sell_price(c, region_id, station_id, tid)

    await asyncio.gather(*[fetch_one(tid) for tid in type_ids])
    return results
