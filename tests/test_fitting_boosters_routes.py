"""Tests for the routes/persistence half of combat boosters (T-049).

`get_booster_info` (app/fitting/boosters.py) is now a real lookup against
the SDE -- the engine side of the T-049 contract landed separately. Most
tests here still monkeypatch it on `app.routes.fitting` (where it's
imported) to pin the routes' own behavior -- shape-mapping, count, error
tolerance -- independent of live SDE data, matching the idiom
tests/test_fitting_compare.py uses for `calculate_fitting_stats`. One test
(search -> save -> load) instead runs it for real against the small SDE
fixture tests/test_fitting_boosters_engine.py builds for the engine side,
so the seam isn't proven only through mocks.

Covers: the `_sanitize_boosters_map` / `_clean_side_effects` /
`_booster_entries` / `_bounded_int` helpers (including malformed and
oversized/overflowing input), the save/load round trip, the search
endpoint's response shape, the stats route's validation of the incoming
`boosters` list, and the compare view's booster-count header.
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
from app.routes.fitting import (
    _sanitize_boosters_map, _clean_side_effects, _booster_entries, _bounded_int,
    MAX_BOOSTERS_PER_REQUEST,
)

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


def test_clean_side_effects_drops_overflowing_values():
    # A JSON literal like 1e400 parses (via json.loads) to the float inf --
    # int(inf) raises OverflowError, not ValueError/TypeError. A merely huge
    # -- but finite -- integer literal converts cleanly and must still be
    # rejected by the range check, not just the exception tuple.
    out = _clean_side_effects([1, float("inf"), float("-inf"), 2, 10**30])
    assert out == [1, 2]


# ── _bounded_int ─────────────────────────────────────────────────────────────

def test_bounded_int_accepts_plain_positive_ints():
    assert _bounded_int(5) == 5
    assert _bounded_int("5") == 5


def test_bounded_int_rejects_non_numeric():
    assert _bounded_int("nope") is None
    assert _bounded_int(None) is None
    assert _bounded_int({"a": 1}) is None


def test_bounded_int_rejects_zero_and_negative():
    assert _bounded_int(0) is None
    assert _bounded_int(-5) is None


def test_bounded_int_rejects_float_overflow():
    # json.loads("1e400") -> the float inf, before this ever sees it.
    assert _bounded_int(float("inf")) is None
    assert _bounded_int(float("-inf")) is None


def test_bounded_int_rejects_a_value_too_big_for_sqlite():
    # Converts cleanly (Python ints are arbitrary precision) but would
    # overflow SQLite's 64-bit INTEGER bind -- must be caught by the range
    # check, not the exception tuple.
    assert _bounded_int(99999999999999999999999) is None
    assert _bounded_int(2 ** 31) is None
    assert _bounded_int(2 ** 31 - 1) == 2 ** 31 - 1


def test_bounded_int_respects_a_custom_upper_bound():
    assert _bounded_int(1001, upper=1001) is None
    assert _bounded_int(1000, upper=1001) == 1000


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


def test_sanitize_boosters_map_drops_overflowing_type_id():
    """Reproduces the reported crash: a huge JSON integer literal converts
    fine in Python but overflows SQLite's 64-bit column, so every later
    load of a fit that stored it died inside get_booster_info. The bound
    has to catch it at save time, before it ever reaches storage."""
    raw = {"1": {"type_id": 99999999999999999999999, "name": "too big"}}
    assert _sanitize_boosters_map(raw) == {}


def test_sanitize_boosters_map_drops_float_overflow_type_id():
    # json.loads on a "1e400"-shaped literal hands this function the float
    # inf for type_id -- int(inf) raises OverflowError, which used to
    # propagate as an unhandled 500.
    raw = {"1": {"type_id": float("inf"), "name": "inf"}}
    assert _sanitize_boosters_map(raw) == {}


def test_sanitize_boosters_map_caps_entry_count():
    # Same cap as the stats route's request list -- a saved fit can't carry
    # thousands of slots into compare or load either.
    raw = {str(i): {"type_id": i, "name": f"Booster {i}"} for i in range(1, 80)}
    out = _sanitize_boosters_map(raw)
    assert len(out) == MAX_BOOSTERS_PER_REQUEST


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
        # No SDE data is seeded in this temp DB, so the real get_booster_info
        # finds no boosterness row for 44502 and returns nothing for it --
        # the route still has to return the key so the builder's merge step
        # has something to call. test_search_then_save_then_load_round_trips_real_booster_info
        # below covers the case where get_booster_info actually finds it.
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


def test_stats_route_survives_boosters_that_are_not_a_list():
    """`{"boosters": 5}` used to raise TypeError when the handler sliced it
    (`boosters_raw[:MAX_BOOSTERS_PER_REQUEST]`) -- a bare int, and any other
    non-list JSON value, must be treated as "no boosters" instead of a
    server error."""
    teardown = _seeded_db()
    try:
        for junk in (5, {"a": 1}, "just a string", True):
            r = _csrf_client().post(
                "/tools/fitting/stats",
                json={"ship_type_id": 587, "items": [], "boosters": junk},
                headers={"X-CSRF-Token": _CSRF})
            assert r.status_code == 200, (junk, r.text)
    finally:
        teardown()


def test_stats_route_survives_overflowing_booster_ints(monkeypatch):
    """The three crash inputs from the original report, all in one request:
    a type_id that's a float-overflow (from a JSON literal like 1e400), a
    side_effects entry with the same problem, and a type_id too big for
    SQLite's 64-bit column but otherwise a perfectly valid Python int. None
    of them should reach the engine; none should 500.

    The body is written out by hand rather than json.dumps'd from a Python
    dict: a literal ``1e400`` is perfectly standard JSON text (just a huge
    exponent) that `json.loads` parses to the float `inf` on the SERVER
    side -- but a Python float that is already `inf` on the CLIENT side
    can't round-trip through json.dumps at all (it raises ValueError
    before the request is even sent), so building the dict first and
    dumping it would test the wrong thing.
    """
    captured = {}
    orig = fitting_mod.calculate_fitting_stats

    async def spy(*args, **kwargs):
        captured["boosters"] = kwargs.get("boosters")
        return await orig(*args, **kwargs)
    monkeypatch.setattr(fitting_mod, "calculate_fitting_stats", spy)

    teardown = _seeded_db()
    try:
        raw = (
            '{"ship_type_id": 587, "items": [], "boosters": ['
            '{"type_id": 1e400, "side_effects": []},'
            '{"type_id": 44502, "side_effects": [1e400, 2]},'
            '{"type_id": 99999999999999999999999, "side_effects": []}'
            ']}'
        )
        r = _csrf_client().post(
            "/tools/fitting/stats", content=raw.encode("utf-8"),
            headers={"X-CSRF-Token": _CSRF, "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
    finally:
        teardown()

    assert captured["boosters"] == [{"type_id": 44502, "side_effects": [2]}]


# ── compare view header ───────────────────────────────────────────────────────

def _seeded_compare_db(fit_a_boosters_json, extra_sde_rows=()):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add_all(extra_sde_rows)
            mine_1 = UserFitting(
                user_id=USER_A, name="Alpha", ship_type_id=587, items_json="[]",
                boosters_json=json.dumps(fit_a_boosters_json),
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
    """Both saved type_ids are real boosters (their own boosterness row,
    slots 5 and 9) -- the count matches the two valid map slots. Slot 1001
    is out of range and dropped by the sanitizer before it's even a
    candidate."""
    (mine_1, mine_2), teardown = _seeded_compare_db(
        {
            "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": [900]},
            "9": {"type_id": 44510, "name": "Crash", "side_effects": []},
            "1001": {"type_id": 1, "name": "out of range"},
        },
        extra_sde_rows=[
            sm.SDEType(type_id=44502, type_name="Blue Pill Booster", group_id=1136, published=True),
            sm.SDETypeDogmaAttribute(type_id=44502, attribute_id=1087, value=5.0),
            sm.SDEType(type_id=44510, type_name="Crash", group_id=1136, published=True),
            sm.SDETypeDogmaAttribute(type_id=44510, attribute_id=1087, value=9.0),
        ],
    )
    try:
        r = _authed_client(USER_A).get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
        assert "boosters as saved (2 vs 0)" in r.text
        assert "implants as saved (0 vs 0)" in r.text
    finally:
        teardown()


def test_compare_booster_count_ignores_a_type_id_that_isnt_a_real_booster():
    """A map entry surviving JSON sanitization only proves it's a
    well-formed {type_id, name, side_effects} record -- not that the
    type_id is actually a booster. apply_booster_bonuses drops anything
    without a boosterness row, so the displayed count must too."""
    (mine_1, mine_2), teardown = _seeded_compare_db(
        {"5": {"type_id": 34, "name": "Tritanium is not a booster", "side_effects": []}},
        extra_sde_rows=[sm.SDEType(type_id=34, type_name="Tritanium", group_id=18, published=True)],
    )
    try:
        r = _authed_client(USER_A).get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
        assert "boosters as saved (0 vs 0)" in r.text
    finally:
        teardown()


def test_compare_booster_count_collapses_two_map_slots_sharing_a_real_slot():
    """Two different type_ids stored under two different MAP keys, but
    which share the same real boosterness -- the engine will only ever
    apply one of them (first entry wins per real slot), so the count must
    say 1, not 2."""
    (mine_1, mine_2), teardown = _seeded_compare_db(
        {
            "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": []},
            "999": {"type_id": 44503, "name": "Also boosterness 5", "side_effects": []},
        },
        extra_sde_rows=[
            sm.SDEType(type_id=44502, type_name="Blue Pill Booster", group_id=1136, published=True),
            sm.SDETypeDogmaAttribute(type_id=44502, attribute_id=1087, value=5.0),
            sm.SDEType(type_id=44503, type_name="Also boosterness 5", group_id=1136, published=True),
            sm.SDETypeDogmaAttribute(type_id=44503, attribute_id=1087, value=5.0),
        ],
    )
    try:
        r = _authed_client(USER_A).get(f"/tools/fitting/compare?a={mine_1}&b={mine_2}")
        assert r.status_code == 200
        assert "boosters as saved (1 vs 0)" in r.text
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

    (mine_1, mine_2), teardown = _seeded_compare_db({
        "5": {"type_id": 44502, "name": "Blue Pill Booster", "side_effects": [900]},
        "9": {"type_id": 44510, "name": "Crash", "side_effects": []},
    })
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


# ── real get_booster_info, no monkeypatch ───────────────────────────────────

def test_search_then_save_then_load_round_trips_real_booster_info():
    """Every other test above monkeypatches get_booster_info to pin the
    routes' own behavior. This one runs it for real, against the same SDE
    fixture tests/test_fitting_boosters_engine.py builds for the engine
    side (Blue Pill: boosterness 1, two side effects), so the seam between
    the routes and the actual engine lookup is proven at least once with
    real data: search finds it with its real slot and side-effect catalog,
    and saving + loading a fit that plugs it in returns that same catalog
    through `booster_info`.
    """
    from tests.test_fitting_boosters_engine import (
        BLUE_PILL, EFF_SHIELD_CAPACITY_PENALTY, EFF_TURRET_OPTIMAL_PENALTY, _rows,
    )

    async def _seed(db):
        db.add_all(_rows())

    teardown = _seeded_db(_seed)
    try:
        r = _client().get("/tools/fitting/search/boosters", params={"q": "Blue Pill"})
        assert r.status_code == 200
        expected_side_effects = [
            {"effect_id": EFF_SHIELD_CAPACITY_PENALTY,
             "label": "Shield capacity -30%", "chance": 0.4},
            {"effect_id": EFF_TURRET_OPTIMAL_PENALTY,
             "label": "Turret optimal range -30%", "chance": 0.3},
        ]
        assert r.json() == [{
            "type_id": BLUE_PILL, "name": "Test Blue Pill Booster", "slot": 1,
            "side_effects": expected_side_effects,
        }]

        client = _authed_client(USER_A)
        save = client.post("/tools/fitting/save", json={
            "ship_type_id": 587, "name": "Real Booster Fit", "items": [],
            "boosters_map": {
                "1": {"type_id": BLUE_PILL, "name": "Test Blue Pill Booster",
                      "side_effects": [EFF_SHIELD_CAPACITY_PENALTY]},
            },
        }, headers={"X-CSRF-Token": _CSRF})
        assert save.status_code == 200, save.text
        fitting_id = save.json()["id"]

        loaded = client.get(f"/tools/fitting/load/{fitting_id}").json()
        assert loaded["boosters"] == {
            "1": {"type_id": BLUE_PILL, "name": "Test Blue Pill Booster",
                  "side_effects": [EFF_SHIELD_CAPACITY_PENALTY]},
        }
        assert loaded["booster_info"] == {
            str(BLUE_PILL): {
                "type_id": BLUE_PILL, "name": "Test Blue Pill Booster", "slot": 1,
                "side_effects": expected_side_effects,
            },
        }
    finally:
        teardown()
