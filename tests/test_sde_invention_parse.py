from app.sde.loader import _parse_invention_item


FIXTURE = {
    "_key": "687",  # Rifter Blueprint
    "activities": {"invention": {
        "materials": [{"typeID": 20410, "quantity": 8},
                      {"typeID": 20424, "quantity": 8}],
        "products": [{"typeID": 11373, "probability": 0.3, "quantity": 1}],
        "skills": [{"typeID": 3402, "level": 1}, {"typeID": 11433, "level": 1},
                   {"typeID": 21791, "level": 1}],
        "time": 63900,
    }},
}


def test_parse_invention_item_full():
    info, mats, skills = _parse_invention_item(FIXTURE)
    assert info == {"blueprint_type_id": 687, "product_blueprint_type_id": 11373,
                    "probability": 0.3, "base_runs": 1, "time": 63900}
    assert {m["material_type_id"] for m in mats} == {20410, 20424}
    assert all(m["blueprint_type_id"] == 687 for m in mats)
    assert {s["skill_type_id"] for s in skills} == {3402, 11433, 21791}


def test_parse_invention_item_absent():
    assert _parse_invention_item({"_key": "1", "activities": {}}) == (None, [], [])


def test_parse_invention_item_no_products():
    item = {"_key": "1", "activities": {"invention": {"materials": [], "skills": []}}}
    assert _parse_invention_item(item) == (None, [], [])
