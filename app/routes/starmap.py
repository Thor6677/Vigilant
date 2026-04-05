"""
Star Map — Interactive 2D map of New Eden with ESI statistics overlays.

Serves a Jinja2 page that loads the React/Pixi.js map app.
Provides API endpoints for live ESI statistics (kills, jumps, sov, fw, incursions).
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# ── In-memory stats cache ────────────────────────────────────────────────────
_stats_cache: dict[str, dict] = {}
_poller_started = False

ESI_BASE = "https://esi.evetech.net/latest"

# (ESI path, cache key, poll interval seconds)
ESI_STATS = [
    ("/universe/system_kills/",  "kills",       3600),
    ("/universe/system_jumps/",  "jumps",       3600),
    ("/sovereignty/map/",        "sovereignty",  3600),
    ("/fw/systems/",             "fw",           1800),
    ("/incursions/",             "incursions",   300),
]

# EVE faction IDs
FACTION_NAMES = {
    500001: "Caldari State",
    500002: "Minmatar Republic",
    500003: "Amarr Empire",
    500004: "Gallente Federation",
    500007: "Ammatar Mandate",
    500010: "Serpentis Corporation",
    500011: "Angel Cartel",
    500012: "Blood Raiders",
    500018: "Triglavian Collective",
    500019: "EDENCOM",
    500020: "CONCORD Assembly",
}


async def _poll_esi_stats():
    """Background loop: fetch public ESI endpoints and cache results."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    await asyncio.sleep(5)
    log.info("Map stats poller started")

    async with httpx.AsyncClient(
        base_url=ESI_BASE,
        timeout=30,
        headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"},
    ) as client:
        while True:
            for path, key, interval in ESI_STATS:
                try:
                    cached = _stats_cache.get(key, {})
                    last = cached.get("updated_at")
                    if last and (datetime.now(timezone.utc) - last).total_seconds() < interval:
                        continue

                    headers = {}
                    if cached.get("etag"):
                        headers["If-None-Match"] = cached["etag"]

                    resp = await client.get(path, headers=headers)

                    remain = resp.headers.get("X-ESI-Error-Limit-Remain")
                    if remain and int(remain) < 20:
                        log.warning("ESI error limit low (%s), pausing map poller 60s", remain)
                        await asyncio.sleep(60)
                        continue

                    if resp.status_code == 304:
                        _stats_cache[key]["updated_at"] = datetime.now(timezone.utc)
                        continue

                    if resp.status_code == 200:
                        _stats_cache[key] = {
                            "data": resp.json(),
                            "etag": resp.headers.get("ETag"),
                            "updated_at": datetime.now(timezone.utc),
                        }
                        log.debug("Map stats updated: %s (%d items)", key, len(resp.json()))
                    else:
                        log.warning("ESI %s returned %d", path, resp.status_code)

                except Exception as e:
                    log.warning("Map stats poll error for %s: %s", key, e)

            await asyncio.sleep(60)


def start_map_poller():
    """Called from main.py startup to launch the background poller."""
    asyncio.create_task(_poll_esi_stats())


# ── Vite manifest reader ─────────────────────────────────────────────────────

def _read_vite_assets() -> dict:
    """Read the Vite manifest to get hashed asset filenames."""
    manifest_path = FRONTEND_DIST / ".vite" / "manifest.json"
    if not manifest_path.exists():
        return {"entry_js": None, "preload_js": []}
    import json
    manifest = json.loads(manifest_path.read_text())
    # Find entry point
    entry = None
    for _key, val in manifest.items():
        if val.get("isEntry"):
            entry = val
            break
    if not entry:
        return {"entry_js": None, "preload_js": []}
    entry_js = "/map/" + entry["file"]
    preload_js = ["/map/" + manifest[imp]["file"] for imp in entry.get("imports", []) if imp in manifest]
    return {"entry_js": entry_js, "preload_js": preload_js}


# ── Page route ───────────────────────────────────────────────────────────────

@router.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    assets = _read_vite_assets()
    return templates.TemplateResponse("map.html", {"request": request, **assets})


# ── React built assets ───────────────────────────────────────────────────────

@router.get("/map/assets/{file_path:path}")
async def map_assets(file_path: str):
    """Serve Vite-built JS/CSS assets."""
    full = FRONTEND_DIST / "assets" / file_path
    if full.exists() and full.is_file():
        return FileResponse(full)
    return HTMLResponse("Not found", status_code=404)


@router.get("/map/data/{file_path:path}")
async def map_data(file_path: str):
    """Serve map data JSON files."""
    full = FRONTEND_DIST / "data" / file_path
    if full.exists() and full.is_file():
        return FileResponse(full)
    return HTMLResponse("Not found", status_code=404)


# ── API Endpoints ────────────────────────────────────────────────────────────

@router.get("/api/map/stats")
async def map_stats(request: Request):
    """Return all cached map statistics for overlay rendering."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    result = {}

    # Kills: {system_id: {ship, npc, pod}}
    kills = {}
    for entry in _stats_cache.get("kills", {}).get("data", []):
        sid = entry.get("system_id")
        if sid:
            kills[str(sid)] = {
                "ship": entry.get("ship_kills", 0),
                "npc": entry.get("npc_kills", 0),
                "pod": entry.get("pod_kills", 0),
            }
    result["kills"] = kills

    # Jumps: {system_id: count}
    jumps = {}
    for entry in _stats_cache.get("jumps", {}).get("data", []):
        sid = entry.get("system_id")
        if sid:
            jumps[str(sid)] = entry.get("ship_jumps", 0)
    result["jumps"] = jumps

    # Sovereignty: {system_id: {alliance_id, faction_id}}
    sov = {}
    for entry in _stats_cache.get("sovereignty", {}).get("data", []):
        sid = entry.get("system_id")
        if sid:
            sov[str(sid)] = {
                "alliance_id": entry.get("alliance_id"),
                "corporation_id": entry.get("corporation_id"),
                "faction_id": entry.get("faction_id"),
            }
    result["sovereignty"] = sov

    # Faction warfare: {system_id: {owner, occupier, contested, vp, vp_threshold}}
    fw = {}
    for entry in _stats_cache.get("fw", {}).get("data", []):
        sid = entry.get("solar_system_id")
        if sid:
            fw[str(sid)] = {
                "owner": entry.get("owner_faction_id"),
                "occupier": entry.get("occupier_faction_id"),
                "contested": entry.get("contested", "uncontested"),
                "vp": entry.get("victory_points", 0),
                "vp_threshold": entry.get("victory_points_threshold", 0),
            }
    result["fw"] = fw

    # Incursions: [{constellation_id, staging_system_id, type, state, systems}]
    incursions = []
    for entry in _stats_cache.get("incursions", {}).get("data", []):
        incursions.append({
            "constellation_id": entry.get("constellation_id"),
            "staging_system_id": entry.get("staging_solar_system_id"),
            "type": entry.get("type"),
            "state": entry.get("state"),
            "systems": entry.get("infested_solar_systems", []),
        })
    result["incursions"] = incursions

    # Cache freshness
    freshness = {}
    for key in ["kills", "jumps", "sovereignty", "fw", "incursions"]:
        updated = _stats_cache.get(key, {}).get("updated_at")
        freshness[key] = updated.isoformat() if updated else None
    result["_freshness"] = freshness

    return JSONResponse(result)
