"""Landing pages for the Industry, Intel, and Tools top-level menus.

Each menu's top-level link now renders a card grid summarizing the
sub-tools, so a user clicking the menu name (not the dropdown) lands
on a readable overview rather than the first tool in the list."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


INDUSTRY_TOOLS = [
    {
        "name": "Manufacturing",
        "url": "/industry/manufacturing",
        "desc": "Blueprint cost + profit calculator. Pick a blueprint, set ME/TE, choose a structure and rigs, and see per-unit material cost, build time, and Jita-spread margin.",
        "features": [
            "Structure + rig material/time bonuses",
            "Security-status penalty modeling",
            "Jita buy/sell price lookups",
            "Per-run material totals and ISK figures",
        ],
    },
    {
        "name": "Active Jobs",
        "url": "/industry/jobs",
        "desc": "Every running or queued industry job across all your characters in one table, sorted by completion time.",
        "features": [
            "Manufacturing, research, invention, reactions",
            "Per-character and per-structure filters",
            "Completion countdown timers",
            "Installer and location labeling",
        ],
    },
    {
        "name": "Compression",
        "url": "/industry/compression",
        "desc": "Ore-to-compressed-ore volume + ISK calculator. Useful for deciding whether to haul raw ore or compress first.",
        "features": [
            "Per-ore compression ratios",
            "Volume savings display",
            "Compressed-ore Jita pricing",
        ],
    },
    {
        "name": "Hauling",
        "url": "/industry/hauling",
        "desc": "Quick hauling calculator — enter a cargo volume and route and get collateral, reward, and per-m³ rate suggestions.",
        "features": [
            "Route gate + jump count",
            "High-sec / low-sec / null-sec pricing tiers",
            "Collateral suggestions",
        ],
    },
    {
        "name": "Appraisal",
        "url": "/industry/appraisal",
        "desc": "Paste a cargo or asset list and get a Jita valuation at current market prices. Works with contract exports, loot drops, and inventory dumps.",
        "features": [
            "Paste any item / qty list",
            "Jita buy vs. sell totals",
            "Per-item breakdown",
        ],
    },
    {
        "name": "Mining Ledger",
        "url": "/industry/mining-ledger",
        "desc": "Per-character mining output — ore type, quantity, ISK value — sourced from the ESI mining ledger and aggregated over time.",
        "features": [
            "Per-day and per-character totals",
            "Ore-type breakdown",
            "ISK valuation at Jita sell",
        ],
    },
    {
        "name": "Planetary Industry",
        "url": "/industry/planetary",
        "desc": "PI schematic browser and chain planner — pick a product and see the input planets, extraction rates, and building requirements.",
        "features": [
            "All P1–P4 schematics",
            "Input planet-type reference",
            "Per-character PI status (if linked)",
        ],
    },
]


INTEL_TOOLS = [
    {
        "name": "D-Scan / Local",
        "url": "/intel/dscan",
        "desc": "Paste a D-scan or local roster and get an analyzed breakdown: ships by class, per-pilot zKillboard links, and corp/alliance affiliations.",
        "features": [
            "D-scan paste parsing",
            "Local chat list parsing",
            "zKillboard deep links per pilot",
            "Saved scan history",
        ],
    },
    {
        "name": "Gate Check",
        "url": "/intel/gatecheck",
        "desc": "Before you jump — paste a local list and see aggregate recent kill activity for the corps and alliances on the other side.",
        "features": [
            "Corp / alliance kill summaries",
            "Recent loss patterns",
            "Activity timestamps",
        ],
    },
    {
        "name": "Star Map",
        "url": "/map",
        "desc": "Live New Eden activity map — kill count heatmap, jumps, sov changes, industry activity, and NPC incursions all overlaid on an interactive canvas.",
        "features": [
            "Real-time ship-kills / jumps heatmap",
            "Sov-change flashing + alliance coloring",
            "Industry and FW system overlays",
            "Route planner with avoidance list",
            "Thera connection surfacing",
        ],
    },
    {
        "name": "Wormhole Systems",
        "url": "/wormholes",
        "desc": "Per-system reference for every J-space system — class, effect, planets, static connections, recent kill activity.",
        "features": [
            "Shattered / Drifter / Thera flags",
            "Kill history + recent fights",
            "Effect and class lookup",
        ],
    },
    {
        "name": "Wormhole Types",
        "url": "/wormholes/types",
        "desc": "Complete wormhole signature reference — K162, A/B/C/..., mass and lifetime by code.",
        "features": [
            "All static/transient wormhole types",
            "Mass / lifetime / jump limits",
            "Destination-class lookup",
        ],
    },
    {
        "name": "System Effects",
        "url": "/wormholes/effects",
        "desc": "Wolf-Rayet, Black Hole, Pulsar, Cataclysmic Variable, Magnetar, Red Giant — per-class effect bonuses and penalties.",
        "features": [
            "Per-effect bonus/penalty tables",
            "By wormhole class",
            "Fit relevance hints",
        ],
    },
]


TOOLS_TOOLS = [
    {
        "name": "Asset Search",
        "url": "/assets",
        "desc": "Search across every linked character's assets at once. Find any item by name, see every stack and location.",
        "features": [
            "All characters in one view",
            "Per-station / per-structure grouping",
            "Free-text name search",
        ],
    },
    {
        "name": "Structure Timers",
        "url": "/structure-timers",
        "desc": "Shared structure-timer tracker with ACL. Add structure hits, share across corp/alliance groups, dashboard banners alert as they approach.",
        "features": [
            "Group-based ACLs (corp / alliance / custom)",
            "Site-wide 24-hour warning banners",
            "UTC time input (no browser TZ confusion)",
            "Archive + audit trail",
        ],
    },
    {
        "name": "Image Host",
        "url": "/tools/images",
        "desc": "Private image uploader with shareable short links. Drop a PNG/JPG/GIF, get back a /i/<hash> URL.",
        "features": [
            "Drag-and-drop or paste upload",
            "Per-user library",
            "Short shareable URLs",
        ],
    },
    {
        "name": "Ship Fitting",
        "url": "/tools/fitting",
        "desc": "Fit a ship and see accurate DPS, EHP, cap stability, and fitting resources — matches Pyfa's numbers closely and threads through character skills.",
        "features": [
            "Character-accurate DPS / EHP / cap",
            "Module browser by market group",
            "Missing-skill warnings",
            "Per-level bonuses with proper damage profiles",
        ],
    },
    {
        "name": "Saved Fits",
        "url": "/tools/fitting/saved",
        "desc": "Your personal fitting library — saved fits organized into nested folders, ready to reopen in the fitting tool.",
        "features": [
            "Folder hierarchy",
            "Quick reopen in fitting tool",
            "Import / export EFT format",
        ],
    },
    {
        "name": "Discord Time",
        "url": "/tools/discordtime",
        "desc": "UTC-to-Discord-timestamp converter for fleet ops. Paste a time and get the Discord `<t:...>` codes for every rendering mode.",
        "features": [
            "All Discord timestamp modes",
            "Copy-paste ready output",
            "Relative / absolute formatting",
        ],
    },
    {
        "name": "Wormhole Mapper",
        "url": "https://mapper.thunderborn.dev",
        "url_label": "mapper.thunderborn.dev",
        "external": True,
        "desc": "Standalone Wanderer wormhole mapper co-hosted on this VPS. Real-time chain mapping, rolling tools, and shared chain state with your corp.",
        "features": [
            "Live chain visualization",
            "Signature / connection tracking",
            "Shared maps across members",
        ],
    },
]


@router.get("/industry", response_class=HTMLResponse)
async def industry_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse("tool_landing.html", {
        "request": request,
        "page_title": "Industry",
        "page_subtitle": "Manufacturing, hauling, compression, PI — pick a tool.",
        "tools": INDUSTRY_TOOLS,
    })


@router.get("/intel", response_class=HTMLResponse)
async def intel_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse("tool_landing.html", {
        "request": request,
        "page_title": "Intel",
        "page_subtitle": "Scans, gate checks, star map, wormhole reference.",
        "tools": INTEL_TOOLS,
    })


@router.get("/tools", response_class=HTMLResponse)
async def tools_landing(request: Request):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse("tool_landing.html", {
        "request": request,
        "page_title": "Tools",
        "page_subtitle": "Fittings, assets, timers, image host, timezone converter.",
        "tools": TOOLS_TOOLS,
    })
