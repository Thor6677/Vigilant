from app.esi.client import ESIClient


async def get_corporation_info(client: ESIClient, corporation_id: int) -> dict:
    return await client.get_public(f"/corporations/{corporation_id}/")


async def get_alliance_info(client: ESIClient, alliance_id: int) -> dict:
    return await client.get_public(f"/alliances/{alliance_id}/")


async def get_corporation_members(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/members/")


async def get_corporation_divisions(client: ESIClient, corporation_id: int) -> dict:
    return await client.get(f"/corporations/{corporation_id}/divisions/")


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
    return await client.get(f"/corporations/{corporation_id}/structures/")


async def get_corporation_contracts(client: ESIClient, corporation_id: int) -> list:
    return await client.get(f"/corporations/{corporation_id}/contracts/")
