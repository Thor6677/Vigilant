"""Tests for the single-source navigation registry (`app.nav`).

The headline test is the **dead-link** guard: every internal URL in
`NAV_GROUPS` (both group-level and item-level) must resolve to a route
registered on the real FastAPI app. This makes future orphan pages or typo'd
URLs a hard test failure rather than a broken link discovered in production.

We import `app.main` to collect the authoritative route table. This is cheap
and side-effect-free under the test env (conftest.py sets the required
EVE_CLIENT_ID / EVE_CLIENT_SECRET / SECRET_KEY vars); the module import builds
the app + routers without starting the server or touching the network, so
importing it is the most faithful source of "what paths actually exist".
"""

import os

from jinja2 import Environment

import app.main as main
from app.nav import NAV_GROUPS, item_active, group_active

_BASE_HTML = os.path.join(
    os.path.dirname(__file__), "..", "app", "templates", "base.html"
)


def _base_html_source():
    with open(_BASE_HTML, encoding="utf-8") as fh:
        return fh.read()


# ── helpers to walk the registry ───────────────────────────────────────────

def _internal_group_urls():
    for group in NAV_GROUPS:
        url = group["url"]
        if url and not url.startswith(("http://", "https://", "#")):
            yield group["label"], url


def _internal_item_urls():
    for group in NAV_GROUPS:
        for item in group["items"]:
            if item.get("external"):
                continue
            url = item["url"]
            if url and not url.startswith(("http://", "https://", "#")):
                yield group["label"], item["label"], url


def _registered_paths():
    return {route.path for route in main.app.routes if hasattr(route, "path")}


def _find(label):
    """Return the first item dict with the given label, searching all groups."""
    for group in NAV_GROUPS:
        for item in group["items"]:
            if item["label"] == label:
                return item
    raise KeyError(label)


def _group(label):
    for group in NAV_GROUPS:
        if group["label"] == label:
            return group
    raise KeyError(label)


# ── dead-link guard ─────────────────────────────────────────────────────────

def test_every_group_url_is_a_registered_route():
    paths = _registered_paths()
    dead = [(label, url) for label, url in _internal_group_urls()
            if url not in paths]
    assert not dead, f"Group URLs with no registered route: {dead}"


def test_every_item_url_is_a_registered_route():
    paths = _registered_paths()
    dead = [(g, lbl, url) for g, lbl, url in _internal_item_urls()
            if url not in paths]
    assert not dead, f"Item URLs with no registered route: {dead}"


# ── helper unit tests ───────────────────────────────────────────────────────

def test_exact_match_only_matches_exact_path():
    # "Overview" label repeats across groups; grab the Industry one explicitly.
    overview = next(i for i in _group("Industry")["items"] if i["label"] == "Overview")
    assert item_active(overview, "/industry") is True
    assert item_active(overview, "/industry/manufacturing") is False


def test_prefix_match_matches_subpaths():
    mfg = _find("Manufacturing")
    assert item_active(mfg, "/industry/manufacturing") is True
    assert item_active(mfg, "/industry/manufacturing/blueprint/123") is True
    assert item_active(mfg, "/industry") is False


def test_dscan_item_matches_intel_prefix_only():
    """Legacy /dscan paths 301-redirect (app/routes/dscan.py), so they are
    no longer resting paths and need no active-state rule."""
    dscan = _find("D-Scan / Local")
    assert item_active(dscan, "/intel/dscan") is True
    assert item_active(dscan, "/intel/dscan/456") is True
    assert item_active(dscan, "/dscan") is False
    assert item_active(dscan, "/intel/watch") is False


def test_image_host_matches_tools_images_and_i_shortlink():
    img = _find("Image Host")
    assert item_active(img, "/tools/images") is True
    assert item_active(img, "/i/abc123") is True
    assert item_active(img, "/tools/fitting") is False


def test_ship_fitting_prefix_vs_saved_fits_exclude():
    # /tools/fitting/saved must light Saved Fits but NOT Ship Fitting;
    # other sub-pages (e.g. /tools/fitting/compare) light Ship Fitting.
    fitting = _find("Ship Fitting")
    saved = _find("Saved Fits")
    assert item_active(fitting, "/tools/fitting") is True
    assert item_active(fitting, "/tools/fitting/compare") is True
    assert item_active(fitting, "/tools/fitting/saved") is False
    assert item_active(saved, "/tools/fitting/saved") is True
    assert item_active(saved, "/tools/fitting/saved/dps") is True


def test_kill_feed_exclude_vs_kill_search():
    """The one case a naive prefix-only impl breaks: /intel/kills/search.

    Kill Feed's broad `/intel/kills` prefix must step aside (via its exclude
    list) on the Kill Search page so the two nav items never light up together.
    """
    feed = _find("Kill Feed")
    search = _find("Kill Search")
    assert item_active(feed, "/intel/kills") is True
    assert item_active(feed, "/intel/kills/top") is True
    assert item_active(feed, "/intel/kills/feed") is True
    assert item_active(feed, "/intel/kills/search") is False      # excluded
    assert item_active(search, "/intel/kills/search") is True
    assert item_active(search, "/intel/kills") is False


def test_group_active_via_child_item():
    tools = _group("Tools")
    assert group_active(tools, "/tools/activity") is True
    assert group_active(tools, "/assets") is True
    assert group_active(tools, "/dashboard") is False


def test_group_active_via_extra_group_match():
    # Dashboard group has no item owning /character/<id>; the group-level
    # extra prefix match must still light the group there.
    dash = _group("Dashboard")
    assert group_active(dash, "/character/90000001") is True
    assert group_active(dash, "/dashboard") is True
    assert group_active(dash, "/characters") is True
    assert group_active(dash, "/intel") is False


def test_intel_group_catchall_covers_shared_and_entity_pages():
    # /intel/<scan_id> shared views and /intel/entity/... combat-stats pages
    # have no owning item; the group-level /intel/ prefix lights the group.
    intel = _group("Intel")
    assert group_active(intel, "/intel/abc123") is True
    assert group_active(intel, "/intel/entity/character/90000001") is True
    assert group_active(intel, "/intel") is True          # Overview item
    assert group_active(intel, "/industry") is False


def test_map_group_catchall_covers_alliance_pages():
    # /alliance/<id> detail pages are linked from Trending (a Map item).
    map_grp = _group("Map")
    assert group_active(map_grp, "/alliance/99000001") is True
    assert group_active(map_grp, "/map") is True
    assert group_active(map_grp, "/intel") is False


def test_skill_plans_lives_in_dashboard_group():
    dash = _group("Dashboard")
    labels = [i["label"] for i in dash["items"]]
    assert "Skill Plans" in labels
    assert not any(g["label"] == "Skill Plans" for g in NAV_GROUPS)
    assert group_active(dash, "/skill-plans/42") is True


def test_market_group_shape():
    # Market is a non-landing group whose parent url is the Prices page
    # itself (items[0]) — the Map/Dashboard pattern.
    market = _group("Market")
    assert market["landing"] is False
    assert market["items"][0]["url"] == market["url"] == "/market"
    labels = [i["label"] for i in market["items"]]
    assert labels == ["Prices", "LP Store ROI", "Trading P&L",
                      "Appraisal", "Net Worth"]
    # Prices' broad /market prefix steps aside for LP / P&L sub-pages.
    prices = _find("Prices")
    assert item_active(prices, "/market") is True
    assert item_active(prices, "/market/type/34") is True
    assert item_active(prices, "/market/lp") is False
    assert item_active(prices, "/market/pnl") is False


def test_plain_link_group_has_no_items_but_matches_by_group_rule():
    corps = _group("Corporations")
    assert corps["items"] == []
    assert group_active(corps, "/corporations") is True
    assert group_active(corps, "/corporations/98000001") is True
    assert group_active(corps, "/intel") is False


def test_admin_group_and_items_flagged_admin():
    admin = _group("Admin")
    assert admin["admin"] is True
    assert all(item["admin"] is True for item in admin["items"])
    # Non-admin groups are not admin-gated.
    assert _group("Intel")["admin"] is False


# ── uniqueness ──────────────────────────────────────────────────────────────

def test_item_urls_are_unique():
    # Scope to item URLs only. Group URLs legitimately equal their Overview
    # item URL (e.g. /industry, /intel, /tools, /map, /dashboard, /admin), so
    # they are deliberately excluded from this uniqueness assertion.
    urls = [url for _g, _lbl, url in _internal_item_urls()]
    # include external item urls too for full duplicate detection
    ext = [item["url"] for grp in NAV_GROUPS for item in grp["items"]
           if item.get("external")]
    all_urls = urls + ext
    dupes = [u for u in set(all_urls) if all_urls.count(u) > 1]
    assert not dupes, f"Duplicate item URLs in registry: {dupes}"


def test_labels_unique_within_each_group():
    for group in NAV_GROUPS:
        labels = [item["label"] for item in group["items"]]
        dupes = [l for l in set(labels) if labels.count(l) > 1]
        assert not dupes, f"Duplicate labels in group {group['label']!r}: {dupes}"


# ── base.html renders from the registry, not hardcoded URLs ─────────────────

def test_base_html_has_no_hardcoded_dropdown_urls():
    """The nav/mobile/footer chrome must render from `nav_groups`, so the old
    hand-maintained dropdown item URLs should no longer appear as literals in
    base.html. If one reappears, someone re-hardcoded a nav link."""
    source = _base_html_source()
    for url in ("/industry/manufacturing", "/tools/discordtime",
                "/wormholes/types"):
        assert source.count(url) == 0, (
            f"{url!r} is hardcoded in base.html; it must come from the "
            f"nav registry instead"
        )


def test_base_html_references_nav_groups():
    """base.html must drive its chrome from the registry global."""
    assert "nav_groups" in _base_html_source()


def test_desktop_dropdown_suppresses_group_url_duplicate():
    """The desktop dropdown must skip the item whose url equals the group's
    own url (Overview / Star Map / Console…) — otherwise the group label link
    and the first dropdown row are two visible links to the same page."""
    assert "item['url'] != group['url']" in _base_html_source()


def test_base_html_is_valid_jinja():
    """Guard against a broken template edit. Environment().parse validates the
    template syntax without needing request/session globals to render."""
    Environment().parse(_base_html_source())


def test_no_landing_group_overrides():
    """Every item's nav home and landing-card home agree — the wormhole
    reference tools moved into Intel (2026-07), so no item needs the
    landing_group escape hatch anymore. If one reappears, make sure the
    split identity is deliberate."""
    overridden = {
        item["label"]: item["landing_group"]
        for grp in NAV_GROUPS
        for item in grp["items"]
        if item.get("landing_group")
    }
    assert overridden == {}


def test_landing_grids_built_from_registry():
    """landings.py card grids derive from NAV_GROUPS — composition pinned here."""
    from app.routes.landings import INDUSTRY_TOOLS, INTEL_TOOLS, TOOLS_TOOLS

    intel_names = [c["name"] for c in INTEL_TOOLS]
    for expected in ("Kill Feed", "Kill Search", "Watchlist",
                     "Wormhole Systems", "Wormhole Types", "System Effects"):
        assert expected in intel_names

    industry_names = [c["name"] for c in INDUSTRY_TOOLS]
    assert len(industry_names) == 8 and "Manufacturing" in industry_names
    assert "Build Finder" in industry_names
    assert "Stockpiles" in industry_names
    # The economy pillar moved to the (non-landing) Market group.
    for moved in ("LP Store ROI", "Trading P&L", "Appraisal"):
        assert moved not in industry_names

    tools_names = [c["name"] for c in TOOLS_TOOLS]
    assert "Structure Age" in tools_names
    for moved in ("Net Worth", "Stockpiles"):
        assert moved not in tools_names

    all_cards = INDUSTRY_TOOLS + INTEL_TOOLS + TOOLS_TOOLS
    assert not any(c["name"] == "Overview" for c in all_cards)
    assert all(c["desc"] and c["features"] for c in all_cards)


def test_legacy_dscan_routes_301_redirect():
    from fastapi.testclient import TestClient
    import app.main as main

    client = TestClient(main.app, follow_redirects=False)
    r = client.get("/dscan")
    assert r.status_code == 301
    assert r.headers["location"] == "/intel/dscan"
    r = client.get("/dscan/abc123")
    assert r.status_code == 301
    assert r.headers["location"] == "/intel/abc123"
    r = client.get("/dscan?foo=1")
    assert r.headers["location"] == "/intel/dscan?foo=1"
