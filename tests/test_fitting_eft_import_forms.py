"""EFT import accepts the forms other tools write (ISS-160, ISS-161).

ISS-160: "Module,Charge" with no space after the comma used to fail the whole
line (it was looked up as one item name) and the module vanished silently.
ISS-161: an in-game export lists spare modules in the cargo hold as "Name xN";
the slot came from the item's type alone, so they were fitted into racks.
"""
import app.db.sde_models as sm
from tests.test_fitting_char_import import (
    BOOSTER, CHARGE, DRONE, LOW_MOD, MED_MOD, TURRET, USER_A, _CSRF,
    _authed_client, _seed_med_slot, _seeded_db,
)

COMMA_GUN = 98091   # a real-looking name that itself contains a comma


async def _seed(db):
    await _seed_med_slot(db)
    db.add(sm.SDEType(type_id=COMMA_GUN, type_name="Test Gun, Heavy", group_id=9602,
                      category_id=7, volume=5.0, published=True))
    db.add(sm.SDEModuleSlot(type_id=COMMA_GUN, slot_type="high",
                            is_turret=True, is_launcher=False))
    await db.commit()


def _import(eft_text):
    teardown, _session, _engine = _seeded_db(_seed)
    try:
        client = _authed_client(USER_A)
        r = client.post("/tools/fitting/import-eft", json={"eft": eft_text},
                        headers={"X-CSRF-Token": _CSRF})
        assert r.status_code == 200
        body = r.json()
        assert "error" not in body, body
        return body["items"]
    finally:
        teardown()


def _of(items, type_id):
    return [i for i in items if i["type_id"] == type_id]


# ── ISS-160: the comma ──────────────────────────────────────────────────────

def test_comma_with_no_space_keeps_the_module_and_loads_the_charge():
    items = _import("[Test Rifter, Fit]\nTest Autocannon,Test EMP Charge")
    gun = _of(items, TURRET)
    assert len(gun) == 1 and gun[0]["slot"] == "high"
    assert gun[0]["charge_type_id"] == CHARGE


def test_comma_with_a_space_still_works():
    items = _import("[Test Rifter, Fit]\nTest Autocannon, Test EMP Charge")
    assert _of(items, TURRET)[0]["charge_type_id"] == CHARGE


def test_several_comma_lines_all_keep_their_modules():
    items = _import("[Test Rifter, Fit]\n" + "\n".join(["Test Autocannon,Test EMP Charge"] * 4))
    assert len(_of(items, TURRET)) == 4
    assert all(i["charge_type_id"] == CHARGE for i in _of(items, TURRET))


def test_a_mid_slot_module_takes_a_no_space_charge_too():
    items = _import("[Test Rifter, Fit]\nTest Injector,Test Booster")
    assert _of(items, MED_MOD)[0]["charge_type_id"] == BOOSTER


def test_an_item_name_that_contains_a_comma_is_tried_whole_first():
    items = _import("[Test Rifter, Fit]\nTest Gun, Heavy")
    gun = _of(items, COMMA_GUN)
    assert len(gun) == 1 and gun[0]["slot"] == "high"
    assert "charge_type_id" not in gun[0]


def test_an_unresolvable_charge_keeps_the_module_without_a_charge():
    items = _import("[Test Rifter, Fit]\nTest Autocannon,No Such Charge")
    gun = _of(items, TURRET)
    assert len(gun) == 1 and "charge_type_id" not in gun[0]


# ── ISS-161: cargo section ──────────────────────────────────────────────────

def test_a_module_listed_with_a_quantity_is_cargo_not_fitted():
    items = _import("[Test Rifter, Fit]\nTest Autocannon\n\nTest Damage Control x1\nTest Injector x2")
    assert _of(items, TURRET)[0]["slot"] == "high"
    dc = _of(items, LOW_MOD)
    assert len(dc) == 1 and dc[0]["slot"] == "cargo" and dc[0]["quantity"] == 1
    inj = _of(items, MED_MOD)
    assert len(inj) == 1 and inj[0]["slot"] == "cargo" and inj[0]["quantity"] == 2


def test_a_fitted_module_with_no_quantity_still_fits():
    items = _import("[Test Rifter, Fit]\nTest Damage Control\nTest Injector")
    assert _of(items, LOW_MOD)[0]["slot"] == "low"
    assert _of(items, MED_MOD)[0]["slot"] == "med"


def test_drones_with_a_quantity_stay_in_the_drone_bay():
    items = _import("[Test Rifter, Fit]\nTest Hobgoblin x5")
    d = _of(items, DRONE)
    assert len(d) == 1 and d[0]["slot"] == "drone" and d[0]["quantity"] == 5


def test_charges_with_a_quantity_stay_in_cargo():
    items = _import("[Test Rifter, Fit]\nTest Autocannon\n\nTest EMP Charge x400")
    stack = _of(items, CHARGE)
    assert len(stack) == 1 and stack[0]["slot"] == "cargo" and stack[0]["quantity"] == 400


def test_a_cargo_module_is_not_given_ammo_or_counted_as_a_weapon():
    items = _import("[Test Rifter, Fit]\nTest Autocannon x1\n\nTest EMP Charge x10")
    gun = _of(items, TURRET)[0]
    assert gun["slot"] == "cargo" and "charge_type_id" not in gun
    assert _of(items, CHARGE)[0]["quantity"] == 10
