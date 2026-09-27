"""POST /dashboard/prefs (T-070): 401 anonymously, persists a save, another
user's prefs are untouched, oversized bodies are rejected.

Mirrors the cookie/session-building pattern in
tests/test_admin_remove_user_cleanup.py.
"""
import base64
import json
import tempfile

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db.models import Base, User, get_db

CSRF = "test-csrf-token"
USER_A = 701
USER_B = 702


def _cookie(secret_key: str, user_id: int | None) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    payload = {"csrf_token": CSRF}
    if user_id is not None:
        payload["user_id"] = user_id
    data = base64.b64encode(json.dumps(payload).encode())
    return signer.sign(data).decode()


@pytest.fixture
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A, role="user"))
            db.add(User(id=USER_B, role="user"))
            await db.commit()

    import asyncio
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    yield SessionLocal
    main.app.dependency_overrides.pop(get_db, None)


def _as(session_local, user_id):
    """A fresh TestClient per identity — reusing one client and swapping its
    session cookie mid-test hits an httpx CookieConflict, because Starlette's
    SessionMiddleware re-issues Set-Cookie with attributes (domain/path) that
    don't dedupe against a manually-`.set()` cookie of the same name."""
    c = TestClient(main.app, base_url="https://testserver")
    c.SessionLocal = session_local
    # Always carry a signed session with the CSRF token, even when logged
    # out — otherwise CSRF (403) fires before the route's own 401 check ever
    # runs, and the test would be proving the wrong gate.
    c.cookies.set("vigilant_session", _cookie(main.settings.secret_key, user_id))
    c.headers.update({"X-CSRF-Token": CSRF})
    return c


def test_401_json_when_logged_out(client):
    c = _as(client, None)
    r = c.post("/dashboard/prefs", json={"mode": "compact"})
    assert r.status_code == 401
    assert r.json()  # a JSON body, not an empty page


def test_saving_persists_and_returns_full_prefs(client):
    c = _as(client, USER_A)
    r = c.post("/dashboard/prefs", json={"mode": "compact", "hidden_sections": ["activity"]})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "compact"
    assert body["hidden_sections"] == ["activity"]

    async def _check():
        async with client() as db:
            row = (await db.execute(
                text("SELECT prefs_json FROM user_dashboard_prefs WHERE user_id = :uid"),
                {"uid": USER_A})).first()
            return json.loads(row[0])
    import asyncio
    stored = asyncio.run(_check())
    assert stored["mode"] == "compact"


def test_another_users_prefs_are_untouched(client):
    _as(client, USER_A).post("/dashboard/prefs", json={"mode": "compact"})

    r = _as(client, USER_B).post("/dashboard/prefs", json={"mode": "table"})
    assert r.json()["mode"] == "table"

    async def _check():
        async with client() as db:
            rows = (await db.execute(
                text("SELECT user_id, prefs_json FROM user_dashboard_prefs ORDER BY user_id"))).fetchall()
            return {uid: json.loads(pj)["mode"] for uid, pj in rows}
    import asyncio
    modes = asyncio.run(_check())
    assert modes[USER_A] == "compact"
    assert modes[USER_B] == "table"


def test_oversized_body_is_rejected(client):
    c = _as(client, USER_A)
    huge_patch = {"tag_filter": ["x" * 24] * 2000}  # well over 8KB serialized
    r = c.post("/dashboard/prefs", content=json.dumps(huge_patch),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 413
