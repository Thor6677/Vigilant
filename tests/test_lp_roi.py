"""Tests for the LP store ROI calculator (`app.market.lp`).

Pure math (`offer_economics` / `rank_offers`) is tested directly against
fixture offers; the cache layers are tested with monkeypatched fetch + clock
seams (no network); route gating via TestClient. Manual-event-loop idiom per
repo convention.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.market import lp


PRICES = {34: 5.0, 100: 1_000_000.0, 200: 250.0}


def _offer(**kw):
    base = {
        "offer_id": 1, "type_id": 100, "quantity": 1,
        "lp_cost": 1000, "isk_cost": 100_000, "required_items": [],
    }
    base.update(kw)
    return base


# ── pure math ────────────────────────────────────────────────────────────────

def test_offer_economics_priced_offer():
    econ = lp.offer_economics(
        _offer(required_items=[{"type_id": 200, "quantity": 100}]), PRICES,
    )
    # sell 1,000,000 − isk 100,000 − materials 25,000 = 875,000 over 1000 LP
    assert econ["priced"] is True
    assert econ["materials_cost"] == 25_000.0
    assert econ["isk_per_lp"] == 875.0


def test_offer_economics_zero_lp_guard():
    econ = lp.offer_economics(_offer(lp_cost=0), PRICES)
    assert econ["isk_per_lp"] is None


def test_offer_economics_unpriced_award_excluded():
    econ = lp.offer_economics(_offer(type_id=999999), PRICES)
    assert econ["priced"] is False
    assert econ["isk_per_lp"] is None


def test_offer_economics_unpriced_required_item_not_zeroed():
    # An unpriced required item must NOT be treated as free — the whole offer
    # becomes unpriced (under-ranking is the safe direction).
    econ = lp.offer_economics(
        _offer(required_items=[{"type_id": 999999, "quantity": 1}]), PRICES,
    )
    assert econ["materials_cost"] is None
    assert econ["isk_per_lp"] is None


def test_rank_offers_priced_first_best_first():
    offers = [
        _offer(offer_id=1, lp_cost=0),                      # unpriced → last
        _offer(offer_id=2, lp_cost=1000),                   # 900 ISK/LP
        _offer(offer_id=3, lp_cost=100, isk_cost=0),        # 10,000 ISK/LP
    ]
    ranked = lp.rank_offers(offers, PRICES)
    assert [r["offer_id"] for r in ranked] == [3, 2, 1]
    assert ranked[-1]["isk_per_lp"] is None


# ── offers cache (24h TTL, stale-on-error) ──────────────────────────────────

def test_offers_cache_ttl_and_stale_on_error(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(lp, "_offers_cache", {})
        monkeypatch.setattr(lp, "_offers_locks", {})

        calls = []

        async def fake_fetch(corp_id):
            calls.append(corp_id)
            return [_offer(offer_id=len(calls))]

        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(lp, "_fetch_offers_esi", fake_fetch)
        monkeypatch.setattr(lp, "_now", lambda: now)

        first = loop.run_until_complete(lp.get_offers(1000125))
        second = loop.run_until_complete(lp.get_offers(1000125))
        assert len(calls) == 1 and first == second  # within TTL → one fetch

        now += timedelta(hours=25)
        monkeypatch.setattr(lp, "_now", lambda: now)
        loop.run_until_complete(lp.get_offers(1000125))
        assert len(calls) == 2  # stale → refetched

        async def failing_fetch(corp_id):
            raise RuntimeError("esi down")

        now += timedelta(hours=25)
        monkeypatch.setattr(lp, "_now", lambda: now)
        monkeypatch.setattr(lp, "_fetch_offers_esi", failing_fetch)
        stale = loop.run_until_complete(lp.get_offers(1000125))
        assert stale  # stale-on-error returns the cached rows

        fresh_corp = loop.run_until_complete(lp.get_offers(1000129))
        assert fresh_corp == []  # error with no cache → empty
    finally:
        loop.close()


# ── NPC corp roster (forever cache; failures not cached) ────────────────────

def test_npc_corps_forever_cache_and_failure_retry(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(lp, "_npc_corp_ids", None)
        monkeypatch.setattr(lp, "_npc_corp_names", {})

        async def failing_ids():
            raise RuntimeError("esi down")

        monkeypatch.setattr(lp, "_fetch_npc_corp_ids_esi", failing_ids)
        assert loop.run_until_complete(lp.get_npc_corps()) == []
        # failure must NOT be cached — a later successful fetch populates
        calls = []

        async def good_ids():
            calls.append(1)
            return [1000125, 1000129]

        async def good_names(ids):
            return {1000125: "Caldari Navy", 1000129: "State War Academy"}

        monkeypatch.setattr(lp, "_fetch_npc_corp_ids_esi", good_ids)
        monkeypatch.setattr(lp, "_fetch_names_esi", good_names)
        corps = loop.run_until_complete(lp.get_npc_corps())
        assert [c["name"] for c in corps] == ["Caldari Navy", "State War Academy"]

        loop.run_until_complete(lp.get_npc_corps())
        assert len(calls) == 1  # forever-cached after success
    finally:
        loop.close()


# ── route gating ─────────────────────────────────────────────────────────────

def test_lp_routes_auth_gated():
    from fastapi.testclient import TestClient
    import app.main as main

    client = TestClient(main.app, follow_redirects=False)
    r = client.get("/market/lp")
    assert r.status_code in (302, 303, 307)  # redirect to login
    r = client.get("/market/lp/offers?corporation_id=1000125")
    assert r.status_code == 401
