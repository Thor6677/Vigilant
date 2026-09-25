"""The compression calculator prices its ore basket at the SELECTED hub (ISS-031).

Before this, `/industry/compression/calculate` rendered a Jita/Amarr/Dodixie/
Hek/Rens selector and echoed the choice as a "Market" stat, but priced every
ore from EVE's global average reference price. Picking Amarr produced Jita's
numbers labelled Amarr — hub-specific financial advice that was nothing of
the kind. The route now asks the same lowest-sell-at-station helper the
appraisal page uses, and says on the result where the prices came from.
"""
import base64
import json

import itsdangerous
from fastapi.testclient import TestClient

import app.main as main
from tests.conftest import ensure_user
import app.routes.industry as industry


_CSRF = "test-csrf-token-0123456789abcdef"


def _client(user_id: int = 7) -> TestClient:
    """Signed session carrying user_id AND the csrf token the middleware
    checks the X-CSRF-Token header against, so no GET is needed first."""
    ensure_user(user_id)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


# Two compressed ores that both yield tritanium, so the solver has a choice.
_ORES = {
    62516: {"name": "Compressed Veldspar", "volume": 0.15, "group_id": 462, "minerals": {34: 400}},
    62520: {"name": "Compressed Scordite", "volume": 0.19, "group_id": 462, "minerals": {34: 300, 35: 150}},
}


def _stub_everything(monkeypatch, hub_prices, calls):
    async def fake_ore_map(db):
        return _ORES

    async def fake_hub_batch(client, hub_key, type_ids, max_concurrent=10):
        calls.append((hub_key, sorted(type_ids)))
        return {tid: hub_prices.get(tid) for tid in type_ids}

    async def fake_global(client):
        return [{"type_id": 62516, "average_price": 999.0},
                {"type_id": 62520, "average_price": 888.0}]

    def fake_solve(target, ore_data, ore_prices, yield_per_ore, mode="isk", mineral_prices=None):
        calls.append(("solver", dict(ore_prices)))
        tid, price = next(iter(ore_prices.items()))
        return {"ores": [{"type_id": tid, "name": ore_data[tid]["name"], "quantity": 10,
                          "price_each": price, "total_price": price * 10, "volume": 1.5}],
                "total_isk": price * 10, "total_volume": 1.5,
                "minerals_produced": {}, "minerals_surplus": {}}

    monkeypatch.setattr(industry.sde, "get_ore_reprocessing_map", fake_ore_map)
    monkeypatch.setattr(industry, "get_hub_prices_batch", fake_hub_batch)
    monkeypatch.setattr(industry.esi_market, "get_market_prices", fake_global)
    monkeypatch.setattr(industry, "solve_compression", fake_solve)


def _post(client, hub):
    return client.post(
        "/industry/compression/calculate",
        data={"mineral_34": "100000", "trade_hub": hub},
        headers={"X-CSRF-Token": _CSRF},
    )


def test_ores_are_priced_at_the_selected_hub(monkeypatch):
    calls = []
    _stub_everything(monkeypatch, {62516: 12.5, 62520: 20.0}, calls)

    r = _post(_client(), "amarr")
    assert r.status_code == 200

    hub_call = next(c for c in calls if c[0] == "amarr")
    assert hub_call == ("amarr", [62516, 62520]), "every ore in the basket is priced at the chosen hub"
    solver_prices = next(c[1] for c in calls if c[0] == "solver")
    assert solver_prices == {62516: 12.5, 62520: 20.0}, "the solver sees hub prices, not the global average"
    assert "lowest sell at Amarr VIII" in r.text
    assert "hub prices unavailable" not in r.text


def test_an_ore_with_no_sell_orders_at_the_hub_drops_out(monkeypatch):
    """Not for sale there means not in the basket — never priced off the
    global average as a stand-in."""
    calls = []
    _stub_everything(monkeypatch, {62516: 12.5, 62520: None}, calls)

    r = _post(_client(), "hek")
    assert r.status_code == 200
    solver_prices = next(c[1] for c in calls if c[0] == "solver")
    assert solver_prices == {62516: 12.5}


def test_unpriceable_hub_falls_back_to_global_average_and_says_so(monkeypatch):
    calls = []
    _stub_everything(monkeypatch, {}, calls)

    r = _post(_client(), "rens")
    assert r.status_code == 200
    solver_prices = next(c[1] for c in calls if c[0] == "solver")
    assert solver_prices == {62516: 999.0, 62520: 888.0}
    assert "EVE global average (hub prices unavailable)" in r.text


def test_unknown_hub_key_is_treated_as_jita(monkeypatch):
    calls = []
    _stub_everything(monkeypatch, {62516: 1.0, 62520: 1.0}, calls)
    r = _post(_client(), "moonbase-alpha")
    assert r.status_code == 200
    assert calls[0][0] == "jita"
    assert "lowest sell at Jita 4-4" in r.text
