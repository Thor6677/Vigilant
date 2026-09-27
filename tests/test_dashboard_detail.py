"""app/dashboard/detail.py (T-076): Detailed/Table-mode pure per-pilot
extras — PI, industry, market, SP, and the wallet sparkline builder.
Covers no_scope / missing / empty data for every one.
"""
from datetime import datetime, timedelta, timezone

from app.dashboard.detail import (
    industry_detail, market_detail, pi_detail, sp_detail,
    sparkline_delta, wallet_sparkline_svg,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


# ── pi_detail ─────────────────────────────────────────────────────────────

def test_pi_detail_none_for_no_scope_and_not_synced():
    assert pi_detail("no_scope") is None
    assert pi_detail(None) is None
    assert pi_detail([]) is None


def test_pi_detail_counts_colonies_and_soonest_expiry():
    planets = [
        {"planet_id": 1, "expiry_time": (NOW + timedelta(hours=10)).isoformat().replace("+00:00", "Z")},
        {"planet_id": 2, "expiry_time": (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")},
    ]
    out = pi_detail(planets, now=NOW)
    assert out["colonies"] == 2
    assert out["expired"] is False
    assert out["expiry_str"]  # non-empty duration string


def test_pi_detail_reports_expired_when_any_colony_is_past_due():
    planets = [
        {"planet_id": 1, "expiry_time": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        {"planet_id": 2, "expiry_time": (NOW + timedelta(hours=5)).isoformat().replace("+00:00", "Z")},
    ]
    out = pi_detail(planets, now=NOW)
    assert out["expired"] is True
    assert out["expiry_str"] == "expired"


def test_pi_detail_handles_missing_expiry_times():
    out = pi_detail([{"planet_id": 1}], now=NOW)
    assert out["colonies"] == 1
    assert out["expiry_str"] == "—"


# ── industry_detail / market_detail ─────────────────────────────────────────

def test_industry_detail_none_for_missing_or_empty():
    assert industry_detail("no_scope") is None
    assert industry_detail(None) is None
    assert industry_detail([]) is None


def test_industry_detail_counts_active_and_ready():
    jobs = [
        {"status": "active"}, {"status": "active"}, {"status": "ready"},
        {"status": "delivered"},
    ]
    out = industry_detail(jobs)
    assert out == {"active": 2, "ready": 1}


def test_market_detail_none_for_missing_or_empty():
    assert market_detail("no_scope") is None
    assert market_detail(None) is None
    assert market_detail([]) is None


def test_market_detail_sums_escrow_and_counts_orders():
    orders = [
        {"is_buy_order": True, "escrow": 1_000_000.0},
        {"is_buy_order": False, "escrow": 0.0},
    ]
    out = market_detail(orders)
    assert out == {"open_orders": 2, "escrow": 1_000_000.0}


# ── sp_detail ────────────────────────────────────────────────────────────

def test_sp_detail_no_scope_passthrough():
    assert sp_detail("no_scope") == "no_scope"


def test_sp_detail_none_for_not_synced():
    assert sp_detail(None) is None


def test_sp_detail_totals():
    out = sp_detail({"total_sp": 5_000_000, "unallocated_sp": 250_000, "levels": {}})
    assert out == {"total_sp": 5_000_000, "unallocated_sp": 250_000}


# ── wallet_sparkline_svg / sparkline_delta ──────────────────────────────────

def test_wallet_sparkline_svg_none_for_no_or_one_point():
    assert wallet_sparkline_svg([]) is None
    assert wallet_sparkline_svg([(NOW, 1.0)]) is None


def test_wallet_sparkline_svg_renders_an_svg_for_two_or_more_points():
    points = [(NOW - timedelta(days=1), 100.0), (NOW, 200.0)]
    svg = wallet_sparkline_svg(points)
    assert svg.startswith("<svg")
    assert "polyline" in svg
    assert "polygon" in svg


def test_sparkline_delta_none_for_insufficient_points():
    assert sparkline_delta([]) is None
    assert sparkline_delta([(NOW, 1.0)]) is None


def test_sparkline_delta_direction_and_amount():
    up = sparkline_delta([(NOW - timedelta(days=1), 100.0), (NOW, 150.0)])
    assert up == {"direction": "up", "amount": 50.0}
    down = sparkline_delta([(NOW - timedelta(days=1), 150.0), (NOW, 100.0)])
    assert down == {"direction": "down", "amount": 50.0}
    flat = sparkline_delta([(NOW - timedelta(days=1), 100.0), (NOW, 100.0)])
    assert flat == {"direction": "flat", "amount": 0.0}
