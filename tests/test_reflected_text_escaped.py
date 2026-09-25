"""Text a user typed comes back in HTML partials as text, not markup.

Each of these handlers answers an htmx request with a short HTML fragment that
repeats part of the input — a system name that was not found, a solver error
naming the requested mode. Jinja templates escape by default; these f-string
fragments have to do it themselves.
"""
import pytest

import app.routes.industry as industry
from app.industry.compression import solve_compression
from app.routes.gatecheck import _err
from tests.test_compression_hub_pricing import _CSRF, _client, _stub_everything

MARKUP = '<form action="https://example.org/"><input name="p"></form>'
ESCAPED = "&lt;form action=&quot;https://example.org/&quot;&gt;"


def test_unknown_pi_system_is_escaped():
    r = _client().get("/industry/planetary/lookup/system", params={"name": MARKUP})
    assert r.status_code == 200
    assert "<form" not in r.text
    assert ESCAPED in r.text


@pytest.mark.parametrize("field", ["origin", "destination"])
def test_unknown_gatecheck_system_is_escaped(field, monkeypatch):
    import app.routes.gatecheck as gatecheck

    async def known(db, name):
        return {"Jita": 30000142, "Amarr": 30002187}.get(name)

    monkeypatch.setattr(gatecheck.sde, "system_name_to_id", known)
    data = {"origin": "Jita", "destination": "Amarr", field: MARKUP}
    r = _client().post("/intel/gatecheck/check", data=data, headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "<form" not in r.text
    assert ESCAPED in r.text


def test_gatecheck_error_line_treats_its_message_as_text():
    out = _err(f"Could not find: {MARKUP}")
    assert "<form" not in out
    assert ESCAPED in out


def test_compression_solver_error_is_escaped(monkeypatch):
    _stub_everything(monkeypatch, {62516: 12.5, 62520: 20.0}, [])
    monkeypatch.setattr(industry, "solve_compression", solve_compression)
    r = _client().post(
        "/industry/compression/calculate",
        data={"mineral_34": "100000", "trade_hub": "jita", "mode": MARKUP},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert r.status_code == 200
    assert "Unknown mode" in r.text
    assert "<form" not in r.text
    assert ESCAPED in r.text
