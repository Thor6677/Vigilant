"""ISS-077: corp contracts cached under the USER's principal go when a
narrowing withdraws one pilot's corporation-contracts permission.

check_corp_contracts caches /corporations/{id}/contracts/ and each contract's
items under principal "user:<id>", and serves them while ANY of the user's
pilots in that corporation still holds the scope. The per-character clear
(principal CHARACTER:EVE:<id>) never matched them, so contracts fetched with
the narrowed pilot's token stayed servable until their TTL (5 min for the
list, 4 h for items). The narrowing's background cache clear now also drops
that user's entries for that pilot's corporation, from the SSO callback and
from a narrowing noticed at token refresh (ISS-078). Keys are built with
app/db/cache.py's own _cache_key, so the matching follows the real format.
"""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import purge
from app.auth import scopes as cat
from app.db import cache as cache_mod
from app.db.cache import ESICache, _cache_key
from app.db.models import Base, Character
from tests.test_permissions_flow import (  # noqa: F401  (env is a fixture)
    ALT_ID, MAIN_ID, OTHER_ID, USER_ID, _pending, _scalar, env,
)
from tests.test_signup_keeps_permissions import refreshes_with, sso_refresh  # noqa: F401

CORP = 98000001          # the corporation test_permissions_flow's metadata stub reports
OTHER_CORP = 98000002
LOOKALIKE_CORP = int(f"{CORP}1")
LOOKALIKE_USER = int(f"{USER_ID}0")


def _principal(user_id):
    return getattr(cache_mod, "user_principal", lambda uid: f"user:{uid}")(user_id)


def _key(path, user_id=None, character_id=None):
    principal = (f"CHARACTER:EVE:{character_id}" if character_id
                 else _principal(user_id) if user_id else None)
    return _cache_key(path, principal=principal)


GOES = {
    _key(f"/corporations/{CORP}/contracts/", USER_ID),
    _key(f"/corporations/{CORP}/contracts/5001/items/", USER_ID),
    _key(f"/corporations/{CORP}/contracts/5002/items/", USER_ID),
}
STAYS = {
    _key(f"/corporations/{OTHER_CORP}/contracts/", USER_ID),               # another corporation
    _key(f"/corporations/{LOOKALIKE_CORP}/contracts/", USER_ID),           # its id starts with CORP's
    _key(f"/corporations/{CORP}/contracts/", OTHER_ID),                    # another user
    _key(f"/corporations/{CORP}/contracts/5001/items/", LOOKALIKE_USER),   # a user id starting with ours
    _key(f"/corporations/{CORP}/"),                                        # public corp info
    _key(f"/characters/{MAIN_ID}/wallet/", character_id=MAIN_ID),           # another pilot's own
}
ALT_OWN = _key(f"/characters/{ALT_ID}/contracts/", character_id=ALT_ID)


def _seed(env, scopes):
    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.scopes = cat.join_scopes(scopes)
        c.corporation_id = CORP
        c.token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)   # due a refresh
        for key in GOES | STAYS | {ALT_OWN}:
            db.add(ESICache(key=key, data="[]", expires_at=datetime(2030, 1, 1)))
        await db.commit()
    env.q(go)


def _keys(env):
    async def go(db):
        return {r.key for r in (await db.execute(select(ESICache))).scalars()}
    return env.q(go)


def _update_alt(env, keep):
    """Re-authorize ALT through the real callback with only `keep`."""
    env.sso_returns(ALT_ID, "Alt Pilot", cat.scopes_for(keep))
    return env.client(_pending("update", keep, ALT_ID, user_id=USER_ID)).get(
        "/auth/callback?code=x&state=S")


def test_withdrawing_corp_contracts_clears_the_users_cached_contracts_for_that_corp(env):
    _seed(env, cat.scopes_for(["wallet", "corp_contracts"]))

    r = _update_alt(env, ["wallet"])

    assert r.status_code == 303
    assert _keys(env) == STAYS


def test_a_narrowing_that_keeps_corp_contracts_leaves_them(env):
    _seed(env, cat.scopes_for(["wallet", "corp_contracts"]))

    _update_alt(env, ["corp_contracts"])

    assert _keys(env) == GOES | STAYS


def test_eve_withdrawing_corp_contracts_at_refresh_clears_them_too(env, sso_refresh):
    from app.esi import client as client_mod
    _seed(env, cat.scopes_for(["wallet", "corp_contracts"]))
    sso_refresh["respond"] = refreshes_with(ALT_ID, [cat.WALLET])

    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        await client_mod.refresh_token(c, db)
        await asyncio.gather(*getattr(purge, "_background_tasks", ()))
    env.q(go)

    assert _keys(env) == STAYS


@pytest.mark.parametrize("path", [
    f"/corporations/{CORP}/contracts/",
    f"/corporations/{CORP}/contracts/123456789/items/",
])
def test_the_pattern_matches_the_keys_cache_set_writes(path):
    """Checked by SQLite's own LIKE, against keys from _cache_key."""
    pattern = purge.narrowing_cache_patterns(USER_ID, CORP, ["corp_contracts"])

    async def go():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        try:
            async with engine.connect() as conn:
                async def like(key):
                    return any([(await conn.execute(text("SELECT :k LIKE :p"), {"k": key, "p": p})).scalar()
                                for p in pattern])
                return (await like(_key(path, USER_ID)),
                        [await like(k) for k in STAYS])
        finally:
            await engine.dispose()

    hit, misses = asyncio.run(go())
    assert hit is True
    assert misses == [False] * len(STAYS)


def test_only_withdrawing_corp_contracts_adds_patterns():
    assert purge.narrowing_cache_patterns(USER_ID, CORP, ["wallet", "assets"]) == ()
    assert purge.narrowing_cache_patterns(USER_ID, None, ["corp_contracts"]) == ()
    assert len(purge.narrowing_cache_patterns(USER_ID, CORP, ["corp_contracts"])) == 1


def test_the_corp_contracts_page_caches_under_the_shared_principal():
    """The principal the clear matches is the one the reader writes with."""
    import inspect
    from app.routes import corporations
    src = inspect.getsource(corporations.check_corp_contracts)
    assert "user_principal(user_id)" in src
