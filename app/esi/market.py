from app.esi.client import ESIClient


async def get_market_prices(client: ESIClient) -> list:
    """Get global average and adjusted prices for all items."""
    return await client.get_public("/markets/prices/")


async def get_character_orders(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/orders/")
