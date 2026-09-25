"""Route tests for the single-character wormhole tracker
(app/routes/wh_tracker.py, T-034 follow-up).

Covers the headline-selection fix: the tracker used to be multi-select —
several characters could be ticked, and the first one found in J-space (in
checkbox order) became the headline, so a selected character's system could
silently differ from what the panel showed. The product decision is single
selection: the headline is always the one selected character, whether it's
in J-space or not.

Pattern follows tests/test_pnl_route.py: signed-session-cookie TestClient
idiom plus a get_db dependency override onto a temp-file sqlite DB, so the
route + real SQL run against an isolated database. ESI itself is never
called — `_fetch_location` is monkeypatched per-test to a fixed location, the
same seam tests/test_lp_roi.py and tests/test_evescout.py use for their
`_fetch_*_esi` functions.
"""
import asyncio
import base64
import json
import re
import tempfile
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routes.wh_tracker as wh_tracker
from app.db.cache import TTL, _ttl_for_path
from app.db.models import Base, Character, User, get_db
from tests.conftest import ensure_user
from app.db.sde_models import SDERegion, SDESystem

USER_ID = 501
CHAR_A = 90050001  # "Pilot A" — the character selected in most tests below
CHAR_B = 90050002  # "Pilot B" — unselected / previously tracked

J_SYSTEM_ID = 31050001
J_SYSTEM_NAME = "J-Test-Alpha"
J_SYSTEM_B_ID = 31050002
J_SYSTEM_B_NAME = "J-Test-Beta"

K_SYSTEM_ID = 30050001
K_SYSTEM_NAME = "K-Test-System"
K_REGION_ID = 10050001
K_REGION_NAME = "Test Region"

_LOCATION_SCOPE = "esi-location.read_location.v1"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _authed_client(user_id=USER_ID):
    """TestClient carrying a signed session cookie for `user_id`. Uses an
    https base_url because the session cookie is Secure outside debug mode."""
    import app.main as main

    ensure_user(user_id)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    data = base64.b64encode(json.dumps({"user_id": user_id}).encode())
    cookie = signer.sign(data).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _make_char(cid, name, user_id=USER_ID, scoped=True):
    return Character(
        character_id=cid,
        character_name=name,
        access_token="dummy-access",
        refresh_token="dummy-refresh",
        token_expiry=datetime(2099, 1, 1),
        scopes=_LOCATION_SCOPE if scoped else "",
        user_id=user_id,
    )


def _seeded_app_db():
    """Temp sqlite DB with Pilot A + Pilot B (both scoped for location) and
    the SDE rows for one J-space pair and one K-space test system. Overrides
    `get_db` on the real app so the route runs against this DB instead of
    the app's production sqlite file. Returns a teardown callable."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(_make_char(CHAR_A, "Pilot A"))
            db.add(_make_char(CHAR_B, "Pilot B"))
            db.add(User(id=USER_ID))
            db.add(SDESystem(system_id=J_SYSTEM_ID, system_name=J_SYSTEM_NAME, security=-1.0))
            db.add(SDESystem(system_id=J_SYSTEM_B_ID, system_name=J_SYSTEM_B_NAME, security=-1.0))
            db.add(SDESystem(system_id=K_SYSTEM_ID, system_name=K_SYSTEM_NAME,
                              security=0.5, region_id=K_REGION_ID))
            db.add(SDERegion(region_id=K_REGION_ID, region_name=K_REGION_NAME))
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown


def _fake_fetch_location(system_id, system_name, is_j):
    """A `_fetch_location` replacement returning a fixed, successful
    location — stands in for the real ESI call."""
    async def _fake(char, db):
        return {"system_id": system_id, "system_name": system_name, "is_j": is_j}, None
    return _fake


def _fake_fetch_location_failing(reason):
    async def _fake(char, db):
        return None, reason
    return _fake


def test_selected_jspace_character_is_always_the_headline(monkeypatch):
    """The headline must be the SELECTED character's system, even when a
    different (unselected, previously-tracked) character is in a different
    wormhole. Pilot B is polled first so it leaves state behind, then Pilot
    A is selected — the old first-in-J-space-wins logic would have no way
    to prefer A, but there is no such contest any more."""
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        client = _authed_client()

        monkeypatch.setattr(wh_tracker, "_fetch_location",
                             _fake_fetch_location(J_SYSTEM_B_ID, J_SYSTEM_B_NAME, True))
        r = client.get("/intel/tracker/poll", params={"char": CHAR_B})
        assert r.status_code == 200
        assert J_SYSTEM_B_NAME in r.text

        monkeypatch.setattr(wh_tracker, "_fetch_location",
                             _fake_fetch_location(J_SYSTEM_ID, J_SYSTEM_NAME, True))
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        assert r.status_code == 200
        body = r.text
        assert "Pilot A" in body
        assert J_SYSTEM_NAME in body
        # Must NOT show Pilot B's system as the headline.
        assert J_SYSTEM_B_NAME not in body
    finally:
        teardown()


def test_selected_character_in_kspace_renders_compact_line(monkeypatch):
    """A k-space location gets the compact '<name> is in <system>' line, no
    wormhole panel, and no error — k-space is a normal location."""
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(wh_tracker, "_fetch_location",
                             _fake_fetch_location(K_SYSTEM_ID, K_SYSTEM_NAME, False))
        client = _authed_client()
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        assert r.status_code == 200
        body = r.text
        assert "Pilot A" in body
        assert K_SYSTEM_NAME in body
        assert "is in" in body
        assert K_REGION_NAME in body
        # No wormhole-panel markup for a k-space location.
        assert "ws-grid" not in body
        assert "System Intelligence" not in body
        assert "Location lookup failed" not in body
    finally:
        teardown()


def test_location_fetch_failure_shows_error_state_with_reason(monkeypatch):
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(
            wh_tracker, "_fetch_location",
            _fake_fetch_location_failing("token revoked — re-link this character"))
        client = _authed_client()
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        assert r.status_code == 200
        body = r.text
        assert "Location lookup failed" in body
        assert "token revoked" in body
    finally:
        teardown()


def test_poll_with_no_selection_shows_none_state():
    teardown = _seeded_app_db()
    try:
        client = _authed_client()
        r = client.get("/intel/tracker/poll", params={"char": ""})
        assert r.status_code == 200
        assert "Select a character above to start tracking." in r.text
    finally:
        teardown()


def test_prev_system_memory_still_records_a_transition(monkeypatch):
    """Keep the 'came from X' transition memory: no prior-system note on the
    first sighting, then a note naming the previous system once the tracked
    character has actually moved."""
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        client = _authed_client()

        monkeypatch.setattr(wh_tracker, "_fetch_location",
                             _fake_fetch_location(J_SYSTEM_ID, J_SYSTEM_NAME, True))
        r1 = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        assert r1.status_code == 200
        assert "came from" not in r1.text

        monkeypatch.setattr(wh_tracker, "_fetch_location",
                             _fake_fetch_location(J_SYSTEM_B_ID, J_SYSTEM_B_NAME, True))
        r2 = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        assert r2.status_code == 200
        assert "came from" in r2.text
        assert J_SYSTEM_NAME in r2.text  # the system it came FROM
    finally:
        teardown()


def test_character_location_ttl_matches_esis_own_cache():
    """ESI's max-age on GET /characters/{id}/location/ is 5s. The old 60s
    entry was fine for the dashboard's own per-character sync cadence (see
    FIELD_CACHE_SECONDS['location'] in app/routes/dashboard.py) but left the
    live tracker showing a system up to 60s stale."""
    assert TTL["character_location"] <= 5
    assert _ttl_for_path("/characters/12345/location/") <= 5


def test_picker_is_a_single_radio_group_not_checkboxes():
    teardown = _seeded_app_db()
    try:
        client = _authed_client()
        r = client.get("/intel/tracker")
        assert r.status_code == 200
        body = r.text

        # Scope to the tracker's own picker inputs — base.html ships
        # unrelated checkboxes elsewhere on the page (notification prefs).
        inputs = re.findall(r'<input[^>]*class="wht-char"[^>]*>', body)
        assert inputs, "expected at least one character radio input"
        names = set()
        for tag in inputs:
            assert 'type="checkbox"' not in tag, f"expected a radio input, got: {tag}"
            assert 'type="radio"' in tag, f"expected a radio input, got: {tag}"
            m = re.search(r'name="([^"]+)"', tag)
            assert m, f"radio input missing a name attribute: {tag}"
            names.add(m.group(1))
        # Exactly one radio group — every wht-char input shares one `name`.
        assert names == {"wht-char"}
    finally:
        teardown()


# ── ISS-052: an unchanged panel is not re-rendered every 15s ────────────────

def _shown(body):
    m = re.search(r'<input type="hidden" id="wht-shown" name="shown" value="([^"]*)"', body)
    assert m, "the panel must carry the key of what it shows"
    return m.group(1)


def test_unchanged_system_refreshes_only_the_checked_stamp(monkeypatch):
    """Re-rendering the whole panel every poll reset the page's scroll (the
    kill list reloads through a one-line placeholder) and wiped the
    structure-age box."""
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(wh_tracker, "_fetch_location",
                            _fake_fetch_location(J_SYSTEM_ID, J_SYSTEM_NAME, True))
        client = _authed_client()
        r1 = client.get("/intel/tracker/poll", params={"char": CHAR_A})
        key = _shown(r1.text)
        assert key

        built = []
        real_build = wh_tracker.build_wh_system_context

        async def counting_build(db, name):
            built.append(name)
            return await real_build(db, name)
        monkeypatch.setattr(wh_tracker, "build_wh_system_context", counting_build)

        r2 = client.get("/intel/tracker/poll", params={"char": CHAR_A, "shown": key})
        assert r2.status_code == 200
        assert r2.headers.get("HX-Reswap") == "none"
        assert 'id="wht-checked"' in r2.text and 'hx-swap-oob="true"' in r2.text
        assert J_SYSTEM_NAME not in r2.text
        assert built == []          # no system context rebuilt for nothing
    finally:
        teardown()


def test_moving_system_renders_the_full_panel(monkeypatch):
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        client = _authed_client()
        monkeypatch.setattr(wh_tracker, "_fetch_location",
                            _fake_fetch_location(J_SYSTEM_ID, J_SYSTEM_NAME, True))
        key = _shown(client.get("/intel/tracker/poll", params={"char": CHAR_A}).text)

        monkeypatch.setattr(wh_tracker, "_fetch_location",
                            _fake_fetch_location(J_SYSTEM_B_ID, J_SYSTEM_B_NAME, True))
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A, "shown": key})
        assert "HX-Reswap" not in r.headers
        assert J_SYSTEM_B_NAME in r.text and "came from" in r.text
        assert _shown(r.text) != key
    finally:
        teardown()


def test_same_system_for_another_character_renders_the_full_panel(monkeypatch):
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(wh_tracker, "_fetch_location",
                            _fake_fetch_location(J_SYSTEM_ID, J_SYSTEM_NAME, True))
        client = _authed_client()
        key_b = _shown(client.get("/intel/tracker/poll", params={"char": CHAR_B}).text)
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A, "shown": key_b})
        assert "HX-Reswap" not in r.headers
        assert "Pilot A" in r.text
    finally:
        teardown()


def test_unchanged_kspace_line_is_not_re_rendered(monkeypatch):
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(wh_tracker, "_fetch_location",
                            _fake_fetch_location(K_SYSTEM_ID, K_SYSTEM_NAME, False))
        client = _authed_client()
        key = _shown(client.get("/intel/tracker/poll", params={"char": CHAR_A}).text)
        r = client.get("/intel/tracker/poll", params={"char": CHAR_A, "shown": key})
        assert r.headers.get("HX-Reswap") == "none"
        assert K_SYSTEM_NAME not in r.text
    finally:
        teardown()


def test_error_and_empty_states_never_claim_to_show_anything(monkeypatch):
    teardown = _seeded_app_db()
    try:
        monkeypatch.setattr(wh_tracker, "_last_seen", {})
        monkeypatch.setattr(wh_tracker, "_fetch_location", _fake_fetch_location_failing("down"))
        client = _authed_client()
        assert _shown(client.get("/intel/tracker/poll", params={"char": CHAR_A}).text) == ""
        assert _shown(client.get("/intel/tracker/poll", params={"char": ""}).text) == ""
    finally:
        teardown()


def test_page_sends_the_shown_key_and_explicit_ticks_clear_it():
    page = open("app/templates/wh_tracker.html").read()
    assert 'hx-include="#tracker-char, #wht-shown"' in page
    tick = page[page.index("function tick()"):page.index("dispatchEvent(new Event('tracker-tick'))")]
    assert "getElementById('wht-shown')" in tick and "shown.value = ''" in tick
    panel = open("app/templates/partials/wh_tracker_panel.html").read()
    assert 'hx-trigger="load, every 60s"' in panel   # the kill list stays live on its own
