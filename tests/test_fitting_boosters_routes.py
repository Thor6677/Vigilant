"""Tests for the routes/persistence half of combat boosters (T-049).

`get_booster_info` and `apply_booster_bonuses` are STUBS on this branch
(app/fitting/boosters.py) -- the engine side of the T-049 contract fills
them in separately, and until it does `get_booster_info` always returns
`{}`. Tests that need booster metadata monkeypatch it on
`app.routes.fitting` (where it's imported), matching the idiom
tests/test_fitting_compare.py uses for `calculate_fitting_stats`.

Covers: the `_sanitize_boosters_map` / `_clean_side_effects` /
`_booster_entries` helpers, the save/load round trip, the search endpoint's
response shape, the stats route's validation of the incoming `boosters`
list, and the compare view's booster-count header.
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
from app.db.models import Base, UserFitting, get_db
import app.routes.fitting as fitting_mod
from app.routes.fitting import _sanitize_boosters_map, _clean_side_effects, _booster_entries

USER_A = 8301
USER_B = 8302
_CSRF = "test-csrf-token-boosters-0123456789"


def _client():
    import app.main as main
    return TestClient(main.app)


def _authed_client(user_id=USER_A):
    """Signed session cookie carrying user_id AND the csrf token the
    middleware checks the X-CSRF-Token header against."""
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _csrf_client():
    """Signed session carrying just the csrf token, no user_id -- the stats
    route needs no login, but every POST still needs the CSRF header."""
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _seeded_db(seed_coro=None):
    """Temp-file sqlite DB, optionally seeded by `seed_coro(db)`; returns
    teardown()."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if seed_coro:
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


# ── _clean_side_effects ─────────────────────────────────────────────────────

def test_clean_side_effects_dedupes_drops_junk_and_caps():
    raw = [1, "2", 2, 3, "junk", None, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
           15, 16, 17, 18, 19, 20, 21, 22]
    out = _clean_side_effects(raw)
    # 1..20 survive in order (dupes/junk dropped); 21/22 never reached once
    # the cap is hit.
    assert out == list(range(1, 21))


def test_clean_side_effects_rejects_non_list():
    assert _clean_side_effects("not a list") == []
    assert _clean_side_effects(None) == []
    assert _clean_side_effects(42) == []


# ── _sanitize_boosters_map ───────────────────────────────────────────────────

def test_sanitize_boosters_map_rejects_non_dict():
    assert _sanitize_boosters_map(None) == {}
    assert _sanitize_boosters_map([1, 2, 3]) == {}
    assert _sanitize_boosters_map("nope") == {}


def test_sanitize_boosters_map_drops_malformed_entries_and_cleans_the_rest():
    raw = {
        "5": {"type_id": 44502, "name": "Blue Pill Booster",
              "side_effects": [900, "901", 900, "junk"]},
        "0": {"type_id": 1, "name": "slot too low"},
        "1001": {"type_id": 1, "name": "slot too high"},
        "abc": {"type_id": 1, "name": "non-numeric slot"},
        "6": "not a dict",
        "7": {"name": "no type_id"},
        "8": {"type_id": "nope"},
    }
    out = _sanitize_boosters_map(raw)
    assert out == {
        "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": [900, 901]},
    }


def test_sanitize_boosters_map_accepts_generous_slot_range():
    # boosterness runs 1..~500+ in the SDE, unlike implantness's 1-10 --
    # a slot like 487 (event booster territory) must survive.
    raw = {"487": {"type_id": 99, "name": "Event Booster"}}
    out = _sanitize_boosters_map(raw)
    assert list(out.keys()) == ["487"]
    assert out["487"]["side_effects"] == []


def test_sanitize_boosters_map_truncates_long_names():
    raw = {"1": {"type_id": 1, "name": "x" * 500}}
    out = _sanitize_boosters_map(raw)
    assert len(out["1"]["name"]) == 128


def test_sanitize_boosters_map_defaults_missing_name():
    raw = {"1": {"type_id": 777}}
    out = _sanitize_boosters_map(raw)
    assert out["1"]["name"] == "Type 777"


# ── _booster_entries ─────────────────────────────────────────────────────────

def test_booster_entries_builds_engine_shape():
    boosters_map = {
        "5": {"type_id": 111, "name": "A", "side_effects": [1, 2]},
        "9": {"type_id": 222, "name": "B", "side_effects": []},
    }
    entries = _booster_entries(boosters_map)
    assert sorted(entries, key=lambda e: e["type_id"]) == [
        {"type_id": 111, "side_effects": [1, 2]},
        {"type_id": 222, "side_effects": []},
    ]


# ── save/load round trip ─────────────────────────────────────────────────────

def test_save_then_load_round_trips_sanitized_boosters():
    teardown = _seeded_db()
    try:
        client = _authed_client(USER_A)
        body = {
            "ship_type_id": 587,
            "name": "Booster Test Fit",
            "items": [],
            "boosters_map": {
                "5": {"type_id": 44502, "name": "Blue Pill Booster",
                      "side_effects": [900, "901", 900]},
                "0": {"type_id": 1, "name": "invalid slot dropped on save"},
            },
        }
        r = client.post("/tools/fitting/save", json=body, headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200, r.text
        fitting_id = r.json()["id"]

        r = client.get(f"/tools/fitting/load/{fitting_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["boosters"] == {
            "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": [900, 901]},
        }
        # get_booster_info is the T-049 engine stub here (always {}) -- the
        # route still has to return the key so the builder's merge step has
        # something to call, even though it's empty until the engine lands.
        assert data["booster_info"] == {}
    finally:
        teardown()


def test_save_update_path_also_persists_boosters():
    """The update branch (fitting_id present) must write boosters_json too,
    not just the insert branch -- mirrors the implants_json coverage gap
    this guards against."""
    teardown = _seeded_db()
    try:
        client = _authed_client(USER_A)
        create = client.post("/tools/fitting/save", json={
            "ship_type_id": 587, "name": "Fit", "items": [], "boosters_map": {},
        }, headers={"X-CSRF-Token": _CSRF})
        fitting_id = create.json()["id"]

        update = client.post("/tools/fitting/save", json={
            "ship_type_id": 587, "name": "Fit", "items": [],
            "fitting_id": fitting_id,
            "boosters_map": {"9": {"type_id": 55, "name": "Crash", "side_effects": [1]}},
        }, headers={"X-CSRF-Token": _CSRF})
        assert update.json()["status"] == "updated"

        loaded = client.get(f"/tools/fitting/load/{fitting_id}").json()
        assert loaded["boosters"] == {
            "9": {"type_id": 55, "name": "Crash", "side_effects": [1]},
        }
    finally:
        teardown()


# ── search endpoint ───────────────────────────────────────────────────────────

async def _seed_booster_type(db):
    # A real booster: published, boosterness (attr 1087) = slot 6.
    db.add(sm.SDEType(type_id=44502, type_name="Blue Pill Booster",
                       group_id=1136, published=True))
    db.add(sm.SDETypeDogmaAttribute(type_id=44502, attribute_id=1087, value=6.0))
    # A published type with NO boosterness row at all -- must never match,
    # even though its name would otherwise satisfy a query.
    db.add(sm.SDEType(type_id=34, type_name="Tritanium", group_id=18, published=True))


def test_search_boosters_enriches_via_get_booster_info(monkeypatch):
    async def fake_get_booster_info(db, type_ids):
        assert type_ids == [44502]
        return {44502: {
            "type_id": 44502, "name": "Blue Pill Booster", "slot": 6,
            "side_effects": [{"effect_id": 900, "label": "Aggression", "chance": 0.1}],
        }}
    monkeypatch.setattr(fitting_mod, "get_booster_info", fake_get_booster_info)

    teardown = _seeded_db(_seed_booster_type)
    try:
        r = _client().get("/tools/fitting/search/boosters?q=blue")
        assert r.status_code == 200
        assert r.json() == [{
            "type_id": 44502, "name": "Blue Pill Booster", "slot": 6,
            "side_effects": [{"effect_id": 900, "label": "Aggression", "chance": 0.1}],
        }]
    finally:
        teardown()


def test_search_boosters_excludes_types_without_boosterness_row(monkeypatch):
    calls = []

    async def fake_get_booster_info(db, type_ids):
        calls.append(type_ids)
        return {}
    monkeypatch.setattr(fitting_mod, "get_booster_info", fake_get_booster_info)

    teardown = _seeded_db(_seed_booster_type)
    try:
        r = _client().get("/tools/fitting/search/boosters?q=tritanium")
        assert r.status_code == 200 and r.json() == []
        # No candidate type ids from the SDE join -- get_booster_info is
        # never even called.
        assert calls == []
    finally:
        teardown()


def test_search_boosters_drops_ids_missing_from_engine_lookup(monkeypatch):
    """A type with a boosterness row but absent from get_booster_info's
    result (the real stub's permanent behavior on this branch) yields no
    result rather than a KeyError."""
    async def fake_get_booster_info(db, type_ids):
        return {}
    monkeypatch.setattr(fitting_mod, "get_booster_info", fake_get_booster_info)

    teardown = _seeded_db(_seed_booster_type)
    try:
        r = _client().get("/tools/fitting/search/boosters?q=blue")
        assert r.status_code == 200 and r.json() == []
    finally:
        teardown()


# ── stats route ───────────────────────────────────────────────────────────────

def test_stats_route_cleans_and_forwards_boosters(monkeypatch):
    """POST /tools/fitting/stats must validate the boosters list to ints,
    dedupe/cap each entry's side_effects, and forward the cleaned list to
    the engine's boosters= kwarg -- the shape app/fitting/boosters.py
    documents. Spies on the real calculate_fitting_stats (rather than
    replacing it) so the response still renders through the real stats
    partial with a real all-zero-attrs dict for an unseeded ship."""
    captured = {}
    orig = fitting_mod.calculate_fitting_stats

    async def spy(*args, **kwargs):
        captured["boosters"] = kwargs.get("boosters")
        return await orig(*args, **kwargs)
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", spy)

    teardown = _seeded_db()
    try:
        body = {
            "ship_type_id": 587,
            "items": [],
            "boosters": [
                {"type_id": 44502, "side_effects": [900, "901", 900, "junk"]},
                {"type_id": "not-an-int", "side_effects": []},  # dropped
                "garbage",                                       # dropped
            ],
        }
        r = _csrf_client().post(
            "/tools/fitting/stats", json=body, headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200, r.text
    finally:
        teardown()

    assert captured["boosters"] == [{"type_id": 44502, "side_effects": [900, 901]}]


def test_stats_route_caps_booster_count(monkeypatch):
    captured = {}
    orig = fitting_mod.calculate_fitting_stats

    async def spy(*args, **kwargs):
        captured["boosters"] = kwargs.get("boosters")
        return await orig(*args, **kwargs)
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", spy)

    teardown = _seeded_db()
    try:
        boosters = [{"type_id": i, "side_effects": []} for i in range(1, 80)]
        r = _csrf_client().post(
            "/tools/fitting/stats",
            json={"ship_type_id": 587, "items": [], "boosters": boosters},
            headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200, r.text
    finally:
        teardown()

    assert len(captured["boosters"]) == fitting_mod.MAX_BOOSTERS_PER_REQUEST


def test_stats_route_defaults_to_no_boosters(monkeypatch):
    """A request with no `boosters` key at all (every stats call before
    this feature shipped) must still work, passing an empty list through."""
    captured = {}
    orig = fitting_mod.calculate_fitting_stats

    async def spy(*args, **kwargs):
        captured["boosters"] = kwargs.get("boosters")
        return await orig(*args, **kwargs)
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", spy)

    teardown = _seeded_db()
    try:
        r = _csrf_client().post(
            "/tools/fitting/stats",
            json={"ship_type_id": 587, "items": []},
            headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200, r.text
    finally:
        teardown()

    assert captured["boosters"] == []


# ── compare view header ───────────────────────────────────────────────────────

def _seeded_compare_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            mine_1 = UserFitting(
                user_id=USER_A, name="Alpha", ship_type_id=587, items_json="[]",
                boosters_json=json.dumps({
                    "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": [900]},
                    "9": {"type_id": 44510, "name": "Crash", "side_effects": []},
                    "1001": {"type_id": 1, "name": "out of range"},
                }),
            )
            mine_2 = UserFitting(
                user_id=USER_A, name="Bravo", ship_type_id=602, items_json="[]",
            )
            db.add_all([mine_1, mine_2])
            await db.commit()
            return mine_1.id, mine_2.id

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ids = loop.run_until_complete(seed())
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

    return ids, teardown


def test_compare_header_shows_booster_counts():
    (mine_1, mine_2), teardown = _seeded_compare_db()
    try:
        r = _authed_client(USER_A).get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
        # Slot 1001 is out of range and dropped by the sanitizer, so only
        # the two valid slots (5, 9) count.
        assert "boosters as saved (2 vs 0)" in r.text
        assert "implants as saved (0 vs 0)" in r.text
    finally:
        teardown()


def test_compare_feeds_each_fits_saved_boosters_to_the_engine(monkeypatch):
    """Each side's sanitised booster entries must reach the same
    calculate_fitting_stats call the builder makes, in the engine's
    {type_id, side_effects} shape."""
    seen = []

    async def fake_stats(db, ship_type_id, items, *args, **kwargs):
        seen.append((ship_type_id, kwargs.get("boosters")))
        return {}
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", fake_stats)
    monkeypatch.setattr(fitting_mod, "build_compare_sections", lambda a, b: [])

    (mine_1, mine_2), teardown = _seeded_compare_db()
    try:
        r = _authed_client(USER_A).get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
    finally:
        teardown()

    by_ship = {ship: boosters for ship, boosters in seen}
    assert sorted(by_ship[587], key=lambda e: e["type_id"]) == [
        {"type_id": 44502, "side_effects": [900]},
        {"type_id": 44510, "side_effects": []},
    ]
    assert by_ship[602] == []
