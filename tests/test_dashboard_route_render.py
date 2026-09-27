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


CHAR_LOW = 90001002
CHAR_HIGH = 90001003


@pytest.fixture
def two_pilot_client():
    """A second fixture (two pilots, distinguishable wallets) so a sort can
    have an observable, checkable effect on row order."""
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
                character_id=CHAR_LOW, character_name="Pilot Low", user_id=USER_ID,
                is_main=True, account_group="Sample Corp", sort_order=0,
                scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                access_token="x", refresh_token="x",
                token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(Character(
                character_id=CHAR_HIGH, character_name="Pilot High", user_id=USER_ID,
                is_main=False, account_group="Sample Corp", sort_order=1,
                scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                access_token="x", refresh_token="x",
                token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(CharacterDashboardCache(character_id=CHAR_LOW, sync_status="idle", wallet=1_000.0))
            db.add(CharacterDashboardCache(character_id=CHAR_HIGH, sync_status="idle", wallet=9_000_000.0))
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


def test_table_sort_is_persisted_and_reflected_in_the_next_render(two_pilot_client):
    """T-076: table_sort is a saved pref, and the initial render is already
    sorted server-side — a fresh GET after a sort-by-wallet-desc save must
    show the richer pilot first without any JS running."""
    r = two_pilot_client.post("/dashboard/prefs", json={"mode": "table", "table_sort": {"key": "wallet", "dir": "desc"}})
    assert r.json()["table_sort"] == {"key": "wallet", "dir": "desc"}

    r = two_pilot_client.get("/dashboard")
    assert r.status_code == 200
    high_idx = r.text.index(f'data-char-id="{CHAR_HIGH}"')
    low_idx = r.text.index(f'data-char-id="{CHAR_LOW}"')
    assert high_idx < low_idx, "wallet-desc sort should place the richer pilot first"

    # Flip to ascending and confirm the order reverses.
    two_pilot_client.post("/dashboard/prefs", json={"table_sort": {"key": "wallet", "dir": "asc"}})
    r2 = two_pilot_client.get("/dashboard")
    high_idx2 = r2.text.index(f'data-char-id="{CHAR_HIGH}"')
    low_idx2 = r2.text.index(f'data-char-id="{CHAR_LOW}"')
    assert low_idx2 < high_idx2, "wallet-asc sort should place the poorer pilot first"
