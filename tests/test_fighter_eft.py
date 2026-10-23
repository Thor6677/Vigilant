"""EFT round trip and squadron splitting for fighters (ISS-136, T5).

The EVE client puts every fighter line of a type into the bay as one total,
pyfa wants one "Name xN" line per squadron and always an xN. So import splits
each line into full squadrons (short last one, never clamped or dropped) and
launches them in order by the hull's tube and class limits; export writes one
line per squadron, launched first, as its own block.

Pure helper tests plus the real routes on a temp SQLite file with fixture-local
type IDs and neutral names.
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.db.models import Base, User, get_db
from app.db.sde_models import SDEGroup, SDEModuleSlot, SDEType, SDETypeDogmaAttribute
from app.fitting.fighters import (
    MAX_SQUADRONS_PER_FIT, MAX_SQUADRONS_PER_LINE, assign_fighter_squadrons,
    fighter_order_key, order_fighters_for_export, split_quantity,
)

USER_ID = 9301
_CSRF = "test-csrf-token-fighter-eft-0123456789"

G_SHIP, G_FIGHTER, G_DRONE, G_MISC = 9310, 9311, 9312, 9313
CARRIER, BRICK = 93001, 93002
LIGHT, SUPPORT, HEAVY, STANDUP, CLASSLESS = 93011, 93012, 93013, 93014, 93015
DRONE, CHARGE = 93021, 93022
GUN_A, GUN_B, AMMO_A, AMMO_B = 93031, 93032, 93033, 93034

CARRIER_LIMITS = {"tubes": 4, "light": 3, "support": 2, "heavy": 0}
INFO = {
    LIGHT: {"class": "light", "max_size": 6},
    SUPPORT: {"class": "support", "max_size": 3},
    HEAVY: {"class": "heavy", "max_size": 6},
    STANDUP: {"class": "standup", "max_size": 6},
    CLASSLESS: {"class": None, "max_size": 6},
}


def _fighter(tid, qty, **extra):
    return {"type_id": tid, "type_name": f"T{tid}", "slot": "fighter", "quantity": qty, **extra}


def _squads(items):
    return [(i["type_id"], i["quantity"], i["online"]) for i in items if i["slot"] == "fighter"]


# ── helper ──────────────────────────────────────────────────────────────

def test_split_quantity_full_squadrons_then_short_last():
    assert split_quantity(18, 6) == [6, 6, 6]
    assert split_quantity(8, 6) == [6, 2]
    assert split_quantity(9, 3) == [3, 3, 3]
    assert split_quantity(4, 6) == [4]
    assert split_quantity(24, 0) == [24]   # unknown size: no split
    assert split_quantity(0, 6) == [1]


def test_helper_x18_and_x8_split_and_launch():
    out = assign_fighter_squadrons([_fighter(LIGHT, 18)], INFO, CARRIER_LIMITS)
    assert _squads(out) == [(LIGHT, 6, True)] * 3
    out = assign_fighter_squadrons([_fighter(LIGHT, 8)], INFO, CARRIER_LIMITS)
    assert _squads(out) == [(LIGHT, 6, True), (LIGHT, 2, True)]


def test_helper_class_limit_leaves_a_reserve():
    out = assign_fighter_squadrons([_fighter(LIGHT, 6) for _ in range(4)], INFO, CARRIER_LIMITS)
    assert _squads(out) == [(LIGHT, 6, True)] * 3 + [(LIGHT, 6, False)]


def test_helper_five_squadrons_on_four_tubes():
    limits = {"tubes": 4, "light": 5, "support": 5, "heavy": 5}
    out = assign_fighter_squadrons([_fighter(LIGHT, 6) for _ in range(5)], INFO, limits)
    assert [o for *_, o in _squads(out)] == [True] * 4 + [False]


def test_helper_oversize_totals_per_type_from_esi():
    items = [_fighter(LIGHT, 24), _fighter(SUPPORT, 6)]
    out = assign_fighter_squadrons(items, INFO, CARRIER_LIMITS)
    assert _squads(out) == [
        (LIGHT, 6, True), (LIGHT, 6, True), (LIGHT, 6, True), (LIGHT, 6, False),
        (SUPPORT, 3, True), (SUPPORT, 3, False),
    ]


def test_helper_keeps_unlaunchable_fighters_as_reserves():
    items = [_fighter(STANDUP, 6), _fighter(HEAVY, 6), _fighter(CLASSLESS, 6), _fighter(99999, 5)]
    out = assign_fighter_squadrons(items, INFO, CARRIER_LIMITS)
    assert _squads(out) == [
        (STANDUP, 6, False), (HEAVY, 6, False), (CLASSLESS, 6, False), (99999, 5, False),
    ]


def test_helper_zero_tube_hull_and_missing_limits_keep_everything():
    zero = {"tubes": 0, "light": 0, "support": 0, "heavy": 0}
    for limits in (zero, {}, None):
        out = assign_fighter_squadrons([_fighter(LIGHT, 12)], INFO, limits)
        assert _squads(out) == [(LIGHT, 6, False), (LIGHT, 6, False)]


def test_helper_leaves_other_items_and_order_alone():
    drone = {"type_id": DRONE, "type_name": "Test Drone", "slot": "drone", "quantity": 5}
    cargo = {"type_id": CHARGE, "type_name": "Test Charge", "slot": "cargo", "quantity": 1}
    out = assign_fighter_squadrons([drone, _fighter(LIGHT, 8), cargo], INFO, CARRIER_LIMITS)
    assert [i["slot"] for i in out] == ["drone", "fighter", "fighter", "cargo"]
    assert out[0] is drone and out[3] is cargo and "online" not in drone


def test_helper_respects_an_explicit_offline_and_does_not_mutate_input():
    src = _fighter(LIGHT, 6, online=False)
    out = assign_fighter_squadrons([src, _fighter(LIGHT, 6)], INFO, CARRIER_LIMITS)
    assert _squads(out) == [(LIGHT, 6, False), (LIGHT, 6, True)]
    assert src["online"] is False and src["quantity"] == 6


def test_order_fighters_for_export_launched_first_then_reserves():
    items = [_fighter(1, 6, online=False), _fighter(2, 6, online=True), _fighter(3, 6)]
    assert [i["type_id"] for i in order_fighters_for_export(items)] == [2, 3, 1]


def test_split_quantity_caps_the_squadron_count_and_keeps_the_remainder():
    parts = split_quantity(999_999_999, 6)
    assert len(parts) == MAX_SQUADRONS_PER_LINE
    assert parts[:-1] == [6] * (MAX_SQUADRONS_PER_LINE - 1)
    assert sum(parts) == 999_999_999   # nothing dropped
    huge = 10 ** 40
    parts = split_quantity(huge, 6)
    assert len(parts) == MAX_SQUADRONS_PER_LINE and sum(parts) == huge


def test_split_quantity_cap_boundary():
    n = MAX_SQUADRONS_PER_LINE
    assert split_quantity(6 * n, 6) == [6] * n                       # exactly at the cap
    assert split_quantity(6 * n + 1, 6) == [6] * (n - 1) + [7]       # one over: oversize last
    assert split_quantity(6 * (n - 1) + 2, 6) == [6] * (n - 1) + [2]  # short last still fits


def test_split_quantity_tolerates_non_int_input():
    assert split_quantity("abc", 6) == [1]
    assert split_quantity(None, 6) == [1]
    assert split_quantity(-3, 6) == [1]


def test_helper_caps_squadrons_per_fit_and_warns():
    warnings = []
    items = [_fighter(LIGHT, 6 * 100) for _ in range(5)]
    out = assign_fighter_squadrons(items, INFO, CARRIER_LIMITS, warnings)
    assert len(out) <= MAX_SQUADRONS_PER_FIT + 5
    assert sum(i["quantity"] for i in out) == 6 * 100 * 5
    assert len(warnings) == 1
    warnings = []
    assign_fighter_squadrons([_fighter(LIGHT, 18)], INFO, CARRIER_LIMITS, warnings)
    assert warnings == []


def test_helper_skips_zero_quantity_lines():
    out = assign_fighter_squadrons([_fighter(LIGHT, 0), _fighter(LIGHT, 6)], INFO, CARRIER_LIMITS)
    assert _squads(out) == [(LIGHT, 6, True)]


def test_fighter_order_key_puts_damage_squadrons_before_support():
    keyed = sorted([(SUPPORT, "A Support"), (HEAVY, "Z Heavy"), (LIGHT, "B Light"), (STANDUP, "A Standup")],
                   key=lambda t: fighter_order_key(INFO[t[0]], t[1]))
    assert [t[0] for t in keyed] == [LIGHT, HEAVY, SUPPORT, STANDUP]


# ── routes ──────────────────────────────────────────────────────────────

def _seed():
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
                SDEGroup(group_id=G_SHIP, category_id=6, group_name="Test Ships"),
                SDEGroup(group_id=G_FIGHTER, category_id=87, group_name="Test Fighters"),
                SDEGroup(group_id=G_DRONE, category_id=18, group_name="Test Drones"),
                SDEGroup(group_id=G_MISC, category_id=999, group_name="Test Misc"),
            ])
            types = [
                (CARRIER, "Test Carrier", G_SHIP, 6), (BRICK, "Test Brick", G_SHIP, 6),
                (LIGHT, "Test Light Fighter", G_FIGHTER, 87),
                (SUPPORT, "Test Support Fighter", G_FIGHTER, 87),
                (HEAVY, "Test Heavy Fighter", G_FIGHTER, 87),
                (STANDUP, "Test Standup Fighter", G_FIGHTER, 87),
                (DRONE, "Test Drone", G_DRONE, 18), (CHARGE, "Test Charge", G_MISC, 999),
            ]
            for tid, name, grp, cat in types:
                db.add(SDEType(type_id=tid, type_name=name, group_id=grp,
                               category_id=cat, published=True, volume=1.0))
            for tid, name in [(GUN_A, "Test Gun A"), (GUN_B, "Test Gun B"),
                              (AMMO_A, "Test Ammo A"), (AMMO_B, "Test Ammo B")]:
                db.add(SDEType(type_id=tid, type_name=name, group_id=G_MISC,
                               category_id=999, published=True))
            db.add_all([SDEModuleSlot(type_id=GUN_A, slot_type="high"),
                        SDEModuleSlot(type_id=GUN_B, slot_type="high")])
            attrs = {
                CARRIER: {2216: 4, 2217: 3, 2218: 2, 2055: 65000.0},
                BRICK: {},
                LIGHT: {2212: 1, 2215: 6}, SUPPORT: {2213: 1, 2215: 3},
                HEAVY: {2214: 1, 2215: 6}, STANDUP: {2740: 1, 2215: 6},
            }
            for tid, a in attrs.items():
                for aid, val in a.items():
                    db.add(SDETypeDogmaAttribute(type_id=tid, attribute_id=aid, value=val))
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

    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)
        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(engine.dispose())
        finally:
            loop2.close()

    return teardown


def _client():
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER_ID, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _import(eft, ship="Test Carrier", crlf=False):
    text = f"[{ship}, Test Fit]\n\n" + eft
    if crlf:
        text = text.replace("\n", "\r\n")
    r = _client().post("/tools/fitting/import-eft", json={"eft": text},
                       headers={"X-CSRF-Token": _CSRF})
    body = r.json()
    assert "error" not in body, body
    return body["items"]


def _export(items, ship_id=CARRIER):
    r = _client().post("/tools/fitting/export-eft",
                       json={"ship_type_id": ship_id, "name": "Test Fit", "items": items},
                       headers={"X-CSRF-Token": _CSRF})
    return r.text


L, S, H, U = "Test Light Fighter", "Test Support Fighter", "Test Heavy Fighter", "Test Standup Fighter"
_NAMES = {L: LIGHT, S: SUPPORT, H: HEAVY, U: STANDUP}


def _got(items):
    return [(i["type_name"], i["quantity"], i["online"]) for i in items if i["slot"] == "fighter"]


def test_import_split_vectors():
    teardown = _seed()
    try:
        assert _got(_import(f"{L} x18")) == [(L, 6, True)] * 3
        assert _got(_import(f"{L} x8")) == [(L, 6, True), (L, 2, True)]
        assert _got(_import(f"{L} x9")) == [(L, 6, True), (L, 3, True)]
        assert _got(_import(f"{L}")) == [(L, 1, True)]   # bare line = 1, the client's rule
        assert _got(_import(f"{L} x6\n{L} x6\n{L} x6\n{L} x6")) == [(L, 6, True)] * 3 + [(L, 6, False)]
        assert _got(_import(f"{L} x24\n{S} x6")) == (
            [(L, 6, True)] * 3 + [(L, 6, False)] + [(S, 3, True), (S, 3, False)])
        assert _got(_import(f"{H} x6\n{U} x6")) == [(H, 6, False), (U, 6, False)]
    finally:
        teardown()


def test_import_fighter_block_before_or_after_cargo_and_with_crlf():
    teardown = _seed()
    try:
        before = f"{L} x6\n{S} x3\n\nTest Charge x100\n"
        after = f"Test Drone x5\n\nTest Charge x100\n\n{L} x6\n{S} x3\n"
        a = _import(before)
        b = _import(after, crlf=True)
        assert _got(a) == _got(b) == [(L, 6, True), (S, 3, True)]
        assert [(i["type_id"], i["quantity"]) for i in b if i["slot"] == "cargo"] == [(CHARGE, 100)]
    finally:
        teardown()


def test_import_on_a_zero_tube_hull_keeps_every_squadron():
    teardown = _seed()
    try:
        items = _import(f"{L} x12\n{S} x3", ship="Test Brick")
        assert _got(items) == [(L, 6, False), (L, 6, False), (S, 3, False)]
    finally:
        teardown()


def test_export_block_layout_always_xn_and_launched_before_reserves():
    teardown = _seed()
    try:
        def f(tid, name, qty, online):
            return {"type_id": tid, "type_name": name, "slot": "fighter", "quantity": qty, "online": online}
        items = [
            f(LIGHT, L, 6, False), f(LIGHT, L, 6, True), f(SUPPORT, S, 3, True),
            f(LIGHT, L, 2, False), f(LIGHT, L, 1, True),
            {"type_id": DRONE, "type_name": "Test Drone", "slot": "drone", "quantity": 1},
            {"type_id": CHARGE, "type_name": "Test Charge", "slot": "cargo", "quantity": 1},
        ]
        text = _export(items)
        assert text == (
            "[Test Carrier, Test Fit]\n\n\n\n\n\n"
            "Test Drone x1\n\n"
            f"{L} x6\n{S} x3\n{L} x1\n{L} x6\n{L} x2\n\n"
            "Test Charge x1"
        )
    finally:
        teardown()


def test_export_then_import_round_trips_the_doc_example():
    """Three launched Light squadrons and a Support one, then a spare Light,
    a spare Support and a short Light squadron in reserve."""
    teardown = _seed()
    try:
        def f(tid, name, qty, online):
            return {"type_id": tid, "type_name": name, "slot": "fighter", "quantity": qty, "online": online}
        original = [
            f(LIGHT, L, 6, True), f(LIGHT, L, 6, True), f(LIGHT, L, 6, True), f(SUPPORT, S, 3, True),
            f(LIGHT, L, 6, False), f(SUPPORT, S, 3, False), f(LIGHT, L, 2, False),
        ]
        text = _export(original)
        assert text.endswith("\n\n" + "\n".join(
            [f"{L} x6"] * 3 + [f"{S} x3", f"{L} x6", f"{S} x3", f"{L} x2"]))
        again = _import(text.split("\n", 1)[1])
        assert _got(again) == [(i["type_name"], i["quantity"], i["online"]) for i in original]
        # Nothing but fighter lines inside the block, so pyfa reads it as a fighter bay.
        block = text.split("\n\n")[-1].split("\n")
        assert len(block) == 7 and all(" x" in ln for ln in block)
    finally:
        teardown()


def test_import_huge_quantity_is_capped_with_a_warning_not_a_crash():
    teardown = _seed()
    try:
        for qty in ("999999999", "9" * 40):
            text = f"[Test Carrier, Test Fit]\n\n{L} x{qty}"
            r = _client().post("/tools/fitting/import-eft", json={"eft": text},
                               headers={"X-CSRF-Token": _CSRF})
            assert r.status_code == 200
            body = r.json()
            fighters = [i for i in body["items"] if i["slot"] == "fighter"]
            assert len(fighters) == MAX_SQUADRONS_PER_LINE
            assert sum(i["quantity"] for i in fighters) == int(qty)
            assert body["warnings"]
        assert "warnings" not in _client().post(
            "/tools/fitting/import-eft", json={"eft": f"[Test Carrier, Test Fit]\n\n{L} x18"},
            headers={"X-CSRF-Token": _CSRF}).json()
    finally:
        teardown()


def test_import_x0_fighter_line_is_skipped():
    teardown = _seed()
    try:
        assert _got(_import(f"{L} x0\n{S} x3")) == [(S, 3, True)]
    finally:
        teardown()


def test_split_fighter_line_does_not_shift_inline_charges_onto_other_modules():
    """Charges ride a list parallel to the items; splitting a fighter line
    before it is consumed used to hand every later charge to the wrong module."""
    teardown = _seed()
    try:
        items = _import(f"{L} x12\nTest Gun A, Test Ammo A\nTest Gun B, Test Ammo B")
        guns = {i["type_name"]: i for i in items if i["slot"] == "high"}
        assert guns["Test Gun A"]["charge_name"] == "Test Ammo A"
        assert guns["Test Gun B"]["charge_name"] == "Test Ammo B"
        assert len([i for i in items if i["slot"] == "fighter"]) == 2
    finally:
        teardown()


def test_export_never_writes_zero_or_negative_quantities():
    teardown = _seed()
    try:
        items = [
            {"type_id": LIGHT, "type_name": L, "slot": "fighter", "quantity": -3, "online": True},
            {"type_id": LIGHT, "type_name": L, "slot": "fighter", "quantity": 0, "online": True},
            {"type_id": LIGHT, "type_name": L, "slot": "fighter", "quantity": 4, "online": True},
            {"type_id": DRONE, "type_name": "Test Drone", "slot": "drone", "quantity": -1},
        ]
        text = _export(items)
        assert "x-" not in text and "x0" not in text
        assert f"{L} x4" in text.split("\n")
        assert "Test Drone" not in text
    finally:
        teardown()


def test_known_limit_a_reserve_with_a_free_tube_reimports_launched():
    """EFT has no launch marker, so export -> import cannot keep a reserve
    squadron a hull could still launch. Pinned so a change is deliberate."""
    teardown = _seed()
    try:
        original = [
            {"type_id": LIGHT, "type_name": L, "slot": "fighter", "quantity": 6, "online": True},
            {"type_id": LIGHT, "type_name": L, "slot": "fighter", "quantity": 6, "online": False},
        ]
        again = _import(_export(original).split("\n", 1)[1])
        assert _got(again) == [(L, 6, True), (L, 6, True)]
    finally:
        teardown()
