"""The star map's file routes serve only files inside their own folder.

`/map/assets/{file_path:path}` and `/map/data/{file_path:path}` join a request
path onto a base folder. The router hands over the path already percent-decoded,
so it can carry `..` segments or be absolute, and pathlib's `/` drops the base
when the right-hand side is absolute. Each case below asks for a file that sits
next to the dist folder and must not be served.

The tree lives in tmp_path and FRONTEND_DIST is monkeypatched, so these tests
never depend on a frontend build being present.
"""
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.routes import starmap

OUTSIDE = "outside-the-dist-folder"


@pytest.fixture
def dist(tmp_path, monkeypatch):
    root = tmp_path / "frontend" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (root / "data" / "systems.json").write_text("[]")
    (tmp_path / "frontend" / "sentinel.txt").write_text(OUTSIDE)
    (tmp_path / "sentinel.txt").write_text(OUTSIDE)
    monkeypatch.setattr(starmap, "FRONTEND_DIST", root)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(main.app, follow_redirects=False)


def test_real_files_are_served(dist, client):
    r = client.get("/map/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.text == "console.log(1)"
    r = client.get("/map/data/systems.json")
    assert r.status_code == 200
    assert r.text == "[]"


@pytest.mark.parametrize("folder", ["assets", "data"])
def test_encoded_parent_segments_are_refused(dist, client, folder):
    for path in (
        f"/map/{folder}/..%2F..%2Fsentinel.txt",
        f"/map/{folder}/..%2f..%2f..%2fsentinel.txt",
        f"/map/{folder}/%2e%2e/%2e%2e/sentinel.txt",
        f"/map/{folder}/%2e%2e%2F%2e%2e%2F%2e%2e%2Fsentinel.txt",
    ):
        r = client.get(path)
        assert r.status_code == 404, path
        assert OUTSIDE not in r.text, path


@pytest.mark.parametrize("folder", ["assets", "data"])
def test_absolute_paths_are_refused(dist, client, folder):
    target = str(dist / "sentinel.txt")
    for path in (
        f"/map/{folder}/{quote(target, safe='')}",   # %2F-encoded absolute path
        f"/map/{folder}/{target}",                    # double slash after the folder
    ):
        r = client.get(path)
        assert r.status_code == 404, path
        assert OUTSIDE not in r.text, path


def test_symlink_out_of_the_folder_is_refused(dist, client):
    link = dist / "frontend" / "dist" / "assets" / "link.txt"
    link.symlink_to(dist / "sentinel.txt")
    r = client.get("/map/assets/link.txt")
    assert r.status_code == 404
    assert OUTSIDE not in r.text


def test_folder_itself_and_missing_files_are_404(dist, client):
    assert client.get("/map/assets/").status_code == 404
    assert client.get("/map/assets/nope.js").status_code == 404
    assert client.get("/map/assets/%00").status_code == 404
