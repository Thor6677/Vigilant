from app.esi.client import ESIClient


async def get_character_assets(client: ESIClient, character_id: int) -> list:
    all_assets = []
    page = 1
    while True:
        data = await client.get(f"/characters/{character_id}/assets/", params={"page": page})
        if not data:
            break
        all_assets.extend(data)
        if len(data) < 1000:
            break
        page += 1
    return all_assets


async def get_corporation_assets(client: ESIClient, corporation_id: int) -> list:
    all_assets = []
    page = 1
    while True:
        data = await client.get(f"/corporations/{corporation_id}/assets/", params={"page": page})
        if not data:
            break
        all_assets.extend(data)
        if len(data) < 1000:
            break
        page += 1
    return all_assets


async def get_asset_names(client: ESIClient, character_id: int, item_ids: list[int]) -> list:
    """Resolve custom names for containers/ships."""
    if not item_ids:
        return []
    return await client.get(
        f"/characters/{character_id}/assets/names/",
    )
