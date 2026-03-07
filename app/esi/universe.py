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
    async with __import__("httpx").AsyncClient(timeout=30) as http:
        resp = await http.post(
            "https://esi.evetech.net/latest/universe/names/",
            json=ids,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


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
