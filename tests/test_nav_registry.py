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
import re
import types

from jinja2 import Environment, FileSystemLoader

import app.main as main
from app.nav import NAV_GROUPS, item_active, group_active

_TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
_BASE_HTML = os.path.join(_TEMPLATES, "base.html")
_COMPONENTS_CSS = os.path.join(
    os.path.dirname(__file__), "..", "design-system", "css", "components.css"
)
_SITE_CSS = os.path.join(
    os.path.dirname(__file__), "..", "static", "css", "site.css"
)


def _base_html_source():
    with open(_BASE_HTML, encoding="utf-8") as fh:
        return fh.read()


def _render_base(is_admin=True, path="/dashboard"):
    """Render base.html's chrome with the real registry globals.

    Cheaper and more direct than a TestClient round-trip, and it asserts on
    what the browser actually receives rather than on template source.
    """
    env = Environment(loader=FileSystemLoader(_TEMPLATES))
    env.globals.update(nav_groups=NAV_GROUPS, nav_item_active=item_active,
                       nav_group_active=group_active)
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path=path),
        state=types.SimpleNamespace(csp_nonce="test-nonce"),
        session={"user_id": 1, "is_admin": is_admin,
                 "active_character_id": 90000001, "csrf_token": "t"},
    )
    return env.get_template("base.html").render(request=request, css_v="1", js_v="1")


def _bar_group_labels(html):
    """Labels of the top-level groups rendered in the desktop bar.

    Group triggers are the only links that carry a caret element directly
    after their label text; the account menu's own trigger wraps its label in
    a span, so it is excluded by construction.
    """
    return re.findall(r'class="b-nav-link[^"]*"[^>]*>([A-Za-z ]+)'
                      r'<span class="b-nav-caret"', html)


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


def test_map_items_folded_into_intel_group():
    """The Map group merged into Intel: a star map and a trending-activity
    map are intel surfaces, and the group was costing a slot in a bar that
    had run out of room."""
    assert not any(g["label"] == "Map" for g in NAV_GROUPS)
    intel = _group("Intel")
    labels = [i["label"] for i in intel["items"]]
    for expected in ("Star Map", "Wormhole Map", "Trending"):
        assert expected in labels
    assert group_active(intel, "/map") is True
    assert group_active(intel, "/map/wormholes") is True
    assert group_active(intel, "/trending") is True
    # /alliance/<id> detail pages are linked from Trending and have no owning
    # item; the group-level catch-all came across with the items.
    assert group_active(intel, "/alliance/99000001") is True
    assert group_active(intel, "/industry") is False


def test_star_map_starts_a_divided_block_in_the_intel_menu():
    """Intel's dropdown is long enough to need visual grouping: live intel
    tools, then the maps, then the wormhole reference."""
    intel = _group("Intel")
    assert _find("Star Map")["divider_before"] is True
    assert _find("Wormhole Systems")["divider_before"] is True
    labels = [i["label"] for i in intel["items"]]
    assert labels.index("Star Map") > labels.index("WH Tracker")
    assert labels.index("Star Map") < labels.index("Wormhole Systems")


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
    assert labels == ["Prices", "LP Store ROI", "Trading & Industry P&L",
                      "Appraisal", "Net Worth"]
    # Prices' broad /market prefix steps aside for LP / P&L sub-pages.
    prices = _find("Prices")
    assert item_active(prices, "/market") is True
    assert item_active(prices, "/market/type/34") is True
    assert item_active(prices, "/market/lp") is False
    assert item_active(prices, "/market/pnl") is False


def test_corporations_lives_in_dashboard_group():
    """Corporations used to be its own top-level group (the only one with no
    dropdown). It is a "my stuff" destination like Characters, so it moved
    into Dashboard rather than spending a slot in the bar."""
    dash = _group("Dashboard")
    assert "Corporations" in [i["label"] for i in dash["items"]]
    assert not any(g["label"] == "Corporations" for g in NAV_GROUPS)
    assert group_active(dash, "/corporations") is True
    assert group_active(dash, "/corporations/98000001") is True
    assert group_active(dash, "/intel") is False


def test_every_group_declares_account_placement():
    """`account` decides whether a group renders in the top-level bar or in
    the account menu. base.html reads it on every group, so every group must
    declare it."""
    missing = [g["label"] for g in NAV_GROUPS if "account" not in g]
    assert not missing, f"Groups missing the 'account' key: {missing}"


def test_account_groups_are_account_and_admin():
    """The bar holds primary destinations only; Characters & Permissions and
    Admin are reachable from the account menu at the right end of the nav."""
    account = [g["label"] for g in NAV_GROUPS if g["account"]]
    assert account == ["Account", "Admin"]


def test_every_user_gets_the_permissions_page_in_the_account_menu():
    for is_admin in (False, True):
        menu = _account_menu(_render_base(is_admin=is_admin))
        assert 'href="/account"' in menu
        assert 'href="/auth/connect"' in menu


def test_top_level_bar_is_five_groups():
    """The bar is a fixed-width surface — every group in it costs horizontal
    room, and the hamburger breakpoint in site.css is measured against this
    count. Adding a sixth group means re-measuring that breakpoint."""
    bar = [g["label"] for g in NAV_GROUPS if not g["account"]]
    assert bar == ["Dashboard", "Industry", "Market", "Intel", "Tools"]


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


def test_desktop_bar_renders_only_non_account_groups():
    """Rendered for an admin — the bar must still be the five primary groups;
    Admin belongs to the account menu."""
    labels = _bar_group_labels(_render_base(is_admin=True))
    assert labels == ["Dashboard", "Industry", "Market", "Intel", "Tools"]
    assert _bar_group_labels(_render_base(is_admin=False)) == labels


def _account_menu(html):
    """The account dropdown's menu, up to the close of its .b-nav-dropdown."""
    return html.split('class="b-nav-dropdown-menu is-right"')[1].split("</span>")[0]


def test_account_menu_carries_admin_add_character_and_logout():
    menu = _account_menu(_render_base(is_admin=True))
    assert 'href="/auth/connect"' in menu
    assert 'href="/admin"' in menu       # Admin group's Console item
    assert 'action="/auth/logout"' in menu


def test_account_menu_has_no_duplicate_destinations():
    """/status redirects to /admin, so listing both gave admins two menu rows
    that land on the same page. Every account-menu link must be distinct."""
    menu = _account_menu(_render_base(is_admin=True))
    assert 'href="/status"' not in menu
    hrefs = re.findall(r'href="([^"]+)"', menu)
    assert len(hrefs) == len(set(hrefs)), hrefs


def test_account_menu_hides_admin_items_from_non_admins():
    html = _render_base(is_admin=False)
    assert 'href="/auth/connect"' in html    # everyone gets this
    # Neither the account menu nor the mobile menu may leak the console.
    # (Matched as hrefs: /status/banner is an unrelated hx-get in the chrome.)
    assert 'href="/admin"' not in html
    assert 'href="/status"' not in html


def test_footer_omits_account_groups():
    footer = _render_base(is_admin=True).split("<footer")[1].split("</footer>")[0]
    assert ">Dashboard<" in footer
    assert ">Admin<" not in footer


def test_mobile_menu_still_lists_account_groups():
    """The bar's account menu is display:none below the nav breakpoint, so the
    mobile menu must keep listing Admin inline or an admin on a phone loses
    the console entirely."""
    html = _render_base(is_admin=True)
    mobile = html.split('id="mobile-menu"')[1].split("<div id=\"esi-banner\"")[0]
    assert "/admin" in mobile
    assert "/auth/logout" in mobile


def test_palette_has_a_visible_handle():
    """Ctrl+K shipped without any visible affordance; the bar's search button
    is it. It dispatches through actions.js, which resolves window[name]."""
    html = _render_base()
    assert 'class="b-nav-search"' in html
    assert 'data-click="openPalette"' in html
    assert "window.openPalette = openPaletteFromChrome;" in html


def _nav_links_row(html):
    """The collapsing group row: .b-nav-links up to its sibling cluster."""
    return html.split('<div class="b-nav-links">')[1].split('class="b-nav-actions"')[0]


def test_actions_cluster_is_outside_the_collapsing_group_row():
    """.b-nav-links is display:none below the nav breakpoint. The bell is how
    structure / fuel / timer alerts reach the user, so it — and search, and
    the account menu — must sit in the sibling cluster that survives it.
    Regression guard: the bell was unreachable on every screen under 1120px
    before the cluster existed."""
    html = _render_base()
    row = _nav_links_row(html)
    for marker in ('id="notif-btn"', 'class="b-nav-search"', 'b-nav-account'):
        assert marker not in row, f"{marker} is back inside the collapsing row"
    cluster = html.split('class="b-nav-actions"')[1].split("</nav>")[0]
    for marker in ('id="notif-btn"', 'class="b-nav-search"',
                   'b-nav-account', 'class="b-hamburger"'):
        assert marker in cluster


def test_bell_is_class_styled_so_the_breakpoint_can_size_it():
    """The bell carried its layout in a style attribute, which no media query
    can override. It needs a bigger tap target at mobile widths now that it
    is reachable there."""
    html = _render_base()
    bell = html.split('id="notif-btn"')[1].split(">")[0]
    assert 'class="b-nav-bell"' in bell
    assert "style=" not in bell


# ── the bar must keep fitting the breakpoint it claims ──────────────────────
#
# Constants measured in Chromium against the rendered chrome (the procedure is
# in test_nav_bar_fits_its_breakpoint). Re-measure if the bar's fixed parts
# change; the test below fails loudly if the action cluster grows, which is the
# change most likely to invalidate them.
_MONO_ADVANCE = 0.6        # JetBrains Mono glyph advance, in em
_CARET_PX = 13.0           # .b-nav-caret: margin-left + glyph + letter-spacing
_FIXED_CHROME_PX = 469.5   # logo (93.5) + action cluster (312) + 2rem padding
_ACTION_CLUSTER = ("b-nav-search", "b-nav-bell", "b-nav-dropdown", "b-hamburger")


def _css_px(css, selector, prop):
    """First `prop` value inside `selector`'s block, in px (rem → px at 16)."""
    block = css.split(selector + " {")[1].split("}")[0]
    match = re.search(prop + r":\s*([0-9.]+)(px|rem)", block)
    assert match, f"{prop} not found in {selector}"
    value = float(match.group(1))
    return value * 16 if match.group(2) == "rem" else value


def _nav_breakpoint_px(css):
    blocks = re.findall(r"@media \(max-width: (\d+)px\) \{(.*?)\n\}", css, re.S)
    nav = [int(px) for px, body in blocks if ".b-nav-links" in body]
    assert len(nav) == 1, "expected exactly one nav breakpoint block"
    return nav[0]


def test_nav_bar_fits_its_breakpoint():
    """The bar must still fit above the width at which it hands over to the
    hamburger. Nothing enforced this before: the breakpoint was set for a
    bar that later grew two groups past it, and the row silently wrapped its
    links out of the 46px bar at every width in between.

    The estimate is browser-free so it runs in CI. The label term tracks the
    CSS (font-size and letter-spacing are read from components.css; the font
    is monospace, so a label's width is just its character count); the caret
    and fixed-chrome terms are measured constants. To re-measure: render the
    nav, then sum .b-nav-logo + .b-nav-actions + the nav's horizontal padding
    for the fixed term, and fit `width = c * len(label) + k` across two group
    triggers for the label terms.
    """
    with open(_COMPONENTS_CSS, encoding="utf-8") as fh:
        components = fh.read()
    with open(_SITE_CSS, encoding="utf-8") as fh:
        site = fh.read()

    font_px = _css_px(components, ".b-nav-link", "font-size")
    tokens_ls = 0.18          # --ls-wider, the .b-nav-link letter-spacing
    per_char = font_px * (_MONO_ADVANCE + tokens_ls)
    gap_px = _css_px(components, ".b-nav-links", "gap")

    labels = [g["label"] for g in NAV_GROUPS if not g["account"]]
    row = sum(per_char * len(label) + _CARET_PX for label in labels)
    row += gap_px * (len(labels) - 1)
    needed = _FIXED_CHROME_PX + row

    breakpoint_px = _nav_breakpoint_px(site)
    assert needed <= breakpoint_px - 40, (
        f"the nav bar needs ~{needed:.0f}px but hands over to the hamburger "
        f"at {breakpoint_px}px — the row will deform in between. Either trim "
        f"the bar or raise the breakpoint (and re-measure, see this test)."
    )


def test_action_cluster_composition_is_pinned():
    """_FIXED_CHROME_PX above is measured against exactly these four children.
    Adding a fifth invalidates it, so fail here rather than let the fit test
    pass on a stale constant."""
    cluster = _render_base().split('class="b-nav-actions"')[1].split("</nav>")[0]
    for name in _ACTION_CLUSTER:
        assert name in cluster
    # Direct children of the cluster, by their opening tag's class/id.
    assert cluster.count('data-click="openPalette"') == 1
    assert cluster.count('id="notif-btn"') == 1
    assert cluster.count('class="b-hamburger"') == 1
    assert cluster.count('b-nav-account') == 1


def test_nav_breakpoint_hides_only_the_group_row():
    """The breakpoint block must not take the action cluster down with it."""
    with open(_SITE_CSS, encoding="utf-8") as fh:
        css = fh.read()
    # An unrelated grid block shares this media query by coincidence, so pick
    # the one that actually governs the nav.
    blocks = [b.split("\n}")[0]
              for b in css.split("@media (max-width: 1000px) {")[1:]]
    nav_blocks = [b for b in blocks if ".b-nav-links" in b]
    assert len(nav_blocks) == 1, "expected exactly one nav breakpoint block"
    block = nav_blocks[0]
    assert ".b-nav-links { display: none; }" in block
    assert ".b-nav-actions { display: none" not in block
    # The account menu is the one action that stands down — its items are all
    # in the mobile menu, and hover-to-open is a poor fit for touch.
    assert ".b-nav-account { display: none; }" in block


def test_nav_caret_is_its_own_element_not_part_of_the_label():
    """A label + space + caret is shrinkable, so it wrapped out of the 46px
    bar once the row was over-full — which is what knocked the dropdown groups
    out of line with the plain links."""
    source = _base_html_source()
    assert '<span class="b-nav-caret"' in source
    assert " \u25be" not in source, "caret is back inside the link text"


def test_nav_links_cannot_wrap_or_shrink():
    """The other half of that fix: without these the row deforms instead of
    staying on one line (see the nav breakpoint comment in site.css)."""
    with open(_COMPONENTS_CSS, encoding="utf-8") as fh:
        css = fh.read()
    block = css.split(".b-nav-link {")[1].split("}")[0]
    assert "white-space: nowrap" in block
    assert "flex-shrink: 0" in block


# ── dropdown accessibility ──────────────────────────────────────────────────

def test_every_dropdown_trigger_is_a_labelled_disclosure():
    """Each trigger must advertise its menu and its state. The menus opened on
    hover/focus with nothing announcing them before."""
    html = _render_base(is_admin=True)
    # The hamburger is a disclosure too, but for the mobile panel rather than
    # a menu — scope this to the popup triggers.
    triggers = re.findall(r'<(?:a|button)\b[^>]*aria-haspopup="true"[^>]*>', html)
    assert len(triggers) == 6, f"expected 5 groups + account, got {len(triggers)}"
    menu_ids = []
    for tag in triggers:
        # Every trigger ships closed; the nav script keeps it honest from there.
        assert 'aria-expanded="false"' in tag, tag
        match = re.search(r'aria-controls="([^"]+)"', tag)
        assert match, f"trigger with no aria-controls: {tag}"
        menu_id = match.group(1)
        assert f'id="{menu_id}"' in html, f"aria-controls={menu_id} has no menu"
        menu_ids.append(menu_id)
    assert len(set(menu_ids)) == len(menu_ids), f"duplicate menu ids: {menu_ids}"


def test_escape_dismissal_rule_follows_the_open_rules():
    """`.is-dismissed` and `:focus-within` have equal specificity, so the
    dismissal only wins on source order. If someone moves it above the open
    rules, Escape silently stops closing keyboard-opened menus."""
    with open(_COMPONENTS_CSS, encoding="utf-8") as fh:
        css = fh.read()
    open_rule = css.index(".b-nav-dropdown:focus-within .b-nav-dropdown-menu")
    dismissed = css.index(".b-nav-dropdown.is-dismissed .b-nav-dropdown-menu")
    assert dismissed > open_rule, (
        "the is-dismissed rule must come after the hover/focus-within rules"
    )


def test_group_triggers_stay_links():
    """Tap-to-open must not turn the group triggers into buttons: they are
    links to their section, which is what makes middle-click, open-in-new-tab
    and a plain mouse click work."""
    html = _render_base()
    row = _nav_links_row(html)
    bar_groups = [g for g in NAV_GROUPS if not g["account"]]
    # One trigger per bar group. (The row also holds each menu's item links,
    # so match the trigger class rather than counting anchors.)
    assert row.count('class="b-nav-link ') == len(bar_groups)
    assert "<button" not in row


def test_nav_script_opens_menus_on_a_first_tap():
    """A group trigger is a link, so on a touch screen a tap navigates and its
    menu can never be seen — there is no hover, and at these widths the
    hamburger has not taken over. Touch gets tap-to-open / tap-again-to-follow;
    a mouse click must still navigate immediately."""
    html = _render_base()
    for marker in ("pointerType", "lastPointerType", "(hover: none)",
                   "btn.tagName === 'A'", "if (!isTouch(e)) return;"):
        assert marker in html, marker


def test_nav_script_handles_escape_and_click_toggling():
    """The behaviours CSS cannot provide, pinned so they are not dropped in a
    refactor: Escape, click toggling for the account trigger, and the
    aria-expanded mirror."""
    html = _render_base()
    for marker in ("is-dismissed", "aria-expanded", "'Escape'",
                   "btn.tagName === 'BUTTON'"):
        assert marker in html, marker


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
