"""T-076 Part A item 4: account-group order moves from the session into
prefs. `/dashboard/group-order` now requires a session and persists to
`UserDashboardPrefs.group_order`; a legacy session value (from before this
shipped) is adopted into prefs exactly once on the next `/dashboard` load,
then dropped from the session.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.auth import scopes as perms
from app.dashboard.prefs import load_prefs
from app.db.models import AsyncSessionLocal, Base, Character, CharacterDashboardCache, User, get_db

CSRF = "test-csrf-token"
USER_ID = 831
CHAR_A = 90301001
CHAR_B = 90301002


def _cookie(secret_key: str, user_id: int | None, extra: dict | None = None) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    payload = {"csrf_token": CSRF}
    if user_id is not None:
        payload["user_id"] = user_id
    if extra:
        payload.update(extra)
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
            db.add(User(id=USER_ID, role="user"))
            for cid, group in ((CHAR_A, "Alpha Corp"), (CHAR_B, "Bravo Corp")):
                db.add(Character(
                    character_id=cid, character_name=f"Pilot {cid}", user_id=USER_ID,
                    is_main=(cid == CHAR_A), account_group=group, sort_order=0,
                    scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                    access_token="x", refresh_token="x",
                    token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
                ))
                db.add(CharacterDashboardCache(character_id=cid, sync_status="idle"))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    global _SessionLocalForTest
    _SessionLocalForTest = SessionLocal
    c = TestClient(main.app, base_url="https://testserver")
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def _load_prefs_sync(session_factory):
    async def _run():
        async with session_factory() as db:
            return await load_prefs(db, USER_ID)
    return asyncio.run(_run())


def test_group_order_post_requires_a_session(client):
    # A session cookie with a valid CSRF token but no user_id — isolates the
    # route's own auth gate from the (separate) CSRF gate.
    client.cookies.set("vigilant_session", _cookie(main.settings.secret_key, None))
    client.headers.update({"X-CSRF-Token": CSRF})
    r = client.post("/dashboard/group-order", json=["Bravo Corp", "Alpha Corp"])
    assert r.status_code == 401


def test_group_order_post_persists_to_prefs(client):
    client.cookies.set("vigilant_session", _cookie(main.settings.secret_key, USER_ID))
    client.headers.update({"X-CSRF-Token": CSRF})
    r = client.post("/dashboard/group-order", json=["Bravo Corp", "Alpha Corp"])
    assert r.status_code == 200
    prefs = _load_prefs_sync(_SessionLocalForTest)
    assert prefs["group_order"] == ["Bravo Corp", "Alpha Corp"]


def test_a_legacy_session_group_order_migrates_into_prefs_once(client):
    """A session carrying the OLD (pre-T-076) group_order key, with nothing
    yet saved in prefs, gets adopted on the next /dashboard load — and the
    session key is dropped so it can't re-migrate over a later real save."""
    client.cookies.set(
        "vigilant_session",
        _cookie(main.settings.secret_key, USER_ID, extra={"group_order": ["Bravo Corp", "Alpha Corp"]}),
    )
    client.headers.update({"X-CSRF-Token": CSRF})
    r = client.get("/dashboard")
    assert r.status_code == 200
    prefs = _load_prefs_sync(_SessionLocalForTest)
    assert prefs["group_order"] == ["Bravo Corp", "Alpha Corp"]


def test_a_stale_session_value_never_overwrites_a_real_saved_order(client):
    """Once prefs hold a real (non-empty) group_order — from an actual
    reorder — a stale/replayed session cookie carrying an old value must
    never clobber it. This is the guard's important case: the [] vs
    "never set" ambiguity only matters before the first real save."""
    client.cookies.set("vigilant_session", _cookie(main.settings.secret_key, USER_ID))
    client.headers.update({"X-CSRF-Token": CSRF})
    client.post("/dashboard/group-order", json=["Alpha Corp", "Bravo Corp"])

    # A different/stale tab whose session cookie still carries the old
    # (pre-T-076) session-only value.
    client.cookies.set(
        "vigilant_session",
        _cookie(main.settings.secret_key, USER_ID, extra={"group_order": ["Should Not Apply"]}),
    )
    client.get("/dashboard")
    prefs = _load_prefs_sync(_SessionLocalForTest)
    assert prefs["group_order"] == ["Alpha Corp", "Bravo Corp"]
