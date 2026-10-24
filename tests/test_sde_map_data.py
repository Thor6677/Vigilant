"""K-space map generator (app/sde/map_data.py) and its loader hook (T-085).

The generator used to live in scripts/, which the image never contains, so
the post-import regeneration failed with a swallowed ImportError in every
container. These tests build a tiny SDE zip in memory and pin the
packaging boundary that hid the bug.
"""
import ast
import io
import json
import logging
import re
import zipfile
from pathlib import Path

import pytest

from app.sde import loader, map_data
from app.sde.map_data import build_from_zip

ROOT = Path(__file__).resolve().parents[1]


def _jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows).encode()


def _system(sid, name, x, y, z=0.0):
    return {
        "_key": sid, "name": {"en": name}, "securityStatus": 0.9,
        "constellationID": 20000001, "regionID": 10000001,
        "position": {"x": x, "y": y, "z": z},
        "position2D": {"x": x, "y": z},
    }


def _make_zip(systems=None):
    if systems is None:
        systems = [
            _system(30000001, "Alpha", 0.0, 0.0, 0.0),
            _system(30000002, "Beta", 9.461e15, 0.0, 9.461e15),
            _system(31000001, "J100000", 5.0, 5.0, 5.0),
        ]
    files = {
        "mapRegions.jsonl": [{"_key": 10000001, "name": {"en": "Region"}}],
        "mapConstellations.jsonl": [
            {"_key": 20000001, "name": {"en": "Con"}, "regionID": 10000001}],
        "mapSolarSystems.jsonl": systems,
        "stationServices.jsonl": [{"_key": 7, "serviceName": {"en": "Market"}}],
        "stationOperations.jsonl": [{"_key": 1, "services": [7, 99]}],
        "npcStations.jsonl": [{"_key": 1, "solarSystemID": 30000001, "operationID": 1}],
        "mapStargates.jsonl": [
            {"solarSystemID": 30000001, "destination": {"solarSystemID": 30000002}},
            {"solarSystemID": 30000002, "destination": {"solarSystemID": 30000001}},
            {"solarSystemID": 30000002, "destination": {"solarSystemID": 31000001}},
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, rows in files.items():
            zf.writestr(name, _jsonl(rows))
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def test_build_from_zip_writes_three_files(tmp_path):
    build_from_zip(_make_zip(), tmp_path)

    systems = json.loads((tmp_path / "systems.json").read_text())
    edges = json.loads((tmp_path / "edges.json").read_text())
    regions = json.loads((tmp_path / "regions.json").read_text())

    assert [s["id"] for s in systems] == [30000001, 30000002]  # J-system dropped
    alpha, beta = systems
    assert alpha["svcs"] == ["market"] and beta["svcs"] == []
    assert alpha["hasStation"] and alpha["stns"] == 1
    assert beta["x3"] == pytest.approx(1.0) and beta["z3"] == pytest.approx(1.0)
    for key in ("x3", "y3", "z3"):
        assert key in alpha
    assert edges == [[30000001, 30000002]]  # reversed duplicate and J gate gone
    assert len(regions) == 1
    assert regions[0]["cx"] == pytest.approx((alpha["x"] + beta["x"]) / 2, abs=0.1)
    assert regions[0]["cy"] == pytest.approx((alpha["y"] + beta["y"]) / 2, abs=0.1)


def test_no_kspace_systems_raises_value_error_not_system_exit(tmp_path):
    # SystemExit would escape the event loop and crash-loop the container.
    zf = _make_zip([_system(31000001, "J100000", 1.0, 1.0)])
    with pytest.raises(ValueError):
        build_from_zip(zf, tmp_path)


def test_failed_write_leaves_previous_files_and_no_temp(tmp_path, monkeypatch):
    build_from_zip(_make_zip(), tmp_path)
    old_edges = (tmp_path / "edges.json").read_text()
    (tmp_path / "systems.json").unlink()  # prove a new one is not swapped in

    real_dump = map_data.json.dump

    def flaky_dump(data, f, **kw):
        if f.name.endswith("edges.json.tmp"):
            f.write("[[")
            raise OSError("disk full")
        return real_dump(data, f, **kw)

    monkeypatch.setattr(map_data.json, "dump", flaky_dump)
    with pytest.raises(OSError):
        build_from_zip(_make_zip(), tmp_path)

    assert (tmp_path / "edges.json").read_text() == old_edges
    assert not (tmp_path / "systems.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_successful_write_leaves_only_the_three_files(tmp_path):
    (tmp_path / "edges.json").write_text("stale")
    build_from_zip(_make_zip(), tmp_path)
    assert json.loads((tmp_path / "edges.json").read_text()) == [[30000001, 30000002]]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "edges.json", "regions.json", "systems.json"]


def test_loader_helper_writes_files(tmp_path):
    loader._regenerate_kspace_map(_make_zip(), tmp_path)
    assert (tmp_path / "systems.json").exists()


def test_loader_helper_is_non_fatal(tmp_path, caplog):
    zf = _make_zip([_system(31000001, "J100000", 1.0, 1.0)])
    with caplog.at_level(logging.WARNING, logger=loader.log.name):
        loader._regenerate_kspace_map(zf, tmp_path)  # must not raise
    assert "Map-data regeneration failed" in caplog.text
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)
    assert list(tmp_path.iterdir()) == []


def _app_trees():
    return [(p, ast.parse(p.read_text())) for p in (ROOT / "app").rglob("*.py")]


def test_nothing_under_app_mutates_sys_path():
    offenders = []
    for path, tree in _app_trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "path"
                    and isinstance(node.value, ast.Name) and node.value.id == "sys"
                    and isinstance(node.ctx, ast.Store)):
                offenders.append(f"{path}:{node.lineno} assigns sys.path")
            if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "path"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "sys"):
                offenders.append(f"{path}:{node.lineno} assigns into sys.path")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"insert", "append", "extend"}
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "path"
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "sys"):
                offenders.append(f"{path}:{node.lineno} sys.path.{node.func.attr}")
    assert not offenders, offenders


def test_loader_imports_generator_from_app_package():
    tree = ast.parse((ROOT / "app/sde/loader.py").read_text())
    top_level = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "app.sde.map_data" and
               any(a.name == "build_from_zip" for a in n.names)
               for n in top_level)


def test_import_regenerates_map_before_clearing_in_progress_flag():
    tree = ast.parse((ROOT / "app/sde/loader.py").read_text())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "download_and_import")
    regen, clear = [], []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "_regenerate_kspace_map":
            regen.append(node.lineno)
        if (isinstance(node.func, ast.Name) and node.func.id == "_set_meta"
                and len(node.args) >= 3
                and [getattr(a, "value", None) for a in node.args[1:3]]
                == ["import_in_progress", "0"]):
            clear.append(node.lineno)
    assert regen, "download_and_import no longer regenerates the K-space map"
    assert clear and min(regen) < min(clear)


def test_dockerfile_still_copies_app_wholesale():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^COPY app/ \./app/$", dockerfile, re.M)


def test_dockerignore_does_not_exclude_app():
    lines = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()]
    bad = [ln for ln in lines if ln and not ln.startswith("#")
           and ln.lstrip("!/").startswith("app")]
    assert not bad, bad
