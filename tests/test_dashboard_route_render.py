"""GET /dashboard through the real route (T-070): every render test elsewhere
in this stream renders dashboard.html by hand-building a context and calling
its `content` block directly (tests/_dashboard_fixture.py) — none of them
prove the actual `dashboard()` handler still returns 200 with the new
context keys (dash_prefs/dash_mode/pilot_summaries) wired in. This does.
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
from app.db.models import Base, Character, CharacterDashboardCache, User, get_db

CSRF = "test-csrf-token"
USER_ID = 801
CHAR_ID = 90001001


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
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
            db.add(Character(
                character_id=CHAR_ID, character_name="Pilot One", user_id=USER_ID,
                is_main=True, account_group="Sample Corp", sort_order=0,
                scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                access_token="x", refresh_token="x",
                token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(CharacterDashboardCache(character_id=CHAR_ID, sync_status="idle"))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", _cookie(main.settings.secret_key, USER_ID))
    c.headers.update({"X-CSRF-Token": CSRF})
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def test_dashboard_renders_cards_by_default(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'class="b-card"' in r.text
    assert "dash-compact-row" not in r.text


def test_dashboard_renders_cards_by_default_name_sort(client):
    r = client.get("/dashboard?sort=name")
    assert r.status_code == 200
    assert 'class="b-card"' in r.text


def test_saved_compact_mode_renders_compact_rows(client):
    r = client.post("/dashboard/prefs", json={"mode": "compact"})
    assert r.status_code == 200
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "dash-compact-row" in r.text
    assert 'class="b-card"' not in r.text


def test_saved_compact_mode_renders_compact_rows_name_sort(client):
    client.post("/dashboard/prefs", json={"mode": "compact"})
    r = client.get("/dashboard?sort=name")
    assert r.status_code == 200
    assert "dash-compact-row" in r.text


def test_a_stored_table_mode_renders_the_table(client):
    """T-076: all four modes render for real now — a stored "table" mode
    (round-tripped without rendering since T-070) now shows the actual
    table, not a Cards fallback."""
    r = client.post("/dashboard/prefs", json={"mode": "table"})
    assert r.json()["mode"] == "table"
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'id="dash-table"' in r.text
    assert 'class="b-card"' not in r.text
    assert "dash-compact-row" not in r.text


def test_a_stored_detailed_mode_renders_cards_with_extra_rows(client):
    r = client.post("/dashboard/prefs", json={"mode": "detailed"})
    assert r.json()["mode"] == "detailed"
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'class="b-card"' in r.text
    assert 'data-canfly-slot' in r.text
