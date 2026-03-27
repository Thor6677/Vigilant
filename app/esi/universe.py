from app.esi.client import ESIClient


async def get_station(client: ESIClient, station_id: int) -> dict:
    return await client.get_public(f"/universe/stations/{station_id}/")


async def get_structure(client: ESIClient, structure_id: int) -> dict:
    return await client.get(f"/universe/structures/{structure_id}/")


async def get_type(client: ESIClient, type_id: int) -> dict:
    return await client.get_public(f"/universe/types/{type_id}/")
