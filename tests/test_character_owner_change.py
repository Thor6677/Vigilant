"""A character that moves to another EVE account starts over in Vigilant.

EVE SSO reports which account owns a character (CharacterOwnerHash, also the
access token's `owner` claim). Vigilant keeps it per character; when a sign-in
reports a different owner than the stored one, the character is removed from
the account that registered it — tokens, caches and history included — and the
sign-in carries on as if the character were new. A sign-in that reports no
owner at all is treated as unknown, never as a change.

Reuses the SSO-stubbed environment from test_permissions_flow.py.
"""
import base64
import json

from sqlalchemy import select

from app.auth import scopes as cat
from app.db.cache import ESICache
from app.db.models import (
    AdminAuditLog, Character, CharacterDashboardCache, RegistrationAllowlist, User,
    WalletSnapshot,
)
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    ALT_ID, MAIN_ID, OTHER_ID, USER_ID, _count, _pending, _scalar, env,
)

import app.auth.routes as auth_routes


def _jwt(**claims) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'sub': 'CHARACTER:EVE:1', **claims})}.x"


def _sso(monkeypatch, character_id, name, owner=None, verify_owner=None, scopes=()):
    """SSO answering for `character_id`, owned by `owner` per the token claim
    and/or `verify_owner` per /oauth/verify."""
    claims = {"owner": owner} if owner else {}

    async def fake_exchange(code):
        verify = {"CharacterID": character_id, "CharacterName": name,
                  "Scopes": " ".join(scopes)}
        if verify_owner:
            verify["CharacterOwnerHash"] = verify_owner
        return ({"access_token": _jwt(**claims), "refresh_token": "buyer-refresh",
                 "expires_in": 1200}, verify)

    monkeypatch.setattr(auth_routes, "_exchange_code", fake_exchange)


def _set_owner(env, cid, owner_hash):
    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == cid))
        c.owner_hash = owner_hash
        await db.commit()
    env.q(go)


def _seed_data(env, cid):
    """Things the old owner's tokens collected for this character."""
    from datetime import datetime, timezone

    async def go(db):
        db.add(CharacterDashboardCache(character_id=cid, wallet=123.0))
        db.add(WalletSnapshot(character_id=cid, balance=123.0,
                              recorded_at=datetime.now(timezone.utc)))
        db.add(ESICache(key=f"h:@CHARACTER:EVE:{cid}|/characters/{cid}/wallet/",
                        data="1", expires_at=datetime.now(timezone.utc)))
        await db.commit()
    env.q(go)


def _login(env):
    return env.client(_pending("login")).get("/auth/callback?code=x&state=S")


def test_first_sign_in_records_the_owner(env, monkeypatch):
    _sso(monkeypatch, MAIN_ID, "Main Pilot", owner="owner-A")
    r = _login(env)
    assert r.status_code == 303
    assert env.session_of(r)["user_id"] == USER_ID
    assert env.char(MAIN_ID).owner_hash == "owner-A"


def test_verify_owner_is_the_fallback(env, monkeypatch):
    _sso(monkeypatch, MAIN_ID, "Main Pilot", verify_owner="owner-V")
    _login(env)
    assert env.char(MAIN_ID).owner_hash == "owner-V"


def test_token_claim_wins_over_verify(env, monkeypatch):
    _sso(monkeypatch, MAIN_ID, "Main Pilot", owner="owner-T", verify_owner="owner-V")
    _login(env)
    assert env.char(MAIN_ID).owner_hash == "owner-T"


def test_same_owner_signs_in_as_before(env, monkeypatch):
    _set_owner(env, MAIN_ID, "owner-A")
    _sso(monkeypatch, MAIN_ID, "Main Pilot", owner="owner-A")
    r = _login(env)
    assert env.session_of(r)["user_id"] == USER_ID
    assert env.char(MAIN_ID).user_id == USER_ID


def test_no_reported_owner_is_not_a_change(env, monkeypatch):
    _set_owner(env, MAIN_ID, "owner-A")
    _sso(monkeypatch, MAIN_ID, "Main Pilot")
    r = _login(env)
    assert env.session_of(r)["user_id"] == USER_ID
    assert env.char(MAIN_ID).owner_hash == "owner-A"


def test_new_owner_does_not_sign_into_the_old_account(env, monkeypatch):
    _set_owner(env, ALT_ID, "owner-A")
    _seed_data(env, ALT_ID)
    _sso(monkeypatch, ALT_ID, "Alt Pilot", owner="owner-B")

    r = _login(env)

    assert r.status_code == 303
    new_user = env.session_of(r)["user_id"]
    assert new_user not in (USER_ID, OTHER_ID)
    char = env.char(ALT_ID)
    assert char.user_id == new_user
    assert char.owner_hash == "owner-B"
    assert char.refresh_token == "buyer-refresh"
    # The old owner's grant is gone with the old row; nothing is carried over.
    assert char.scopes == ""
    assert _count(env, WalletSnapshot, character_id=ALT_ID) == 0
    assert _count(env, CharacterDashboardCache, character_id=ALT_ID) == 0
    assert env.q(lambda db: _all_cache_keys(db, ALT_ID)) == []
    # The old owner's other character is untouched.
    assert env.char(MAIN_ID).user_id == USER_ID
    # Recorded against the account that lost it.
    events = env.q(lambda db: _events(db, "character_transferred"))
    assert [(e.user_id, e.character_id) for e in events] == [(USER_ID, ALT_ID)]
    # Old tokens are never revoked: EVE's one grant per character per app is now the buyer's.
    assert env.calls["revoked"] == []


def test_new_owner_of_an_admins_main_gets_no_admin(env, monkeypatch):
    async def make_admin(db):
        u = await _scalar(db, select(User).where(User.id == USER_ID))
        u.role, u.is_admin = "admin", True
        await db.commit()
    env.q(make_admin)
    _set_owner(env, MAIN_ID, "owner-A")
    _sso(monkeypatch, MAIN_ID, "Main Pilot", owner="owner-B")

    session = env.session_of(_login(env))

    assert session["user_id"] != USER_ID
    assert session["role"] == "user"
    assert session["is_admin"] is False


def test_new_owner_can_add_it_to_their_own_account(env, monkeypatch):
    """Before, the stale row made this read as 'already claimed by someone else'."""
    _set_owner(env, ALT_ID, "owner-A")
    _sso(monkeypatch, ALT_ID, "Alt Pilot", owner="owner-B", scopes=[cat.WALLET])
    r = env.client(_pending("add", ["wallet"], user_id=OTHER_ID)).get(
        "/auth/callback?code=x&state=S")
    assert r.status_code == 303
    assert "character_claimed" not in r.headers["location"]
    char = env.char(ALT_ID)
    assert char.user_id == OTHER_ID
    assert char.owner_hash == "owner-B"
    assert char.is_main is False


def test_the_allowlist_still_applies_to_the_new_owner(env, monkeypatch):
    async def restrict(db):
        db.add(RegistrationAllowlist(entry_type="character", eve_id=1))
        await db.commit()
    env.q(restrict)
    _set_owner(env, ALT_ID, "owner-A")
    _sso(monkeypatch, ALT_ID, "Alt Pilot", owner="owner-B")

    r = _login(env)

    assert r.status_code == 200
    assert "Registration is restricted" in r.text
    assert "user_id" not in env.session_of(r)
    # The old account has lost it regardless.
    assert env.char(ALT_ID) is None


async def _events(db, event_type):
    return (await db.execute(select(AdminAuditLog).where(
        AdminAuditLog.event_type == event_type))).scalars().all()


async def _all_cache_keys(db, cid):
    return [k for k in (await db.execute(select(ESICache.key))).scalars().all()
            if f"CHARACTER:EVE:{cid}|" in k]
