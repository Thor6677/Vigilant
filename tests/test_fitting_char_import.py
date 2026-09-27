"""Tests for T-069: bulk-importing a character's in-game fits into Saved
Fits (POST /tools/fitting/import-character/{id}/save), plus the extended
GET .../fittings response and the user_fittings source-column migration.

Coverage:
1. The conversion gate — for the same raw ESI fit, the direct conversion
   path (_prepare_bulk_import_cache + _convert_esi_fit_items) must produce
   the same items as feeding that fit's EFT text through the existing
   POST /tools/fitting/import-eft parser. Compared as a Counter, not a set
   or an ordered list: a dropped duplicate turret would still pass a set
   comparison, and import-eft's item order follows _to_eft's section order
   (low/med/high/rig/subsystem/service/drone/fighter/cargo) rather than the
   resolved-slot order the direct path would naturally produce, so pinning
   order would fail on a technicality unrelated to correctness.
2. Bulk import creates the character root folder and one subfolder per ship
   class; a second run against the same fits imports 0 and skips all.
3. A fit already saved by the older one-at-a-time import (NULL source
   columns, matching ship_type_id + name) is skipped and not backlinked.
4. Isolation: an unresolvable ship or item type fails only that fit — the
   others in the same request still import. Validated before any DB write
   (see _validate_bulk_fit), so there is nothing to roll back.
5. Security: another user's character, a character missing the fittings
   scope, POST-body items being ignored (the server always re-fetches from
   ESI), and a missing CSRF token — each refused, and for the first two,
   ESI is never even called.
6. _ship_class_map: a direct market-group child, a walk-up through a
   grandchild, and the SDE-group-name / "Other" fallbacks.
7. The user_fittings migration: an old-shape table gets both columns and
   the index, and running it twice is a no-op.
8. Query-count evidence: importing 25 fits built from a small, reused set
   of distinct types costs roughly the same number of non-trivial queries
   as importing 5 — the batching claim, measured rather than eyeballed.
"""
import asyncio
import base64
import json
import re
import tempfile
from collections import Counter
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
from app.db.models import (
    Base, Character, User, UserFitting, UserFittingFolder, get_db,
    ensure_user_fittings_columns,
)
import app.routes.fitting as fitting_mod
import app.routes.fittings as fittings_mod
from app.routes.fitting import (
    _prepare_bulk_import_cache, _convert_esi_fit_items, _ship_class_map,
)
from app.routes.fittings import _parse_fitting, _to_eft

USER_A = 8701
USER_B = 8702
CHAR_A = 96700001   # belongs to USER_A
CHAR_B = 96700002   # belongs to USER_B
_CSRF = "test-csrf-token-charimport-0123456789"

# Ship types
SHIP_FRIGATE = 98001     # market_group_id -> Frigates (direct child of Ships)
SHIP_CRUISER = 98002     # market_group_id -> Cruisers via a grandchild hop
SHIP_NO_MARKET = 98003   # no market_group_id at all -> falls back to group name

# Modules / charges
TURRET = 98011     # high slot, has a compatible cargo charge
LOW_MOD = 98012    # low slot, no charges
CHARGE = 98021     # compatible ammo, sized to fit TURRET
DRONE = 98031      # category 18 (drone bay)
UNKNOWN_ITEM = 99999   # never inserted into sde_types at all
UNKNOWN_SHIP = 99998   # never inserted into sde_types at all

# Med-slot regression fixture (the "mid" vs "med" bug) — a mid-slot module
# with its own compatible cargo charge, sized/grouped distinctly from
# TURRET/CHARGE above so the two pairs can't cross-match by accident.
MED_MOD = 98013    # med slot, has a compatible cargo charge
BOOSTER = 98022    # compatible with MED_MOD only

# Market groups
MG_SHIPS_ROOT = 9000
MG_FRIGATES = 9001
MG_CRUISERS_LEAF = 9002   # grandchild — must walk up to find MG_CRUISERS
MG_CRUISERS = 9003


def _client():
    import app.main as main
    return TestClient(main.app, base_url="https://testserver")


def _authed_client(user_id):
    """Signed session cookie carrying user_id AND the csrf token the
    middleware checks the X-CSRF-Token header against (T-069's save
    endpoint is a POST like every other mutating route here)."""
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = _client()
    client.cookies.set("vigilant_session", cookie)
    return client


def _item(type_id, flag, qty=1):
    return {"type_id": type_id, "flag": flag, "quantity": qty}


async def _seed_base(db):
    """Ship classes, a turret + compatible cargo ammo, a low module, a
    drone — the small SDE slice every test below builds its fits from."""
    db.add_all([
        sm.SDEMarketGroup(market_group_id=MG_SHIPS_ROOT, parent_group_id=None,
                          market_group_name="Ships"),
        sm.SDEMarketGroup(market_group_id=MG_FRIGATES, parent_group_id=MG_SHIPS_ROOT,
                          market_group_name="Frigates"),
        sm.SDEMarketGroup(market_group_id=MG_CRUISERS, parent_group_id=MG_SHIPS_ROOT,
                          market_group_name="Cruisers"),
        sm.SDEMarketGroup(market_group_id=MG_CRUISERS_LEAF, parent_group_id=MG_CRUISERS,
                          market_group_name="Test T2 Cruiser Variant"),
    ])
    db.add_all([
        sm.SDEGroup(group_id=9600, category_id=6, group_name="Test Frigate Hulls"),
        sm.SDEGroup(group_id=9601, category_id=6, group_name="Test Misc Hulls"),
        sm.SDEGroup(group_id=9602, category_id=7, group_name="Test Modules"),
        sm.SDEGroup(group_id=9603, category_id=18, group_name="Test Drones"),
        sm.SDEGroup(group_id=9604, category_id=8, group_name="Test Charges"),
    ])
    db.add_all([
        sm.SDEType(type_id=SHIP_FRIGATE, type_name="Test Rifter", group_id=9600,
                   category_id=6, market_group_id=MG_FRIGATES, published=True),
        sm.SDEType(type_id=SHIP_CRUISER, type_name="Test Stabber", group_id=9600,
                   category_id=6, market_group_id=MG_CRUISERS_LEAF, published=True),
        sm.SDEType(type_id=SHIP_NO_MARKET, type_name="Test Unlisted Hull", group_id=9601,
                   category_id=6, market_group_id=None, published=True),
        sm.SDEType(type_id=TURRET, type_name="Test Autocannon", group_id=9602,
                   category_id=7, published=True),
        sm.SDEType(type_id=LOW_MOD, type_name="Test Damage Control", group_id=9602,
                   category_id=7, published=True),
        sm.SDEType(type_id=CHARGE, type_name="Test EMP Charge", group_id=9604,
                   category_id=8, published=True),
        sm.SDEType(type_id=DRONE, type_name="Test Hobgoblin", group_id=9603,
                   category_id=18, published=True),
    ])
    db.add_all([
        sm.SDEModuleSlot(type_id=TURRET, slot_type="high", is_turret=True, is_launcher=False),
        sm.SDEModuleSlot(type_id=LOW_MOD, slot_type="low", is_turret=False, is_launcher=False),
    ])
    # TURRET's chargeGroup1 (attr 604) points at CHARGE's group; chargeSize
    # (attr 128) matches on both sides.
    db.add_all([
        sm.SDETypeDogmaAttribute(type_id=TURRET, attribute_id=604, value=float(9604)),
        sm.SDETypeDogmaAttribute(type_id=TURRET, attribute_id=128, value=1.0),
        sm.SDETypeDogmaAttribute(type_id=CHARGE, attribute_id=128, value=1.0),
    ])


def _raw_fit(fitting_id, name, ship_type_id, items):
    return {"fitting_id": fitting_id, "name": name, "description": "",
            "ship_type_id": ship_type_id, "items": items}


def _fit_frigate(fitting_id=501, name="Test Frigate Fit"):
    return _raw_fit(fitting_id, name, SHIP_FRIGATE, [
        _item(TURRET, "HiSlot0"), _item(LOW_MOD, "LoSlot0"), _item(CHARGE, "Cargo", qty=10),
    ])


def _fit_cruiser(fitting_id=502, name="Test Cruiser Fit"):
    return _raw_fit(fitting_id, name, SHIP_CRUISER, [_item(DRONE, "DroneBay", qty=3)])


def _fit_bad_item(fitting_id=503, name="Test Bad Item Fit"):
    return _raw_fit(fitting_id, name, SHIP_FRIGATE, [_item(UNKNOWN_ITEM, "HiSlot0")])


def _fit_bad_ship(fitting_id=504, name="Test Bad Ship Fit"):
    return _raw_fit(fitting_id, name, UNKNOWN_SHIP, [_item(LOW_MOD, "LoSlot0")])


async def _seed_med_slot(db):
    """_seed_base plus a mid-slot module + its own compatible cargo charge
    — the fixture the "mid" vs "med" regression tests need."""
    await _seed_base(db)
    db.add(sm.SDEType(type_id=MED_MOD, type_name="Test Injector", group_id=9602,
                       category_id=7, published=True))
    db.add(sm.SDEType(type_id=BOOSTER, type_name="Test Booster", group_id=9604,
                       category_id=8, published=True))
    db.add(sm.SDEModuleSlot(type_id=MED_MOD, slot_type="med", is_turret=False, is_launcher=False))
    db.add_all([
        sm.SDETypeDogmaAttribute(type_id=MED_MOD, attribute_id=604, value=float(9604)),
        sm.SDETypeDogmaAttribute(type_id=MED_MOD, attribute_id=128, value=2.0),
        sm.SDETypeDogmaAttribute(type_id=BOOSTER, attribute_id=128, value=2.0),
    ])


def _seeded_db(seed_coro=_seed_base, extra_users=(USER_A, USER_B)):
    """Temp-file sqlite DB seeded by `seed_coro(db)` plus users/characters
    for both test accounts. Returns (teardown, SessionLocal, engine)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add_all([User(id=u) for u in extra_users])
            db.add(Character(
                character_id=CHAR_A, character_name="Test Pilot One", user_id=USER_A,
                access_token="a", refresh_token="r", token_expiry=datetime(2099, 1, 1),
                scopes="esi-fittings.read_fittings.v1",
            ))
            db.add(Character(
                character_id=CHAR_B, character_name="Test Pilot Two", user_id=USER_B,
                access_token="a", refresh_token="r", token_expiry=datetime(2099, 1, 1),
                scopes="esi-fittings.read_fittings.v1",
            ))
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
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(engine.dispose())
        finally:
            loop2.close()

    return teardown, SessionLocal, engine


def _run_async(SessionLocal, fn):
    """Run `fn(db)` (a coroutine function taking a fresh session) in its
    own event loop, returning its result."""
    async def _wrapped():
        async with SessionLocal() as db:
            return await fn(db)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakeESIClient:
    def __init__(self, *a, **kw):
        pass


def _mock_esi(monkeypatch, fits_by_char: dict, call_counts: dict | None = None):
    async def fake_refresh_token(char, db):
        if call_counts is not None:
            call_counts["refresh_token"] = call_counts.get("refresh_token", 0) + 1
        return "fake-token"

    async def fake_get_fittings(client, character_id):
        if call_counts is not None:
            call_counts["get_fittings"] = call_counts.get("get_fittings", 0) + 1
        return fits_by_char.get(character_id, [])

    monkeypatch.setattr(fitting_mod, "refresh_token", fake_refresh_token)
    monkeypatch.setattr(fitting_mod, "ESIClient", _FakeESIClient)
    monkeypatch.setattr(fitting_mod.esi_char, "get_fittings", fake_get_fittings)
    # fittings.py's own bound names — the character Fittings PAGE route
    # (fittings_list) is a separate module from the import-character routes
    # tested above, and each imported `refresh_token`/`ESIClient` into its
    # own globals, so patching fitting_mod's doesn't reach it.
    monkeypatch.setattr(fittings_mod, "refresh_token", fake_refresh_token)
    monkeypatch.setattr(fittings_mod, "ESIClient", _FakeESIClient)


def _item_key(item: dict) -> tuple:
    return tuple(sorted(item.items()))


# ── 1. Conversion gate ──────────────────────────────────────────────────────

def test_conversion_matches_import_eft_for_equivalent_fit():
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit = _fit_frigate()

        async def _direct(db):
            cache = await _prepare_bulk_import_cache(db, [fit])
            return _convert_esi_fit_items(fit["items"], cache)
        direct_items = _run_async(SessionLocal, _direct)

        type_names = {TURRET: "Test Autocannon", LOW_MOD: "Test Damage Control",
                      CHARGE: "Test EMP Charge"}
        parsed = _parse_fitting(fit, type_names, "Test Rifter", {})
        eft_text = _to_eft(parsed)

        client = _authed_client(USER_A)
        r = client.post("/tools/fitting/import-eft", json={"eft": eft_text},
                         headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body, body
        eft_items = body["items"]

        assert Counter(_item_key(i) for i in direct_items) == Counter(_item_key(i) for i in eft_items)
        # And the charge really did get auto-loaded onto the turret, not left
        # sitting in cargo as its own line — otherwise this gate would pass
        # trivially on two empty autoload results.
        turret_item = next(i for i in direct_items if i["type_id"] == TURRET)
        assert turret_item.get("charge_type_id") == CHARGE
        assert not any(i["type_id"] == CHARGE and i["slot"] == "cargo" for i in direct_items)
    finally:
        teardown()


# ── 1b. The "mid" vs "med" regression, pinned directly ──────────────────────
# The gate above can't pin this: both the direct conversion path and
# import-eft's own parser call the SAME _autoload_cargo_charges helper, so a
# reintroduced "mid" typo would make both sides wrong identically and the
# Counter comparison would still pass. These two hit import-eft only.

def test_import_eft_attaches_med_slot_charge_from_cargo():
    teardown, SessionLocal, _engine = _seeded_db(_seed_med_slot)
    try:
        client = _authed_client(USER_A)
        eft_text = "[Test Rifter, Test Injector Fit]\nTest Injector\n\nTest Booster"
        r = client.post("/tools/fitting/import-eft", json={"eft": eft_text},
                         headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body, body
        injector = next(i for i in body["items"] if i["type_id"] == MED_MOD)
        assert injector["slot"] == "med"
        assert injector.get("charge_type_id") == BOOSTER
        assert not any(i["type_id"] == BOOSTER and i["slot"] == "cargo" for i in body["items"])
    finally:
        teardown()


def test_import_eft_attaches_inline_med_slot_charge():
    teardown, SessionLocal, _engine = _seeded_db(_seed_med_slot)
    try:
        client = _authed_client(USER_A)
        eft_text = "[Test Rifter, Test Injector Fit]\nTest Injector, Test Booster"
        r = client.post("/tools/fitting/import-eft", json={"eft": eft_text},
                         headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body, body
        injector = next(i for i in body["items"] if i["type_id"] == MED_MOD)
        assert injector.get("charge_type_id") == BOOSTER
        assert injector.get("charge_name") == "Test Booster"
    finally:
        teardown()


# ── 2. Folders + idempotent re-run ──────────────────────────────────────────

def test_bulk_import_creates_character_and_class_folders(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit_a, fit_b = _fit_frigate(), _fit_cruiser()
        _mock_esi(monkeypatch, {CHAR_A: [fit_a, fit_b]})
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200
        body = r.json()
        assert body["imported"] == 2
        assert body["skipped"] == 0
        assert body["failed"] == []
        root_id = body["folder_id"]
        assert root_id is not None

        async def _check(db):
            root = (await db.execute(
                UserFittingFolder.__table__.select().where(UserFittingFolder.id == root_id)
            )).fetchone()
            assert root.name == "Test Pilot One · in-game"
            assert root.parent_id is None

            subs = (await db.execute(
                UserFittingFolder.__table__.select().where(UserFittingFolder.parent_id == root_id)
            )).fetchall()
            names = {s.name for s in subs}
            assert names == {"Frigates", "Cruisers"}

            fits = (await db.execute(
                UserFitting.__table__.select().where(UserFitting.user_id == USER_A)
            )).fetchall()
            assert len(fits) == 2
            for f in fits:
                assert f.source_character_id == CHAR_A
                assert f.source_fitting_id in (fit_a["fitting_id"], fit_b["fitting_id"])
                sub = next(s for s in subs if s.id == f.folder_id)
                expected = "Frigates" if f.ship_type_id == SHIP_FRIGATE else "Cruisers"
                assert sub.name == expected

        _run_async(SessionLocal, _check)

        # Second run against the same fits: nothing new, everything skipped,
        # same folder — no duplicate root or subfolders.
        r2 = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                          json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        body2 = r2.json()
        assert body2 == {"imported": 0, "skipped": 2, "failed": [], "folder_id": root_id}

        async def _count_folders(db):
            rows = (await db.execute(UserFittingFolder.__table__.select())).fetchall()
            return len(rows)
        assert _run_async(SessionLocal, _count_folders) == 3  # root + 2 class folders, not 6
    finally:
        teardown()


# ── 3. Hand-saved same-name fit is skipped, not linked ──────────────────────

def test_hand_saved_same_name_fit_is_skipped_and_not_linked(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit = _fit_frigate(name="Test Legacy Fit")
        _mock_esi(monkeypatch, {CHAR_A: [fit]})

        async def _preexist(db):
            db.add(UserFitting(
                user_id=USER_A, folder_id=None, name="Test Legacy Fit",
                ship_type_id=SHIP_FRIGATE, items_json="[]",
                source_character_id=None, source_fitting_id=None,
            ))
            await db.commit()
        _run_async(SessionLocal, _preexist)

        client = _authed_client(USER_A)
        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        body = r.json()
        assert body["imported"] == 0
        assert body["skipped"] == 1
        assert body["failed"] == []

        async def _check(db):
            rows = (await db.execute(
                UserFitting.__table__.select().where(UserFitting.user_id == USER_A)
            )).fetchall()
            assert len(rows) == 1   # no duplicate created
            assert rows[0].source_character_id is None   # not backlinked
            assert rows[0].source_fitting_id is None
        _run_async(SessionLocal, _check)
    finally:
        teardown()


# ── 4. Isolation ─────────────────────────────────────────────────────────────

def test_isolation_bad_type_fails_alone(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        good = _fit_frigate()
        bad_item = _fit_bad_item()
        bad_ship = _fit_bad_ship()
        _mock_esi(monkeypatch, {CHAR_A: [good, bad_item, bad_ship]})
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        body = r.json()
        assert body["imported"] == 1
        assert body["skipped"] == 0
        assert len(body["failed"]) == 2
        reasons = {f["name"]: f["reason"] for f in body["failed"]}
        assert str(UNKNOWN_ITEM) in reasons["Test Bad Item Fit"]
        assert str(UNKNOWN_SHIP) in reasons["Test Bad Ship Fit"]

        async def _check(db):
            rows = (await db.execute(
                UserFitting.__table__.select().where(UserFitting.user_id == USER_A)
            )).fetchall()
            assert len(rows) == 1
            assert rows[0].name == "Test Frigate Fit"
        _run_async(SessionLocal, _check)
    finally:
        teardown()


# ── 5. Security ──────────────────────────────────────────────────────────────

def test_other_users_character_is_refused(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        calls = {}
        _mock_esi(monkeypatch, {CHAR_A: [_fit_frigate()]}, calls)
        client = _authed_client(USER_B)   # not CHAR_A's owner

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        assert "error" in r.json()
        assert not calls   # ESI must never be touched for a character that isn't theirs
    finally:
        teardown()


def test_character_without_scope_is_refused(monkeypatch):
    async def _seed_noscope(db):
        await _seed_base(db)

    teardown, SessionLocal, _engine = _seeded_db(_seed_noscope)
    try:
        async def _strip_scope(db):
            r = (await db.execute(
                Character.__table__.select().where(Character.character_id == CHAR_A)
            )).fetchone()
            await db.execute(
                Character.__table__.update()
                .where(Character.character_id == CHAR_A)
                .values(scopes="")
            )
            await db.commit()
        _run_async(SessionLocal, _strip_scope)

        calls = {}
        _mock_esi(monkeypatch, {CHAR_A: [_fit_frigate()]}, calls)
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        assert "error" in r.json()
        assert not calls
    finally:
        teardown()


def test_items_in_request_body_are_ignored(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit = _fit_frigate()
        _mock_esi(monkeypatch, {CHAR_A: [fit]})
        client = _authed_client(USER_A)

        r = client.post(
            f"/tools/fitting/import-character/{CHAR_A}/save",
            json={
                "fitting_ids": [fit["fitting_id"]],
                # A crafted body trying to plant its own items/ship — must
                # be completely ignored; the server only ever re-fetches
                # from ESI and converts server-side.
                "items": [{"type_id": 1, "type_name": "Malicious", "slot": "high", "quantity": 999}],
                "ship_type_id": UNKNOWN_SHIP,
            },
            headers={"X-CSRF-Token": _CSRF},
        )
        body = r.json()
        assert body["imported"] == 1

        async def _check(db):
            rows = (await db.execute(
                UserFitting.__table__.select().where(UserFitting.user_id == USER_A)
            )).fetchall()
            assert len(rows) == 1
            assert rows[0].ship_type_id == SHIP_FRIGATE
            items = json.loads(rows[0].items_json)
            assert all(i["type_id"] != 1 for i in items)
        _run_async(SessionLocal, _check)
    finally:
        teardown()


def test_post_without_csrf_token_is_refused(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        calls = {}
        _mock_esi(monkeypatch, {CHAR_A: [_fit_frigate()]}, calls)

        import app.main as main
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        payload = {"user_id": USER_A, "csrf_token": _CSRF}
        cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
        client = TestClient(main.app, base_url="https://testserver")
        client.cookies.set("vigilant_session", cookie)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save", json={"all": True})
        assert r.status_code == 403
        assert not calls
    finally:
        teardown()


# ── 6. Ship class mapping ────────────────────────────────────────────────────

def test_ship_class_map_walks_up_and_falls_back():
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        async def _run(db):
            return await _ship_class_map(db, [SHIP_FRIGATE, SHIP_CRUISER, SHIP_NO_MARKET])
        result = _run_async(SessionLocal, _run)
        assert result[SHIP_FRIGATE] == "Frigates"          # direct child of Ships
        assert result[SHIP_CRUISER] == "Cruisers"           # walked up through a grandchild
        assert result[SHIP_NO_MARKET] == "Test Misc Hulls"  # no market group -> SDE group name
    finally:
        teardown()


# ── 7. Migration ─────────────────────────────────────────────────────────────

def test_user_fittings_migration_adds_columns_and_index_and_is_idempotent():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _make_old_shape():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE user_fittings ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, folder_id INTEGER, "
                "name TEXT NOT NULL, description TEXT, ship_type_id INTEGER NOT NULL, "
                "items_json TEXT NOT NULL DEFAULT '[]', implants_json TEXT NOT NULL DEFAULT '{}', "
                "boosters_json TEXT NOT NULL DEFAULT '{}', "
                "created_at DATETIME, updated_at DATETIME)"
            ))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_make_old_shape())

        async def _migrate_twice():
            async with SessionLocal() as db:
                await ensure_user_fittings_columns(db)
            async with SessionLocal() as db:
                await ensure_user_fittings_columns(db)  # must be a no-op

        loop.run_until_complete(_migrate_twice())

        async def _inspect():
            async with SessionLocal() as db:
                cols = {r[1] for r in (await db.execute(text("PRAGMA table_info(user_fittings)"))).fetchall()}
                idx = (await db.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_user_fittings_source'"
                ))).fetchone()
                return cols, idx
        cols, idx = loop.run_until_complete(_inspect())
        assert {"source_character_id", "source_fitting_id", "dps_cached", "dps_cache_key"} <= cols
        assert idx is not None
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()
        asyncio.set_event_loop(None)


# ── 8. Query-count evidence ──────────────────────────────────────────────────

def test_bulk_import_query_count_stays_flat_as_fit_count_grows(monkeypatch):
    """25 fits built from the SAME small set of distinct types must cost
    exactly the same number of non-INSERT statements a 5-fit import does —
    every SDE/skip-rule/folder lookup is batched once per request, not once
    per fit. INSERT count is excluded on purpose: one per imported fit is
    real, necessary write work, not something batching removes.
    """
    teardown, SessionLocal, engine = _seeded_db()
    try:
        def _fits(n):
            return [_fit_frigate(fitting_id=600 + i, name=f"Test Bulk Fit {i}") for i in range(n)]

        def _count_queries(n):
            fits = _fits(n)
            _mock_esi(monkeypatch, {CHAR_A: fits})
            client = _authed_client(USER_A)

            counts = {"total": 0, "non_insert": 0}
            def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                counts["total"] += 1
                if not statement.lstrip().upper().startswith("INSERT"):
                    counts["non_insert"] += 1
            event.listen(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
            try:
                r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                                 json={"all": True}, headers={"X-CSRF-Token": _CSRF})
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
            assert r.json()["imported"] == n

            async def _wipe(db):
                await db.execute(UserFitting.__table__.delete())
                await db.execute(UserFittingFolder.__table__.delete())
                await db.commit()
            _run_async(SessionLocal, _wipe)
            return counts

        c5 = _count_queries(5)
        c25 = _count_queries(25)
        c173 = _count_queries(173)
        assert c5["non_insert"] == c25["non_insert"] == c173["non_insert"]
    finally:
        teardown()


# ── 9. Page renders ──────────────────────────────────────────────────────────

def test_saved_fittings_page_shows_import_button_with_zero_fits():
    """The button is the entry point for a user with nothing saved yet —
    it must render outside the {% if rows %} block, not only once there's
    something in the list."""
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        client = _authed_client(USER_A)
        r = client.get("/tools/fitting/saved")
        assert r.status_code == 200
        html = r.text
        assert "Import from character" in html
        assert 'id="charimport-dialog"' in html
        assert "+ New Fit" in html
        assert not re.search(r'\bon[a-z]+\s*=\s*"', html)
    finally:
        teardown()


def test_fittings_page_shows_save_all_and_per_fit_saved_state(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit_a, fit_b = _fit_frigate(), _fit_cruiser()
        _mock_esi(monkeypatch, {CHAR_A: [fit_a, fit_b]})

        async def _preexist(db):
            db.add(UserFitting(
                user_id=USER_A, folder_id=None, name=fit_a["name"],
                ship_type_id=SHIP_FRIGATE, items_json="[]",
                source_character_id=CHAR_A, source_fitting_id=fit_a["fitting_id"],
            ))
            await db.commit()
        _run_async(SessionLocal, _preexist)

        client = _authed_client(USER_A)
        r = client.get(f"/character/{CHAR_A}/fittings")
        assert r.status_code == 200
        html = r.text
        assert "Save all to my fits" in html
        assert not re.search(r'\bon[a-z]+\s*=\s*"', html)

        idx_a = html.index(f'id="fit-{fit_a["fitting_id"]}"')
        idx_b = html.index(f'id="fit-{fit_b["fitting_id"]}"')
        block_a, block_b = (html[idx_a:idx_b], html[idx_b:]) if idx_a < idx_b else (html[idx_a:], html[idx_b:idx_a])
        assert "Saved" in block_a
        assert "Save to my fits" not in block_a
        assert "Save to my fits" in block_b
    finally:
        teardown()


# ── 10. Follow-up fixes: lazy folder, class order, ESI-named failures ──────

def test_all_fits_failing_creates_no_folder(monkeypatch):
    """A first-ever call where every fit is unresolvable must not leave an
    empty "<character> · in-game" folder behind."""
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        _mock_esi(monkeypatch, {CHAR_A: [_fit_bad_ship()]})
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        body = r.json()
        assert body["imported"] == 0
        assert body["folder_id"] is None

        async def _check(db):
            rows = (await db.execute(UserFittingFolder.__table__.select())).fetchall()
            assert rows == []
        _run_async(SessionLocal, _check)
    finally:
        teardown()


def test_ship_class_sort_key_orders_known_classes_then_alphabetical_then_other():
    from app.routes.fitting import _ship_class_sort_key
    names = ["Zzz Custom", "Cruisers", "Other", "Frigates", "Battleships", "Aaa Custom"]
    ordered = sorted(names, key=_ship_class_sort_key)
    assert ordered == ["Frigates", "Cruisers", "Battleships", "Aaa Custom", "Zzz Custom", "Other"]


def test_import_character_fittings_groups_in_canonical_class_order(monkeypatch):
    """ESI's own fit order has no relation to hull size — feed it scrambled
    (Cruisers fit first, then Frigates) and check the response comes back
    in canonical class order, so the checklist dialog (which just groups by
    array order) doesn't show "Other"/an alphabetical class ahead of the
    well-known ones."""
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        fit_cruiser = _fit_cruiser()
        fit_frigate = _fit_frigate()
        fit_misc = _raw_fit(505, "Test Misc Hull Fit", SHIP_NO_MARKET, [])
        _mock_esi(monkeypatch, {CHAR_A: [fit_cruiser, fit_frigate, fit_misc]})
        client = _authed_client(USER_A)

        r = client.get(f"/tools/fitting/import-character/{CHAR_A}/fittings")
        body = r.json()
        classes_in_order = [f["ship_class"] for f in body["fittings"]]
        assert classes_in_order == ["Frigates", "Cruisers", "Test Misc Hulls"]
    finally:
        teardown()


def test_unresolvable_fit_reasons_use_esi_names_with_404_retry(monkeypatch):
    """/universe/names/ 404s the WHOLE batch if even one id in it is
    entirely unknown to ESI (per app/routes/character_detail.py's
    _sender_names) — the fixture here mimics exactly that: the bulk call
    (both unknown ids) 404s, and only the per-id retry finds the one ESI
    actually can name."""
    import httpx as _httpx

    teardown, SessionLocal, _engine = _seeded_db()
    try:
        bad_item = _fit_bad_item()
        bad_ship = _fit_bad_ship()
        _mock_esi(monkeypatch, {CHAR_A: [bad_item, bad_ship]})

        class _NamesClient:
            def __init__(self, *a, **kw):
                pass

            async def post_public(self, path, body):
                if len(body) > 1:
                    req = _httpx.Request("POST", "https://esi.evetech.net" + path)
                    resp = _httpx.Response(404, request=req)
                    raise _httpx.HTTPStatusError("404", request=req, response=resp)
                tid = body[0]
                if tid == UNKNOWN_ITEM:
                    return [{"id": tid, "name": "Test Removed Module"}]
                return []   # ESI genuinely can't name UNKNOWN_SHIP either

        monkeypatch.setattr(fitting_mod, "ESIClient", _NamesClient)
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        reasons = {f["name"]: f["reason"] for f in r.json()["failed"]}
        assert reasons["Test Bad Item Fit"] == "Uses items no longer in the game: Test Removed Module"
        assert reasons["Test Bad Ship Fit"] == f"Hull not in game data: type {UNKNOWN_SHIP}"
    finally:
        teardown()


def test_unresolvable_fit_reasons_fall_back_when_names_call_fails(monkeypatch):
    teardown, SessionLocal, _engine = _seeded_db()
    try:
        bad_item = _fit_bad_item()
        _mock_esi(monkeypatch, {CHAR_A: [bad_item]})

        class _FailingNamesClient:
            def __init__(self, *a, **kw):
                pass

            async def post_public(self, path, body):
                raise RuntimeError("ESI unavailable")

        monkeypatch.setattr(fitting_mod, "ESIClient", _FailingNamesClient)
        client = _authed_client(USER_A)

        r = client.post(f"/tools/fitting/import-character/{CHAR_A}/save",
                         json={"all": True}, headers={"X-CSRF-Token": _CSRF})
        reasons = {f["name"]: f["reason"] for f in r.json()["failed"]}
        assert reasons["Test Bad Item Fit"] == f"Uses items no longer in the game: type {UNKNOWN_ITEM}"
    finally:
        teardown()
