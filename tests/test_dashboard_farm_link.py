"""T-076 Part B item 6: the Dashboard "Skill farm · N injectors ready" link
— app/dashboard/farm.py (unit) plus its appearance in the rendered page only
when the user actually has farm pilots.
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
from app.dashboard.farm import load_farm_summary
from app.db.models import (
    AsyncSessionLocal, Base, Character, CharacterDashboardCache,
    SkillFarmPilot, User, get_db,
)

CSRF = "test-csrf-token"
USER_ID = 851
CHAR_FARM = 90501001
CHAR_PLAIN = 90501002


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    return signer.sign(data).decode()


def _char(cid, main_pilot=False):
    return Character(
        character_id=cid, character_name=f"Pilot {cid}", user_id=USER_ID,
        is_main=main_pilot, account_group="Sample Corp", sort_order=0,
        scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
        access_token="x", refresh_token="x",
        token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
    )


@pytest.fixture
def client_factory():
    def _make(with_farm_pilot: bool):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

        async def seed():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with SessionLocal() as db:
                db.add(User(id=USER_ID, role="user"))
                db.add(_char(CHAR_FARM, main_pilot=True))
                db.add(_char(CHAR_PLAIN))
                db.add(CharacterDashboardCache(
                    character_id=CHAR_FARM, sync_status="idle",
                    skills_json=json.dumps({"total_sp": 20_000_000, "unallocated_sp": 0, "levels": {}}),
                ))
                db.add(CharacterDashboardCache(character_id=CHAR_PLAIN, sync_status="idle"))
                if with_farm_pilot:
                    db.add(SkillFarmPilot(user_id=USER_ID, character_id=CHAR_FARM, base_sp=5_000_000))
                await db.commit()
        asyncio.run(seed())

        async def _override():
            async with SessionLocal() as s:
                yield s
        main.app.dependency_overrides[get_db] = _override
        c = TestClient(main.app, base_url="https://testserver")
        c.cookies.set("vigilant_session", _cookie(main.settings.secret_key, USER_ID))
        c.headers.update({"X-CSRF-Token": CSRF})
        return c
    yield _make
    main.app.dependency_overrides.pop(get_db, None)


def test_farm_link_absent_with_no_farm_pilots(client_factory):
    c = client_factory(with_farm_pilot=False)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Skill farm" not in r.text


def test_farm_link_present_with_a_farm_pilot(client_factory):
    c = client_factory(with_farm_pilot=True)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "Skill farm" in r.text
    assert 'href="/tools/skill-farm"' in r.text
    # 20M SP - 5M floor = 15M surplus -> 30 injectors at 500k each.
    assert "30 injector" in r.text


def test_load_farm_summary_none_with_no_pilots():
    async def _run():
        async with AsyncSessionLocal() as db:
            return await load_farm_summary(db, 999_999, [], {})
    assert asyncio.run(_run()) is None
