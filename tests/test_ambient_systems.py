"""Tests the systems.json slimming logic used by /api/ambient/systems.json.

The full route touches the filesystem (runtime /data/map/ vs. bundled
frontend/dist/data/ fallback) and process-lifetime caching, so we test
the extracted pure function instead of the FastAPI route object — same
approach as test_ambient_kills.py.
"""
from app.routes.ambient import _slim_systems


def test_slim_systems_drops_extra_keys_keeps_expected():
    raw = [
        {
            "id": 30000001,
            "name": "Tanoo",
            "x": 5749.2,
            "y": 5309.4,
            "sec": 0.86,
            "conId": 20000001,
            "conName": "San Matar",
            "regId": 10000001,
            "regName": "Derelik",
            "hasStation": True,
            "stns": 2,
            "svcs": ["cloning", "market"],
            "x3": -9.3553,
            "y3": 4.4783,
            "z3": -4.7049,
        }
    ]
    result = _slim_systems(raw)
    assert result == [
        {
            "id": 30000001,
            "name": "Tanoo",
            "x3": -9.3553,
            "y3": 4.4783,
            "z3": -4.7049,
        }
    ]


def test_slim_systems_empty_list():
    assert _slim_systems([]) == []


def test_slim_systems_multiple_entries_preserve_order():
    raw = [
        {"id": 1, "name": "A", "x3": 1.0, "y3": 2.0, "z3": 3.0, "sec": 1.0},
        {"id": 2, "name": "B", "x3": 4.0, "y3": 5.0, "z3": 6.0, "sec": 0.5},
    ]
    result = _slim_systems(raw)
    assert [r["id"] for r in result] == [1, 2]
    assert all(set(r.keys()) == {"id", "name", "x3", "y3", "z3"} for r in result)
