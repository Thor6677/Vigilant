"""Choosing, changing and withdrawing ESI permissions (T-063, T-064, T-065).

SSO itself is stubbed at the two seams the callback uses — the code exchange
and the public-metadata lookup — so these tests drive the real routes, session
handling and database writes, and assert what the promise is about: what gets
requested, what gets stored, what is revoked, and what is deleted.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import scopes as cat
from app.auth import status as perm_status
from app.db.cache import ESICache
from app.db.models import (
    AdminAuditLog, Base, Character, CharacterCorpRoles, CharacterDashboardCache,
    User, WalletSnapshot, get_db,
)

USER_ID = 601
OTHER_ID = 602
MAIN_ID = 90000001
ALT_ID = 90000002
STRANGER_ID = 90000009
CSRF = "test-csrf-token"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _char(cid, user_id, name, scopes, declined="", main=False, refresh="old-refresh"):
    return Character(
        character_id=cid, character_name=name, user_id=user_id, is_main=main,
        access_token="old-access", refresh_token=refresh,
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=cat.join_scopes(scopes), declined_scopes=cat.join_scopes(declined),
    )


@pytest.fixture
def env(monkeypatch):
    import app.main as main
    import app.auth.routes as auth_routes

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID, role="user"))
            db.add(User(id=OTHER_ID, role="user"))
            db.add(_char(MAIN_ID, USER_ID, "Main Pilot", cat.ALL_SCOPES, main=True))
            db.add(_char(ALT_ID, USER_ID, "Alt Pilot", [cat.WALLET, cat.ASSETS], declined=[cat.MAIL]))
            db.add(_char(STRANGER_ID, OTHER_ID, "Someone Else", cat.ALL_SCOPES, main=True))
            await db.commit()

    _run(seed())

    async def _override():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = _override

    calls = {"revoked": [], "refreshed": [], "synced": []}

    async def fake_revoke(token):
        calls["revoked"].append(token)
        return True

    async def fake_refresh(char, db):
        calls["refreshed"].append(char.character_id)
        return char.access_token

    async def fake_meta(access_token, character_id):
        return {"corporation_id": 98000001, "alliance_id": None, "security_status": 1.0,
                "birthday": None, "corporation_name": "Test Corp", "alliance_name": None}

    monkeypatch.setattr(auth_routes, "revoke_refresh_token", fake_revoke)
    monkeypatch.setattr(auth_routes, "_do_refresh", fake_refresh)
    monkeypatch.setattr(auth_routes, "_public_metadata", fake_meta)
    monkeypatch.setattr(auth_routes, "_queue_sync", lambda cid: calls["synced"].append(cid))

    def sso_returns(character_id, name, scopes, refresh="new-refresh"):
        async def fake_exchange(code):
            return ({"access_token": "new-access", "refresh_token": refresh, "expires_in": 1200},
                    {"CharacterID": character_id, "CharacterName": name,
                     "Scopes": " ".join(scopes)})
        monkeypatch.setattr(auth_routes, "_exchange_code", fake_exchange)

    def client(session: dict | None = None):
        c = TestClient(main.app, base_url="https://testserver", raise_server_exceptions=False,
                       follow_redirects=False)
        if session is not None:
            signer = itsdangerous.TimestampSigner(main.settings.secret_key)
            data = base64.b64encode(json.dumps({"csrf_token": CSRF, **session}).encode())
            c.cookies.set("vigilant_session", signer.sign(data).decode())
            c.headers.update({"X-CSRF-Token": CSRF})
        return c

    def session_of(response_or_client) -> dict:
        cookie = response_or_client.cookies.get("vigilant_session")
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        return json.loads(base64.b64decode(signer.unsign(cookie)))

    def q(fn):
        async def go():
            async with SessionLocal() as db:
                return await fn(db)
        return _run(go())

    def char(cid):
        return q(lambda db: _scalar(db, select(Character).where(Character.character_id == cid)))

    class Env:
        pass

    e = Env()
    e.client, e.session_of, e.q, e.char, e.calls, e.sso_returns = client, session_of, q, char, calls, sso_returns
    e.user = lambda: client({"user_id": USER_ID})
    yield e
    main.app.dependency_overrides.pop(get_db, None)


async def _scalar(db, stmt):
    return (await db.execute(stmt)).scalar_one_or_none()


def _pending(intent, keys=(), character_id=None, purge=False, **extra):
    return {"oauth_state": "S", "oauth_pending": {"intent": intent, "keys": list(keys),
            "character_id": character_id, "purge": purge}, **extra}


# ── What gets requested ─────────────────────────────────────────────────────

def test_login_asks_for_no_scopes(env):
    r = env.client().get("/auth/login")
    assert r.status_code == 303
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "scope" not in qs
    assert qs["client_id"]


def test_picker_for_a_new_visitor_offers_every_permission_everything_ticked(env):
    r = env.client().get("/auth/connect")
    assert r.status_code == 200
    for p in cat.PERMISSIONS:
        assert f'value="{p.key}" checked' in r.text, p.key
    assert 'name="intent" value="signup"' in r.text
    assert 'name="csrf_token"' in r.text


def test_picker_link_can_preselect_one_permission(env):
    r = env.client().get("/auth/connect?p=wallet")
    assert 'value="wallet" checked' in r.text
    assert 'value="assets" checked' not in r.text


def test_authorize_requests_exactly_the_ticked_scopes(env):
    c = env.client({})
    r = c.post("/auth/authorize", data={"intent": "signup", "p": ["wallet", "corp_wallets"],
                                        "csrf_token": CSRF})
    assert r.status_code == 303
    requested = set(parse_qs(urlparse(r.headers["location"]).query)["scope"][0].split())
    # corp_roles comes along with any corporation permission, by rule.
    assert requested == {cat.WALLET, cat.CORP_WALLETS, cat.CORP_ROLES}


def test_authorize_is_csrf_protected(env):
    c = TestClient(__import__("app.main", fromlist=["app"]).app, base_url="https://testserver",
                   follow_redirects=False)
    c.get("/auth/connect")  # a session, but no token sent back
    r = c.post("/auth/authorize", data={"intent": "signup", "p": ["wallet"]})
    assert r.status_code == 403


def test_cannot_start_an_update_for_someone_elses_character(env):
    r = env.user().post("/auth/authorize", data={"intent": "update", "character_id": STRANGER_ID,
                                                 "p": ["wallet"], "csrf_token": CSRF})
    assert r.status_code == 303 and r.headers["location"] == "/account"


# ── Identity-only login ─────────────────────────────────────────────────────

def test_login_never_changes_what_an_existing_character_shares(env):
    env.sso_returns(ALT_ID, "Alt Pilot", [])  # a scopeless login token
    before = env.char(ALT_ID)
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    after = env.char(ALT_ID)
    assert after.scopes == before.scopes
    assert after.refresh_token == "old-refresh"
    assert after.declined_scopes == before.declined_scopes
    assert after.corporation_name == "Test Corp"      # metadata still refreshes
    assert env.calls["revoked"] == []                  # nothing revoked on login
    assert env.session_of(r)["user_id"] == USER_ID


def test_login_with_an_unknown_character_goes_to_the_picker(env):
    env.sso_returns(90000077, "Brand New", [])
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    assert r.status_code == 303
    assert r.headers["location"] == "/account/permissions/90000077?welcome=1"
    new = env.char(90000077)
    assert new.scopes == "" and new.is_main


def test_state_mismatch_is_rejected(env):
    env.sso_returns(ALT_ID, "Alt Pilot", [])
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=WRONG")
    assert r.status_code == 400


# ── Signing up / adding with a selection ────────────────────────────────────

def test_signup_stores_what_was_granted_and_what_was_declined(env):
    keys = ["wallet", "skills"]
    requested = cat.scopes_for(keys)
    env.sso_returns(90000055, "Newcomer", requested)
    r = env.client(_pending("signup", keys)).get("/auth/callback?code=x&state=S")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    c = env.char(90000055)
    assert cat.parse_scopes(c.scopes) == set(requested)
    assert cat.parse_scopes(c.declined_scopes) == set(cat.ALL_SCOPES) - set(requested)
    assert env.calls["synced"] == [90000055]


def test_adding_a_character_owned_by_another_account_is_refused(env):
    env.sso_returns(STRANGER_ID, "Someone Else", [cat.WALLET])
    r = env.client(_pending("add", ["wallet"], user_id=USER_ID)).get("/auth/callback?code=x&state=S")
    assert r.headers["location"] == "/dashboard?error=character_claimed"
    assert env.char(STRANGER_ID).user_id == OTHER_ID


def test_scopes_eve_did_not_grant_are_neither_granted_nor_declined(env):
    env.sso_returns(90000056, "Partial", [cat.WALLET])   # asked for wallet + skills
    env.client(_pending("signup", ["wallet", "skills"])).get("/auth/callback?code=x&state=S")
    c = env.char(90000056)
    state = cat.state_of(cat.BY_KEY["skills"], c.scopes, c.declined_scopes)
    assert state == cat.NEW   # offered again later, never treated as a refusal


# ── Changing permissions ────────────────────────────────────────────────────

def _seed_alt_data(env):
    async def go(db):
        db.add(WalletSnapshot(character_id=ALT_ID, balance=1e9, recorded_at=datetime(2026, 9, 1)))
        db.add(CharacterDashboardCache(character_id=ALT_ID, wallet=1e9))
        db.add(ESICache(key=f"abc:@CHARACTER:EVE:{ALT_ID}|/characters/{ALT_ID}/wallet/",
                        data="1", expires_at=datetime(2030, 1, 1)))
        db.add(ESICache(key=f"def:@CHARACTER:EVE:{MAIN_ID}|/characters/{MAIN_ID}/wallet/",
                        data="1", expires_at=datetime(2030, 1, 1)))
        await db.commit()
    env.q(go)


def _count(env, model, **where):
    async def go(db):
        stmt = select(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return len((await db.execute(stmt)).scalars().all())
    return env.q(go)


def test_narrowing_revokes_the_old_token_and_clears_the_esi_cache(env):
    _seed_alt_data(env)
    env.sso_returns(ALT_ID, "Alt Pilot", [cat.ASSETS])   # wallet withdrawn
    r = env.client(_pending("update", ["assets"], ALT_ID, user_id=USER_ID)).get(
        "/auth/callback?code=x&state=S")
    assert r.headers["location"] == "/account"
    c = env.char(ALT_ID)
    assert cat.parse_scopes(c.scopes) == {cat.ASSETS}
    assert c.refresh_token == "new-refresh"
    assert env.calls["revoked"] == ["old-refresh"]
    assert env.calls["refreshed"] == [ALT_ID]           # the new token was proven
    # No purge requested: history stays, only the raw ESI cache goes.
    assert _count(env, WalletSnapshot, character_id=ALT_ID) == 1
    keys = env.q(lambda db: _all_keys(db))
    assert not any(f"CHARACTER:EVE:{ALT_ID}|" in k for k in keys)
    assert any(f"CHARACTER:EVE:{MAIN_ID}|" in k for k in keys)   # other characters untouched


async def _all_keys(db):
    return [r.key for r in (await db.execute(select(ESICache))).scalars().all()]


def test_narrowing_with_purge_deletes_what_was_collected(env):
    _seed_alt_data(env)
    env.sso_returns(ALT_ID, "Alt Pilot", [cat.ASSETS])
    env.client(_pending("update", ["assets"], ALT_ID, purge=True, user_id=USER_ID)).get(
        "/auth/callback?code=x&state=S")
    assert _count(env, WalletSnapshot, character_id=ALT_ID) == 0
    cache = env.q(lambda db: _scalar(db, select(CharacterDashboardCache).where(
        CharacterDashboardCache.character_id == ALT_ID)))
    assert cache.wallet is None
    events = [e for (e,) in env.q(lambda db: _events(db))]
    assert "permissions_changed" in events and "permissions_purged" in events


async def _events(db):
    return (await db.execute(select(AdminAuditLog.event_type))).all()


def test_update_with_the_wrong_character_changes_nothing(env):
    env.sso_returns(MAIN_ID, "Main Pilot", [cat.WALLET])   # picked the main by mistake
    r = env.client(_pending("update", ["wallet"], ALT_ID, user_id=USER_ID)).get(
        "/auth/callback?code=x&state=S")
    assert r.headers["location"] == "/account"
    assert set(cat.parse_scopes(env.char(MAIN_ID).scopes)) == set(cat.ALL_SCOPES)
    assert env.char(ALT_ID).refresh_token == "old-refresh"
    assert "Nothing was changed" in env.session_of(r)["flash"]["text"]


def test_removing_a_character_revokes_its_token(env):
    r = env.user().post(f"/auth/remove/{ALT_ID}", data={"csrf_token": CSRF})
    assert r.status_code == 303
    assert env.char(ALT_ID) is None
    assert env.calls["revoked"] == ["old-refresh"]


# ── Account page ────────────────────────────────────────────────────────────

def test_account_page_shows_each_characters_state(env):
    r = env.user().get("/account")
    assert r.status_code == 200
    assert "Main Pilot" in r.text and "Alt Pilot" in r.text
    assert "Someone Else" not in r.text
    assert 'acct-perm is-declined' in r.text      # the alt declined mail
    assert 'acct-perm is-new' in r.text           # the alt was never asked for skills


def test_change_page_preticks_current_plus_requested(env):
    r = env.user().get(f"/account/permissions/{ALT_ID}?add=mail")
    assert r.status_code == 200
    for key in ("wallet", "assets", "mail"):
        assert f'value="{key}" checked' in r.text, key
    assert 'value="skills" checked' not in r.text
    assert 'name="intent" value="update"' in r.text
    assert 'id="perm-purge"' in r.text


def test_change_page_refuses_someone_elses_character(env):
    r = env.user().get(f"/account/permissions/{STRANGER_ID}")
    assert r.status_code == 303 and r.headers["location"] == "/account"


def test_notifications_partial_checks_ownership(env):
    """Used to return any character's cached notifications to any session."""
    r = env.user().get(f"/character/{STRANGER_ID}/notifications-partial")
    assert r.status_code == 404


# ── Notices: permission vs in-game role ─────────────────────────────────────

def _render_notice(characters, key, roles=None):
    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    env.globals.update(perms=cat, perm_status=perm_status)
    tmpl = env.from_string(
        '{% from "partials/_permission_notice.html" import permission_notice %}'
        '{{ permission_notice(chars, key, roles) }}')
    return tmpl.render(chars=characters, key=key, roles=roles)


def _d(cid, name, scopes, declined=()):
    return {"character_id": cid, "character_name": name,
            "scopes": cat.join_scopes(scopes), "declined_scopes": cat.join_scopes(declined)}


def test_notice_is_silent_when_everyone_is_covered():
    assert "perm-notice" not in _render_notice([_d(1, "A", [cat.WALLET])], "wallet")


def test_notice_separates_declined_from_new():
    html = _render_notice([_d(1, "Decliner", [], [cat.WALLET]), _d(2, "Oldtimer", [])], "wallet")
    assert "You chose not to share" in html and "Decliner" in html
    assert "newer than" in html and "Oldtimer" in html


def test_notice_names_the_missing_in_game_role():
    c = _d(1, "Clerk", [cat.CORP_WALLETS, cat.CORP_ROLES])
    html = _render_notice([c], "corp_wallets", roles={1: {"Trader"}})
    assert "in-game role" in html
    assert "Accountant or Junior Accountant" in html
    assert "You chose not to share" not in html


def test_notice_is_silent_when_the_role_is_held():
    c = _d(1, "Bookkeeper", [cat.CORP_WALLETS, cat.CORP_ROLES])
    assert "perm-notice" not in _render_notice([c], "corp_wallets", roles={1: {"Junior_Accountant"}})


def test_notice_says_roles_are_unknown_without_the_roles_permission():
    c = _d(1, "Private", [cat.CORP_WALLETS])
    html = _render_notice([c], "corp_wallets", roles={})
    assert "can't check" in html and "Your corporation roles" in html
