"""Tests for pilot role tags and private notes (T-074, app/tags.py +
app/routes/character_tags.py).

Pure validation tests need no DB at all. The storage-helper tests use the
manual-event-loop + temp-file DB idiom (mirrors tests/test_stockpiles.py): a
real sqlite file so the upsert-or-delete logic and the JSON tag list execute
against the actual SQLite dialect. Route tests follow tests/test_wh_tracker.py:
a signed session cookie plus a `get_db` dependency override onto a second
temp-file DB, so the routes and the ownership check run against real SQL.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import tags as tags_mod
from app.auth.purge import PER_CHARACTER_USER_TABLES, purge_character_user_rows
from app.db.models import Base, Character, CharacterTag, User, get_db
from app.tags import (
    MAX_NOTE_LEN,
    MAX_TAGS,
    SUGGESTED_TAGS,
    TagError,
    load_character_tags,
    normalize_note,
    parse_tags_input,
    save_character_tags,
    user_tag_vocabulary,
    validate_tags,
)

USER_ID = 601
OTHER_USER_ID = 602
CHAR_ID = 90060001
OTHER_CHAR_ID = 90060002


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _temp_session_factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ── Pure validation ──────────────────────────────────────────────────────────

def test_validate_tags_trims_and_accepts_allowed_characters():
    assert validate_tags([" Cyno ", "Home_Defense", "T3.5", "Book-Keeper"]) == [
        "Cyno", "Home_Defense", "T3.5", "Book-Keeper",
    ]


def test_validate_tags_rejects_disallowed_characters():
    with pytest.raises(TagError):
        validate_tags(["Cyno!"])
    with pytest.raises(TagError):
        validate_tags(["Alt/Main"])
    with pytest.raises(TagError):
        validate_tags(["tag,withcomma"])


def test_validate_tags_enforces_length_bounds():
    with pytest.raises(TagError):
        validate_tags(["x" * 25])  # over MAX_TAG_LEN
    # Exactly the max length is fine.
    assert validate_tags(["x" * 24]) == ["x" * 24]


def test_validate_tags_enforces_max_count():
    ok = [f"tag{i}" for i in range(MAX_TAGS)]
    assert validate_tags(ok) == ok
    with pytest.raises(TagError):
        validate_tags([f"tag{i}" for i in range(MAX_TAGS + 1)])


def test_validate_tags_case_insensitive_dedupe_keeps_first_spelling():
    assert validate_tags(["Cyno", "cyno", "CYNO", "Hauler"]) == ["Cyno", "Hauler"]


def test_validate_tags_drops_blank_entries_without_rejecting():
    # Blank/whitespace-only entries are a formatting artifact (stray commas),
    # not a tag the user typed, so they are silently dropped rather than
    # counted against the length or char-class rules.
    assert validate_tags(["Cyno", "  ", "", "Hauler"]) == ["Cyno", "Hauler"]


def test_parse_tags_input_splits_on_commas_and_dedupes():
    assert parse_tags_input("Cyno, Hauler, cyno,  , Indy") == ["Cyno", "Hauler", "Indy"]


def test_parse_tags_input_empty_string_is_no_tags():
    assert parse_tags_input("") == []
    assert parse_tags_input(None) == []


def test_normalize_note_collapses_newlines_and_trims():
    assert normalize_note("  our scout\nwatch for burn  ") == "our scout watch for burn"


def test_normalize_note_empty_becomes_none():
    assert normalize_note("") is None
    assert normalize_note("   ") is None
    assert normalize_note(None) is None


def test_normalize_note_rejects_over_length():
    with pytest.raises(TagError):
        normalize_note("x" * (MAX_NOTE_LEN + 1))
    assert normalize_note("x" * MAX_NOTE_LEN) == "x" * MAX_NOTE_LEN


def test_suggested_tags_is_the_documented_tuple():
    assert SUGGESTED_TAGS == ("Main", "Alt", "Cyno", "Scout", "Hauler", "Indy",
                              "PI", "Trader", "Farm", "Alpha")


# ── Storage helpers (upsert / load / vocabulary) ────────────────────────────

def test_save_character_tags_upserts_then_deletes_when_emptied():
    engine, SessionLocal = _temp_session_factory()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            await save_character_tags(db, USER_ID, CHAR_ID, ["Cyno", "Hauler"], "our scout")
            await db.commit()
        async with SessionLocal() as db:
            row = (await db.execute(
                select(CharacterTag).where(CharacterTag.character_id == CHAR_ID)
            )).scalar_one()
            assert json.loads(row.tags_json) == ["Cyno", "Hauler"]
            assert row.note == "our scout"

            # Re-save with the same (user, character) updates in place —
            # exactly one row, not two.
            await save_character_tags(db, USER_ID, CHAR_ID, ["Cyno"], None)
            await db.commit()
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(CharacterTag).where(CharacterTag.character_id == CHAR_ID)
            )).scalars().all()
            assert len(rows) == 1
            assert json.loads(rows[0].tags_json) == ["Cyno"]
            assert rows[0].note is None

            # Emptying both tags and note deletes the row rather than
            # leaving an empty one behind.
            await save_character_tags(db, USER_ID, CHAR_ID, [], None)
            await db.commit()
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(CharacterTag).where(CharacterTag.character_id == CHAR_ID)
            )).scalars().all()
            assert rows == []
        await engine.dispose()

    _run(run())


def test_load_character_tags_scoped_to_the_user():
    engine, SessionLocal = _temp_session_factory()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            await save_character_tags(db, USER_ID, CHAR_ID, ["Cyno"], "note a")
            await save_character_tags(db, OTHER_USER_ID, OTHER_CHAR_ID, ["Hauler"], "note b")
            await db.commit()
        async with SessionLocal() as db:
            mine = await load_character_tags(db, USER_ID)
            assert mine == {CHAR_ID: {"tags": ["Cyno"], "note": "note a"}}
            theirs = await load_character_tags(db, OTHER_USER_ID)
            assert theirs == {OTHER_CHAR_ID: {"tags": ["Hauler"], "note": "note b"}}
        await engine.dispose()

    _run(run())


def test_user_tag_vocabulary_counts_and_sorts():
    engine, SessionLocal = _temp_session_factory()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            await save_character_tags(db, USER_ID, 1, ["Cyno", "Alt"], None)
            await save_character_tags(db, USER_ID, 2, ["cyno", "Hauler"], None)
            await save_character_tags(db, USER_ID, 3, ["Alt"], None)
            # A different user's tags must never bleed into this count.
            await save_character_tags(db, OTHER_USER_ID, 4, ["Cyno", "Cyno-should-not-merge"], None)
            await db.commit()
        async with SessionLocal() as db:
            vocab = await user_tag_vocabulary(db, USER_ID)
            # "Cyno" appears on 2 pilots (case-insensitively merged, first
            # spelling kept), "Alt" on 2, "Hauler" on 1 — count desc, then name.
            assert vocab == [("Alt", 2), ("Cyno", 2), ("Hauler", 1)]
        await engine.dispose()

    _run(run())


# ── Purge on character removal ──────────────────────────────────────────────

def test_character_tags_is_registered_for_purge():
    assert "character_tags" in PER_CHARACTER_USER_TABLES


def test_purge_character_user_rows_deletes_this_characters_tags():
    engine, SessionLocal = _temp_session_factory()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            await save_character_tags(db, USER_ID, CHAR_ID, ["Cyno"], "gone with the character")
            await save_character_tags(db, USER_ID, OTHER_CHAR_ID, ["Hauler"], "stays")
            await db.commit()
        async with SessionLocal() as db:
            deleted = await purge_character_user_rows(db, CHAR_ID)
            await db.commit()
            remaining = await load_character_tags(db, USER_ID)
        await engine.dispose()
        return deleted, remaining

    deleted, remaining = _run(run())
    assert deleted == 1
    assert remaining == {OTHER_CHAR_ID: {"tags": ["Hauler"], "note": "stays"}}


# ── Routes: auth, IDOR, validation round-trip, escaping ─────────────────────

def _make_char(cid, name, user_id):
    return Character(
        character_id=cid, character_name=name,
        access_token="dummy-access", refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1), scopes="", user_id=user_id,
    )


def _seeded_app_db():
    """A temp-file DB with two users and one character each, wired onto the
    real app via a `get_db` override — mirrors tests/test_wh_tracker.py.

    Returns (teardown, SessionLocal) — the session factory lets a test read
    the same DB the routes just wrote to, to confirm an IDOR attempt wrote
    nothing.
    """
    engine, SessionLocal = _temp_session_factory()

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            db.add(User(id=OTHER_USER_ID))
            db.add(_make_char(CHAR_ID, "Pilot One", USER_ID))
            db.add(_make_char(OTHER_CHAR_ID, "Pilot Two", OTHER_USER_ID))
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown, SessionLocal


CSRF = "test-csrf-token"


def _client(user_id=None):
    """TestClient with a signed session cookie carrying a CSRF token (so a
    POST clears CSRFMiddleware and the route's own session check is what
    answers) and `user_id` when given."""
    import app.main as main

    client = TestClient(main.app, base_url="https://testserver")
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    session = {"csrf_token": CSRF}
    if user_id is not None:
        session["user_id"] = user_id
    data = base64.b64encode(json.dumps(session).encode())
    client.cookies.set("vigilant_session", signer.sign(data).decode())
    return client


def _post(client, path, data):
    return client.post(path, data=data, headers={"X-CSRF-Token": CSRF})


def test_get_requires_a_session():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        r = _client().get(f"/character/{CHAR_ID}/tags")
        assert r.status_code == 401
        assert r.text == ""
    finally:
        teardown()


def test_post_requires_a_session():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        r = _post(_client(), f"/character/{CHAR_ID}/tags", {"tags": "Cyno", "note": ""})
        assert r.status_code == 401
        assert r.text == ""
    finally:
        teardown()


def test_get_another_users_character_is_404():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        r = client.get(f"/character/{OTHER_CHAR_ID}/tags")
        assert r.status_code == 404
    finally:
        teardown()


def test_post_another_users_character_is_404_and_writes_nothing():
    teardown, SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        r = _post(client, f"/character/{OTHER_CHAR_ID}/tags", {"tags": "Cyno", "note": "hijacked"})
        assert r.status_code == 404

        async def check():
            async with SessionLocal() as db:
                return (await db.execute(
                    select(CharacterTag).where(CharacterTag.character_id == OTHER_CHAR_ID)
                )).scalars().all()

        assert _run(check()) == []
    finally:
        teardown()


def test_get_renders_view_state_with_chips_and_note():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        _post(client, f"/character/{CHAR_ID}/tags", {"tags": "Cyno, Hauler", "note": "reliable"})
        r = client.get(f"/character/{CHAR_ID}/tags")
        assert r.status_code == 200
        assert 'id="char-tags"' in r.text
        assert "Cyno" in r.text and "Hauler" in r.text
        assert "reliable" in r.text
        # T-077: the note is CSS-truncated, not server-sliced -- the full
        # text is still in the DOM (once in the visible span, once in its
        # title tip, once more in the hidden edit form's input value).
        assert r.text.count("reliable") == 3
        assert 'title="reliable"' in r.text
        # Compact view state: no big bordered section any more.
        assert 'class="b-empty"' not in r.text
    finally:
        teardown()


def test_get_renders_compact_none_and_edit_button_with_no_tags():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        r = client.get(f"/character/{CHAR_ID}/tags")
        assert r.status_code == 200
        assert 'id="char-tags"' in r.text
        assert "none" in r.text
        assert "tags-edit-btn" in r.text
        assert "Edit" in r.text
    finally:
        teardown()


def test_post_saves_and_rerenders_the_partial():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        r = _post(client, f"/character/{CHAR_ID}/tags", {"tags": "Cyno, Hauler", "note": "our scout"})
        assert r.status_code == 200
        assert 'id="char-tags"' in r.text
        assert "Cyno" in r.text
        assert "our scout" in r.text
    finally:
        teardown()


def test_post_invalid_tag_reshows_the_form_with_error_and_preserves_input():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        r = _post(client, f"/character/{CHAR_ID}/tags", {"tags": "Cyno, Bad!Tag", "note": ""})
        assert r.status_code == 200
        assert "aren&#39;t allowed" in r.text or "aren't allowed" in r.text or "not allowed" in r.text
        # What was typed comes back rather than being discarded.
        assert "Bad!Tag" in r.text
    finally:
        teardown()


def test_post_script_tag_and_note_are_escaped_not_executed():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        # A tag can't contain "<" or ">" (not in the allowed character set),
        # so the injection attempt itself is invalid input — assert it's
        # rejected, not rendered raw.
        r = _post(client, f"/character/{CHAR_ID}/tags",
                  {"tags": "<script>alert(1)</script>", "note": ""})
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text

        # A note has no character restriction, so it's the one that must be
        # escaped by the template rather than rejected.
        r = _post(client, f"/character/{CHAR_ID}/tags",
                  {"tags": "Cyno", "note": "<script>alert(1)</script>"})
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text
    finally:
        teardown()


def test_post_more_than_eight_tags_is_rejected():
    teardown, _SessionLocal = _seeded_app_db()
    try:
        client = _client(USER_ID)
        too_many = ", ".join(f"tag{i}" for i in range(MAX_TAGS + 1))
        r = _post(client, f"/character/{CHAR_ID}/tags", {"tags": too_many, "note": ""})
        assert r.status_code == 200
        assert "At most 8 tags" in r.text
    finally:
        teardown()
