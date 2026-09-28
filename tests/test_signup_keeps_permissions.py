"""ISS-068: signing up or adding a character that already belongs to an account
must not silently narrow what it shares.

The trap: the landing page's primary button was "New here? Choose what to
share", and the picker's Minimal preset reads "Just log in". A returning user
who did exactly that replaced a registered pilot's full token with a scopeless
one and lost every permission, with no warning. Now a narrower signup/add
first refreshes the STORED token (under the sync layer's per-character lock):
if it still works and still covers everything, it is kept and this is treated
as a login; if it is dead, or EVE has already narrowed it, the new grant is
taken as before, with a warning.

Reuses test_permissions_flow's fixture, which stubs the SSO code exchange and
the public-metadata lookup. SSO's refresh endpoint is stubbed here, at the
HTTP client _do_refresh uses, so the real refresh code runs.
"""
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.auth import scopes as cat
from app.db.cache import ESICache
from app.db.models import AdminAuditLog, Character, CharacterCorpRoles, CharacterDashboardCache
from tests.test_permissions_flow import (  # noqa: F401  (env is a fixture)
    ALT_ID, MAIN_ID, USER_ID, _count, _pending, _scalar, env,
)

CALLBACK = "/auth/callback?code=x&state=S"
SYNCED_AT = "2026-09-01T00:00:00+00:00"
LEGACY_SCOPE = "esi-industry.read_corporation_mining.v1"   # asked for by old releases, read by nothing
SKILLS = cat.scopes_for(["skills"])


def _jwt(character_id, scopes):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    claims = {"azp": "test", "sub": f"CHARACTER:EVE:{character_id}", "scp": list(scopes)}
    return f"{b64({'alg': 'none'})}.{b64(claims)}.x"


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def refreshes_with(character_id, scopes):
    """SSO accepts the stored refresh token and rotates it."""
    return lambda data: _Resp(200, {"access_token": _jwt(character_id, scopes),
                                    "refresh_token": "rotated-refresh", "expires_in": 1200})


def rejected(data):
    return _Resp(400, {"error": "invalid_grant"})


def unreachable(data):
    raise httpx.ConnectError("no route to SSO")


@pytest.fixture
def sso_refresh(monkeypatch):
    """EVE SSO's token endpoint as _do_refresh reaches it. Records every
    refresh; `respond` decides the answer."""
    from app.esi import client as client_mod
    state = {"calls": [], "respond": None}

    class FakeHTTP:
        async def post(self, url, headers=None, data=None):
            state["calls"].append(dict(data or {}))
            return state["respond"](data)

    monkeypatch.setattr(client_mod, "get_http_client", lambda: FakeHTTP())
    return state


def _seed_live_state(env, cid):
    """What a synced pilot has: live values, sync bookkeeping, ESI cache
    entries and corp roles — everything a narrowing would clear."""
    async def go(db):
        db.add(CharacterDashboardCache(
            character_id=cid, wallet=1e9,
            field_synced_json=json.dumps({"wallet": SYNCED_AT}),
            sync_warnings_json=json.dumps({"assets": "esi_error: ReadTimeout"})))
        db.add(ESICache(key=f"abc:@CHARACTER:EVE:{cid}|/characters/{cid}/wallet/",
                        data="1", expires_at=datetime(2030, 1, 1)))
        db.add(CharacterCorpRoles(character_id=cid, roles_json='["Director"]'))
        await db.commit()
    env.q(go)


def _cache(env, cid):
    return env.q(lambda db: _scalar(db, select(CharacterDashboardCache).where(
        CharacterDashboardCache.character_id == cid)))


def _audit(env, cid):
    async def go(db):
        rows = (await db.execute(select(AdminAuditLog).where(
            AdminAuditLog.character_id == cid).order_by(AdminAuditLog.id))).scalars().all()
        return [(r.event_type, r.user_id, r.detail) for r in rows]
    return env.q(go)


def _esi_keys(env):
    async def go(db):
        return [r.key for r in (await db.execute(select(ESICache))).scalars().all()]
    return env.q(go)


# ── The stored authorization still works: keep it ──────────────────────────

def test_minimal_signup_on_a_registered_pilot_changes_nothing(env, sso_refresh):
    """The reported case: logged out, "New here?", Minimal, an existing pilot."""
    _seed_live_state(env, MAIN_ID)
    before = env.char(MAIN_ID)
    sso_refresh["respond"] = refreshes_with(MAIN_ID, cat.ALL_SCOPES)
    env.sso_returns(MAIN_ID, "Main Pilot", [])

    r = env.client(_pending("signup", [])).get(CALLBACK)

    assert r.status_code == 303 and r.headers["location"] == "/account"
    # The STORED token was refreshed (forced: it had an hour left), not the new one.
    assert [c["refresh_token"] for c in sso_refresh["calls"]] == ["old-refresh"]
    after = env.char(MAIN_ID)
    assert cat.parse_scopes(after.scopes) == set(cat.ALL_SCOPES)
    assert after.declined_scopes == before.declined_scopes
    assert after.refresh_token == "rotated-refresh"       # the stored one, rotated
    assert after.access_token != "new-access"             # the new grant was dropped...
    assert env.calls["revoked"] == []                     # ...and never revoked
    # Live state and sync bookkeeping are untouched.
    cache = _cache(env, MAIN_ID)
    assert cache.wallet == 1e9
    assert json.loads(cache.field_synced_json) == {"wallet": SYNCED_AT}
    assert json.loads(cache.sync_warnings_json) == {"assets": "esi_error: ReadTimeout"}
    assert any(f"CHARACTER:EVE:{MAIN_ID}|" in k for k in _esi_keys(env))
    assert _count(env, CharacterCorpRoles, character_id=MAIN_ID) == 1
    # Audited as its own event, not as a change.
    events = _audit(env, MAIN_ID)
    assert [e for e, _u, _d in events] == ["permissions_kept"]
    assert events[0][1] == USER_ID
    # Logged in as the owner, told plainly, and synced as on any login.
    session = env.session_of(r)
    assert session["user_id"] == USER_ID and session["active_character_id"] == MAIN_ID
    assert session["flash"]["kind"] == "ok"
    assert "Nothing about what Main Pilot shares was changed" in session["flash"]["text"]
    assert f"still shares {len(cat.PERMISSIONS)} permissions" in session["flash"]["text"]
    assert env.calls["synced"] == [MAIN_ID]


def test_narrower_add_by_the_owner_changes_nothing(env, sso_refresh):
    sso_refresh["respond"] = refreshes_with(ALT_ID, [cat.WALLET, cat.ASSETS])
    env.sso_returns(ALT_ID, "Alt Pilot", [cat.WALLET])       # assets left out

    r = env.client(_pending("add", ["wallet"], user_id=USER_ID)).get(CALLBACK)

    assert r.headers["location"] == "/account"
    after = env.char(ALT_ID)
    assert cat.parse_scopes(after.scopes) == {cat.WALLET, cat.ASSETS}
    assert cat.parse_scopes(after.declined_scopes) == {cat.MAIL}   # unchanged
    assert after.refresh_token == "rotated-refresh"
    assert after.user_id == USER_ID
    assert env.calls["revoked"] == []
    assert [e for e, _u, _d in _audit(env, ALT_ID)] == ["permissions_kept"]
    session = env.session_of(r)
    assert session["user_id"] == USER_ID and session["active_character_id"] == ALT_ID
    assert "still shares 2 permissions" in session["flash"]["text"]


def test_the_probe_runs_before_the_callback_writes_anything(env, sso_refresh):
    """A pilot whose owner row is gone: signup recreates the user (INSERT +
    flush, which takes SQLite's write lock). The probe commits in its own
    session, so it must run first — after the flush it would wait on that lock,
    fail, and wrongly fall through to the narrowing path."""
    async def orphan(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.user_id = 699                                 # no such user
        await db.commit()
    env.q(orphan)
    sso_refresh["respond"] = refreshes_with(ALT_ID, [cat.WALLET, cat.ASSETS])
    env.sso_returns(ALT_ID, "Alt Pilot", [])
    r = env.client(_pending("signup", [])).get(CALLBACK)
    assert r.headers["location"] == "/account"
    after = env.char(ALT_ID)
    assert cat.parse_scopes(after.scopes) == {cat.WALLET, cat.ASSETS}
    assert after.refresh_token == "rotated-refresh"
    assert after.user_id not in (699, USER_ID) and after.is_main
    assert env.session_of(r)["user_id"] == after.user_id
    assert [e for e, _u, _d in _audit(env, ALT_ID)] == ["permissions_kept"]


def test_the_probe_holds_the_sync_layers_token_locks(env, sso_refresh):
    """EVE rotates refresh tokens: a scheduler refresh racing the probe would
    leave one side holding a retired token. Both per-character locks the sync
    path takes (dashboard._client_for -> client.refresh_token) must be held."""
    from app.esi import client as client_mod
    from app.routes.dashboard import _get_token_lock
    held = []
    ok = refreshes_with(MAIN_ID, cat.ALL_SCOPES)

    def respond(data):
        held.append((_get_token_lock(MAIN_ID).locked(),
                     client_mod._get_refresh_lock(MAIN_ID).locked()))
        return ok(data)

    sso_refresh["respond"] = respond
    env.sso_returns(MAIN_ID, "Main Pilot", [])
    env.client(_pending("signup", [])).get(CALLBACK)
    assert held == [(True, True)]
    assert not _get_token_lock(MAIN_ID).locked()


# ── The stored authorization is gone or already narrowed: take the new one ─

def _assert_new_grant_taken_with_a_warning(env, r, reason_text, audit_note):
    assert r.status_code == 303 and r.headers["location"] == "/account"
    after = env.char(MAIN_ID)
    assert cat.parse_scopes(after.scopes) == set(SKILLS)
    assert after.refresh_token == "new-refresh"
    assert env.calls["revoked"] == []
    # Measured against what the pilot HAD, so the withdrawn wallet's live
    # value is cleared even when the probe had already narrowed the row.
    assert _cache(env, MAIN_ID).wallet is None
    events = _audit(env, MAIN_ID)
    assert [e for e, _u, _d in events] == ["permissions_changed"]
    assert audit_note in events[0][2]
    session = env.session_of(r)
    assert session["user_id"] == USER_ID
    flash = session["flash"]
    assert flash["kind"] == "warn"
    assert "Main Pilot now shares fewer permissions: 1 instead of" in flash["text"]
    assert reason_text in flash["text"]
    assert "Change permissions" in flash["text"]


def test_narrower_signup_takes_the_new_grant_when_the_stored_token_is_revoked(env, sso_refresh):
    _seed_live_state(env, MAIN_ID)
    sso_refresh["respond"] = rejected                     # 400 -> TokenRevoked
    env.sso_returns(MAIN_ID, "Main Pilot", SKILLS)
    r = env.client(_pending("signup", ["skills"])).get(CALLBACK)
    assert len(sso_refresh["calls"]) == 1
    _assert_new_grant_taken_with_a_warning(env, r, "no longer worked", "stored token no longer refreshed")


def test_narrower_signup_takes_the_new_grant_when_eve_cannot_be_reached(env, sso_refresh):
    """Any refresh failure: the new token is the only one known to work."""
    _seed_live_state(env, MAIN_ID)
    sso_refresh["respond"] = unreachable
    env.sso_returns(MAIN_ID, "Main Pilot", SKILLS)
    r = env.client(_pending("signup", ["skills"])).get(CALLBACK)
    _assert_new_grant_taken_with_a_warning(env, r, "no longer worked", "stored token no longer refreshed")


def test_narrower_signup_takes_the_new_grant_when_eve_already_narrowed_it(env, sso_refresh):
    """If the new authorization replaced the old scope set at EVE, the stored
    token refreshes fine but carries only the new grant."""
    _seed_live_state(env, MAIN_ID)
    sso_refresh["respond"] = refreshes_with(MAIN_ID, SKILLS)
    env.sso_returns(MAIN_ID, "Main Pilot", SKILLS)
    r = env.client(_pending("signup", ["skills"])).get(CALLBACK)
    _assert_new_grant_taken_with_a_warning(env, r, "EVE had already limited",
                                           "stored token already narrowed at EVE")


def test_narrower_add_with_a_dead_stored_token_takes_the_new_grant(env, sso_refresh):
    sso_refresh["respond"] = rejected
    env.sso_returns(ALT_ID, "Alt Pilot", [cat.WALLET])
    r = env.client(_pending("add", ["wallet"], user_id=USER_ID)).get(CALLBACK)
    assert r.headers["location"] == "/account"
    after = env.char(ALT_ID)
    assert cat.parse_scopes(after.scopes) == {cat.WALLET}
    assert after.refresh_token == "new-refresh"
    assert "Alt Pilot now shares fewer permissions: 1 instead of 2" in env.session_of(r)["flash"]["text"]


# ── Everything else: exactly as before, and no extra SSO call ──────────────

@pytest.mark.parametrize("intent,session", [("add", {"user_id": USER_ID}), ("signup", {})])
def test_a_wider_grant_applies_as_before_without_a_probe(env, sso_refresh, intent, session):
    keys = ["wallet", "assets", "skills"]
    env.sso_returns(ALT_ID, "Alt Pilot", cat.scopes_for(keys))
    r = env.client(_pending(intent, keys, **session)).get(CALLBACK)
    assert sso_refresh["calls"] == []
    assert r.headers["location"] == "/dashboard"
    after = env.char(ALT_ID)
    assert cat.parse_scopes(after.scopes) == set(cat.scopes_for(keys))
    assert after.refresh_token == "new-refresh"
    assert [e for e, _u, _d in _audit(env, ALT_ID)] == ["permissions_changed"]
    assert env.session_of(r)["user_id"] == USER_ID


def test_renewing_a_dead_pilot_with_everything_still_works(env, sso_refresh):
    async def dead(db):
        db.add(CharacterDashboardCache(character_id=MAIN_ID,
                                       sync_warnings_json=json.dumps({"wallet": "token_revoked"})))
        await db.commit()
    env.q(dead)
    env.sso_returns(MAIN_ID, "Main Pilot", cat.ALL_SCOPES)
    r = env.client(_pending("add", [p.key for p in cat.PERMISSIONS], user_id=USER_ID)).get(CALLBACK)
    assert sso_refresh["calls"] == []
    assert r.headers["location"] == "/dashboard"
    assert env.char(MAIN_ID).refresh_token == "new-refresh"
    assert _cache(env, MAIN_ID).sync_warnings_json is None


def test_a_scope_nothing_reads_does_not_count_as_narrowing(env, sso_refresh):
    """The stored token still carries a scope older releases asked for. Losing
    it is not narrowing, so an Add with Everything (which also widens) is
    applied — a live stored token must not swallow the widening."""
    async def legacy(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.scopes = cat.join_scopes([cat.WALLET, cat.ASSETS, LEGACY_SCOPE])
        await db.commit()
    env.q(legacy)
    env.sso_returns(ALT_ID, "Alt Pilot", cat.ALL_SCOPES)
    r = env.client(_pending("add", [p.key for p in cat.PERMISSIONS], user_id=USER_ID)).get(CALLBACK)
    assert sso_refresh["calls"] == []
    assert r.headers["location"] == "/dashboard"
    assert cat.parse_scopes(env.char(ALT_ID).scopes) == set(cat.ALL_SCOPES)


def test_update_still_narrows_through_its_own_flow_without_a_probe(env, sso_refresh):
    env.sso_returns(ALT_ID, "Alt Pilot", [cat.ASSETS])
    r = env.client(_pending("update", ["assets"], ALT_ID, user_id=USER_ID)).get(CALLBACK)
    assert sso_refresh["calls"] == []
    assert r.headers["location"] == "/account"
    assert cat.parse_scopes(env.char(ALT_ID).scopes) == {cat.ASSETS}
    assert env.char(ALT_ID).refresh_token == "new-refresh"


def test_login_and_new_characters_never_probe(env, sso_refresh):
    env.sso_returns(ALT_ID, "Alt Pilot", [])
    env.client(_pending("login")).get(CALLBACK)
    env.sso_returns(90000071, "Brand New", [])
    r = env.client(_pending("signup", [])).get(CALLBACK)
    assert sso_refresh["calls"] == []
    assert r.headers["location"] == "/dashboard"
    assert env.char(90000071).scopes == ""
    assert env.char(ALT_ID).refresh_token == "old-refresh"


def test_the_kept_event_is_filed_under_permissions():
    from app.routes.admin import audit_group
    assert audit_group("permissions_kept") == "permissions"
