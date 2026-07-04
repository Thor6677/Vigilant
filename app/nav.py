"""Single source of truth for site navigation.

`NAV_GROUPS` is THE canonical description of the top-level nav: every group,
every dropdown item, its URL, its active-state match rules, and (for landing
pages) its card `desc`/`features`. Three chrome surfaces render from it:

  * `app/templates/base.html` — desktop nav, mobile menu, and footer.
  * `app/routes/landings.py` — the Industry / Intel / Tools card grids.

Both are wired via Jinja globals (`nav_groups`, `nav_item_active`,
`nav_group_active`) pushed to every Jinja2Templates instance in `main.py`.

**Adding a page = add an entry here.** Do not hand-edit the nav markup in
base.html or the landing constants; add the item to the right group in this
module and both surfaces pick it up. The dead-link test in
`tests/test_nav_registry.py` enforces this: every internal URL in this registry
must resolve to a registered FastAPI route, so an orphaned page or a typo'd URL
becomes a test failure instead of a broken link in production.

Data shape (plain dicts, no classes):

  item  = {"label", "url", "match": [("exact"|"prefix", path), ...],
           "exclude": [prefix, ...],            # optional: suppress active-state
           "desc": str, "features": [str, ...], # optional: landing-card content
           "landing_group": str | None,         # landing_group: surface this item's card on another group's landing page
           "admin", "external", "divider_before",
           "in_dropdown", "in_landing"}         # flags, sensible defaults
  group = {"label", "url", "match": [...extra group-level rules...],
           "items": [...], "admin", "landing"}

Active-state is data, not template logic: an item is active iff any of its
`match` rules matches the current path AND none of its `exclude` prefixes match;
a group is active iff any of its items is active OR any of its own extra `match`
rules matches. Match kinds are "exact" (path == target) and "prefix"
(path.startswith(target)). Longest-wins is intentionally NOT modeled — this
replicates the historical base.html semantics (any match -> active).
"""

from __future__ import annotations


def _item(label, url, match=None, *, desc=None, features=None, exclude=None,
          landing_group=None, admin=False, external=False, divider_before=False,
          in_dropdown=True, in_landing=True):
    """Build a nav item dict with consistent defaults.

    `landing_group=None` (the default) means the item's card renders on its
    own group's landing page (if that group has one and `in_landing` is True);
    a group label surfaces the card on that other group's landing instead.
    """
    return {
        "label": label,
        "url": url,
        "match": list(match) if match else [],
        "exclude": list(exclude) if exclude else [],
        "desc": desc,
        "features": list(features) if features else [],
        "landing_group": landing_group,
        "admin": admin,
        "external": external,
        "divider_before": divider_before,
        "in_dropdown": in_dropdown,
        "in_landing": in_landing,
    }


NAV_GROUPS = [
    # ── Dashboard ──────────────────────────────────────────────────────────
    {
        "label": "Dashboard",
        "url": "/dashboard",
        # Group also lights up on character-detail pages (/character/<id>/...).
        "match": [("prefix", "/character/")],
        "admin": False,
        "landing": False,
        "items": [
            _item("Overview", "/dashboard", [("exact", "/dashboard")],
                  in_landing=False),
            _item("Characters", "/characters", [("prefix", "/characters")],
                  in_landing=False),
        ],
    },

    # ── Corporations (plain link) ──────────────────────────────────────────
    {
        "label": "Corporations",
        "url": "/corporations",
        "match": [("prefix", "/corporations")],
        "admin": False,
        "landing": False,
        "items": [],
    },

    # ── Skill Plans (plain link) ───────────────────────────────────────────
    {
        "label": "Skill Plans",
        "url": "/skill-plans",
        "match": [("prefix", "/skill-plans")],
        "admin": False,
        "landing": False,
        "items": [],
    },

    # ── Industry ───────────────────────────────────────────────────────────
    {
        "label": "Industry",
        "url": "/industry",
        "match": [],
        "admin": False,
        "landing": True,
        "items": [
            _item("Overview", "/industry", [("exact", "/industry")],
                  in_landing=False),
            _item(
                "Manufacturing", "/industry/manufacturing",
                [("prefix", "/industry/manufacturing")],
                desc="Blueprint cost + profit calculator. Pick a blueprint, set ME/TE, choose a structure and rigs, and see per-unit material cost, build time, and Jita-spread margin.",
                features=[
                    "Structure + rig material/time bonuses",
                    "Security-status penalty modeling",
                    "Jita buy/sell price lookups",
                    "Per-run material totals and ISK figures",
                ],
            ),
            _item(
                "Active Jobs", "/industry/jobs",
                [("prefix", "/industry/jobs")],
                desc="Every running or queued industry job across all your characters in one table, sorted by completion time.",
                features=[
                    "Manufacturing, research, invention, reactions",
                    "Per-character and per-structure filters",
                    "Completion countdown timers",
                    "Installer and location labeling",
                ],
            ),
            _item(
                "Compression", "/industry/compression",
                [("exact", "/industry/compression")],
                desc="Ore-to-compressed-ore volume + ISK calculator. Useful for deciding whether to haul raw ore or compress first.",
                features=[
                    "Per-ore compression ratios",
                    "Volume savings display",
                    "Compressed-ore Jita pricing",
                ],
            ),
            _item(
                "Hauling", "/industry/hauling",
                [("exact", "/industry/hauling")],
                desc="Quick hauling calculator — enter a cargo volume and route and get collateral, reward, and per-m³ rate suggestions.",
                features=[
                    "Route gate + jump count",
                    "High-sec / low-sec / null-sec pricing tiers",
                    "Collateral suggestions",
                ],
            ),
            _item(
                "Appraisal", "/industry/appraisal",
                [("exact", "/industry/appraisal")],
                desc="Paste a cargo or asset list and get a Jita valuation at current market prices. Works with contract exports, loot drops, and inventory dumps.",
                features=[
                    "Paste any item / qty list",
                    "Jita buy vs. sell totals",
                    "Per-item breakdown",
                ],
            ),
            _item(
                "Mining Ledger", "/industry/mining-ledger",
                [("prefix", "/industry/mining-ledger")],
                desc="Per-character mining output — ore type, quantity, ISK value — sourced from the ESI mining ledger and aggregated over time.",
                features=[
                    "Per-day and per-character totals",
                    "Ore-type breakdown",
                    "ISK valuation at Jita sell",
                ],
            ),
            _item(
                "Planetary Industry", "/industry/planetary",
                [("prefix", "/industry/planetary")],
                desc="PI schematic browser and chain planner — pick a product and see the input planets, extraction rates, and building requirements.",
                features=[
                    "All P1–P4 schematics",
                    "Input planet-type reference",
                    "Per-character PI status (if linked)",
                ],
            ),
        ],
    },

    # ── Intel ──────────────────────────────────────────────────────────────
    {
        "label": "Intel",
        "url": "/intel",
        "match": [],
        "admin": False,
        "landing": True,
        "items": [
            _item("Overview", "/intel", [("exact", "/intel")],
                  in_landing=False),
            _item(
                "Kill Feed", "/intel/kills",
                [("prefix", "/intel/kills")],
                # Kill Search lives under /intel/kills/search; keep it out of
                # Kill Feed's prefix so both don't light up simultaneously.
                exclude=["/intel/kills/search"],
                desc="Live killboard for tracked entities — a rolling feed of recent kills and losses with ISK values, ship classes, and system locations, refreshed continuously.",
                features=[
                    "Live-polling recent killmails",
                    "Top kills ranked by ISK destroyed",
                    "Per-kill ship, system, and value",
                    "Victim and final-blow attacker detail",
                ],
            ),
            _item(
                "Kill Search", "/intel/kills/search",
                [("prefix", "/intel/kills/search")],
                desc="Advanced killmail search across the full history — filter by time window, space type, entity, and ship, with special-case flags for awox, high-sec ganks, and blob padding.",
                features=[
                    "Time-window and date-range filters",
                    "Space type, system, and region scoping",
                    "Entity (character / corp / alliance) and ship-type filters",
                    "Awox, HS Gank, Padding flags + AT Ships chip",
                ],
            ),
            _item(
                "D-Scan / Local", "/intel/dscan",
                # Legacy /dscan prefix kept until the Task 4 301 redirects ship.
                [("prefix", "/intel/dscan")],
                desc="Paste a D-scan or local roster and get an analyzed breakdown: ships by class, per-pilot zKillboard links, and corp/alliance affiliations.",
                features=[
                    "D-scan paste parsing",
                    "Local chat list parsing",
                    "zKillboard deep links per pilot",
                    "Saved scan history",
                ],
            ),
            _item(
                "Watchlist", "/intel/watch",
                [("prefix", "/intel/watch")],
                desc="Live kill alerts from the killmail stream. Watch specific systems (e.g. your home, your asset hubs) and hunter entities (corps/alliances/characters) — alerts fire within seconds.",
                features=[
                    "System watches with one-click bulk-add from your assets",
                    "Hunter watches by character / corporation / alliance",
                    "72h alert history",
                    "Live notifications via dashboard poll",
                ],
            ),
            _item(
                "Gate Check", "/intel/gatecheck",
                [("prefix", "/intel/gatecheck")],
                desc="Before you jump — paste a local list and see aggregate recent kill activity for the corps and alliances on the other side.",
                features=[
                    "Corp / alliance kill summaries",
                    "Recent loss patterns",
                    "Activity timestamps",
                ],
            ),
        ],
    },

    # ── Map (new group; parent url is the map itself, no landing page) ──────
    {
        "label": "Map",
        "url": "/map",
        "match": [],
        "admin": False,
        "landing": False,
        "items": [
            _item("Star Map", "/map", [("exact", "/map")],
                  in_landing=False),
            _item("Wormhole Map", "/map/wormholes",
                  [("exact", "/map/wormholes")], in_landing=False),
            _item("Trending", "/trending", [("prefix", "/trending")],
                  in_landing=False),
            _item(
                "Wormhole Systems", "/wormholes",
                [("exact", "/wormholes"), ("prefix", "/wormholes/system")],
                divider_before=True,
                # Nav item lives in Map; its card stays on the /intel landing.
                landing_group="Intel",
                desc="Per-system reference for every J-space system — class, effect, planets, static connections, recent kill activity.",
                features=[
                    "Shattered / Drifter / Thera flags",
                    "Kill history + recent fights",
                    "Effect and class lookup",
                ],
            ),
            _item(
                "Wormhole Types", "/wormholes/types",
                [("prefix", "/wormholes/types")],
                landing_group="Intel",
                desc="Complete wormhole signature reference — K162, A/B/C/..., mass and lifetime by code.",
                features=[
                    "All static/transient wormhole types",
                    "Mass / lifetime / jump limits",
                    "Destination-class lookup",
                ],
            ),
            _item(
                "System Effects", "/wormholes/effects",
                [("exact", "/wormholes/effects")],
                landing_group="Intel",
                desc="Wolf-Rayet, Black Hole, Pulsar, Cataclysmic Variable, Magnetar, Red Giant — per-class effect bonuses and penalties.",
                features=[
                    "Per-effect bonus/penalty tables",
                    "By wormhole class",
                    "Fit relevance hints",
                ],
            ),
            _item("Wanderer", "https://mapper.thunderborn.dev",
                  external=True, in_landing=False),
        ],
    },

    # ── Tools ──────────────────────────────────────────────────────────────
    {
        "label": "Tools",
        "url": "/tools",
        "match": [],
        "admin": False,
        "landing": True,
        "items": [
            _item("Overview", "/tools", [("exact", "/tools")],
                  in_landing=False),
            _item(
                "Activity", "/tools/activity",
                [("prefix", "/tools/activity")],
                desc="EVE server health + universe-wide ISK destruction over time. PCU and kill data overlaid on one chart, with historical archive going back to 2003 sourced from eve-offline.net + eve-offline.com.",
                features=[
                    "1d / 7d / 30d / 90d / 1y / 5y / all-time windows",
                    "Player-count + ISK destroyed overlay",
                    "Peak / mean PCU markers",
                    "Cross-validated historical archive (Chribba + Adminor)",
                ],
            ),
            _item(
                "Asset Search", "/assets",
                [("prefix", "/assets")],
                desc="Search across every linked character's assets at once. Find any item by name, see every stack and location.",
                features=[
                    "All characters in one view",
                    "Per-station / per-structure grouping",
                    "Free-text name search",
                ],
            ),
            _item(
                "Structure Timers", "/structure-timers",
                [("prefix", "/structure-timers")],
                desc="Shared structure-timer tracker with ACL. Add structure hits, share across corp/alliance groups, dashboard banners alert as they approach.",
                features=[
                    "Group-based ACLs (corp / alliance / custom)",
                    "Site-wide 24-hour warning banners",
                    "UTC time input (no browser TZ confusion)",
                    "Archive + audit trail",
                ],
            ),
            _item(
                "Image Host", "/tools/images",
                [("prefix", "/tools/images"), ("prefix", "/i/")],
                desc="Private image uploader with shareable short links. Drop a PNG/JPG/GIF, get back a /i/<hash> URL.",
                features=[
                    "Drag-and-drop or paste upload",
                    "Per-user library",
                    "Short shareable URLs",
                ],
            ),
            _item(
                "Ship Fitting", "/tools/fitting",
                [("exact", "/tools/fitting")],
                desc="Fit a ship and see accurate DPS, EHP, cap stability, and fitting resources — matches Pyfa's numbers closely and threads through character skills.",
                features=[
                    "Character-accurate DPS / EHP / cap",
                    "Module browser by market group",
                    "Missing-skill warnings",
                    "Per-level bonuses with proper damage profiles",
                ],
            ),
            _item(
                "Saved Fits", "/tools/fitting/saved",
                [("prefix", "/tools/fitting/saved")],
                desc="Your personal fitting library — saved fits organized into nested folders, ready to reopen in the fitting tool.",
                features=[
                    "Folder hierarchy",
                    "Quick reopen in fitting tool",
                    "Import / export EFT format",
                ],
            ),
            _item(
                "Discord Time", "/tools/discordtime",
                [("prefix", "/tools/discordtime")],
                desc="UTC-to-Discord-timestamp converter for fleet ops. Paste a time and get the Discord `<t:...>` codes for every rendering mode.",
                features=[
                    "All Discord timestamp modes",
                    "Copy-paste ready output",
                    "Relative / absolute formatting",
                ],
            ),
            _item(
                "Structure Age", "/tools/structure-age",
                [("prefix", "/tools/structure-age")],
                desc="Estimate when an Upwell structure was anchored by pasting its in-game showinfo link. Uses 36k local calibration points — no external API needed.",
                features=[
                    "Paste showinfo link from chat",
                    "Anchor date estimate with confidence window",
                    "Parses system J-code and owner corp",
                ],
            ),
        ],
    },

    # ── Admin (admin-only) ─────────────────────────────────────────────────
    {
        "label": "Admin",
        "url": "/admin",
        "match": [],
        "admin": True,
        "landing": False,
        "items": [
            _item("Console", "/admin", [("prefix", "/admin")],
                  admin=True, in_landing=False),
            _item("Status", "/status", [("prefix", "/status")],
                  admin=True, in_landing=False),
        ],
    },
]


def _match_rule(rule, path):
    """Evaluate a single ("exact"|"prefix", target) match rule against a path."""
    kind, target = rule
    if kind == "exact":
        return path == target
    if kind == "prefix":
        return path.startswith(target)
    return False


def item_active(item, path):
    """True iff `item` should render active for the given request path.

    An item is active when any of its `match` rules matches AND none of its
    `exclude` prefixes matches (the exclude list lets Kill Feed's broad
    `/intel/kills` prefix step aside for the more specific Kill Search page).
    """
    if any(path.startswith(prefix) for prefix in item.get("exclude", [])):
        return False
    return any(_match_rule(rule, path) for rule in item.get("match", []))


def group_active(group, path):
    """True iff `group` should render active for the given request path.

    Active when any child item is active OR any of the group's own extra
    `match` rules matches (e.g. the Dashboard group also lights up on
    `/character/<id>` detail pages that no single item owns).
    """
    if any(item_active(item, path) for item in group.get("items", [])):
        return True
    return any(_match_rule(rule, path) for rule in group.get("match", []))
