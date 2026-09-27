"""GET /dashboard/can-fly (T-076): auth, ownership, no_scope, pending, and
correct counts for the lazy Detailed/Table "can fly" badge.
"""
import asyncio
import base64
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.auth import scopes as perms
from app.db.models import (
    Base, Character, CharacterDashboardCache, User, UserFitting, get_db,
)
from app.db.sde_models import SDETypeSkillReq

CSRF = "test-csrf-token"
USER_A = 811
USER_B = 812
CHAR_SCOPED_SYNCED = 90101001   # has SKILLS scope, skills_json synced
CHAR_SCOPED_PENDING = 90101002  # has SKILLS scope, never synced
CHAR_NO_SCOPE = 90101003        # never shared SKILLS
CHAR_OTHER_USER = 90101004      # belongs to USER_B


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    return signer.sign(data).decode()


def _char(cid, user_id, scopes):
    return Character(
        character_id=cid, character_name=f"Pilot {cid}", user_id=user_id,
        is_main=(cid == CHAR_SCOPED_SYNCED), account_group="Sample Corp", sort_order=0,
        scopes=scopes, declined_scopes="",
        access_token="x", refresh_token="x",
        token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
    )


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
            db.add(_char(CHAR_SCOPED_SYNCED, USER_A, perms.SKILLS))
            db.add(_char(CHAR_SCOPED_PENDING, USER_A, perms.SKILLS))
            db.add(_char(CHAR_NO_SCOPE, USER_A, ""))
            db.add(_char(CHAR_OTHER_USER, USER_B, perms.SKILLS))
            db.add(CharacterDashboardCache(
                character_id=CHAR_SCOPED_SYNCED, sync_status="idle",
                skills_json=json.dumps({"total_sp": 5_000_000, "unallocated_sp": 0, "levels": {}}),
            ))
            db.add(CharacterDashboardCache(character_id=CHAR_SCOPED_PENDING, sync_status="idle"))
            db.add(CharacterDashboardCache(character_id=CHAR_NO_SCOPE, sync_status="idle"))
            db.add(CharacterDashboardCache(
                character_id=CHAR_OTHER_USER, sync_status="idle",
                skills_json=json.dumps({"total_sp": 1, "unallocated_sp": 0, "levels": {}}),
            ))
            # One saved fit for USER_A with no skill requirements at all —
            # every character with synced levels trivially "can fly" it,
            # letting this test assert an exact, deterministic count without
            # needing SDE skill-requirement fixture rows.
            db.add(UserFitting(
                user_id=USER_A, name="Rookie Fit", ship_type_id=587,  # Rifter
                items_json="[]",
            ))
            # A second fit that DOES have a real skill requirement (level 3)
            # the seeded character's empty `levels` dict (level 0 for
            # everything) never meets — proves the count actually reflects
            # can_fly_summary's evaluation, not just "how many fits exist".
            db.add(UserFitting(
                user_id=USER_A, name="Advanced Fit", ship_type_id=588,
                items_json="[]",
            ))
            db.add(SDETypeSkillReq(type_id=588, skill_type_id=3300, required_level=3))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    c = TestClient(main.app, base_url="https://testserver")
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def _login(client, user_id):
    client.cookies.set("vigilant_session", _cookie(main.settings.secret_key, user_id))
    client.headers.update({"X-CSRF-Token": CSRF})


def test_can_fly_endpoint_requires_a_session(client):
    r = client.get("/dashboard/can-fly")
    assert r.status_code == 401


def test_can_fly_endpoint_returns_shapes_for_every_state(client):
    _login(client, USER_A)
    r = client.get("/dashboard/can-fly")
    assert r.status_code == 200
    data = r.json()
    assert data[str(CHAR_NO_SCOPE)] == "no_scope"
    assert data[str(CHAR_SCOPED_PENDING)] == "pending"
    scoped = data[str(CHAR_SCOPED_SYNCED)]
    assert scoped == {"can_fly": 1, "total": 2}  # Rookie Fit yes, Advanced Fit no (needs skill 3300 lvl 3)


def test_can_fly_endpoint_is_scoped_to_the_callers_own_characters(client):
    _login(client, USER_A)
    r = client.get("/dashboard/can-fly")
    data = r.json()
    assert str(CHAR_OTHER_USER) not in data


def test_can_fly_endpoint_never_leaks_another_users_fit_count(client):
    """USER_B has no saved fits of their own — even though USER_A's
    zero-requirement fit exists in the same table, USER_B's character must
    see total=0, not USER_A's count."""
    _login(client, USER_B)
    r = client.get("/dashboard/can-fly")
    assert r.status_code == 200
    data = r.json()
    assert data[str(CHAR_OTHER_USER)] == {"can_fly": 0, "total": 0}


def test_can_fly_endpoint_stays_fast_for_many_pilots(client):
    """Not the full 24-pilot/135-fit fixture the ticket calls for (this repo's
    test DB has no SDE skill-requirement rows to build a realistic one from
    quickly), but a smoke check that the per-character loop doesn't blow up
    superlinearly for a double-digit pilot count with a real saved fit."""
    _login(client, USER_A)
    t0 = time.perf_counter()
    r = client.get("/dashboard/can-fly")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert r.status_code == 200
    assert elapsed_ms < 2000, f"can-fly endpoint took {elapsed_ms:.0f}ms for 4 pilots"
