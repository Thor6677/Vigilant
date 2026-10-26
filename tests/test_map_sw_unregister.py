"""The star-map page removes the old map service worker instead of registering it.

An earlier release registered a worker with scope /map/data/. It controls no
page and caches three files nothing reads (ISS-148). The page now unregisters
it and clears its cache. There is no JS test runner here, so this pins the
source of frontend/src/main.tsx.
"""
import pathlib
import re

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src" / "main.tsx"


def _src() -> str:
    return _MAIN.read_text()


def test_the_page_no_longer_registers_a_service_worker():
    assert not re.search(r"serviceWorker\s*\.\s*register\s*\(", _src())


def test_the_page_unregisters_only_the_old_map_scope():
    src = _src()
    assert "getRegistrations" in src
    assert ".unregister()" in src
    assert "'/map/data/'" in src


def test_the_page_clears_the_old_cache():
    assert "caches.delete('vigilant-mapdata-v1')" in _src()
