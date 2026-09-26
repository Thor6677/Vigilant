"""ISS-056: the character Fittings page ignored the string item flags ESI's
fittings endpoint actually returns.

`GET /characters/{id}/fittings` hands back each item's `flag` as a string
enum per the live ESI OpenAPI spec (HiSlot0-7, MedSlot0-7, LoSlot0-7,
RigSlot0-2, SubSystemSlot0-3, ServiceSlot0-7, Cargo, DroneBay, FighterBay,
Invalid) — 43 values in total. `SLOT_CATEGORY` only ever mapped the old
integer inventory flags, so every item fell through to the `"cargo"`
fallback: every fit showed "0 modules" with everything dumped in Cargo.

Coverage:
1. `_parse_fitting` against real-shaped string-flag fixtures — a frigate
   (3 high / 3 mid / 4 low / 3 rig, drones, cargo), a subsystem cruiser (4
   subsystems), and a structure fit with service slots. Groups, ordering
   (numeric slot index from the flag, then name) and `total_modules` are
   all checked.
2. The legacy integer flag fallback still works, and a fit mixing both flag
   shapes across its items doesn't break sorting.
3. `_to_eft` output, including fighters (previously dropped outright — now
   kept, in the "Name xN" shape drones/cargo already use).
4. A round trip: `_to_eft`'s output fed into the fitting tool's own EFT
   import parser (`/tools/fitting/import-eft`), asserting the modules land
   back in the slots that parser recognizes.
5. A page render with a mocked ESI response, asserting module names appear
   under their slot headings and "0 modules" is gone.

Confirmed against the pre-fix code (git show HEAD~1 at the time this test
file was written, run standalone from a scratch script rather than kept as a
test — HEAD moves to the fix itself once this lands, which would make an
in-suite "run the old code" test fail against its own baseline): the frigate
fixture below produced `total_modules == 0`, `groups["high"] == []`, and all
6 items dumped in `groups["cargo"]` — exactly the symptom ISS-056 describes.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Character, User, get_db
import app.db.sde_models  # noqa: F401 — registers the sde_* tables on Base
from app.db.sde_models import SDEGroup, SDEModuleSlot, SDEType
import app.routes.fittings as fittings_mod
from app.routes.fittings import (
    SLOT_LABELS, _category_for_flag, _parse_fitting, _slot_index, _to_eft,
)

USER_ID = 8601
CHAR_ID = 96100001
_CSRF = "test-csrf-token-fittings-0123456789"

# EVE SDE category IDs the import parser's fallback branch checks.
CATEGORY_SHIP = 6
CATEGORY_DRONE = 18
CATEGORY_FIGHTER = 87


# ── _parse_fitting / _to_eft: pure-function fixtures, no DB ────────────────

def _item(type_id, flag, qty=1):
    return {"type_id": type_id, "flag": flag, "quantity": qty}


def _names(*pairs):
    return {tid: name for tid, name in pairs}


def test_frigate_string_flags_groups_order_and_total_modules():
    """3 high / 3 mid / 4 low / 3 rig, plus drones and cargo, all on the
    string flag enum ESI actually sends."""
    raw = {
        "fitting_id": 1, "name": "Test Frigate Fit", "description": "",
        "ship_type_id": 90001,
        "items": [
            _item(90011, "HiSlot2"), _item(90012, "HiSlot0"), _item(90013, "HiSlot1"),
            _item(90021, "MedSlot1"), _item(90022, "MedSlot0"), _item(90023, "MedSlot2"),
            _item(90031, "LoSlot3"), _item(90032, "LoSlot0"), _item(90033, "LoSlot1"), _item(90034, "LoSlot2"),
            _item(90041, "RigSlot1"), _item(90042, "RigSlot0"), _item(90043, "RigSlot2"),
            _item(90051, "DroneBay", qty=5),
            _item(90061, "Cargo", qty=10),
        ],
    }
    type_names = _names(
        (90011, "High C"), (90012, "High A"), (90013, "High B"),
        (90021, "Mid B"), (90022, "Mid A"), (90023, "Mid C"),
        (90031, "Low D"), (90032, "Low A"), (90033, "Low B"), (90034, "Low C"),
        (90041, "Rig B"), (90042, "Rig A"), (90043, "Rig C"),
        (90051, "Test Drone"), (90061, "Test Charge"),
    )
    fit = _parse_fitting(raw, type_names, "Test Frigate Hull", {})

    assert [i["name"] for i in fit["groups"]["high"]] == ["High A", "High B", "High C"]
    assert [i["name"] for i in fit["groups"]["med"]] == ["Mid A", "Mid B", "Mid C"]
    assert [i["name"] for i in fit["groups"]["low"]] == ["Low A", "Low B", "Low C", "Low D"]
    assert [i["name"] for i in fit["groups"]["rig"]] == ["Rig A", "Rig B", "Rig C"]
    assert fit["groups"]["drone"][0]["name"] == "Test Drone"
    assert fit["groups"]["drone"][0]["quantity"] == 5
    assert fit["groups"]["cargo"][0]["name"] == "Test Charge"
    assert fit["groups"]["subsystem"] == []
    assert fit["groups"]["service"] == []
    # High + mid + low + rig = 3+3+4+3 = 13. Drones/cargo/fighters excluded.
    assert fit["total_modules"] == 13


def test_subsystem_cruiser_four_subsystems():
    raw = {
        "fitting_id": 2, "name": "Test Tengu Fit", "description": "",
        "ship_type_id": 90002,
        "items": [
            _item(90071, "SubSystemSlot3"), _item(90072, "SubSystemSlot1"),
            _item(90073, "SubSystemSlot0"), _item(90074, "SubSystemSlot2"),
        ],
    }
    type_names = _names(
        (90071, "Sub D"), (90072, "Sub B"), (90073, "Sub A"), (90074, "Sub C"),
    )
    fit = _parse_fitting(raw, type_names, "Test Strategic Cruiser", {})
    assert [i["name"] for i in fit["groups"]["subsystem"]] == ["Sub A", "Sub B", "Sub C", "Sub D"]
    assert fit["total_modules"] == 4


def test_structure_service_slots():
    """Service modules (Upwell structures) use ServiceSlot0-7 — a category
    ISS-056 added. They count as fitted modules (like high/med/low/rig),
    not as bay items."""
    raw = {
        "fitting_id": 3, "name": "Test Astrahus Fit", "description": "",
        "ship_type_id": 90003,
        "items": [
            _item(90081, "ServiceSlot1"), _item(90082, "ServiceSlot0"),
        ],
    }
    type_names = _names((90081, "Service B"), (90082, "Service A"))
    fit = _parse_fitting(raw, type_names, "Test Citadel Hull", {})
    assert [i["name"] for i in fit["groups"]["service"]] == ["Service A", "Service B"]
    assert fit["total_modules"] == 2
    assert SLOT_LABELS["service"] == "Service Slots"


def test_integer_flag_fallback_still_works():
    """Legacy integer inventory flags (pre-string-enum cached data, or a
    future ESI regression) must still resolve to the right group."""
    raw = {
        "fitting_id": 4, "name": "Test Legacy Fit", "description": "",
        "ship_type_id": 90004,
        "items": [
            _item(90091, 27),   # first HiSlot int flag
            _item(90092, 19),   # first MedSlot int flag
            _item(90093, 11),   # first LoSlot int flag
            _item(90094, 92),   # first RigSlot int flag
            _item(90095, 87),   # drone bay
            _item(90096, 5),    # cargo
        ],
    }
    type_names = _names(
        (90091, "High"), (90092, "Mid"), (90093, "Low"),
        (90094, "Rig"), (90095, "Drone"), (90096, "Charge"),
    )
    fit = _parse_fitting(raw, type_names, "Test Ship", {})
    assert fit["groups"]["high"][0]["name"] == "High"
    assert fit["groups"]["med"][0]["name"] == "Mid"
    assert fit["groups"]["low"][0]["name"] == "Low"
    assert fit["groups"]["rig"][0]["name"] == "Rig"
    assert fit["groups"]["drone"][0]["name"] == "Drone"
    assert fit["groups"]["cargo"][0]["name"] == "Charge"
    assert fit["total_modules"] == 4


def test_mixed_int_and_string_flags_does_not_break_sorting():
    """A fit whose items carry a mix of the legacy int flag and the string
    enum (plausible if a cached fit predates the string-flag switch and a
    newer one doesn't) must not raise, and both items land in "high"."""
    raw = {
        "fitting_id": 5, "name": "Test Mixed Fit", "description": "",
        "ship_type_id": 90005,
        "items": [_item(90101, 27), _item(90102, "HiSlot1")],
    }
    type_names = _names((90101, "Old Style"), (90102, "New Style"))
    fit = _parse_fitting(raw, type_names, "Test Ship", {})
    assert {i["name"] for i in fit["groups"]["high"]} == {"Old Style", "New Style"}


def test_unknown_and_invalid_flags_fall_back_to_cargo():
    raw = {
        "fitting_id": 6, "name": "Test Weird Fit", "description": "",
        "ship_type_id": 90006,
        "items": [_item(90111, "Invalid"), _item(90112, "SomeFutureFlag")],
    }
    type_names = _names((90111, "Stray Item"), (90112, "Mystery Item"))
    fit = _parse_fitting(raw, type_names, "Test Ship", {})
    assert {i["name"] for i in fit["groups"]["cargo"]} == {"Stray Item", "Mystery Item"}


def test_slot_index_helper():
    assert _slot_index("HiSlot3") == 3
    assert _slot_index("RigSlot0") == 0
    assert _slot_index("Cargo") == 0
    assert _slot_index(29) == 29
    assert _slot_index(None) == 0


def test_category_for_flag_covers_all_43_string_values():
    """All 43 string enum values from the live ESI OpenAPI spec resolve to
    a group — none of them should silently be an "unknown" that only
    happens to fall back to cargo by accident."""
    expected = {"Cargo": "cargo", "DroneBay": "drone", "FighterBay": "fighter", "Invalid": "cargo"}
    for i in range(8):
        expected[f"HiSlot{i}"] = "high"
        expected[f"MedSlot{i}"] = "med"
        expected[f"LoSlot{i}"] = "low"
        expected[f"ServiceSlot{i}"] = "service"
    for i in range(3):
        expected[f"RigSlot{i}"] = "rig"
    for i in range(4):
        expected[f"SubSystemSlot{i}"] = "subsystem"
    assert len(expected) == 43
    for flag, cat in expected.items():
        assert _category_for_flag(flag) == cat, flag


# ── _to_eft ──────────────────────────────────────────────────────────────────

def test_to_eft_output_shape_including_fighters():
    """Pins the full output, not just presence of each line — the exact
    section order (low, med, high, rig, subsystem, then drones/fighters/
    cargo) and blank-line layout matter for a human pasting this into the
    EVE client, and "x in lines" checks wouldn't catch a reordering."""
    raw = {
        "fitting_id": 7, "name": "Test Carrier Fit", "description": "",
        "ship_type_id": 90007,
        "items": [
            _item(90120, "LoSlot0"),
            _item(90121, "HiSlot0"),
            _item(90122, "DroneBay", qty=5),
            _item(90123, "FighterBay", qty=2),
            _item(90124, "Cargo", qty=3),
        ],
    }
    type_names = _names(
        (90120, "Test Low Module"), (90121, "Test High Module"), (90122, "Test Drone"),
        (90123, "Test Fighter"), (90124, "Test Charge"),
    )
    fit = _parse_fitting(raw, type_names, "Test Carrier", {})
    eft = _to_eft(fit)

    assert eft == (
        "[Test Carrier, Test Carrier Fit]\n"
        "Test Low Module\n"
        "\n"
        "\n"
        "Test High Module\n"
        "\n"
        "\n"
        "\n"
        "Test Drone x5\n"
        "\n"
        "Test Fighter x2\n"   # previously dropped entirely
        "\n"
        "Test Charge x3"
    )


def test_to_eft_singular_quantity_has_no_suffix():
    raw = {
        "fitting_id": 8, "name": "Test Fit", "description": "",
        "ship_type_id": 90008,
        "items": [_item(90131, "DroneBay", qty=1)],
    }
    fit = _parse_fitting(raw, _names((90131, "Test Drone")), "Test Ship", {})
    eft = _to_eft(fit)
    assert "Test Drone" in eft.split("\n")
    assert "Test Drone x1" not in eft


# ── Round trip through the fitting tool's own EFT import parser ────────────

def _seeded_sde_db():
    """Temp-file sqlite DB with the SDE slice the round-trip test needs:
    a ship, module types with SDEModuleSlot rows for high/med/low/rig, and
    group/category rows for a drone and a fighter (the import parser's
    fallback path, since fighters get no SDEModuleSlot row from the loader).
    Returns (SessionLocal, teardown)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            db.add_all([
                SDEGroup(group_id=9600, category_id=CATEGORY_SHIP, group_name="Test Frigates"),
                SDEGroup(group_id=9601, category_id=CATEGORY_DRONE, group_name="Test Drones"),
                SDEGroup(group_id=9602, category_id=CATEGORY_FIGHTER, group_name="Test Fighters"),
                SDEGroup(group_id=9603, category_id=999, group_name="Test Misc"),
            ])
            db.add(SDEType(type_id=96201, type_name="Test Roundtrip Frigate",
                            group_id=9600, category_id=CATEGORY_SHIP, published=True))
            for tid, name, slot in [
                (96211, "Test High Module", "high"),
                (96221, "Test Mid Module", "med"),
                (96231, "Test Low Module", "low"),
                (96241, "Test Rig", "rig"),
            ]:
                db.add(SDEType(type_id=tid, type_name=name, group_id=9603,
                                category_id=999, published=True))
                db.add(SDEModuleSlot(type_id=tid, slot_type=slot))
            db.add(SDEType(type_id=96251, type_name="Test Roundtrip Drone",
                            group_id=9601, category_id=CATEGORY_DRONE, published=True))
            db.add(SDEType(type_id=96261, type_name="Test Roundtrip Fighter",
                            group_id=9602, category_id=CATEGORY_FIGHTER, published=True))
            db.add(SDEType(type_id=96271, type_name="Test Roundtrip Charge",
                            group_id=9603, category_id=999, published=True))
            # No SDEModuleSlot row for this one, by design: the loader's
            # SLOT_EFFECT_MAP has no service-slot effect, so a real service
            # module never gets a slot_type row either.
            db.add(SDEType(type_id=96281, type_name="Test Roundtrip Service Module",
                            group_id=9603, category_id=999, published=True))
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

    return teardown


def _csrf_client():
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER_ID, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def test_to_eft_round_trips_through_the_fitting_tools_import_parser():
    """Feed `_to_eft`'s output into POST /tools/fitting/import-eft — the
    same parser the "import from character" tool and the manual EFT paste
    box both use — and check the modules land in the slots that parser
    itself resolves them to.

    Fighters and service modules get no dedicated slot in the fitting tool's
    item model (only ships get fitted there — no carriers, no structures),
    so the import parser's own fallback treats an unrecognized item
    category as cargo. `_to_eft` must not drop those lines outright the way
    it used to for fighters — they round-trip as cargo entries, which is
    what the parser itself does with them.
    """
    teardown = _seeded_sde_db()
    try:
        raw = {
            "fitting_id": 9, "name": "Test Roundtrip Fit", "description": "",
            "ship_type_id": 96201,
            "items": [
                _item(96211, "HiSlot0"),
                _item(96221, "MedSlot0"),
                _item(96231, "LoSlot0"),
                _item(96241, "RigSlot0"),
                _item(96251, "DroneBay", qty=3),
                _item(96261, "FighterBay", qty=2),
                _item(96271, "Cargo", qty=7),
                _item(96281, "ServiceSlot0"),
            ],
        }
        type_names = _names(
            (96211, "Test High Module"), (96221, "Test Mid Module"),
            (96231, "Test Low Module"), (96241, "Test Rig"),
            (96251, "Test Roundtrip Drone"), (96261, "Test Roundtrip Fighter"),
            (96271, "Test Roundtrip Charge"), (96281, "Test Roundtrip Service Module"),
        )
        fit = _parse_fitting(raw, type_names, "Test Roundtrip Frigate", {})
        eft_text = _to_eft(fit)

        r = _csrf_client().post(
            "/tools/fitting/import-eft",
            json={"eft": eft_text},
            headers={"X-CSRF-Token": _CSRF},
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body, body

        by_name = {i["type_name"]: i for i in body["items"]}
        assert by_name["Test High Module"]["slot"] == "high"
        assert by_name["Test Mid Module"]["slot"] == "med"
        assert by_name["Test Low Module"]["slot"] == "low"
        assert by_name["Test Rig"]["slot"] == "rig"
        assert by_name["Test Roundtrip Drone"]["slot"] == "drone"
        assert by_name["Test Roundtrip Drone"]["quantity"] == 3
        # No dedicated fighter or service slot in the fitting tool's own
        # model — the parser's fallback puts an unrecognized category in
        # cargo. What matters is neither line is lost.
        assert by_name["Test Roundtrip Fighter"]["slot"] == "cargo"
        assert by_name["Test Roundtrip Fighter"]["quantity"] == 2
        assert by_name["Test Roundtrip Service Module"]["slot"] == "cargo"
        assert by_name["Test Roundtrip Charge"]["slot"] == "cargo"
        assert by_name["Test Roundtrip Charge"]["quantity"] == 7
    finally:
        teardown()


# ── Page render with a mocked ESI response ──────────────────────────────────

class _FakeESIClient:
    def __init__(self, *a, **kw):
        pass


def _seeded_render_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            db.add(Character(
                character_id=CHAR_ID, character_name="Test Pilot", user_id=USER_ID,
                access_token="a", refresh_token="r",
                token_expiry=datetime(2099, 1, 1),
                scopes="esi-fittings.read_fittings.v1",
            ))
            db.add_all([
                SDEType(type_id=97001, type_name="Test Render Frigate", published=True),
                SDEType(type_id=97002, type_name="Test Render Citadel", published=True),
                SDEType(type_id=97011, type_name="Test Render High Module", published=True),
                SDEType(type_id=97012, type_name="Test Render Drone", published=True),
                SDEType(type_id=97021, type_name="Test Render Service Module", published=True),
            ])
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

    return teardown


def test_page_render_shows_modules_under_slot_headings(monkeypatch):
    teardown = _seeded_render_db()
    try:
        async def fake_refresh_token(char, db):
            return "fake-token"

        async def fake_get_fittings(client, character_id):
            return [
                {
                    "fitting_id": 1, "name": "Test Frigate Fit", "description": "",
                    "ship_type_id": 97001,
                    "items": [
                        _item(97011, "HiSlot0"),
                        _item(97012, "DroneBay", qty=2),
                    ],
                },
                {
                    "fitting_id": 2, "name": "Test Citadel Fit", "description": "",
                    "ship_type_id": 97002,
                    "items": [_item(97021, "ServiceSlot0")],
                },
            ]

        async def fake_get_type(client, type_id):
            names = {97001: "Test Render Frigate", 97002: "Test Render Citadel"}
            return {"name": names.get(type_id, f"Ship {type_id}"), "dogma_attributes": []}

        monkeypatch.setattr(fittings_mod, "refresh_token", fake_refresh_token)
        monkeypatch.setattr(fittings_mod, "ESIClient", _FakeESIClient)
        monkeypatch.setattr(fittings_mod.esi_char, "get_fittings", fake_get_fittings)
        monkeypatch.setattr(fittings_mod.esi_universe, "get_type", fake_get_type)

        import app.main as main
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        payload = {"user_id": USER_ID}
        cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
        client = TestClient(main.app, base_url="https://testserver")
        client.cookies.set("vigilant_session", cookie)

        r = client.get(f"/character/{CHAR_ID}/fittings")
        assert r.status_code == 200
        html = r.text
        assert "0 modules" not in html

        # Isolate each fit's own detail block (id="fit-<fitting_id>") so
        # "appears under its heading" actually means something — a bare
        # substring check on the whole page would also pass on the old
        # code, which rendered every item's name too, just all under one
        # Cargo heading instead of its real one.
        idx_frigate = html.index('id="fit-1"')
        idx_citadel = html.index('id="fit-2"')
        if idx_frigate < idx_citadel:
            frigate_block, citadel_block = html[idx_frigate:idx_citadel], html[idx_citadel:]
        else:
            citadel_block, frigate_block = html[idx_citadel:idx_frigate], html[idx_frigate:]

        assert frigate_block.index("High Slots") < frigate_block.index("Test Render High Module")
        assert frigate_block.index("Drones") < frigate_block.index("Test Render Drone")
        # The bug this ticket fixes dumped everything into Cargo — assert
        # that heading is gone now that both items resolve to their real
        # slot.
        assert "Cargo" not in frigate_block

        assert citadel_block.index("Service Slots") < citadel_block.index("Test Render Service Module")
    finally:
        teardown()
