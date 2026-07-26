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
