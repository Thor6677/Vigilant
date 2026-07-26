"""Tests for the EVE-Scout Thera/Turnur feed module (Phase 3 Task 2).

`app.intel.evescout` is the server-side home for fetching + parsing EVE-Scout's
public wormhole signatures. It exposes:

  * `parse_connections`  — pure transform to undirected (src, dst, via) edges,
  * `get_signatures`     — TTL-cached raw fetch (single-flight, stale-on-error),
  * `get_connections`    — fetch + parse.

The route-planner UI already splices these edges into its client-side graph; the
edge-injection seam that matters server-side is `parse_connections` producing
correct system-id pairs, which these tests assert against a captured fixture.

Network is never hit — `_fetch_raw` and the clock (`_now`) are monkeypatched.
Sync-style loop per tests/test_discord_alert_relay.py.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.intel.evescout as evescout

FIXTURE = Path(__file__).parent / "fixtures" / "evescout_signatures.json"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _load_fixture():
    return json.loads(FIXTURE.read_text())


def _reset_cache(monkeypatch):
    """Clear the module cache so each test starts cold."""
    monkeypatch.setattr(evescout, "_cache_data", None)
    monkeypatch.setattr(evescout, "_cache_fetched_at", None)


# ── parse_connections ─────────────────────────────────────────────────────────

def test_parse_fixture_yields_anchored_edges():
    conns = evescout.parse_connections(_load_fixture())

    assert conns, "fixture should parse to at least one connection"
    # Every connection is anchored at Thera or Turnur on its `src` side.
    for c in conns:
        assert c.src in evescout.ANCHOR_SYSTEM_IDS
        assert c.via in ("Thera", "Turnur")
        assert isinstance(c.dst, int) and c.dst != c.src
        # life_hours is an int or None; wh_type/signature are strings.
        assert c.life_hours is None or isinstance(c.life_hours, int)
        assert isinstance(c.wh_type, str)

    # Fixture was captured with both hubs represented.
    vias = {c.via for c in conns}
    assert vias == {"Thera", "Turnur"}
    # Anchor IDs match the verified live values.
    assert evescout.THERA_SYSTEM_ID == 31000005
    assert evescout.TURNUR_SYSTEM_ID == 30002086


def test_parse_filters_and_dedupes():
    rows = [
        # Valid Thera wormhole.
        {"signature_type": "wormhole", "out_system_id": 31000005,
         "in_system_id": 30000142, "wh_type": "Q063", "remaining_hours": 4,
         "out_signature": "ABC-001"},
        # Duplicate of the above (undirected) — should be dropped.
        {"signature_type": "wormhole", "out_system_id": 31000005,
         "in_system_id": 30000142, "wh_type": "Q063", "remaining_hours": 3,
         "out_signature": "ABC-001"},
        # Not a wormhole (e.g. a gate signature) — dropped.
        {"signature_type": "gate", "out_system_id": 30002086,
         "in_system_id": 30000144},
        # Out side is not an anchor system — dropped.
        {"signature_type": "wormhole", "out_system_id": 30000142,
         "in_system_id": 30000144},
        # Missing in_system_id — dropped.
        {"signature_type": "wormhole", "out_system_id": 30002086,
         "in_system_id": None, "wh_type": "K162"},
        # Valid Turnur wormhole.
        {"signature_type": "wormhole", "out_system_id": 30002086,
         "in_system_id": 30003504, "wh_type": "K162"},
    ]
    conns = evescout.parse_connections(rows)

    pairs = {(c.src, c.dst) for c in conns}
    assert pairs == {(31000005, 30000142), (30002086, 30003504)}
    assert len(conns) == 2


def test_parse_empty_or_none():
    assert evescout.parse_connections([]) == []
    assert evescout.parse_connections(None) == []


# ── get_signatures: TTL cache ─────────────────────────────────────────────────

def test_ttl_cache_serves_cache_within_window(monkeypatch):
    _reset_cache(monkeypatch)
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return [{"signature_type": "wormhole", "out_system_id": 31000005,
                 "in_system_id": 30000142}]

    t0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    monkeypatch.setattr(evescout, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(evescout, "_now", lambda: clock["t"])

    # First call fetches.
    data1 = _run(evescout.get_signatures())
    assert calls["n"] == 1
    assert len(data1) == 1

    # Within TTL: served from cache, no second fetch.
    clock["t"] = t0 + timedelta(seconds=evescout.CACHE_TTL_SECONDS - 1)
    _run(evescout.get_signatures())
    assert calls["n"] == 1

    # Past TTL: refetches.
    clock["t"] = t0 + timedelta(seconds=evescout.CACHE_TTL_SECONDS + 1)
    _run(evescout.get_signatures())
    assert calls["n"] == 2


def test_force_bypasses_ttl(monkeypatch):
    _reset_cache(monkeypatch)
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return []

    monkeypatch.setattr(evescout, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(evescout, "_now",
                        lambda: datetime(2026, 7, 4, tzinfo=timezone.utc))

    _run(evescout.get_signatures())
    _run(evescout.get_signatures(force=True))
    assert calls["n"] == 2


# ── get_signatures: stale-on-error ────────────────────────────────────────────

def test_stale_on_error_returns_last_good(monkeypatch):
    _reset_cache(monkeypatch)
    good = [{"signature_type": "wormhole", "out_system_id": 31000005,
             "in_system_id": 30000142}]
    state = {"fail": False}

    async def fake_fetch():
        if state["fail"]:
            raise RuntimeError("eve-scout down")
        return good

    t0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    clock = {"t": t0}
    monkeypatch.setattr(evescout, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(evescout, "_now", lambda: clock["t"])

    # Prime the cache.
    assert _run(evescout.get_signatures()) == good

    # Expire TTL and make the fetch fail — should still return the stale cache.
    state["fail"] = True
    clock["t"] = t0 + timedelta(seconds=evescout.CACHE_TTL_SECONDS + 1)
    assert _run(evescout.get_signatures()) == good


def test_error_with_no_cache_returns_empty(monkeypatch):
    _reset_cache(monkeypatch)

    async def fake_fetch():
        raise RuntimeError("boom")

    monkeypatch.setattr(evescout, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(evescout, "_now",
                        lambda: datetime(2026, 7, 4, tzinfo=timezone.utc))

    assert _run(evescout.get_signatures()) == []


# ── get_connections: fetch + parse together ───────────────────────────────────

def test_get_connections_end_to_end(monkeypatch):
    _reset_cache(monkeypatch)
    fixture = _load_fixture()

    async def fake_fetch():
        return fixture

    monkeypatch.setattr(evescout, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(evescout, "_now",
                        lambda: datetime(2026, 7, 4, tzinfo=timezone.utc))

    conns = _run(evescout.get_connections())
    assert conns == evescout.parse_connections(fixture)
    assert all(c.via in ("Thera", "Turnur") for c in conns)
