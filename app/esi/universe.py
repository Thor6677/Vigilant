from app.esi.client import ESIClient


async def get_system(client: ESIClient, system_id: int) -> dict:
    return await client.get_public(f"/universe/systems/{system_id}/")


async def get_station(client: ESIClient, station_id: int) -> dict:
    return await client.get_public(f"/universe/stations/{station_id}/")


async def get_structure(client: ESIClient, structure_id: int) -> dict:
    return await client.get(f"/universe/structures/{structure_id}/")


async def get_type(client: ESIClient, type_id: int) -> dict:
    return await client.get_public(f"/universe/types/{type_id}/")


async def get_route(client: ESIClient, origin: int, destination: int, flag: str = "shortest") -> list:
    """Calculate route between two solar systems. flag: shortest, secure, insecure."""
    return await client.get_public(f"/route/{origin}/{destination}/", params={"flag": flag})


async def resolve_ids(client: ESIClient, ids: list[int]) -> list:
    """Bulk resolve IDs to names."""
    if not ids:
        return []
    # Deduplicate and chunk to 1000 max per ESI limit
    ids = list(set(ids))
    results = []
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i+1000]
        data = await client.post_public("/universe/names/", chunk)
        results.extend(data)
    return results


async def search_universe(client: ESIClient, query: str, categories: list[str]) -> dict:
    """Search for systems, stations, types, characters etc."""
    return await client.get_public(
        "/search/",
        params={
            "categories": ",".join(categories),
            "search": query,
            "strict": "false",
        },
    )


async def get_constellation(client: ESIClient, constellation_id: int) -> dict:
    return await client.get_public(f"/universe/constellations/{constellation_id}/")


async def get_region(client: ESIClient, region_id: int) -> dict:
    return await client.get_public(f"/universe/regions/{region_id}/")


async def get_system_kills(client: ESIClient) -> list:
    return await client.get_public("/universe/system_kills/")


async def get_system_jumps(client: ESIClient) -> list:
    return await client.get_public("/universe/system_jumps/")
