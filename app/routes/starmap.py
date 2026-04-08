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
    response = templates.TemplateResponse("map.html", {"request": request, **assets})
    # Never cache the HTML — it embeds content-hashed bundle filenames that
    # change on every deploy. Caching the HTML causes the browser/Cloudflare
    # to reference deleted bundle hashes after a deploy, leaving a black map.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


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

@router.get("/api/map/characters")
async def map_characters(request: Request):
    """Return current user's character locations from dashboard cache."""
    import json as _json
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.db.models import AsyncSessionLocal, Character, CharacterDashboardCache

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        chars = (await db.execute(
            select(Character).where(Character.user_id == user_id, Character.is_active == True)
        )).scalars().all()

        result = []
        for char in chars:
            cache = (await db.execute(
                select(CharacterDashboardCache).where(
                    CharacterDashboardCache.character_id == char.character_id
                )
            )).scalar_one_or_none()

            loc = None
            if cache and cache.location_json:
                try:
                    loc = _json.loads(cache.location_json)
                except Exception:
                    pass

            result.append({
                "character_id": char.character_id,
                "character_name": char.character_name,
                "system_id": loc.get("solar_system_id") if loc else None,
                "system_name": loc.get("system_name") if loc else None,
                "is_main": bool(char.is_main),
            })

    return JSONResponse(result)


# ── Alliance name cache ───────────────────────────────────────────────────
_alliance_cache: dict[int, str] = {}


@router.get("/api/map/alliances")
async def map_alliances(request: Request):
    """Resolve alliance IDs to names. Accepts ?ids=123,456,789."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    ids_param = request.query_params.get("ids", "")
    if not ids_param:
        return JSONResponse({})

    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"error": "Invalid IDs"}, status_code=400)

    # Check cache first
    result = {}
    missing = []
    for aid in ids:
        if aid in _alliance_cache:
            result[str(aid)] = _alliance_cache[aid]
        else:
            missing.append(aid)

    # Fetch missing from ESI
    if missing:
        async with httpx.AsyncClient(
            base_url=ESI_BASE, timeout=10,
            headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"},
        ) as client:
            for aid in missing[:50]:  # Limit batch size
                try:
                    resp = await client.get(f"/alliances/{aid}/")
                    if resp.status_code == 200:
                        name = resp.json().get("name", f"Alliance {aid}")
                        _alliance_cache[aid] = name
                        result[str(aid)] = name
                except Exception:
                    result[str(aid)] = f"Alliance {aid}"

    return JSONResponse(result)


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


# ── Gate route planner: avoid list ───────────────────────────────────────────

_VALID_AVOID_KINDS = {"system", "constellation", "region"}


@router.get("/api/map/avoid")
async def list_avoid_entries(request: Request):
    """List the current user's avoid entries."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, UserAvoidEntry

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(UserAvoidEntry).where(UserAvoidEntry.user_id == user_id)
        )).scalars().all()

    return JSONResponse([
        {"id": r.id, "kind": r.kind, "entity_id": r.entity_id}
        for r in rows
    ])


@router.post("/api/map/avoid")
async def add_avoid_entry(request: Request):
    """Add an avoid entry. Body: {kind, entity_id}."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.db.models import AsyncSessionLocal, UserAvoidEntry

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    kind = body.get("kind")
    entity_id = body.get("entity_id")
    if kind not in _VALID_AVOID_KINDS or not isinstance(entity_id, int):
        return JSONResponse({"error": "Invalid kind or entity_id"}, status_code=400)

    async with AsyncSessionLocal() as db:
        entry = UserAvoidEntry(user_id=user_id, kind=kind, entity_id=entity_id)
        db.add(entry)
        try:
            await db.commit()
        except IntegrityError:
            # Already exists — fetch and return existing row
            await db.rollback()
            existing = (await db.execute(
                select(UserAvoidEntry).where(
                    UserAvoidEntry.user_id == user_id,
                    UserAvoidEntry.kind == kind,
                    UserAvoidEntry.entity_id == entity_id,
                )
            )).scalar_one()
            return JSONResponse({"id": existing.id, "kind": existing.kind, "entity_id": existing.entity_id})
        await db.refresh(entry)
        return JSONResponse({"id": entry.id, "kind": entry.kind, "entity_id": entry.entity_id}, status_code=201)


@router.delete("/api/map/avoid/{entry_id}")
async def delete_avoid_entry(entry_id: int, request: Request):
    """Remove an avoid entry. Only the owner can delete."""
    from sqlalchemy import select, delete
    from app.db.models import AsyncSessionLocal, UserAvoidEntry

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(UserAvoidEntry).where(
                UserAvoidEntry.id == entry_id,
                UserAvoidEntry.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not existing:
            return JSONResponse({"error": "Not found"}, status_code=404)

        await db.execute(
            delete(UserAvoidEntry).where(UserAvoidEntry.id == entry_id)
        )
        await db.commit()

    return JSONResponse({"deleted": entry_id})


# ── Gate route planner: saved routes ─────────────────────────────────────────

import json as _route_json
import secrets as _route_secrets


def _serialize_saved_route(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "origin_system_id": r.origin_system_id,
        "dest_system_id": r.dest_system_id,
        "waypoints": _route_json.loads(r.waypoints_json or "[]"),
        "preference": r.preference,
        "avoid": _route_json.loads(r.avoid_json or "[]"),
        "share_token": r.share_token,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _validate_route_body(body: dict) -> tuple[dict | None, str | None]:
    """Returns (clean_dict, error_message). Either is None."""
    if not isinstance(body, dict):
        return None, "Invalid body"
    name = body.get("name")
    origin = body.get("origin_system_id")
    dest = body.get("dest_system_id")
    waypoints = body.get("waypoints", [])
    preference = body.get("preference", "shortest")
    avoid = body.get("avoid", [])

    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        return None, "Invalid name"
    if not isinstance(origin, int) or not isinstance(dest, int):
        return None, "origin_system_id and dest_system_id must be integers"
    if not isinstance(waypoints, list) or not all(isinstance(w, int) for w in waypoints):
        return None, "waypoints must be a list of integers"
    if preference not in {"shortest", "highsec", "lowsec", "nullsec", "safest"}:
        return None, "Invalid preference"
    if not isinstance(avoid, list) or not all(isinstance(a, int) for a in avoid):
        return None, "avoid must be a list of integers"

    return {
        "name": name.strip(),
        "origin_system_id": origin,
        "dest_system_id": dest,
        "waypoints_json": _route_json.dumps(waypoints),
        "preference": preference,
        "avoid_json": _route_json.dumps(avoid),
    }, None


@router.get("/api/map/routes")
async def list_saved_routes(request: Request):
    """List the current user's saved gate routes."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SavedGateRoute)
            .where(SavedGateRoute.user_id == user_id)
            .order_by(SavedGateRoute.updated_at.desc())
        )).scalars().all()

    return JSONResponse([_serialize_saved_route(r) for r in rows])


@router.post("/api/map/routes")
async def create_saved_route(request: Request):
    """Create a new saved gate route."""
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    clean, err = _validate_route_body(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    async with AsyncSessionLocal() as db:
        route = SavedGateRoute(user_id=user_id, **clean)
        db.add(route)
        await db.commit()
        await db.refresh(route)
        return JSONResponse(_serialize_saved_route(route), status_code=201)


@router.put("/api/map/routes/{route_id}")
async def update_saved_route(route_id: int, request: Request):
    """Update a saved route. Owner only."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    clean, err = _validate_route_body(body)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    async with AsyncSessionLocal() as db:
        route = (await db.execute(
            select(SavedGateRoute).where(
                SavedGateRoute.id == route_id,
                SavedGateRoute.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not route:
            return JSONResponse({"error": "Not found"}, status_code=404)

        for key, value in clean.items():
            setattr(route, key, value)
        route.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(route)
        return JSONResponse(_serialize_saved_route(route))


@router.delete("/api/map/routes/{route_id}")
async def delete_saved_route(route_id: int, request: Request):
    """Delete a saved route. Owner only."""
    from sqlalchemy import select, delete
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(SavedGateRoute).where(
                SavedGateRoute.id == route_id,
                SavedGateRoute.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not existing:
            return JSONResponse({"error": "Not found"}, status_code=404)

        await db.execute(
            delete(SavedGateRoute).where(SavedGateRoute.id == route_id)
        )
        await db.commit()

    return JSONResponse({"deleted": route_id})


@router.post("/api/map/routes/{route_id}/share")
async def toggle_share_saved_route(route_id: int, request: Request):
    """Toggle sharing on a saved route. Generates a share_token if not set,
    clears it if already set."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        route = (await db.execute(
            select(SavedGateRoute).where(
                SavedGateRoute.id == route_id,
                SavedGateRoute.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not route:
            return JSONResponse({"error": "Not found"}, status_code=404)

        if route.share_token:
            route.share_token = None
        else:
            route.share_token = _route_secrets.token_urlsafe(12)
        route.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(route)
        return JSONResponse(_serialize_saved_route(route))


@router.get("/api/map/routes/shared/{share_token}")
async def get_shared_route(share_token: str):
    """Public read of a shared saved route. No auth required."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SavedGateRoute

    async with AsyncSessionLocal() as db:
        route = (await db.execute(
            select(SavedGateRoute).where(SavedGateRoute.share_token == share_token)
        )).scalar_one_or_none()
        if not route:
            return JSONResponse({"error": "Not found or sharing disabled"}, status_code=404)
        return JSONResponse(_serialize_saved_route(route))


# ── ESI autopilot waypoint push ──────────────────────────────────────────────

@router.post("/api/character/{character_id}/autopilot/waypoint")
async def set_autopilot_waypoint(character_id: int, request: Request):
    """Push a destination or waypoint to the in-game autopilot.

    Body: {system_id: int, clear: bool, add_to_beginning: bool}
      - clear=True replaces the entire route with this single destination
      - clear=False, add_to_beginning=False appends as the next waypoint
      - add_to_beginning=True inserts as the very next stop (override current)

    Returns 204 on success, 403 if the character lacks the
    esi-ui.write_waypoint.v1 scope (re-auth required).
    """
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, Character
    from app.esi.client import get_client_safe

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    system_id = body.get("system_id")
    clear = bool(body.get("clear", False))
    add_to_beginning = bool(body.get("add_to_beginning", False))
    if not isinstance(system_id, int):
        return JSONResponse({"error": "system_id must be an integer"}, status_code=400)

    # Verify the character belongs to the current user AND has the scope
    async with AsyncSessionLocal() as db:
        char = (await db.execute(
            select(Character).where(
                Character.character_id == character_id,
                Character.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not char:
            return JSONResponse({"error": "Character not found"}, status_code=404)
        if "esi-ui.write_waypoint.v1" not in (char.scopes or ""):
            return JSONResponse({
                "error": "missing_scope",
                "message": "This character needs to be re-authorized to enable the autopilot push feature.",
                "character_name": char.character_name,
            }, status_code=403)

    try:
        client = await get_client_safe(char)
        status = await client.post(
            "/ui/autopilot/waypoint/",
            params={
                "destination_id": system_id,
                "clear_other_waypoints": str(clear).lower(),
                "add_to_beginning": str(add_to_beginning).lower(),
            },
        )
        if status >= 400:
            log.warning("Autopilot waypoint push failed: char=%s status=%s", character_id, status)
            return JSONResponse(
                {"error": f"ESI returned HTTP {status}"},
                status_code=status if 400 <= status < 600 else 502,
            )
        return JSONResponse({"ok": True}, status_code=200)
    except Exception as e:
        log.warning("Autopilot waypoint push exception: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


# ── Gate route safety: per-hop kill / threat intel ───────────────────────────

@router.post("/api/map/route-safety")
async def route_safety(request: Request):
    """Enrich a list of system IDs with last-hour kill data, threat level,
    and smartbomb / interdictor / heavy-interdictor warning flags. Used by
    the gate route planner panel to show per-hop danger info.

    Body: {system_ids: [int]}
    Returns: dict keyed by system_id (string) → safety info
    """
    from app.db.models import AsyncSessionLocal
    from app.intel.safety import check_route_systems

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    system_ids = body.get("system_ids")
    if not isinstance(system_ids, list) or not all(isinstance(s, int) for s in system_ids):
        return JSONResponse({"error": "system_ids must be a list of ints"}, status_code=400)
    if len(system_ids) == 0:
        return JSONResponse({})
    if len(system_ids) > 200:
        return JSONResponse({"error": "Too many systems (max 200)"}, status_code=400)

    async with AsyncSessionLocal() as db:
        results = await check_route_systems(system_ids, db)

    # Re-key by system_id (string) so the frontend can lookup by ID directly
    return JSONResponse({
        str(r["system_id"]): {
            "kills": r["kill_count"],
            "pvp_kills": r["pvp_kills"],
            "threat": r["threat"],
            "has_smartbombs": r["has_smartbombs"],
            "has_dictors": r["has_dictors"],
            "has_hics": r["has_hics"],
            "total_value": r["total_value"],
            "total_value_str": r["total_value_str"],
            "top_kills": r["kills"][:5],
        }
        for r in results
    })
