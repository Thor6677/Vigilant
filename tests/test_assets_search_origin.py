"""ISS-171: /assets/search measures jump distances from a reference character.
The signed cookie's active_character_id can outlive the character's place in
the account, so it is only a fallback when the character still belongs to the
caller; otherwise another owner's location cache would set the origin."""
import asyncio
import json
import types
from datetime import datetime

from app.db.models import Character, CharacterAssetCache, CharacterDashboardCache, User
from app.routes import assets as assets_mod
from tests.test_character_removal import _fresh_engine

_NOW = datetime(2026, 9, 1, 12, 0, 0)
MINE, THEIRS = 90000001, 90000002


def _origin_for(session, from_char=""):
    """Run assets_search and return the system the BFS was started from."""
    seen = []

    async def fake_jumps(db, origin):
        seen.append(origin)
        return {}

    async def go():
        engine, SessionLocal = await _fresh_engine()
        try:
            async with SessionLocal() as db:
                db.add_all([User(id=1), User(id=2)])
                db.add(Character(character_id=MINE, character_name="Mine", user_id=1,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                db.add(Character(character_id=THEIRS, character_name="Theirs", user_id=2,
                                 access_token="x", refresh_token="y", token_expiry=_NOW))
                db.add(CharacterAssetCache(
                    character_id=MINE,
                    assets_json=json.dumps([{"type_name": "Tritanium", "type_id": 34, "quantity": 5, "security": 0.9, "location_name": "Jita", "region": "Forge", "location_flag": "Hangar", "system_id": 30000142}])))
                db.add(CharacterDashboardCache(
                    character_id=MINE, location_json=json.dumps({"system_id": 111})))
                db.add(CharacterDashboardCache(
                    character_id=THEIRS, location_json=json.dumps({"system_id": 222})))
                await db.commit()
                request = types.SimpleNamespace(session=session)
                orig = assets_mod.sde.jump_distances_from
                assets_mod.sde.jump_distances_from = fake_jumps
                try:
                    resp = await assets_mod.assets_search(request, q="trit", from_char=from_char, db=db)
                finally:
                    assets_mod.sde.jump_distances_from = orig
                assert resp.status_code == 200
        finally:
            await engine.dispose()
    asyncio.run(go())
    return seen[0] if seen else None


def test_stale_active_character_of_another_owner_is_not_the_origin():
    assert _origin_for({"user_id": 1, "active_character_id": THEIRS}) is None
    assert _origin_for({"user_id": 1, "active_character_id": THEIRS}, from_char="abc") is None
    assert _origin_for({"user_id": 1, "active_character_id": THEIRS}, from_char=str(THEIRS)) is None


def test_own_active_character_is_still_the_fallback():
    assert _origin_for({"user_id": 1, "active_character_id": MINE}) == 111
    assert _origin_for({"user_id": 1, "active_character_id": MINE}, from_char="abc") == 111


def test_explicit_owned_from_char_wins():
    assert _origin_for({"user_id": 1, "active_character_id": THEIRS}, from_char=str(MINE)) == 111
