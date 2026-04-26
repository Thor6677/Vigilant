"""
Star Map — Interactive 2D map of New Eden with ESI statistics overlays.

Serves a Jinja2 page that loads the React/Pixi.js map app.
Provides API endpoints for live ESI statistics (kills, jumps, sov, fw, incursions).
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
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
_prev_sov_state: dict[int, tuple] | None = None
_last_sov_cleanup: datetime | None = None

ESI_BASE = "https://esi.evetech.net/latest"

# (ESI path, cache key, poll interval seconds)
ESI_STATS = [
    ("/universe/system_kills/",       "kills",        3600),
    ("/universe/system_jumps/",       "jumps",        3600),
    ("/sovereignty/map/",             "sovereignty",  3600),
    ("/sovereignty/structures/",      "sov_structs",  3600),   # ADM per IHUB
    ("/fw/systems/",                  "fw",           1800),
    ("/incursions/",                  "incursions",    300),
    ("/industry/systems/",            "indices",      3600),   # industry cost indices
]

# External (non-ESI) data sources. Polled with their own cadence.
EVE_SCOUT_URL = "https://api.eve-scout.com/v2/public/signatures"
EVE_SCOUT_POLL_SECONDS = 600   # 10 minutes — Eve-Scout updates infrequently

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


async def _diff_and_store_sov_changes(new_data: list[dict]):
    """Compare new sov data against previous state, store change events."""
    global _prev_sov_state, _last_sov_cleanup
    from app.db.models import AsyncSessionLocal, SovereigntyChangeEvent

    # Normalize to {system_id: (alliance_id, faction_id)}
    new_state: dict[int, tuple] = {}
    for entry in new_data:
        sid = entry.get("system_id")
        if sid:
            new_state[sid] = (entry.get("alliance_id"), entry.get("faction_id"))

    if _prev_sov_state is None:
        _prev_sov_state = new_state
        log.info("Sov baseline set: %d systems", len(new_state))
        return

    # Find changes
    changes = []
    now = datetime.now(timezone.utc)
    all_sids = set(_prev_sov_state) | set(new_state)
    for sid in all_sids:
        old = _prev_sov_state.get(sid, (None, None))
        new = new_state.get(sid, (None, None))
        if old != new:
            changes.append(SovereigntyChangeEvent(
                system_id=sid,
                old_alliance_id=old[0],
                new_alliance_id=new[0],
                old_faction_id=old[1],
                new_faction_id=new[1],
                changed_at=now,
            ))

    _prev_sov_state = new_state

    if changes:
        try:
            async with AsyncSessionLocal() as db:
                db.add_all(changes)
                await db.commit()
            log.info("Sov changes recorded: %d systems", len(changes))
        except Exception as e:
            log.warning("Failed to store sov changes: %s", e)

    # Daily cleanup: remove rows older than 13 months
    if _last_sov_cleanup is None or (now - _last_sov_cleanup).total_seconds() > 86400:
        _last_sov_cleanup = now
        try:
            from sqlalchemy import delete as sa_delete
            cutoff = now - timedelta(days=395)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    sa_delete(SovereigntyChangeEvent).where(SovereigntyChangeEvent.changed_at < cutoff)
                )
                await db.commit()
                if result.rowcount:
                    log.info("Cleaned up %d old sov change rows", result.rowcount)
        except Exception as e:
            log.warning("Sov cleanup failed: %s", e)


async def _snapshot_activity(kills_data: list[dict], jumps_data: list[dict]):
    """Persist hourly kill/jump snapshots so the map can render a 48h sparkline.
    Called once per poll cycle after the kills + jumps caches update."""
    from app.db.models import AsyncSessionLocal, SystemActivitySnapshot
    from sqlalchemy import delete as sa_delete

    by_sys: dict[int, dict] = {}
    for entry in kills_data:
        sid = entry.get("system_id")
        if not sid:
            continue
        by_sys.setdefault(sid, {})
        by_sys[sid]["ship"] = entry.get("ship_kills", 0)
        by_sys[sid]["pod"] = entry.get("pod_kills", 0)
        by_sys[sid]["npc"] = entry.get("npc_kills", 0)
    for entry in jumps_data:
        sid = entry.get("system_id")
        if not sid:
            continue
        by_sys.setdefault(sid, {})
        by_sys[sid]["jumps"] = entry.get("ship_jumps", 0)

    now = datetime.now(timezone.utc)
    # Only snapshot systems that had some activity — avoids writing 5,200 rows
    # every hour for systems that were empty anyway.
    rows = [
        SystemActivitySnapshot(
            system_id=sid,
            captured_at=now,
            ship_kills=v.get("ship", 0),
            pod_kills=v.get("pod", 0),
            npc_kills=v.get("npc", 0),
            jumps=v.get("jumps", 0),
        )
        for sid, v in by_sys.items()
        if any((v.get("ship", 0), v.get("pod", 0), v.get("npc", 0), v.get("jumps", 0)))
    ]
    if not rows:
        return

    try:
        async with AsyncSessionLocal() as db:
            db.add_all(rows)
            await db.commit()
            # Trim snapshots older than 72 hours (sparkline only needs 48h;
            # the extra 24h gives us a buffer for late-arriving queries)
            cutoff = now - timedelta(hours=72)
            await db.execute(
                sa_delete(SystemActivitySnapshot).where(
                    SystemActivitySnapshot.captured_at < cutoff
                )
            )
            await db.commit()
    except Exception as e:
        log.warning("Activity snapshot persistence error: %s", e)


async def _poll_eve_scout(client: httpx.AsyncClient):
    """Fetch current Thera/Turnur wormhole connections from Eve-Scout.
    Cached under the 'thera' stats key; no auth required."""
    cached = _stats_cache.get("thera", {})
    last = cached.get("updated_at")
    if last and (datetime.now(timezone.utc) - last).total_seconds() < EVE_SCOUT_POLL_SECONDS:
        return
    try:
        resp = await client.get(EVE_SCOUT_URL, timeout=20)
        if resp.status_code != 200:
            log.warning("Eve-Scout returned %d", resp.status_code)
            return
        _stats_cache["thera"] = {
            "data": resp.json(),
            "updated_at": datetime.now(timezone.utc),
        }
    except Exception as e:
        log.warning("Eve-Scout fetch error: %s", e)


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
        async with httpx.AsyncClient(timeout=20,
            headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"}) as ext_client:
            while True:
                kills_updated = False
                jumps_updated = False
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
                            new_data = resp.json()
                            _stats_cache[key] = {
                                "data": new_data,
                                "etag": resp.headers.get("ETag"),
                                "updated_at": datetime.now(timezone.utc),
                            }
                            log.debug("Map stats updated: %s (%d items)", key, len(new_data))
                            # Diff sovereignty changes
                            if key == "sovereignty":
                                try:
                                    await _diff_and_store_sov_changes(new_data)
                                except Exception as sov_err:
                                    log.warning("Sov diff error: %s", sov_err)
                                # Fire-and-forget warm of the alliance name
                                # cache so clicks on system panels never
                                # block on per-alliance ESI lookups.
                                try:
                                    asyncio.create_task(_warm_alliance_cache_from_sov())
                                except Exception as warm_err:
                                    log.warning("Alliance warm schedule error: %s", warm_err)
                            if key == "kills":
                                kills_updated = True
                            elif key == "jumps":
                                jumps_updated = True
                        else:
                            log.warning("ESI %s returned %d", path, resp.status_code)

                    except Exception as e:
                        log.warning("Map stats poll error for %s: %s", key, e)

                # After a poll cycle where either kills or jumps just refreshed,
                # persist an activity snapshot so the 48h graph has data.
                if kills_updated or jumps_updated:
                    try:
                        await _snapshot_activity(
                            _stats_cache.get("kills", {}).get("data", []),
                            _stats_cache.get("jumps", {}).get("data", []),
                        )
                    except Exception as snap_err:
                        log.warning("Snapshot error: %s", snap_err)

                # External sources
                try:
                    await _poll_eve_scout(ext_client)
                except Exception as ext_err:
                    log.warning("Eve-Scout poll error: %s", ext_err)

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


# ── Alliance name cache (in-memory hot layer over the DB) ─────────────────
#
# The DB is the source of truth; this dict is a per-process hot cache so we
# don't round-trip SQLite for every panel click. Populated from the DB on
# first read and refreshed whenever we resolve new IDs via bulk ESI.
_alliance_cache: dict[int, str] = {}
_alliance_cache_ttl_days = 30


async def _load_alliance_cache_from_db():
    """Warm the in-memory alliance name cache from the persistent DB table.
    Called once on first access and re-called when the process starts."""
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, AllianceNameCache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AllianceNameCache.alliance_id, AllianceNameCache.name)
            )).all()
            for aid, name in rows:
                _alliance_cache[aid] = name
    except Exception as e:
        log.warning("Alliance cache warm failed: %s", e)


async def _resolve_alliance_names_bulk(ids: list[int]) -> dict[int, str]:
    """Resolve a list of alliance IDs to names using the bulk
    POST /universe/names/ endpoint (≤ 1,000 IDs per call).

    Writes results to both the in-memory cache and the DB. Returns a dict
    of {alliance_id: name} for every ID that resolved successfully."""
    from app.db.models import AsyncSessionLocal, AllianceNameCache

    out: dict[int, str] = {}
    if not ids:
        return out

    async with httpx.AsyncClient(
        base_url=ESI_BASE, timeout=15,
        headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"},
    ) as client:
        # Chunk into groups of 1,000 (ESI's max)
        for chunk_start in range(0, len(ids), 1000):
            chunk = ids[chunk_start:chunk_start + 1000]
            try:
                resp = await client.post("/universe/names/", json=chunk)
                if resp.status_code != 200:
                    log.warning("Bulk /universe/names returned %d", resp.status_code)
                    continue
                for entry in resp.json():
                    if entry.get("category") != "alliance":
                        continue
                    aid = entry.get("id")
                    name = entry.get("name")
                    if isinstance(aid, int) and isinstance(name, str):
                        out[aid] = name
                        _alliance_cache[aid] = name
            except Exception as e:
                log.warning("Bulk /universe/names error: %s", e)

    # Persist to DB in one commit
    if out:
        try:
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                existing = (await db.execute(
                    select(AllianceNameCache).where(
                        AllianceNameCache.alliance_id.in_(list(out.keys()))
                    )
                )).scalars().all()
                by_id = {r.alliance_id: r for r in existing}
                for aid, name in out.items():
                    if aid in by_id:
                        by_id[aid].name = name
                        by_id[aid].cached_at = now
                    else:
                        db.add(AllianceNameCache(alliance_id=aid, name=name, cached_at=now))
                await db.commit()
        except Exception as e:
            log.warning("Alliance cache DB write failed: %s", e)

    return out


async def _warm_alliance_cache_from_sov():
    """Prime the alliance name cache from all alliances currently holding sov.
    Runs after each sovereignty poll so repeat map loads never block on
    per-alliance ESI lookups."""
    if not _alliance_cache:
        await _load_alliance_cache_from_db()

    sov_ids: set[int] = set()
    for entry in _stats_cache.get("sovereignty", {}).get("data", []):
        aid = entry.get("alliance_id")
        if aid and aid not in _alliance_cache:
            sov_ids.add(aid)

    if sov_ids:
        log.info("Alliance cache: resolving %d new sov alliance names in bulk", len(sov_ids))
        await _resolve_alliance_names_bulk(list(sov_ids))


@router.get("/api/map/alliances")
async def map_alliances(request: Request):
    """Resolve alliance IDs to names. Accepts ?ids=123,456,789.

    Caching layers (fastest → slowest):
      1. In-memory `_alliance_cache` dict (warm from DB on first access)
      2. `alliance_name_cache` DB table (30-day TTL)
      3. Bulk POST /universe/names/ ESI lookup (≤ 1,000 IDs per call)
    """
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

    # Lazy-warm the in-memory cache from DB on first request of the process
    if not _alliance_cache:
        await _load_alliance_cache_from_db()

    result: dict[str, str] = {}
    missing: list[int] = []
    for aid in ids:
        if aid in _alliance_cache:
            result[str(aid)] = _alliance_cache[aid]
        else:
            missing.append(aid)

    # One bulk ESI call resolves up to 1,000 missing IDs
    if missing:
        resolved = await _resolve_alliance_names_bulk(missing)
        for aid, name in resolved.items():
            result[str(aid)] = name
        # Fill in any still-unresolved with the placeholder so callers don't
        # retry endlessly on bad IDs
        for aid in missing:
            if str(aid) not in result:
                result[str(aid)] = f"Alliance {aid}"

    return JSONResponse(result)


SOV_RANGE_DELTAS = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "6m":  timedelta(days=182),
    "1y":  timedelta(days=365),
}


@router.get("/api/map/sov-changes")
async def map_sov_changes(request: Request):
    """Return sovereignty changes within a time range."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    range_key = request.query_params.get("range", "7d")
    delta = SOV_RANGE_DELTAS.get(range_key)
    if not delta:
        return JSONResponse({"error": "Invalid range"}, status_code=400)

    cutoff = datetime.now(timezone.utc) - delta
    from app.db.models import AsyncSessionLocal, SovereigntyChangeEvent
    from sqlalchemy import select, func

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SovereigntyChangeEvent)
            .where(SovereigntyChangeEvent.changed_at >= cutoff)
            .order_by(SovereigntyChangeEvent.changed_at.asc())
        )
        rows = result.scalars().all()

    # Aggregate per system: earliest old_*, latest new_*, count
    changes: dict[str, dict] = {}
    for row in rows:
        sid = str(row.system_id)
        if sid not in changes:
            changes[sid] = {
                "old_alliance_id": row.old_alliance_id,
                "new_alliance_id": row.new_alliance_id,
                "old_faction_id": row.old_faction_id,
                "new_faction_id": row.new_faction_id,
                "first_change": row.changed_at.isoformat(),
                "last_change": row.changed_at.isoformat(),
                "change_count": 1,
            }
        else:
            entry = changes[sid]
            entry["new_alliance_id"] = row.new_alliance_id
            entry["new_faction_id"] = row.new_faction_id
            entry["last_change"] = row.changed_at.isoformat()
            entry["change_count"] += 1

    return JSONResponse({
        "changes": changes,
        "range": range_key,
        "since": cutoff.isoformat(),
    })


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

    # Faction warfare: {system_id: {owner, occupier, contested, vp, vp_threshold, vp_pct}}
    fw = {}
    for entry in _stats_cache.get("fw", {}).get("data", []):
        sid = entry.get("solar_system_id")
        if sid:
            vp = entry.get("victory_points", 0) or 0
            vpt = entry.get("victory_points_threshold", 0) or 0
            fw[str(sid)] = {
                "owner": entry.get("owner_faction_id"),
                "occupier": entry.get("occupier_faction_id"),
                "contested": entry.get("contested", "uncontested"),
                "vp": vp,
                "vp_threshold": vpt,
                "vp_pct": round(100 * vp / vpt, 1) if vpt > 0 else 0.0,
            }
    result["fw"] = fw

    # Incursions: [{constellation_id, staging_system_id, type, state, systems, influence, has_boss}]
    incursions = []
    for entry in _stats_cache.get("incursions", {}).get("data", []):
        incursions.append({
            "constellation_id": entry.get("constellation_id"),
            "staging_system_id": entry.get("staging_solar_system_id"),
            "type": entry.get("type"),
            "state": entry.get("state"),
            "influence": entry.get("influence"),
            "has_boss": entry.get("has_boss", False),
            "systems": entry.get("infested_solar_systems", []),
        })
    result["incursions"] = incursions

    # Industry cost indices: {system_id: {manufacturing, me, te, copying, invention, reaction}}
    indices = {}
    for entry in _stats_cache.get("indices", {}).get("data", []):
        sid = entry.get("solar_system_id")
        if not sid:
            continue
        by_act = {a.get("activity"): a.get("cost_index", 0) for a in entry.get("cost_indices", [])}
        indices[str(sid)] = {
            "manufacturing": by_act.get("manufacturing", 0),
            "me":            by_act.get("researching_material_efficiency", 0),
            "te":            by_act.get("researching_time_efficiency", 0),
            "copying":       by_act.get("copying", 0),
            "invention":     by_act.get("invention", 0),
            "reaction":      by_act.get("reaction", 0),
        }
    result["indices"] = indices

    # ADM (Activity Defense Multiplier) per sov system, from the sov structures feed.
    # IHUB rows carry `vulnerability_occupancy_level`; TCU rows don't. We take the
    # max across structures in a system so a system with both still shows the IHUB ADM.
    adm: dict[str, float] = {}
    for entry in _stats_cache.get("sov_structs", {}).get("data", []):
        sid = entry.get("solar_system_id")
        level = entry.get("vulnerability_occupancy_level")
        if sid and level is not None:
            try:
                v = float(level)
            except (TypeError, ValueError):
                continue
            if adm.get(str(sid), 0) < v:
                adm[str(sid)] = v
    result["adm"] = adm

    # Thera/Turnur public wormhole connections from Eve-Scout.
    # Each entry: {in_signature, in_system_id, out_system_id, wh_type, ...}
    # We expose an array of {src, dst, src_system, dst_system, type, mass_status, life_status}.
    thera = []
    for sig in _stats_cache.get("thera", {}).get("data", []) or []:
        src = sig.get("out_system_id")
        dst = sig.get("in_system_id")
        if not src or not dst:
            continue
        thera.append({
            "src": src,
            "dst": dst,
            "src_name": (sig.get("out_system_name") or ""),
            "dst_name": (sig.get("in_system_name") or ""),
            "type": sig.get("wh_type") or "",
            "mass_status": sig.get("remaining_hours") and "ok" or (sig.get("wh_mass") or ""),
            "life_hours": sig.get("remaining_hours"),
            "sig": sig.get("out_signature") or "",
            "created_at": sig.get("created_at"),
        })
    result["thera"] = thera

    # Cache freshness
    freshness = {}
    for key in ["kills", "jumps", "sovereignty", "sov_structs", "fw", "incursions", "indices", "thera"]:
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


# ── Planet types per system (for PI site-selection overlay) ───────────────

# SDE invType IDs for the 9 planet types
PLANET_TYPE_NAMES: dict[int, str] = {
    11:    "Temperate",
    12:    "Ice",
    13:    "Gas",
    2014:  "Oceanic",
    2015:  "Lava",
    2016:  "Barren",
    2017:  "Storm",
    2063:  "Plasma",
    30889: "Shattered",
}

_planet_types_cache: dict | None = None


@router.get("/api/map/planet-types")
async def map_planet_types(request: Request):
    """Return per-system planet-type counts from the SDE.

    Shape: {"types": {"11": "Temperate", ...},
            "systems": {"30000142": {"11": 2, "2016": 3}, ...}}

    Cached in-memory for the process lifetime — the SDE only reloads on startup.
    """
    global _planet_types_cache

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if _planet_types_cache is not None:
        return JSONResponse(_planet_types_cache)

    from sqlalchemy import select, func
    from app.db.models import AsyncSessionLocal
    from app.db.sde_models import SDEPlanet

    systems: dict[str, dict[str, int]] = {}
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SDEPlanet.system_id, SDEPlanet.planet_type_id, func.count().label("n"))
                .group_by(SDEPlanet.system_id, SDEPlanet.planet_type_id)
            )).all()
            for sid, ptid, n in rows:
                if ptid not in PLANET_TYPE_NAMES:
                    continue
                systems.setdefault(str(sid), {})[str(ptid)] = int(n)
    except Exception as e:
        log.warning("planet-types query failed: %s", e)
        return JSONResponse({"types": PLANET_TYPE_NAMES, "systems": {}})

    _planet_types_cache = {
        "types": {str(k): v for k, v in PLANET_TYPE_NAMES.items()},
        "systems": systems,
    }
    return JSONResponse(_planet_types_cache)


# ── 48h activity history (sparkline in system info panel) ─────────────────

# ── Wormhole-space synthetic map data ─────────────────────────────────────
# J-systems have no canonical positions — see app/intel/wormhole_layout.py
# for the synthetic 3×3 class-grid layout. Three endpoints mirror the
# k-space static bundles so the React app can use the same useMapData
# hook with a different base URL.

@router.get("/map/wormholes", response_class=HTMLResponse)
async def map_wormholes_page(request: Request):
    """Wormhole-space spatial map — same React bundle as /map, fed
    a synthetic per-class layout for J-systems."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    assets = _read_vite_assets()
    response = templates.TemplateResponse(
        "map_wormholes.html", {"request": request, **assets}
    )
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/api/map/wormholes-data/systems.json")
async def map_wormholes_systems(request: Request):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from app.intel.wormhole_layout import build_wormhole_layout
    layout = await build_wormhole_layout()
    return JSONResponse(layout["systems"])


@router.get("/api/map/wormholes-data/edges.json")
async def map_wormholes_edges(request: Request):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from app.intel.wormhole_layout import build_wormhole_layout
    layout = await build_wormhole_layout()
    return JSONResponse(layout["edges"])


@router.get("/api/map/wormholes-data/regions.json")
async def map_wormholes_regions(request: Request):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from app.intel.wormhole_layout import build_wormhole_layout
    layout = await build_wormhole_layout()
    return JSONResponse(layout["regions"])


# Per-(window, space) cache for the bulk heatmap response. Buckets are
# stable within a window (e.g. hourly in 1d) so we can serve the same
# JSON to every scrubber tick. Short TTL keeps live data fresh.
_HEATMAP_CACHE: dict[tuple[str, str], tuple[datetime, dict]] = {}
_HEATMAP_TTL_SECONDS = 30


@router.get("/api/map/kill-heatmap")
async def map_kill_heatmap(request: Request, window: str = "1d", space: str = "k"):
    """Bulk per-system kill counts time-bucketed for the heatmap scrubber.

    window=1d  → 24 hourly buckets (sourced from SystemActivitySnapshot)
    window=7d  → 7  daily  buckets (sourced from killmails)
    window=30d → 30 daily  buckets (sourced from killmails)

    space=k → k-space systems only (id < 31000000)
    space=w → wormhole systems only (id >= 31000000)

    Sparse encoding: response.data only includes systems with ≥1 kill in
    any bucket. All other systems are implicit zeros.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if window not in ("1d", "7d", "30d"):
        return JSONResponse({"error": "invalid window"}, status_code=400)
    if space not in ("k", "w"):
        return JSONResponse({"error": "invalid space"}, status_code=400)

    cache_key = (window, space)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cached = _HEATMAP_CACHE.get(cache_key)
    if cached is not None:
        expires_at, body = cached
        if now_utc < expires_at:
            return JSONResponse(body)

    from sqlalchemy import Integer, func, select
    from app.db.models import AsyncSessionLocal, Killmail, SystemActivitySnapshot

    if window == "1d":
        # 24 hourly buckets aligned to wall-clock hours, ending at the top
        # of the current hour (so the rightmost bucket is "the most recent
        # full hour"). Source: SystemActivitySnapshot.
        end = now_utc.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=24)
        bucket_seconds = 3600
        num_buckets = 24
        buckets = [(start + timedelta(seconds=i * bucket_seconds)).isoformat() + "Z"
                   for i in range(num_buckets)]

        async with AsyncSessionLocal() as db:
            sys_filter = (
                SystemActivitySnapshot.system_id < 31000000 if space == "k"
                else SystemActivitySnapshot.system_id >= 31000000
            )
            # bucket index = (captured_at - start) / 3600s, integer.
            bin_expr = func.cast(
                (func.julianday(SystemActivitySnapshot.captured_at) - func.julianday(start))
                * 24.0,
                Integer,
            )
            rows = (await db.execute(
                select(
                    SystemActivitySnapshot.system_id,
                    bin_expr.label("b"),
                    func.sum(SystemActivitySnapshot.ship_kills + SystemActivitySnapshot.pod_kills).label("k"),
                )
                .where(
                    SystemActivitySnapshot.captured_at >= start,
                    SystemActivitySnapshot.captured_at < end,
                    sys_filter,
                )
                .group_by(SystemActivitySnapshot.system_id, "b")
            )).all()
    else:
        days = 7 if window == "7d" else 30
        end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=days)
        bucket_seconds = 86400
        num_buckets = days
        buckets = [(start + timedelta(days=i)).date().isoformat()
                   for i in range(num_buckets)]

        async with AsyncSessionLocal() as db:
            sys_filter = (
                Killmail.solar_system_id < 31000000 if space == "k"
                else Killmail.solar_system_id >= 31000000
            )
            bin_expr = func.cast(
                func.julianday(Killmail.killmail_time) - func.julianday(start),
                Integer,
            )
            rows = (await db.execute(
                select(
                    Killmail.solar_system_id.label("system_id"),
                    bin_expr.label("b"),
                    func.count().label("k"),
                )
                .where(
                    Killmail.killmail_time >= start,
                    Killmail.killmail_time < end,
                    sys_filter,
                )
                .group_by(Killmail.solar_system_id, "b")
            )).all()

    # Pivot to {system_id: [v0, v1, ...]} — sparse, only systems with ≥1 kill.
    data: dict[str, list[int]] = {}
    max_value = 0
    for sys_id, b, k in rows:
        if sys_id is None or b is None:
            continue
        bi = int(b)
        if bi < 0 or bi >= num_buckets:
            continue
        kv = int(k or 0)
        if kv <= 0:
            continue
        arr = data.get(str(sys_id))
        if arr is None:
            arr = [0] * num_buckets
            data[str(sys_id)] = arr
        arr[bi] = kv
        if kv > max_value:
            max_value = kv

    body = {
        "window": window,
        "space": space,
        "bucket_seconds": bucket_seconds,
        "buckets": buckets,
        "max_value": max_value,
        "data": data,
    }
    _HEATMAP_CACHE[cache_key] = (
        now_utc + timedelta(seconds=_HEATMAP_TTL_SECONDS),
        body,
    )
    return JSONResponse(body)


@router.get("/api/map/history/{system_id}")
async def map_system_history(system_id: int, request: Request):
    """Return hourly kill/jump snapshots for a system over the last 48 hours.
    Caller: SystemInfoPanel renders this as a sparkline."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SystemActivitySnapshot

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SystemActivitySnapshot)
            .where(
                SystemActivitySnapshot.system_id == system_id,
                SystemActivitySnapshot.captured_at >= cutoff,
            )
            .order_by(SystemActivitySnapshot.captured_at.asc())
        )).scalars().all()

    return JSONResponse({
        "system_id": system_id,
        "hours": [
            {
                "t": r.captured_at.isoformat(),
                "ship": r.ship_kills,
                "pod": r.pod_kills,
                "npc": r.npc_kills,
                "jumps": r.jumps,
            }
            for r in rows
        ],
    })


# ── User bookmarks on the star map ────────────────────────────────────────

_VALID_BOOKMARK_KINDS = {"system", "constellation", "region"}


def _serialize_bookmark(b) -> dict:
    return {
        "id": b.id,
        "kind": b.kind,
        "entity_id": b.entity_id,
        "label": b.label,
        "color": b.color,
        "notes": b.notes,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


@router.get("/api/map/bookmarks")
async def list_bookmarks(request: Request):
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, UserMapBookmark

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(UserMapBookmark)
            .where(UserMapBookmark.user_id == user_id)
            .order_by(UserMapBookmark.created_at.desc())
        )).scalars().all()

    return JSONResponse([_serialize_bookmark(b) for b in rows])


@router.post("/api/map/bookmarks")
async def create_bookmark(request: Request):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, UserMapBookmark

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    kind = body.get("kind")
    entity_id = body.get("entity_id")
    label = body.get("label")
    color = body.get("color")
    notes = body.get("notes")

    if kind not in _VALID_BOOKMARK_KINDS or not isinstance(entity_id, int):
        return JSONResponse({"error": "Invalid kind or entity_id"}, status_code=400)
    if label is not None and (not isinstance(label, str) or len(label) > 64):
        return JSONResponse({"error": "Invalid label"}, status_code=400)
    if color is not None and (not isinstance(color, str) or len(color) > 8):
        return JSONResponse({"error": "Invalid color"}, status_code=400)

    async with AsyncSessionLocal() as db:
        bm = UserMapBookmark(
            user_id=user_id, kind=kind, entity_id=entity_id,
            label=label, color=color, notes=notes,
        )
        db.add(bm)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = (await db.execute(
                select(UserMapBookmark).where(
                    UserMapBookmark.user_id == user_id,
                    UserMapBookmark.kind == kind,
                    UserMapBookmark.entity_id == entity_id,
                )
            )).scalar_one()
            return JSONResponse(_serialize_bookmark(existing))
        await db.refresh(bm)
        return JSONResponse(_serialize_bookmark(bm), status_code=201)


@router.patch("/api/map/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: int, request: Request):
    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, UserMapBookmark

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    async with AsyncSessionLocal() as db:
        bm = (await db.execute(
            select(UserMapBookmark).where(
                UserMapBookmark.id == bookmark_id,
                UserMapBookmark.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not bm:
            return JSONResponse({"error": "Not found"}, status_code=404)

        if "label" in body:
            v = body["label"]
            if v is not None and (not isinstance(v, str) or len(v) > 64):
                return JSONResponse({"error": "Invalid label"}, status_code=400)
            bm.label = v
        if "color" in body:
            v = body["color"]
            if v is not None and (not isinstance(v, str) or len(v) > 8):
                return JSONResponse({"error": "Invalid color"}, status_code=400)
            bm.color = v
        if "notes" in body:
            v = body["notes"]
            if v is not None and not isinstance(v, str):
                return JSONResponse({"error": "Invalid notes"}, status_code=400)
            bm.notes = v
        await db.commit()
        await db.refresh(bm)
        return JSONResponse(_serialize_bookmark(bm))


@router.delete("/api/map/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int, request: Request):
    from sqlalchemy import select, delete
    from app.db.models import AsyncSessionLocal, UserMapBookmark

    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(UserMapBookmark).where(
                UserMapBookmark.id == bookmark_id,
                UserMapBookmark.user_id == user_id,
            )
        )).scalar_one_or_none()
        if not existing:
            return JSONResponse({"error": "Not found"}, status_code=404)
        await db.execute(
            delete(UserMapBookmark).where(UserMapBookmark.id == bookmark_id)
        )
        await db.commit()

    return JSONResponse({"deleted": bookmark_id})


# ── Trending: sov changes + most violent (serves the trending page) ───────

@router.get("/api/map/trending/sov")
async def trending_sov(request: Request):
    """Aggregate sov changes over the last 7 days, grouped by new_alliance_id
    (systems gained) and old_alliance_id (systems lost). Returns a leaderboard."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    from sqlalchemy import select
    from app.db.models import AsyncSessionLocal, SovereigntyChangeEvent

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SovereigntyChangeEvent)
            .where(SovereigntyChangeEvent.changed_at >= cutoff)
            .order_by(SovereigntyChangeEvent.changed_at.asc())
        )).scalars().all()

    # Use final state for each system in the window (last event wins)
    latest: dict[int, SovereigntyChangeEvent] = {}
    for row in rows:
        latest[row.system_id] = row

    gained: dict[int, int] = {}   # alliance_id → systems gained (net)
    lost: dict[int, int] = {}     # alliance_id → systems lost
    for sid, row in latest.items():
        old_a = row.old_alliance_id
        new_a = row.new_alliance_id
        if old_a == new_a:
            continue
        if new_a:
            gained[new_a] = gained.get(new_a, 0) + 1
        if old_a:
            lost[old_a] = lost.get(old_a, 0) + 1

    return JSONResponse({
        "since": cutoff.isoformat(),
        "total_changes": len(latest),
        "top_gained": sorted(gained.items(), key=lambda kv: -kv[1])[:20],
        "top_lost": sorted(lost.items(), key=lambda kv: -kv[1])[:20],
    })


@router.get("/api/map/trending/violent")
async def trending_violent(request: Request):
    """Most violent systems in the last 3 hours, sourced from the stored
    hourly snapshots. Returns top N by combined ship + pod kills."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    from sqlalchemy import select, func
    from app.db.models import AsyncSessionLocal, SystemActivitySnapshot

    cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                SystemActivitySnapshot.system_id,
                func.sum(SystemActivitySnapshot.ship_kills).label("ship"),
                func.sum(SystemActivitySnapshot.pod_kills).label("pod"),
                func.sum(SystemActivitySnapshot.npc_kills).label("npc"),
                func.sum(SystemActivitySnapshot.jumps).label("jumps"),
            )
            .where(SystemActivitySnapshot.captured_at >= cutoff)
            .group_by(SystemActivitySnapshot.system_id)
            .order_by((
                func.sum(SystemActivitySnapshot.ship_kills) +
                func.sum(SystemActivitySnapshot.pod_kills)
            ).desc())
            .limit(50)
        )).all()

    return JSONResponse({
        "since": cutoff.isoformat(),
        "systems": [
            {
                "system_id": sid,
                "ship_kills": int(ship or 0),
                "pod_kills": int(pod or 0),
                "npc_kills": int(npc or 0),
                "jumps": int(jumps or 0),
            }
            for sid, ship, pod, npc, jumps in rows
            if (ship or 0) + (pod or 0) > 0
        ],
    })


@router.get("/trending", response_class=HTMLResponse)
async def trending_page(request: Request):
    """HTML page — sov leaderboards + most violent systems."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse("trending.html", {"request": request})


# ── Alliance detail page (sov summary, 7d changes) ────────────────────────

@router.get("/api/map/alliance/{alliance_id}")
async def alliance_detail(alliance_id: int, request: Request):
    """Aggregated alliance snapshot derived from the sov map cache + 7d change log.

    Returns current sov count, 7d gained/lost, and a recent-changes timeline.
    ESI's /alliances/{id}/ gives us the alliance header (name/ticker)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # 1. Current sov systems for this alliance
    current_sys: list[int] = []
    for entry in _stats_cache.get("sovereignty", {}).get("data", []):
        if entry.get("alliance_id") == alliance_id:
            current_sys.append(entry.get("system_id"))

    # 2. 7d change activity
    from sqlalchemy import select, or_
    from app.db.models import AsyncSessionLocal, SovereigntyChangeEvent

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(SovereigntyChangeEvent)
            .where(
                SovereigntyChangeEvent.changed_at >= cutoff,
                or_(
                    SovereigntyChangeEvent.new_alliance_id == alliance_id,
                    SovereigntyChangeEvent.old_alliance_id == alliance_id,
                ),
            )
            .order_by(SovereigntyChangeEvent.changed_at.desc())
        )).scalars().all()

    gained_7d = sum(1 for r in rows if r.new_alliance_id == alliance_id and r.old_alliance_id != alliance_id)
    lost_7d = sum(1 for r in rows if r.old_alliance_id == alliance_id and r.new_alliance_id != alliance_id)

    # 3. Fetch alliance header from ESI. The alliance detail endpoint returns
    # more metadata (ticker, creator, founded date) than the bulk /universe/names/
    # call, so this page specifically needs the full response — we still
    # update the name cache as a side effect.
    header = {}
    async with httpx.AsyncClient(
        base_url=ESI_BASE, timeout=10,
        headers={"Accept": "application/json", "User-Agent": "Vigilant/1.0"},
    ) as client:
        try:
            resp = await client.get(f"/alliances/{alliance_id}/")
            if resp.status_code == 200:
                h = resp.json()
                header = {
                    "name": h.get("name"),
                    "ticker": h.get("ticker"),
                    "creator_id": h.get("creator_id"),
                    "executor_corporation_id": h.get("executor_corporation_id"),
                    "date_founded": h.get("date_founded"),
                    "faction_id": h.get("faction_id"),
                }
                name = h.get("name", f"Alliance {alliance_id}")
                ticker = h.get("ticker")
                _alliance_cache[alliance_id] = name
                # Persist to the DB-backed name cache so subsequent panel
                # clicks skip the ESI round-trip entirely.
                try:
                    from sqlalchemy import select
                    from app.db.models import AsyncSessionLocal, AllianceNameCache
                    async with AsyncSessionLocal() as db:
                        existing = (await db.execute(
                            select(AllianceNameCache).where(
                                AllianceNameCache.alliance_id == alliance_id
                            )
                        )).scalar_one_or_none()
                        if existing:
                            existing.name = name
                            existing.ticker = ticker
                            existing.cached_at = datetime.now(timezone.utc)
                        else:
                            db.add(AllianceNameCache(
                                alliance_id=alliance_id, name=name, ticker=ticker,
                                cached_at=datetime.now(timezone.utc),
                            ))
                        await db.commit()
                except Exception:
                    pass
        except Exception:
            pass

    return JSONResponse({
        "alliance_id": alliance_id,
        **header,
        "sov_system_count": len(current_sys),
        "sov_system_ids": current_sys[:500],  # cap payload size
        "sov_gained_7d": gained_7d,
        "sov_lost_7d": lost_7d,
        "recent_changes": [
            {
                "system_id": r.system_id,
                "changed_at": r.changed_at.isoformat(),
                "old_alliance_id": r.old_alliance_id,
                "new_alliance_id": r.new_alliance_id,
                "direction": "gain" if r.new_alliance_id == alliance_id else "loss",
            }
            for r in rows[:100]
        ],
    })


@router.get("/alliance/{alliance_id}", response_class=HTMLResponse)
async def alliance_page(alliance_id: int, request: Request):
    """HTML page — alliance profile with sov history."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse("alliance_detail.html", {
        "request": request,
        "alliance_id": alliance_id,
    })
