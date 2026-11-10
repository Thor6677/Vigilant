"""Free-text values and ids that went straight to ESI are checked first (ISS-178).

Each failed ESI lookup spends the error budget the whole instance shares, so
a value the UI never sends should not reach ESI:

  * gate check `flag`: only shortest / secure / insecure (the form's three
    options) reach ESI's /route/; anything else falls back to shortest.
  * `/intel/entity/{kind}/{id}`: an id outside ESI's int32 range 404s, as an
    unknown kind does, with no name lookup.
  * structure-timer ACL search `category`: only the picker's three types; any
    other value gets the short-query answer with no ESI call.

ESI is stubbed and records each call; the network is never reached. Ids and
names are invented.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.esi.client as esi_client
import app.routes.gatecheck as gatecheck
import app.routes.intel_entity as intel_entity
from app.auth import scopes as perms
from app.db.models import Base, Character, User, get_db
from tests.test_compression_hub_pricing import _CSRF, _client

USER = 9179
CHAR = 96917901


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── gate check flag ───────────────────────────────────────────────────

@pytest.fixture
def route_calls(monkeypatch):
    """Every /route/ request the gate check makes: (path, params)."""
    calls = []

    async def known(db, name):
        return {"Sample Origin": 30000142, "Sample Dest": 30002187}.get(name)

    class _ESI:
        def __init__(self, *args, **kwargs):
            pass

        async def get_public(self, path, params=None, bypass_cache=False):
            calls.append((path, dict(params or {})))
            return []   # "no route": the handler stops before any kill lookups

    monkeypatch.setattr(gatecheck.sde, "system_name_to_id", known)
    monkeypatch.setattr(gatecheck, "ESIClient", _ESI)
    return calls


def _check(**form):
    data = {"origin": "Sample Origin", "destination": "Sample Dest", **form}
    r = _client().post("/intel/gatecheck/check", data=data, headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    return r


@pytest.mark.parametrize("flag", ["fastest", "", "SECURE", "secure&avoid=30000142",
                                  "x" * 500])
def test_gatecheck_unknown_flag_never_reaches_esi(route_calls, flag):
    _check(flag=flag)
    assert route_calls == [("/route/30000142/30002187/", {"flag": "shortest"})]


@pytest.mark.parametrize("flag", ["shortest", "secure", "insecure"])
def test_gatecheck_form_flags_pass_through(route_calls, flag):
    _check(flag=flag)
    assert route_calls == [("/route/30000142/30002187/", {"flag": flag})]


def test_gatecheck_missing_flag_is_shortest(route_calls):
    _check()
    assert route_calls == [("/route/30000142/30002187/", {"flag": "shortest"})]


# ── /intel/entity/{kind}/{id} ─────────────────────────────────────────

@pytest.fixture
def name_lookups(monkeypatch):
    calls = []

    async def fake_resolve(ids):
        calls.append(list(ids))
        return {i: "Sample Pilot" for i in ids}

    monkeypatch.setattr(intel_entity, "resolve_entity_names", fake_resolve)
    return calls


@pytest.mark.parametrize("entity_id", [-1, 0, 2 ** 31, 2 ** 40, 2 ** 63 + 5])
def test_entity_page_out_of_range_id_is_404_without_a_lookup(name_lookups, entity_id):
    r = _client().get(f"/intel/entity/character/{entity_id}", follow_redirects=False)
    assert r.status_code == 404
    assert name_lookups == []


@pytest.mark.parametrize("entity_id", [1, 1000125, 99000001, 2112000001, 2 ** 31 - 1])
def test_entity_page_in_range_id_still_resolves(name_lookups, entity_id):
    r = _client().get(f"/intel/entity/corporation/{entity_id}", follow_redirects=False)
    assert r.status_code == 200
    assert "Sample Pilot" in r.text
    assert name_lookups == [[entity_id]]


# ── structure-timer ACL search category ──────────────────────────────

@pytest.fixture
def acl_env(monkeypatch):
    """A temp DB behind get_db with one user, an authed client, and recorded
    ESI calls. `acl_env.scoped(True)` gives the user a search-scoped pilot."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER))
            await db.commit()
    _run(seed())

    async def add_scoped_pilot():
        async with SessionLocal() as db:
            db.add(Character(character_id=CHAR, character_name="Sample Pilot", user_id=USER,
                             access_token="x", refresh_token="x",
                             token_expiry=datetime(2099, 1, 1),
                             scopes=perms.STRUCTURE_SEARCH))
            await db.commit()

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    calls = []

    class _Authed:
        async def get(self, path, params=None, bypass_cache=False):
            calls.append(("search", path, dict(params or {})))
            return {params["categories"]: [98000001]}

    async def fake_get_client(char, db):
        return _Authed()

    async def fake_post_public(self, path, body):
        calls.append(("public", path, list(body)))
        if path == "/universe/names/":
            return [{"id": i, "name": "Sample Holding", "category": "corporation"} for i in body]
        return {"corporations": [{"id": 98000001, "name": "Sample Holding"}]}

    monkeypatch.setattr(esi_client, "get_client", fake_get_client)
    monkeypatch.setattr(esi_client.ESIClient, "post_public", fake_post_public)

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(json.dumps(
        {"user_id": USER, "csrf_token": _CSRF}).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)

    class Env:
        pass
    env = Env()
    env.client, env.calls = client, calls
    env.scoped = lambda: _run(add_scoped_pilot())
    yield env

    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


def _acl_search(env, category):
    r = env.client.get("/structure-timers/acl/search",
                       params={"q": "Sample", "category": category})
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("category", ["agent", "solar_system", "corporation,alliance",
                                      "Corporation", ""])
def test_acl_search_unknown_category_makes_no_esi_call(acl_env, scoped, category):
    if scoped:
        acl_env.scoped()
    assert _acl_search(acl_env, category) == ""
    assert acl_env.calls == []


@pytest.mark.parametrize("category", ["corporation", "alliance", "character"])
def test_acl_search_picker_categories_still_search(acl_env, category):
    acl_env.scoped()
    html = _acl_search(acl_env, category)
    assert "Sample Holding" in html
    assert acl_env.calls[0] == ("search", f"/characters/{CHAR}/search/",
                                {"categories": category, "search": "Sample", "strict": "false"})


def test_acl_search_without_a_scoped_pilot_still_falls_back(acl_env):
    html = _acl_search(acl_env, "corporation")
    assert "Sample Holding" in html
    assert acl_env.calls == [("public", "/universe/ids/", ["Sample"])]
