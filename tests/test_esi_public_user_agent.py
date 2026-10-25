"""ISS-144: public ESI GETs identify the app with a contactable User-Agent."""
import asyncio

import httpx

import app.esi.client as client_mod


def test_get_public_sends_a_user_agent(monkeypatch):
    seen = {}

    class FakeHttp:
        async def get(self, url, headers=None, params=None):
            seen["headers"] = headers
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(client_mod, "get_http_client", lambda: FakeHttp())
    c = client_mod.ESIClient("", cache_enabled=False)
    asyncio.run(c.get_public("/markets/prices/"))
    assert seen["headers"]["User-Agent"].startswith("Vigilant/")
    assert seen["headers"]["Accept"] == "application/json"
