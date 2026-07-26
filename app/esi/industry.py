from app.esi.client import ESIClient


async def get_character_jobs(client: ESIClient, character_id: int, include_completed: bool = False) -> list:
    return await client.get(
        f"/characters/{character_id}/industry/jobs/",
        params={"include_completed": str(include_completed).lower()},
    )


async def get_corporation_jobs(client: ESIClient, corporation_id: int, include_completed: bool = False) -> list:
    return await client.get(
        f"/corporations/{corporation_id}/industry/jobs/",
        params={"include_completed": str(include_completed).lower()},
    )
