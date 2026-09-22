"""Tests for the fitting-tool implant picker.

Three things landed together here and each gets its own coverage:

1. `search_implants` had a stray local import that shadowed the correct
   module-scope names with ones that don't exist, so every call raised
   `ImportError` before the query ever ran — the endpoint always 500'd.
   `test_search_implants_*` proves the fixed endpoint actually returns rows
   for a seeded implant, respects the slot filter, and still excludes
   published types with no implantness (attr 331) row.
2. `/tools/fitting/characters` now returns every linked character (not just
   skill-scope holders) tagged with `has_skills_scope` / `has_implants_scope`,
   since it's the shared source for both the skill-check selector and the
   new implant character picker.
3. The implant section's template contract: the character `<select>` exists
   with its dispatcher binding, the old single-purpose button is gone
   (absorbed into the select), and the name-search input is still present
   as the secondary path.

Route tests use the signed-session-cookie + get_db override idiom from
tests/test_fitting_compare.py / tests/test_networth.py.
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
from app.db.models import Base, Character, get_db

USER_A = 8101
USER_B = 8102
CHAR_A = 95100001
CHAR_B = 95100002


def _client():
    import app.main as main
    return TestClient(main.app)


def _authed_client(user_id):
    """TestClient carrying a signed session cookie for `user_id`. Uses an
    https base_url because the session cookie is Secure outside debug mode."""
    import app.main as main

    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _seeded_db(seed_coro):
    """Temp-file sqlite DB seeded by `seed_coro(db)`; returns a teardown()."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            await seed_coro(db)
            await db.commit()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown


# ── search_implants: import-bug regression + query contract ────────────────

async def _seed_implants(db):
    # A real stat implant: published, implantness (attr 331) = slot 1.
    db.add(sm.SDEType(type_id=19540, type_name="High-grade Ascendancy Alpha",
                       group_id=300, published=True))
    db.add(sm.SDETypeDogmaAttribute(type_id=19540, attribute_id=331, value=1.0))
    # A combat implant in slot 6, for the slot-filter test.
    db.add(sm.SDEType(type_id=27222, type_name="Zainou 'Deadeye' Small Hybrid Turret SH-603",
                       group_id=301, published=True))
    db.add(sm.SDETypeDogmaAttribute(type_id=27222, attribute_id=331, value=6.0))
    # A published type with NO implantness row at all — must never match,
    # even though its name would otherwise satisfy a query.
    db.add(sm.SDEType(type_id=34, type_name="Tritanium", group_id=18, published=True))


def test_search_implants_finds_seeded_implant_by_partial_name():
    """Regression test for the actual bug: search_implants had a stray
    `from app.db.models import SDEType, SDETypeDogmaAttribute` inside the
    function body. Those classes live in app.db.sde_models (already imported
    at module scope) — app.db.models doesn't define them, so the local import
    raised ImportError on every single call, before the query ever ran. The
    UI's fetch always got a 500, which is what "the implant search does not
    look like it is working" actually was.
    """
    teardown = _seeded_db(_seed_implants)
    try:
        r = _client().get("/tools/fitting/search/implants?q=ascend")
        assert r.status_code == 200
        rows = r.json()
        assert rows == [{"type_id": 19540, "name": "High-grade Ascendancy Alpha", "slot": 1}]
    finally:
        teardown()


def test_search_implants_is_case_insensitive_partial_match():
    teardown = _seeded_db(_seed_implants)
    try:
        r = _client().get("/tools/fitting/search/implants?q=ASCEND")
        assert r.status_code == 200
        assert len(r.json()) == 1
    finally:
        teardown()


def test_search_implants_slot_filter():
    teardown = _seeded_db(_seed_implants)
    try:
        # Name matches, but the implant is actually slot 6 — filtering to
        # slot 1 must exclude it.
        r = _client().get("/tools/fitting/search/implants?q=deadeye&slot=1")
        assert r.status_code == 200 and r.json() == []

        r = _client().get("/tools/fitting/search/implants?q=deadeye&slot=6")
        assert r.status_code == 200
        assert len(r.json()) == 1 and r.json()[0]["slot"] == 6
    finally:
        teardown()


def test_search_implants_excludes_types_without_implantness_row():
    """A published type with no attr-331 dogma row (an ore, here) can't
    satisfy the join and must not appear even on an exact name hit."""
    teardown = _seeded_db(_seed_implants)
    try:
        r = _client().get("/tools/fitting/search/implants?q=tritanium")
        assert r.status_code == 200 and r.json() == []
    finally:
        teardown()


# ── clone_implants: documented error shapes ─────────────────────────────────

async def _seed_char_no_implants_scope(db):
    db.add(Character(
        character_id=CHAR_A, character_name="Pilot A",
        access_token="dummy-access", refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes="esi-skills.read_skills.v1",
        user_id=USER_A,
    ))


def test_clone_implants_requires_login():
    teardown = _seeded_db(_seed_char_no_implants_scope)
    try:
        r = _client().get(f"/tools/fitting/clone-implants/{CHAR_A}")
        assert r.json() == {"error": "Not logged in"}
    finally:
        teardown()


def test_clone_implants_character_not_found_for_a_different_user():
    """Ownership check: a character id that belongs to someone else 404s in
    spirit (here, the documented {"error": ...} shape) rather than leaking
    another user's implant loadout."""
    teardown = _seeded_db(_seed_char_no_implants_scope)
    try:
        client = _authed_client(USER_B)
        r = client.get(f"/tools/fitting/clone-implants/{CHAR_A}")
        assert r.json() == {"error": "Character not found"}
    finally:
        teardown()


def test_clone_implants_missing_scope_returns_documented_error_shape():
    """A character that hasn't granted esi-clones.read_implants.v1 must not
    attempt the ESI call — it gets the same {"error": "..."} shape the JS
    surfaces directly in the picker's status line."""
    teardown = _seeded_db(_seed_char_no_implants_scope)
    try:
        client = _authed_client(USER_A)
        r = client.get(f"/tools/fitting/clone-implants/{CHAR_A}")
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert "implants scope" in body["error"]
    finally:
        teardown()


# ── /tools/fitting/characters: shared picker source ─────────────────────────

async def _seed_two_chars_mixed_scopes(db):
    db.add(Character(
        character_id=CHAR_A, character_name="Pilot A",
        access_token="x", refresh_token="y", token_expiry=datetime(2099, 1, 1),
        scopes="esi-skills.read_skills.v1", user_id=USER_A,
    ))
    db.add(Character(
        character_id=CHAR_B, character_name="Pilot B",
        access_token="x", refresh_token="y", token_expiry=datetime(2099, 1, 1),
        scopes="esi-clones.read_implants.v1", user_id=USER_A,
    ))


def test_fitting_characters_returns_every_linked_character_with_scope_flags():
    """Both pickers read this one endpoint (fetchFittingCharacters() in
    fitting_tool.html) — it must return every linked character, not just
    scope-holders, so the implant picker can show a disabled option with a
    reason for a character missing the clones scope instead of omitting it
    (the skill-check selector filters has_skills_scope client-side)."""
    teardown = _seeded_db(_seed_two_chars_mixed_scopes)
    try:
        client = _authed_client(USER_A)
        r = client.get("/tools/fitting/characters")
        assert r.status_code == 200
        chars = {c["name"]: c for c in r.json()["characters"]}
        assert set(chars) == {"Pilot A", "Pilot B"}
        assert chars["Pilot A"]["has_skills_scope"] is True
        assert chars["Pilot A"]["has_implants_scope"] is False
        assert chars["Pilot B"]["has_skills_scope"] is False
        assert chars["Pilot B"]["has_implants_scope"] is True
    finally:
        teardown()


def test_fitting_characters_empty_when_not_logged_in():
    teardown = _seeded_db(_seed_two_chars_mixed_scopes)
    try:
        r = _client().get("/tools/fitting/characters")
        assert r.status_code == 200 and r.json() == {"characters": []}
    finally:
        teardown()


# ── Template contract ────────────────────────────────────────────────────────
# Style matches tests/test_update_banner.py: read the template source and
# assert on the markup/script contract directly, rather than rendering it.

def _template_src():
    return open("app/templates/fitting_tool.html").read()


def test_implant_character_select_exists_with_dispatcher_binding():
    html = _template_src()
    assert 'id="implant-char-select"' in html
    assert 'data-change="onImplantCharChange"' in html


def test_old_single_purpose_clone_button_is_gone():
    """The character select absorbs the old button — one obvious control,
    not two (:217 in the original layout)."""
    html = _template_src()
    assert 'id="btn-clone-implants"' not in html
    assert 'data-click="loadCloneImplants"' not in html


def test_implant_name_search_remains_as_secondary_path():
    html = _template_src()
    assert 'id="implant-search"' in html
    assert 'data-input="searchImplant"' in html


def test_implant_picker_js_functions_are_globally_defined():
    """actions.js dispatches data-<event> bindings via window[name] and warns
    (see tests/test_csp_inline_handlers.py::test_every_data_binding_resolves)
    if the name isn't reachable — assert the new top-level declarations
    actually exist in the page script."""
    html = _template_src()
    assert "function onImplantCharChange(" in html
    assert "function loadImplantCharList(" in html
    assert "function fetchFittingCharacters(" in html
