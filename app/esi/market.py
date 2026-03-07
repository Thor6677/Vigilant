from app.esi.client import ESIClient

# Common region IDs
REGIONS = {
    "the forge": 10000002,      # Jita
    "domain": 10000043,          # Amarr
    "sinq laison": 10000032,     # Dodixie
    "metropolis": 10000042,      # Hek
    "heimatar": 10000030,        # Rens
}


async def get_market_orders(client: ESIClient, region_id: int, type_id: int = None) -> list:
    all_orders = []
    page = 1
    while True:
        params = {"page": page}
        if type_id:
            params["type_id"] = type_id
        data = await client.get_public(f"/markets/{region_id}/orders/", params=params)
        if not data:
            break
        all_orders.extend(data)
        if len(data) < 1000:
            break
        page += 1
    return all_orders


async def get_market_prices(client: ESIClient) -> list:
    """Get global average and adjusted prices for all items."""
    return await client.get_public("/markets/prices/")


async def get_character_orders(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/orders/")


async def get_character_order_history(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/orders/history/")


async def search_type_id(client: ESIClient, item_name: str) -> list:
    """Search for type IDs by name."""
    return await client.get_public(
        "/search/",
        params={"categories": "inventory_type", "search": item_name, "strict": "false"},
    )
