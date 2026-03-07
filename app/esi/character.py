from app.esi.client import ESIClient


async def get_location(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/location/")


async def get_ship(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/ship/")


async def get_clones(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/clones/")


async def get_implants(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/implants/")


async def get_online(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/online/")


async def get_wallet(client: ESIClient, character_id: int) -> float:
    return await client.get(f"/characters/{character_id}/wallet/")


async def get_public_info(client: ESIClient, character_id: int) -> dict:
    return await client.get_public(f"/characters/{character_id}/")


async def get_corporation_roles(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/roles/")
