"""blueprints.jsonl -> sde_blueprint_info + sde_blueprint_mfg_skills
(_parse_blueprint_info_item).

Fixtures are trimmed copies of real 2026-10-20 SDE entries."""
from app.sde.loader import _parse_blueprint_info_item

ARCHON_BLUEPRINT = {
    "_key": 23758,
    "blueprintTypeID": 23758,
    "maxProductionLimit": 1,
    "activities": {
        "copying": {"time": 1600000},
        "manufacturing": {
            "materials": [{"typeID": 21017, "quantity": 6}],
            "products": [{"typeID": 23757, "quantity": 1}],
            "skills": [{"typeID": 22242, "level": 3}],
            "time": 1800000,
        },
        "research_material": {"time": 1150000},
        "research_time": {"time": 1150000},
    },
}


def test_parse_reads_copy_fields_and_manufacturing_skills():
    info, skills = _parse_blueprint_info_item(ARCHON_BLUEPRINT)
    assert info == {
        "blueprint_type_id": 23758,
        "product_type_id": 23757,
        "manufacturing_time": 1800000,
        "product_quantity": 1,
        "max_production_limit": 1,
        "copying_time": 1600000,
    }
    assert skills == [{"blueprint_type_id": 23758, "skill_type_id": 22242, "level": 3}]


def test_parse_keeps_product_quantity():
    # Life Support Backup Unit: 3 units per run, 200 runs per copy.
    item = {"_key": 57523, "maxProductionLimit": 200, "activities": {
        "copying": {"time": 1440},
        "manufacturing": {"products": [{"typeID": 57486, "quantity": 3}], "time": 1800},
    }}
    info, skills = _parse_blueprint_info_item(item)
    assert (info["product_quantity"], info["max_production_limit"], info["copying_time"]) == (3, 200, 1440)
    assert skills == []


def test_parse_blueprint_without_copying_has_null_copying_time():
    # A T2 blueprint: invented, never copied.
    item = {"_key": 11373, "maxProductionLimit": 10, "activities": {
        "manufacturing": {"products": [{"typeID": 11371, "quantity": 1}], "time": 600,
                          "skills": [{"typeID": 3380, "level": 1}]},
    }}
    info, skills = _parse_blueprint_info_item(item)
    assert info["copying_time"] is None
    assert skills == [{"blueprint_type_id": 11373, "skill_type_id": 3380, "level": 1}]


def test_parse_skips_reaction_formulas():
    item = {"_key": 46166, "maxProductionLimit": 1000, "activities": {
        "reaction": {"products": [{"typeID": 16670, "quantity": 200}], "time": 10800},
    }}
    assert _parse_blueprint_info_item(item) == (None, [])


def test_parse_skips_unparseable_entries():
    item = {"_key": "not-a-number", "activities": {
        "manufacturing": {"products": [{"typeID": 1, "quantity": 1}], "time": 1},
    }}
    assert _parse_blueprint_info_item(item) == (None, [])


def _mfg(**extra):
    return {"_key": 100, "activities": {
        "manufacturing": {"products": [{"typeID": 200, "quantity": 2}], "time": 60},
        **extra.pop("acts", {})}, **extra}


def test_parse_malformed_max_production_limit_keeps_row():
    info, _ = _parse_blueprint_info_item(_mfg(maxProductionLimit="abc"))
    assert info == {
        "blueprint_type_id": 100, "product_type_id": 200,
        "manufacturing_time": 60, "product_quantity": 2,
        "max_production_limit": None, "copying_time": None,
    }


def test_parse_missing_max_production_limit_is_none():
    info, _ = _parse_blueprint_info_item(_mfg())
    assert info["max_production_limit"] is None


def test_parse_copying_without_time_is_none_not_zero():
    for copying in ({}, {"time": 0}, {"time": None}, "bad"):
        info, _ = _parse_blueprint_info_item(_mfg(acts={"copying": copying}))
        assert info is not None and info["copying_time"] is None


def test_parse_skips_bad_skill_entries_but_keeps_row():
    item = _mfg()
    item["activities"]["manufacturing"]["skills"] = [
        {"level": 2},                    # no typeID
        {"typeID": 3380, "level": None},  # null level -> 1
        {"typeID": 3381},                # missing level -> 1
        "garbage",                       # not a dict
        {"typeID": "x", "level": 1},     # non-integer typeID
        {"typeID": 3382, "level": "y"},  # non-integer level
    ]
    info, skills = _parse_blueprint_info_item(item)
    assert info is not None
    assert skills == [
        {"blueprint_type_id": 100, "skill_type_id": 3380, "level": 1},
        {"blueprint_type_id": 100, "skill_type_id": 3381, "level": 1},
    ]
