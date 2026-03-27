from app.esi.client import ESIClient


async def get_corporation_info(client: ESIClient, corporation_id: int) -> dict:
    return await client.get_public(f"/corporations/{corporation_id}/")


async def get_alliance_info(client: ESIClient, alliance_id: int) -> dict:
    return await client.get_public(f"/alliances/{alliance_id}/")


async def get_corporation_members(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/members/")


async def get_corporation_wallets(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/wallets/")


async def get_corporation_jobs(client: ESIClient, corporation_id: int) -> list:
    return await client.get(
        f"/corporations/{corporation_id}/industry/jobs/",
        params={"include_completed": "false"},
    )


async def get_corporation_orders(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/orders/")


async def get_corporation_structures(client: ESIClient, corporation_id: int) -> list:
    """Fetch all corp structures with pagination support."""
    all_structures = []
    page = 1
    while True:
        data = await client.get(
            f"/corporations/{corporation_id}/structures/",
            params={"page": page} if page > 1 else {}
        )
        if not isinstance(data, list):
            return []
        if not data:
            break
        all_structures.extend(data)
        if len(data) < 1000:
            break
        page += 1
    return all_structures


async def get_corporation_contracts(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/contracts/")


async def get_corporation_wallet_journal(client: ESIClient, corporation_id: int, division: int = 1, page: int = 1) -> list:
    return await client.get(
        f"/corporations/{corporation_id}/wallets/{division}/journal/",
        params={"page": page},
    )


async def get_corporation_blueprints(client: ESIClient, corporation_id: int, page: int = 1) -> list:
    return await client.get(f"/corporations/{corporation_id}/blueprints/", params={"page": page})
