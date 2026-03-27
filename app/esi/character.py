from app.esi.client import ESIClient


async def get_location(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/location/")


async def get_ship(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/ship/")


async def get_clones(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/clones/")


async def get_online(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/online/")


async def get_wallet(client: ESIClient, character_id: int) -> float:
    return await client.get(f"/characters/{character_id}/wallet/")


async def get_wallet_journal(client: ESIClient, character_id: int, page: int = 1) -> list:
    return await client.get(f"/characters/{character_id}/wallet/journal/", params={"page": page})


async def get_public_info(client: ESIClient, character_id: int) -> dict:
    return await client.get_public(f"/characters/{character_id}/")


async def get_skill_queue(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/skillqueue/")


async def get_mail_headers(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/mail/")


async def get_notifications(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/notifications/")


async def get_contracts(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/contracts/")


async def get_planets(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/planets/")


async def get_planet_details(client: ESIClient, character_id: int, planet_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/planets/{planet_id}/")


async def get_mail(client: ESIClient, character_id: int, mail_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/mail/{mail_id}/")


async def get_attributes(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/attributes/")


async def get_skills(client: ESIClient, character_id: int) -> dict:
    return await client.get(f"/characters/{character_id}/skills/")


async def get_fittings(client: ESIClient, character_id: int) -> list:
    return await client.get(f"/characters/{character_id}/fittings/")


async def get_blueprints(client: ESIClient, character_id: int, page: int = 1) -> list:
    return await client.get(f"/characters/{character_id}/blueprints/", params={"page": page})


async def get_mining(client: ESIClient, character_id: int, page: int = 1) -> list:
    return await client.get(f"/characters/{character_id}/mining/", params={"page": page})
