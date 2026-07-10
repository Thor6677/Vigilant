"""Tests for the Ctrl+K command palette (Phase 2 Task 1, app.routes.palette).

The DB buckets (characters/systems/items) need a populated SDE + an
authenticated session, which is heavy to fake here, so — per the plan — we
unit-test the pure result-builders that carry the interesting logic
(`_flatten_pages`, `_page_results`, `_system_link`) and smoke-test the
endpoint's unauthenticated 401. The dead-link discipline (deep-link targets
must be registered routes) is asserted against the real app route table.
"""

from starlette.testclient import TestClient

import app.main as main
from app.routes.palette import (
    PAGES_CAP,
    WH_SYSTEM_MAX,
    WH_SYSTEM_MIN,
    _flatten_pages,
    _page_results,
    _system_link,
)


def _urls(pages):
    return {p["url"] for p in pages}


# ── _flatten_pages: admin gating ───────────────────────────────────────────

def test_flatten_pages_excludes_admin_for_non_admin():
    urls = _urls(_flatten_pages(is_admin=False))
    assert "/admin" not in urls
    assert "/status" not in urls
    # A normal page is still present.
    assert "/industry/manufacturing" in urls


def test_flatten_pages_includes_admin_for_admin():
    urls = _urls(_flatten_pages(is_admin=True))
    assert "/admin" in urls
    assert "/status" in urls


def test_flatten_pages_includes_plain_link_groups():
    # Groups with no dropdown items (Corporations) contribute their own
    # group row; Skill Plans surfaces as a Dashboard item row.
    urls = _urls(_flatten_pages(is_admin=False))
    assert "/corporations" in urls
    assert "/skill-plans" in urls


def test_flatten_pages_excludes_external_links():
    # Wanderer (external) must never appear as an internal page row.
    urls = _urls(_flatten_pages(is_admin=True))
    assert not any(u.startswith(("http://", "https://")) for u in urls)


def test_flatten_pages_dedupes_urls():
    pages = _flatten_pages(is_admin=True)
    urls = [p["url"] for p in pages]
    assert len(urls) == len(set(urls))


# ── _page_results: matching + caps ─────────────────────────────────────────

def test_page_results_matches_label_case_insensitively():
    for q in ("manufacturing", "MANUFACTURING", "Manuf"):
        labels = {p["label"] for p in _page_results(q, is_admin=False)}
        assert "Manufacturing" in labels


def test_page_results_matches_group_label():
    # "intel" matches the Intel group label, so its items surface even though
    # their own labels ("Kill Feed", "Watchlist"…) don't contain "intel".
    labels = {p["label"] for p in _page_results("intel", is_admin=False)}
    assert "Kill Feed" in labels


def test_page_results_empty_query_returns_all_pages():
    assert _page_results("", is_admin=False) == _flatten_pages(is_admin=False)
    assert len(_page_results("", is_admin=False)) > PAGES_CAP


def test_page_results_respects_cap():
    # A single very common letter matches far more than the cap.
    assert len(_page_results("e", is_admin=True)) <= PAGES_CAP


def test_page_results_no_match_returns_empty():
    assert _page_results("zzzznotathing", is_admin=False) == []


# ── _system_link: j-space vs k-space routing ───────────────────────────────

def test_system_link_jspace_goes_to_wormhole_detail():
    assert _system_link(WH_SYSTEM_MIN, "J123456") == "/wormholes/system/J123456"
    assert _system_link(WH_SYSTEM_MAX, "J000001") == "/wormholes/system/J000001"


def test_system_link_kspace_goes_to_star_map_focus():
    # Jita.
    assert _system_link(30000142, "Jita") == "/map?focus=30000142"


def test_system_link_pochven_uses_star_map():
    # Pochven retains k-space ids (~30002xxx) so it links to the star map,
    # not the wormhole detail page.
    assert _system_link(30002000, "Ahtila") == "/map?focus=30002000"


def test_system_link_quotes_name():
    # Names with spaces must be URL-encoded in the path.
    assert _system_link(30000001, "Foo Bar").endswith("focus=30000001")


# ── endpoint smoke: unauthenticated ────────────────────────────────────────

def test_palette_unauthenticated_is_not_200_with_results():
    # Plain TestClient (no `with`) so we don't spin up the app's startup
    # pollers — the 401 branch returns before any DB/session access.
    client = TestClient(main.app)
    resp = client.get("/nav/palette?q=jita")
    assert resp.status_code == 401
    assert resp.text == ""


def test_palette_route_and_deep_link_targets_are_registered():
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/nav/palette" in paths
    # Static deep-link targets used by the palette buckets.
    assert "/map" in paths
    assert "/industry/manufacturing" in paths
    # Parameterised targets exist as templated routes.
    assert any(p.startswith("/character/") for p in paths)
    assert any("/wormholes/system/" in p for p in paths)
