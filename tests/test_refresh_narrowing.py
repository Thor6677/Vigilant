"""ISS-078: a narrowing EVE applies on its own, noticed at token refresh,
clears the same live state a user's narrowing does.

refresh_token() keeps Character.scopes equal to the refreshed token's `scp`
claim. When that set comes back smaller, the withdrawn permissions' live state
(dashboard-cache columns, asset cache, corp roles) goes in the refresh's own
commit, the ESI response cache is cleared after it in the background, and the
audit log says EVE narrowed it. History is never touched here: only the user
can ask for that. Widening, or a token that can't be read, changes only what
it changed before.

Reuses test_permissions_flow's fixture and test_signup_keeps_permissions'
stub of SSO's token endpoint, so the real refresh code runs.
"""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.auth import purge
from app.auth import scopes as cat
from app.db.cache import ESICache
from app.db.models import (
    AdminAuditLog, Character, CharacterAssetCache, CharacterCorpRoles, CharacterDashboardCache,
    NetWorthSnapshot, WalletSnapshot, WalletTransaction,
)
from app.esi import client as client_mod
from tests.test_permissions_flow import (  # noqa: F401  (env is a fixture)
    ALT_ID, MAIN_ID, USER_ID, _count, _pending, _scalar, env,
)
from tests.test_signup_keeps_permissions import (  # noqa: F401  (sso_refresh is a fixture)
    CALLBACK, SKILLS, refreshes_with, sso_refresh,
)

SYNCED = "2026-09-01T00:00:00+00:00"


async def _drain():
    """Let the post-refresh ESI cache clear finish before the loop closes."""
    pending = list(getattr(purge, "_background_tasks", ()))
    if pending:
        await asyncio.gather(*pending)


def _seed(env, scopes):
    """ALT with live state for wallet and assets, a year of history in
    miniature, corp roles, and ESI cache entries for it and for MAIN."""
    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.scopes = cat.join_scopes(scopes)
        c.token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)   # due a refresh
        db.add(CharacterDashboardCache(
            character_id=ALT_ID, wallet=1e9,
            field_synced_json=json.dumps({"wallet": SYNCED, "assets": SYNCED, "roles": SYNCED})))
        db.add(CharacterAssetCache(character_id=ALT_ID, assets_json="[]"))
        db.add(CharacterCorpRoles(character_id=ALT_ID, roles_json='["Director"]'))
        for i in range(2):
            db.add(WalletSnapshot(character_id=ALT_ID, balance=1e9,
                                  recorded_at=datetime(2026, 9, 1) + timedelta(hours=i)))
        db.add(WalletTransaction(transaction_id=8101, character_id=ALT_ID, date=datetime(2026, 9, 1),
                                 type_id=34, quantity=1, unit_price=5.0, is_buy=True))
        db.add(NetWorthSnapshot(character_id=ALT_ID, date=date(2026, 9, 1), user_id=USER_ID,
                                wallet=1e9, assets_value=5.0, total=1e9 + 5.0))
        for cid in (ALT_ID, MAIN_ID):
            db.add(ESICache(key=f"k{cid}:@CHARACTER:EVE:{cid}|/characters/{cid}/assets/",
                            data="1", expires_at=datetime(2030, 1, 1)))
        await db.commit()
    env.q(go)


def _refresh(env):
    """refresh_token() on ALT in one session, as a page or the sync would,
    counting the commits it makes on that session."""
    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        commits = []
        real_commit = db.commit

        async def commit():
            commits.append(1)
            await real_commit()
        db.commit = commit
        await client_mod.refresh_token(c, db)
        await _drain()
        return len(commits)
    return env.q(go)


def _state(env):
    async def go(db):
        cache = await _scalar(db, select(CharacterDashboardCache).where(
            CharacterDashboardCache.character_id == ALT_ID))
        nw = await _scalar(db, select(NetWorthSnapshot).where(NetWorthSnapshot.character_id == ALT_ID))
        keys = sorted(r.key.split(":@")[1] for r in (await db.execute(select(ESICache))).scalars())
        audit = [(a.event_type, a.user_id, a.detail) for a in (await db.execute(
            select(AdminAuditLog).where(AdminAuditLog.character_id == ALT_ID))).scalars()]
        return {
            "scopes": cat.parse_scopes(
                (await _scalar(db, select(Character).where(Character.character_id == ALT_ID))).scopes),
            "wallet": cache.wallet,
            "synced": sorted(json.loads(cache.field_synced_json or "{}")),
            "asset_cache": await _scalar(db, select(CharacterAssetCache.character_id).where(
                CharacterAssetCache.character_id == ALT_ID)) is not None,
            "corp_roles": await _scalar(db, select(CharacterCorpRoles.character_id).where(
                CharacterCorpRoles.character_id == ALT_ID)) is not None,
            "nw_assets": nw.assets_value,
            "esi_keys": keys,
            "audit": audit,
        }
    return env.q(go)


ALT_KEY = f"CHARACTER:EVE:{ALT_ID}|/characters/{ALT_ID}/assets/"
MAIN_KEY = f"CHARACTER:EVE:{MAIN_ID}|/characters/{MAIN_ID}/assets/"


def test_a_refresh_that_comes_back_narrower_clears_the_withdrawn_live_state(env, sso_refresh):
    _seed(env, [cat.WALLET, cat.ASSETS, cat.CORP_ROLES])
    sso_refresh["respond"] = refreshes_with(ALT_ID, [cat.WALLET])   # assets and corp roles gone

    commits = _refresh(env)

    s = _state(env)
    assert s["scopes"] == {cat.WALLET}
    # Withdrawn: the asset cache, the corp roles, their sync bookkeeping...
    assert s["asset_cache"] is False and s["corp_roles"] is False
    assert s["synced"] == ["wallet"]
    # ...and the character's ESI response cache, after the refresh.
    assert s["esi_keys"] == [MAIN_KEY]
    # Still granted: the wallet's live value stays.
    assert s["wallet"] == 1e9
    # History is the user's call, never the refresh's.
    assert _count(env, WalletSnapshot, character_id=ALT_ID) == 2
    assert _count(env, WalletTransaction, character_id=ALT_ID) == 1
    assert s["nw_assets"] == 5.0
    # Audited as a permissions change EVE made, under the owner.
    [(event, user_id, detail)] = s["audit"]
    assert (event, user_id) == ("permissions_changed", USER_ID)
    assert "refresh" in detail and "EVE" in detail
    assert "assets" in detail and "corp_roles" in detail
    # One commit, as before: the clear rides on the refresh's own.
    assert commits == 1


def test_a_refresh_that_comes_back_wider_only_updates_the_scopes(env, sso_refresh):
    _seed(env, [cat.WALLET, cat.ASSETS])
    sso_refresh["respond"] = refreshes_with(ALT_ID, [cat.WALLET, cat.ASSETS, cat.SKILLS])

    _refresh(env)

    s = _state(env)
    assert s["scopes"] == {cat.WALLET, cat.ASSETS, cat.SKILLS}
    assert (s["wallet"], s["asset_cache"], s["corp_roles"]) == (1e9, True, True)
    assert s["synced"] == ["assets", "roles", "wallet"]
    assert s["esi_keys"] == sorted([ALT_KEY, MAIN_KEY])
    assert s["audit"] == []


def test_a_refresh_with_an_unreadable_token_clears_nothing(env, sso_refresh):
    from tests.test_signup_keeps_permissions import _Resp
    _seed(env, [cat.WALLET, cat.ASSETS])
    sso_refresh["respond"] = lambda data: _Resp(200, {
        "access_token": "opaque", "refresh_token": "r2", "expires_in": 1200})

    _refresh(env)

    s = _state(env)
    assert s["scopes"] == {cat.WALLET, cat.ASSETS}
    assert (s["wallet"], s["asset_cache"], s["corp_roles"]) == (1e9, True, True)
    assert s["esi_keys"] == sorted([ALT_KEY, MAIN_KEY])
    assert s["audit"] == []


def test_the_signup_probe_leaves_the_narrowing_to_the_callback(env, sso_refresh, monkeypatch):
    """ISS-068's probe refreshes the stored token to see whether EVE already
    narrowed it, then the callback narrows from what the pilot HAD. The
    refresh must not clear (or audit, or schedule a cache clear) on its own
    as well."""
    import app.auth.routes as auth_routes
    clears, scheduled, handed_off = [], [], []
    real_clear = purge.clear_live_state

    async def counting_clear(db, character_id, keys):
        clears.append(sorted(keys))
        return await real_clear(db, character_id, keys)

    async def record_only(*args, **kwargs):
        handed_off.append(args[1])
    monkeypatch.setattr(purge, "clear_live_state", counting_clear)
    monkeypatch.setattr(auth_routes, "clear_live_state", counting_clear)
    monkeypatch.setattr(purge, "schedule_esi_cache_clear",
                        lambda *a, **k: scheduled.append(a), raising=False)
    monkeypatch.setattr(auth_routes, "clear_esi_cache_in_background", record_only)

    sso_refresh["respond"] = refreshes_with(MAIN_ID, SKILLS)       # EVE already narrowed it
    env.sso_returns(MAIN_ID, "Main Pilot", SKILLS)
    r = env.client(_pending("signup", ["skills"])).get(CALLBACK)

    assert r.status_code == 303
    assert len(sso_refresh["calls"]) == 1                         # the probe ran
    assert len(clears) == 1 and "wallet" in clears[0]             # the callback's own clear
    assert scheduled == [] and handed_off == [MAIN_ID]
    events = env.q(lambda db: _events(db, MAIN_ID))
    assert events == ["permissions_changed"]


async def _events(db, cid):
    return [a.event_type for a in (await db.execute(
        select(AdminAuditLog).where(AdminAuditLog.character_id == cid))).scalars()]
