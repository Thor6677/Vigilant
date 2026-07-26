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

from app.config import get_settings

# Optional external tools live behind config, not a hardcoded URL: a self-hosted
# instance must not ship nav links to somebody else's deployment. Items whose URL
# resolves empty are stripped by `_prune_unconfigured_items()` at the bottom of
# this module, so an unset variable means the item simply doesn't exist.
_WANDERER_URL = get_settings().wanderer_url


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
            _item("Skill Plans", "/skill-plans", [("prefix", "/skill-plans")],
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
                desc="Blueprint build-cost calculator. Pick a blueprint, set ME/TE, choose a structure and rigs, and see material quantities, total cost, and parallel vs. sequential build time — with a nested build-vs-buy drill-down into every component.",
                features=[
                    "Structure + rig material/time bonuses",
                    "Security-status penalty modeling",
                    "Materials at EVE's global average reference price",
                    "Per-run material totals and ISK figures",
                ],
            ),
            _item(
                "Build Finder", "/industry/build-finder",
                [("prefix", "/industry/build-finder")],
                desc="What should you build? Pick a group and rank its buildable items by manufacturing margin — build cost per unit vs. reference price, at your ME / structure / rig / security assumptions. T2 rankings include expected invention cost (datacores, decryptors, failed attempts).",
                features=[
                    "Margin ISK + margin % per item, best-first",
                    "T2 invention math: character skills or manual levels + decryptor picker",
                    "Reuses the manufacturing cost engine (ME / rig / security)",
                    "Products and materials at EVE's global average reference price",
                ],
            ),
            _item(
                "Active Jobs", "/industry/jobs",
                [("prefix", "/industry/jobs")],
                desc="Every running or queued industry job across all your characters and corporations in one table, sorted by completion time.",
                features=[
                    "Manufacturing, research, copying, invention, reactions",
                    "Per-activity, per-character, and per-corp filters",
                    "Completion countdown timers",
                    "Installer and location labeling",
                ],
            ),
            _item(
                "Compression", "/industry/compression",
                [("exact", "/industry/compression")],
                desc="Enter the minerals you need and get the cheapest basket of compressed ore that reprocesses into them, solved against your refining yield.",
                features=[
                    "Yield from structure, rig, security, implant, and skills",
                    "Optimize for lowest ISK cost or lowest mineral waste",
                    "Ore at EVE's global average reference price",
                    "Copy-for-multibuy list, or send straight to Hauling",
                ],
            ),
            _item(
                "Hauling", "/industry/hauling",
                [("exact", "/industry/hauling")],
                desc="Fleet cargo planner — build a fleet of haulers, see effective capacity per bay, and find out how many trips your load takes.",
                features=[
                    "Multi-ship fleet with cargo expanders, rigs, and skill level",
                    "Per-bay capacity (cargo, ore, fleet hangar, and the rest)",
                    "Paste an item list to resolve volumes and get ship suggestions",
                ],
            ),
            _item(
                "Mining Ledger", "/industry/mining-ledger",
                [("prefix", "/industry/mining-ledger")],
                desc="Per-character mining output — ore type, quantity, ISK value — sourced from the ESI mining ledger and aggregated over time.",
                features=[
                    "Per-day and per-character totals",
                    "Ore-type breakdown",
                    "Raw ore at EVE's global average reference price",
                ],
            ),
            _item(
                "Planetary Industry", "/industry/planetary",
                [("prefix", "/industry/planetary")],
                desc="PI colony tracker and chain planner — watch every character's planets and extractor expiry, browse the P0–P4 schematic chain, and lay out the colonies a target product needs.",
                features=[
                    "Live per-character colonies with extractor expiry",
                    "Full P0–P4 schematic chain browser",
                    "Colony planner: per-character planet + factory layout",
                    "Per-system planet-type lookup",
                ],
            ),
            _item(
                "Stockpiles", "/tools/stockpiles",
                [("prefix", "/tools/stockpiles")],
                desc="Set target quantities for the items you keep on hand — ammo, doctrine hulls, reaction fuel — and see current stock across all your characters vs. target, with deficits highlighted. Get a browser + Discord alert when a stockpile runs low.",
                features=[
                    "Per-item target quantity vs. account-wide holdings",
                    "Deficit highlighting for under-stocked items",
                    "Type search add form (htmx CRUD)",
                    "Sync-time \"stockpile low\" alerts (24h dedup)",
                ],
            ),
        ],
    },

    # ── Market (non-landing; parent url is the price-chart page itself) ─────
    # The economy pillar: prices, LP conversion, realized P&L, valuation, and
    # net worth in one place. `Prices` is the group's own destination
    # (url == group url) so the chrome suppresses its duplicate dropdown row;
    # it stays as items[0] for active-state matching and the mobile menu.
    {
        "label": "Market",
        "url": "/market",
        "match": [],
        "admin": False,
        "landing": False,
        "items": [
            _item(
                "Prices", "/market",
                [("prefix", "/market")],
                # LP Store ROI (/market/lp) and Trading P&L (/market/pnl) live
                # under the /market prefix too; keep them out of Prices' broad
                # prefix so the items don't light up together (same pattern as
                # Kill Feed / Kill Search).
                exclude=["/market/lp", "/market/pnl"],
                in_landing=False,
            ),
            _item(
                "LP Store ROI", "/market/lp",
                [("prefix", "/market/lp")],
                desc="Pick an NPC corporation and rank its loyalty-point store offers by ISK/LP — required-item cost and item value are taken from EVE's global average reference price.",
                features=[
                    "Full NPC corporation roster",
                    "Per-offer required-items cost",
                    "ISK/LP ratio, sorted best-first",
                    "Blueprint offers flagged when unpriced",
                ],
            ),
            _item(
                "Trading & Industry P&L", "/market/pnl",
                [("prefix", "/market/pnl")],
                desc="Realized profit from trading AND manufacturing — market buys and completed industry jobs (valued at completion-date build cost) FIFO-matched against sells, with broker fees and sales tax applied at flat rates.",
                features=[
                    "Trading / Industry / Total profit split",
                    "Per-item realized ISK, units flipped, cost-weighted margin",
                    "Monthly realized-profit chart, stacked by source",
                    "Per-character or account-wide filter",
                ],
            ),
            _item(
                "Appraisal", "/industry/appraisal",
                [("exact", "/industry/appraisal")],
                desc="Paste a cargo or asset list and value it against live sell orders at any major trade hub. Works with contract exports, loot drops, and inventory dumps.",
                features=[
                    "Paste any item / qty list",
                    "Jita, Amarr, Dodixie, Hek, or Rens lowest sell order",
                    "Per-item ISK and volume breakdown",
                    "Send the list straight to Hauling",
                ],
            ),
            _item(
                "Net Worth", "/tools/networth",
                [("prefix", "/tools/networth")],
                desc="Track your total net worth over time — a daily snapshot of every character's wallet, assets, market escrow, and industry work-in-progress, valued at EVE's global average reference price and stacked into one chart.",
                features=[
                    "Per-character stacked area + account total",
                    "30d / 90d / 1y range toggles",
                    "Daily automatic snapshots + on-demand \"Snapshot now\"",
                    "Unpriced-item count shown for transparency",
                ],
            ),
        ],
    },

    # ── Intel ──────────────────────────────────────────────────────────────
    {
        "label": "Intel",
        "url": "/intel",
        # Group also lights up on pages no single item owns: shared scan views
        # (/intel/<scan_id>) and entity combat-stats pages (/intel/entity/...).
        "match": [("prefix", "/intel/")],
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
                desc="Live universe-wide killboard from the killmail stream — a rolling feed of recent kills with ISK values, ship types, and system security bands, refreshed continuously.",
                features=[
                    "Live-polling feed with space-class, ship, and entity filters",
                    "\"Most Valuable\" last-7-day structure and ship strip",
                    "Per-kill ship, system, and value",
                    "Click-to-expand detail: victim fit, ISK, and attackers",
                ],
            ),
            _item(
                "Kill Search", "/intel/kills/search",
                [("prefix", "/intel/kills/search")],
                desc="Advanced killmail search across the full history — filter by time window, space type, entity, and ship, with special-case flags for awox, high-sec ganks, and blob padding.",
                features=[
                    "Time-window and date-range filters",
                    "Space type, wormhole class, ISK, and attacker-count scoping",
                    "Attacker / victim / either entity filters, plus ship type",
                    "Awox, HS Gank, Padding flags + AT Ships chip",
                ],
            ),
            _item(
                "D-Scan / Local", "/intel/dscan",
                # Legacy /dscan prefix kept until the Task 4 301 redirects ship.
                [("prefix", "/intel/dscan")],
                desc="Paste a D-scan or local roster and get an analyzed breakdown: ships by class and distance, or pilots grouped by corporation and alliance.",
                features=[
                    "D-scan paste parsing",
                    "Local chat list parsing",
                    "Shareable scan links with merge + extend",
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
                desc="Before you undock — check gate kills at every system on a route, find the gatecamps currently running, or pull recent activity for a specific entity.",
                features=[
                    "Route checker: per-system gate kills, avoid list, hi/lo/null routing",
                    "Gatecamp finder: systems with 3+ PvP kills in the past hour",
                    "War targets: kills and losses by system for any character, corp, or alliance",
                ],
            ),
            _item(
                "WH Tracker", "/intel/tracker",
                [("prefix", "/intel/tracker")],
                desc="Second-monitor live view for wormhole diving — polls your character's location and auto-renders the current J-system's reference plus killmail-archive intelligence.",
                features=[
                    "Auto-detects the tracked character's J-system",
                    "Statics, class, effect at a glance",
                    "Who lives here + capital activity + last structure kill",
                    "Structure-age paste box and live kill feed",
                ],
            ),
            # Wormhole reference — recon you do before a scan or a fight.
            # Nav home and landing cards both live here (no landing_group).
            _item(
                "Wormhole Systems", "/wormholes",
                [("exact", "/wormholes"), ("prefix", "/wormholes/system")],
                divider_before=True,
                desc="Per-system reference for every J-space system — class, effect, planets, static connections, recent kill activity.",
                features=[
                    "Filter by class, statics, planet types, and effect",
                    "Shattered, Drifter, and Perfect-PI filters",
                    "Per-system celestial diagram + recent kill activity",
                ],
            ),
            _item(
                "Wormhole Types", "/wormholes/types",
                [("prefix", "/wormholes/types")],
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
                desc="Wolf-Rayet, Black Hole, Pulsar, Cataclysmic Variable, Magnetar, Red Giant — per-class effect bonuses and penalties.",
                features=[
                    "Per-effect bonus/penalty tables",
                    "Values scaled C1 through C6",
                    "Jump-links for all six effects",
                ],
            ),
        ],
    },

    # ── Map (live maps only; parent url is the map itself, no landing page) ─
    {
        "label": "Map",
        "url": "/map",
        # Alliance detail pages (/alliance/<id>, linked from Trending) have no
        # owning item; light the group there like Dashboard does /character/.
        "match": [("prefix", "/alliance/")],
        "admin": False,
        "landing": False,
        "items": [
            _item("Star Map", "/map", [("exact", "/map")],
                  in_landing=False),
            _item("Wormhole Map", "/map/wormholes",
                  [("exact", "/map/wormholes")], in_landing=False),
            _item("Trending", "/trending", [("prefix", "/trending")],
                  in_landing=False),
            _item("Wanderer", _WANDERER_URL,
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
                desc="EVE server health + universe-wide ISK destruction over time. PCU and kill data overlaid on one chart, with a historical archive going back to 2003 sourced from eve-offline.net, eve-offline.com, and zKillboard.",
                features=[
                    "1d / 7d / 30d / 90d / 1y / 5y / all-time windows",
                    "Player-count + ISK destroyed overlay",
                    "Peak / mean PCU markers",
                    "Cross-validated historical archive (Chribba, Adminor, zKillboard)",
                ],
            ),
            _item(
                "Asset Search", "/assets",
                [("prefix", "/assets")],
                desc="Search across every linked character's assets at once. Find any item by name, see every stack and location.",
                features=[
                    "All characters in one view, grouped by account group",
                    "Jump distance from a chosen character's location",
                    "Free-text name search",
                ],
            ),
            _item(
                "Structure Timers", "/structure-timers",
                [("prefix", "/structure-timers")],
                desc="Shared structure-timer tracker with ACL. Add structure hits, share across corp/alliance groups, dashboard banners alert as they approach.",
                features=[
                    "Named ACL groups of characters, corps, and alliances",
                    "Site-wide 24-hour warning banners",
                    "UTC time input (no browser TZ confusion)",
                    "Auto-archive on expiry, kept for 30 days",
                ],
            ),
            _item(
                "Image Host", "/tools/images",
                [("prefix", "/tools/images"), ("prefix", "/i/")],
                desc="Private image uploader with shareable short links. Upload a PNG/JPG/GIF, get back a /i/<hash> URL.",
                features=[
                    "Re-encoded to JPEG on upload",
                    "Optional expiry per image",
                    "Per-user library",
                    "Short shareable URLs",
                ],
            ),
            _item(
                "Ship Fitting", "/tools/fitting",
                # Prefix covers sub-pages like /tools/fitting/compare; the
                # exclude keeps Saved Fits pages from lighting both items.
                [("prefix", "/tools/fitting")],
                exclude=["/tools/fitting/saved"],
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
                    "Sort by DPS, cost, ship, or folder",
                    "Side-by-side compare of any two fits",
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


def _prune_unconfigured_items():
    """Drop nav items whose URL is empty because their config variable is unset.

    Optional integrations (currently just the Wanderer link, gated on
    `WANDERER_URL`) are declared inline in NAV_GROUPS above so the registry stays
    one flat readable literal. Stripping them here keeps that readability without
    shipping a dead `href=""` link to instances that haven't configured them.

    Every real item has a URL, so this only ever removes opt-in externals.
    """
    for group in NAV_GROUPS:
        group["items"] = [item for item in group["items"] if item["url"]]


_prune_unconfigured_items()


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
