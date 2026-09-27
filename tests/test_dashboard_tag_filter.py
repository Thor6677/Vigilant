"""T-076 Part B item 4: the Dashboard tag filter bar.

Any-match filtering (a pilot matches if it holds ANY selected tag), groups
left with no visible pilot are hidden, the "Filtered by:" indicator with its
clear control appears, all tag text is autoescaped, and the bar itself is
absent when the user has no tags at all.
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
from app.db.models import (
    Base, Character, CharacterDashboardCache, CharacterTag, User, get_db,
)

CSRF = "test-csrf-token"
USER_ID = 841
CHAR_CYNO = 90401001     # tagged "Cyno", account Alpha Corp
CHAR_HAULER = 90401002   # tagged "Hauler", account Alpha Corp
CHAR_UNTAGGED = 90401003  # no tags, account Bravo Corp (its own group)


def _cookie(secret_key: str, user_id: int) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())
    return signer.sign(data).decode()


def _char(cid, group, main_pilot=False):
    return Character(
        character_id=cid, character_name=f"Pilot {cid}", user_id=USER_ID,
        is_main=main_pilot, account_group=group, sort_order=0,
        scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
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
            db.add(User(id=USER_ID, role="user"))
            db.add(_char(CHAR_CYNO, "Alpha Corp", main_pilot=True))
            db.add(_char(CHAR_HAULER, "Alpha Corp"))
            db.add(_char(CHAR_UNTAGGED, "Bravo Corp"))
            for cid in (CHAR_CYNO, CHAR_HAULER, CHAR_UNTAGGED):
                db.add(CharacterDashboardCache(character_id=cid, sync_status="idle"))
            db.add(CharacterTag(user_id=USER_ID, character_id=CHAR_CYNO, tags_json=json.dumps(["Cyno"])))
            db.add(CharacterTag(user_id=USER_ID, character_id=CHAR_HAULER, tags_json=json.dumps(["Hauler"])))
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


@pytest.fixture
def client_no_tags():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    other_user = USER_ID + 1
    other_char = CHAR_UNTAGGED + 1000

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=other_user, role="user"))
            db.add(Character(
                character_id=other_char, character_name="Pilot Untagged", user_id=other_user,
                is_main=True, account_group="Solo Corp", sort_order=0,
                scopes=" ".join(perms.ALL_SCOPES), declined_scopes="",
                access_token="x", refresh_token="x",
                token_expiry=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.add(CharacterDashboardCache(character_id=other_char, sync_status="idle"))
            await db.commit()
    asyncio.run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", _cookie(main.settings.secret_key, other_user))
    c.headers.update({"X-CSRF-Token": CSRF})
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def test_filter_bar_present_when_tags_exist(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Tags:" in r.text


def test_filter_bar_absent_with_no_tags(client_no_tags):
    """A user with zero tags anywhere never sees the bar at all."""
    r = client_no_tags.get("/dashboard")
    assert r.status_code == 200
    assert "Tags:" not in r.text


def test_tag_filter_any_match_hides_non_matching_pilots(client):
    client.post("/dashboard/prefs", json={"tag_filter": ["Cyno"]})
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert f"/character/{CHAR_CYNO}" in r.text
    assert f"/character/{CHAR_HAULER}" not in r.text
    assert f"/character/{CHAR_UNTAGGED}" not in r.text


def test_tag_filter_matches_any_of_multiple_selected_tags(client):
    client.post("/dashboard/prefs", json={"tag_filter": ["Cyno", "Hauler"]})
    r = client.get("/dashboard")
    assert f"/character/{CHAR_CYNO}" in r.text
    assert f"/character/{CHAR_HAULER}" in r.text
    assert f"/character/{CHAR_UNTAGGED}" not in r.text


def test_tag_filter_hides_a_group_left_with_no_visible_pilot(client):
    """Bravo Corp holds only the untagged pilot — filtering by Cyno must
    drop that whole group, not render it empty."""
    client.post("/dashboard/prefs", json={"tag_filter": ["Cyno"]})
    r = client.get("/dashboard?sort=custom")
    assert "Bravo Corp" not in r.text
    assert "Alpha Corp" in r.text


def test_filtered_by_indicator_shown_with_clear_control(client):
    client.post("/dashboard/prefs", json={"tag_filter": ["Cyno"]})
    r = client.get("/dashboard")
    assert "Filtered by:" in r.text
    assert 'data-click="clearTagFilter"' in r.text


def test_no_indicator_when_filter_is_empty(client):
    r = client.get("/dashboard")
    assert "Filtered by:" not in r.text


def test_tag_text_is_autoescaped_and_never_used_with_innerhtml():
    """Tag text (app.tags's validator allows letters/digits/space/-_. only,
    but this proves the template layer itself never trusts that — it
    autoescapes whatever it's given) never reaches the page unescaped, and
    no page script assigns it via innerHTML."""
    from tests._dashboard_fixture import render_full
    html = render_full(
        "custom", dash_mode="compact",
        wallet_deltas={}, tags_by_char={
            1001: {"tags": ["<script>bad</script>"], "note": None},
        },
    )
    assert "<script>bad</script>" not in html
    assert "&lt;script&gt;" in html
    # The tag-filter JS itself (toggleTagFilter/clearTagFilter) never touches
    # innerHTML — other, unrelated pre-existing script in this page does
    # (e.g. the "+ Add Account" builder), so this checks those two functions
    # specifically rather than the whole page.
    start = html.index("function toggleTagFilter")
    end = html.index("function clearTagFilter")
    end = html.index("\n}\n", end) + 3
    tag_js = html[start:end]
    assert ".innerHTML" not in tag_js


def test_stale_persisted_filter_tag_is_dropped_not_hiding_everyone(client):
    """A tag_filter referencing a tag no longer in the vocabulary (e.g. its
    last pilot was untagged) must not silently hide every pilot with no
    visible way to clear it."""
    client.post("/dashboard/prefs", json={"tag_filter": ["NoLongerExists"]})
    r = client.get("/dashboard")
    assert f"/character/{CHAR_CYNO}" in r.text
    assert f"/character/{CHAR_HAULER}" in r.text
    assert "Filtered by:" not in r.text
