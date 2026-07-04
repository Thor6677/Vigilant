"""Tests for the PWA manifest (Phase 3 Task 3).

Covers: the manifest is served under the /static mount as valid JSON with
the required fields, and the rendered landing page references it via
<link rel="manifest"> + a matching theme-color meta tag.
"""

import json

from fastapi.testclient import TestClient

import app.main as main

REQUIRED_KEYS = {
    "name",
    "short_name",
    "start_url",
    "display",
    "background_color",
    "theme_color",
    "icons",
}


def _client():
    return TestClient(main.app)


def test_manifest_served_as_valid_json_with_required_keys():
    client = _client()
    r = client.get("/static/manifest.json")
    assert r.status_code == 200

    data = json.loads(r.text)
    missing = REQUIRED_KEYS - data.keys()
    assert not missing, f"manifest.json missing required keys: {missing}"

    assert data["name"] == "Vigilant"
    assert data["short_name"] == "Vigilant"
    assert data["start_url"] == "/dashboard"
    assert data["display"] == "standalone"
    assert isinstance(data["icons"], list) and len(data["icons"]) >= 1
    for icon in data["icons"]:
        assert {"src", "sizes", "type"} <= icon.keys()


def test_landing_page_references_manifest_and_theme_color():
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    assert 'rel="manifest" href="/static/manifest.json"' in r.text
    assert 'name="theme-color" content="#080808"' in r.text


def test_manifest_icons_are_reachable():
    client = _client()
    r = client.get("/static/manifest.json")
    data = json.loads(r.text)
    for icon in data["icons"]:
        icon_resp = client.get(icon["src"])
        assert icon_resp.status_code == 200, f"{icon['src']} not served"
        assert icon_resp.headers["content-type"] == "image/png"
