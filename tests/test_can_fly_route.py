"""Tests for GET /character/{id}/can-fly (T-072).

Coverage:
1. Auth: 401 anonymous (empty body), 404 for another user's character
   (IDOR).
2. Missing esi-skills.read_skills.v1 renders "needs the skills permission"
   — never "0 of N".
3. A skills fetch that fails or returns empty renders an unavailable
   message — never "0 of N" either.
4. Zero saved fits renders "No saved fits", not "0 of 0".
5. The happy path: one flyable fit and one missing-skill fit render in
   their respective sections, with the missing fit's skill name and an
   approx. training time.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
import app.routes.character_detail as cd
from app.db.models import Base, Character, User, UserFitting, get_db

USER_A = 9101
USER_B = 9102
CHAR_A = 96900001   # belongs to USER_A
CHAR_OTHER_USER = 96900002   # belongs to USER_B
_CSRF = "test-csrf-token-canfly-0123456789"

SHIP_TYPE_ID = 99201
MODULE_TYPE_ID = 99202
SKILL_ID = 33101


def _client():
    import app.main as main
    return TestClient(main.app, base_url="https://testserver")


def _authed_client(user_id):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = _client()
    client.cookies.set("vigilant_session", cookie)
    return client


def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


_FULL_SCOPES = "esi-skills.read_skills.v1"


def _seeded_db(*, char_scopes: str = _FULL_SCOPES, n_fits: int = 0):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A))
            db.add(User(id=USER_B))
            db.add(Character(
                character_id=CHAR_A, character_name="Pilot One", user_id=USER_A,
                access_token="x", refresh_token="x",
                token_expiry=datetime(2099, 1, 1), scopes=char_scopes,
            ))
            db.add(Character(
                character_id=CHAR_OTHER_USER, character_name="Pilot Two", user_id=USER_B,
                access_token="x", refresh_token="x",
                token_expiry=datetime(2099, 1, 1), scopes=char_scopes,
            ))
            db.add(sm.SDEMeta(key="last_updated", value="2026-01-01T00:00:00+00:00"))
            db.add(sm.SDEType(type_id=SHIP_TYPE_ID, type_name="Test Frigate"))
            db.add(sm.SDEType(type_id=SKILL_ID, type_name="Test Gunnery Skill"))
            db.add(sm.SDETypeSkillReq(type_id=MODULE_TYPE_ID, skill_type_id=SKILL_ID, required_level=5))
            for i in range(n_fits):
                db.add(UserFitting(
                    user_id=USER_A, folder_id=None, name=f"Test Fit {i}",
                    ship_type_id=SHIP_TYPE_ID,
                    items_json=json.dumps([{"type_id": MODULE_TYPE_ID, "slot": "high", "quantity": 1}]),
                    implants_json="{}", boosters_json="{}",
                ))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)

    return teardown, SessionLocal


# ── 1. Auth ──────────────────────────────────────────────────────────────────

def test_anonymous_gets_401_with_empty_body():
    teardown, _ = _seeded_db()
    try:
        r = _client().get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 401
        assert r.text == ""
    finally:
        teardown()


def test_another_users_character_is_404_not_redirect():
    teardown, _ = _seeded_db()
    try:
        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_OTHER_USER}/can-fly")
        assert r.status_code == 404
    finally:
        teardown()


# ── 2. Missing scope ─────────────────────────────────────────────────────────

def test_missing_skills_scope_says_needs_the_permission_never_zero_of_n():
    teardown, _ = _seeded_db(char_scopes="")
    try:
        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 200
        assert "needs the skills permission" in r.text
        assert "0 of" not in r.text
    finally:
        teardown()


# ── 3. Skills unavailable ────────────────────────────────────────────────────

def test_skills_fetch_exception_is_unavailable_not_zero_of_n(monkeypatch):
    teardown, _ = _seeded_db(n_fits=2)
    try:
        async def _boom(db, char):
            raise RuntimeError("token revoked")
        monkeypatch.setattr(cd.fitting_mod, "_character_skills_map", _boom)

        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 200
        assert "Could not load" in r.text
        assert "0 of" not in r.text
    finally:
        teardown()


def test_skills_fetch_returning_empty_is_unavailable_not_zero_of_n(monkeypatch):
    teardown, _ = _seeded_db(n_fits=2)
    try:
        async def _empty(db, char):
            return {}
        monkeypatch.setattr(cd.fitting_mod, "_character_skills_map", _empty)

        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 200
        assert "Could not load" in r.text
        assert "0 of" not in r.text
    finally:
        teardown()


# ── 4. Zero saved fits ───────────────────────────────────────────────────────

def test_zero_saved_fits_says_no_saved_fits(monkeypatch):
    teardown, _ = _seeded_db(n_fits=0)
    try:
        async def _levels(db, char):
            return {SKILL_ID: 5}
        monkeypatch.setattr(cd.fitting_mod, "_character_skills_map", _levels)

        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 200
        assert "No saved fits" in r.text
        assert "0 of 0" not in r.text
    finally:
        teardown()


# ── 5. Happy path ────────────────────────────────────────────────────────────

def test_can_fly_and_missing_sections_render(monkeypatch):
    teardown, SessionLocal = _seeded_db(n_fits=1)
    try:
        # Add a second fit the character CAN fly (no items -> no requirements).
        async def _add_flyable(db):
            db.add(UserFitting(
                user_id=USER_A, folder_id=None, name="Flyable Fit",
                ship_type_id=SHIP_TYPE_ID, items_json="[]",
                implants_json="{}", boosters_json="{}",
            ))
            await db.commit()

        async def _wrapped():
            async with SessionLocal() as db:
                await _add_flyable(db)
        _run_async(_wrapped)

        async def _levels(db, char):
            return {SKILL_ID: 0}   # untrained -> "Test Fit 0" is missing SKILL_ID
        monkeypatch.setattr(cd.fitting_mod, "_character_skills_map", _levels)

        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/can-fly")
        assert r.status_code == 200
        html = r.text
        assert "Flies 1 of 2 saved fits" in html
        assert "Can fly now" in html
        assert "Missing skills" in html
        assert "Test Gunnery Skill" in html
        assert "approx." in html
        assert "/tools/fitting?load=" in html
        assert "<script" not in html
    finally:
        teardown()
