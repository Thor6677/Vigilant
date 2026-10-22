"""Form parsing for /industry/copy-packs/plan (app/industry/copy_pack_form.py).

Every value comes from a browser, so each test feeds the parser something a
hand-edited request could send and checks it is clamped or dropped."""
import json

from app.industry import copy_pack as cp
from app.industry.copy_pack_form import MAX_QUANTITY, parse_plan_form


def _form(**fields):
    base = {"type_id": "23757", "quantity": "2", "character_id": "90000001",
            "structure": "azbel", "rig": "none", "security": "nullsec"}
    base.update({k: (json.dumps(v) if k.endswith("_json") and not isinstance(v, str) else v)
                 for k, v in fields.items()})
    return base


def test_plain_fields():
    f = parse_plan_form(_form())
    assert (f.type_id, f.quantity, f.character_id) == (23757, 2, 90000001)
    assert (f.structure, f.rig, f.security) == ("azbel", "none", "nullsec")


def test_quantity_is_clamped():
    assert parse_plan_form(_form(quantity="5000")).quantity == MAX_QUANTITY
    assert parse_plan_form(_form(quantity="0")).quantity == 1
    assert parse_plan_form(_form(quantity="abc")).quantity == 1


def test_facility_values_outside_the_whitelists_fall_back():
    f = parse_plan_form(_form(structure="keepstar", rig="<script>", security="wspace"))
    assert (f.structure, f.rig, f.security) == ("npc_station", "none", "highsec")


def test_state_overrides_are_parsed_and_clamped():
    f = parse_plan_form(_form(state_json={
        "build": {"21017": False, "21025": True},
        "bpo": {"23757": 1234567890123},
        "me": {"21025": 15},
        "te": {"21025": -3},
        "ignore_bpc": [21013, "x", -4],
    }))
    assert f.build == {21017: False, 21025: True}
    assert f.bpo_choice == {23757: 1234567890123}
    assert f.me_override == {21025: 10}
    assert f.te_override == {21025: 0}
    assert f.ignore_bpcs == frozenset({21013})


def test_junk_keys_and_values_are_skipped():
    f = parse_plan_form(_form(state_json={
        "build": {"abc": True, "0": True, "21017": "yes"},
        "me": {"21025": "lots"},
    }))
    assert f.build == {} and f.me_override == {}


def test_unparseable_or_oversized_json_is_ignored():
    assert parse_plan_form(_form(state_json="{not json")).build == {}
    assert parse_plan_form(_form(state_json="[1, 2]")).build == {}
    big = json.dumps({"build": {str(i): True for i in range(1, 5000)}})
    assert parse_plan_form(_form(state_json=big)).build == {}


def test_skill_overrides_only_for_planner_skills_and_clamped():
    f = parse_plan_form(_form(skills_json={
        str(cp.MASS_PRODUCTION): 9, str(cp.SCIENCE): -1, "22242": 5,
        str(cp.INDUSTRY): "",
    }))
    assert f.skill_overrides == {cp.MASS_PRODUCTION: 5, cp.SCIENCE: 0}


def test_missing_fields_get_defaults():
    f = parse_plan_form({})
    assert (f.type_id, f.quantity, f.character_id) == (0, 1, 0)
    assert f.structure == "npc_station" and f.skill_overrides == {}
