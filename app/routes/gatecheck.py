"""Gate Check — Route safety checker, gatecamp finder, and war target intel.

Checks kill activity at stargates along your route using zKillboard + ESI data.
All features included: detailed kill intel, gatecamp finder, war target tracking.

The shared zKillboard + kill-analysis code lives in app/intel/safety.py and is
also consumed by /api/map/route-safety. This file owns the HTML routes and
the gatecamp-finder / war-target features that aren't shared.
"""

import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal, get_db, Character, CharacterDashboardCache
from app.esi.client import ESIClient, get_http_client
from app.sde import lookup as sde
from app.intel.safety import (
    zkb_get,
    get_system_gates,
    fetch_killmail,
    is_gate_location,
    DICTOR_GROUPS,
    HIC_GROUPS,
    sec_color,
    format_isk,
    time_ago,
    analyze_kills,
    resolve_type_ids,
    enrich_kills,
    check_route_systems,
)

router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger(__name__)

# Backward-compatible aliases for any in-file references that still use the
# original underscored names. Most of the file body uses these — keeping the
# aliases avoids touching every call site.
_zkb_get = zkb_get
_get_system_gates = get_system_gates
_fetch_killmail = fetch_killmail
_is_gate_location = is_gate_location
_sec_color = sec_color
_format_isk = format_isk
_time_ago = time_ago
_analyze_kills = analyze_kills
_resolve_type_ids = resolve_type_ids
_enrich_kills = enrich_kills
_check_route_systems = check_route_systems


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/intel/gatecheck", response_class=HTMLResponse)
async def gatecheck_page(request: Request, db: AsyncSession = Depends(get_db)):
    # Load logged-in user's characters with cached locations
    char_locations = []
    user_id = request.session.get("user_id")
    if user_id:
        result = await db.execute(select(Character).where(Character.user_id == user_id))
        characters = result.scalars().all()
        cids = [c.character_id for c in characters]
        if cids:
            cache_result = await db.execute(
                select(CharacterDashboardCache).where(
                    CharacterDashboardCache.character_id.in_(cids)
                )
            )
            caches = {c.character_id: c for c in cache_result.scalars().all()}
            for char in characters:
                cache = caches.get(char.character_id)
                loc = None
                if cache and cache.location_json:
                    try:
                        loc = json.loads(cache.location_json)
                    except Exception:
                        pass
                if loc and loc.get("system_name"):
                    char_locations.append({
                        "character_name": char.character_name,
                        "character_id": char.character_id,
                        "system_name": loc["system_name"],
                    })

    return templates.TemplateResponse("gatecheck.html", {
        "request": request,
        "char_locations": char_locations,
    })


@router.get("/intel/gatecheck/systems", response_class=JSONResponse)
async def system_autocomplete(q: str = Query(""), db: AsyncSession = Depends(get_db)):
    if len(q) < 2:
        return []
    return await sde.search_systems(db, q, limit=8)


@router.post("/intel/gatecheck/check", response_class=HTMLResponse)
async def check_route(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    origin = form.get("origin", "").strip()
    dest = form.get("destination", "").strip()
    flag = form.get("flag", "shortest")
    avoid_text = form.get("avoid", "").strip()

    if not origin or not dest:
        return HTMLResponse(_err("Enter both origin and destination systems."))

    origin_id = await sde.system_name_to_id(db, origin)
    dest_id = await sde.system_name_to_id(db, dest)
    if not origin_id:
        return HTMLResponse(_err(f"Unknown system: {origin}"))
    if not dest_id:
        return HTMLResponse(_err(f"Unknown system: {dest}"))

    avoid_ids = []
    if avoid_text:
        for line in avoid_text.splitlines():
            name = line.strip()
            if name:
                aid = await sde.system_name_to_id(db, name)
                if aid:
                    avoid_ids.append(aid)

    async with AsyncSessionLocal() as esi_db:
        client = ESIClient("", db=esi_db)
        params: dict = {"flag": flag}
        if avoid_ids:
            params["avoid"] = avoid_ids
        try:
            route = await client.get_public(
                f"/route/{origin_id}/{dest_id}/", params=params,
            )
            if not isinstance(route, list) or not route:
                return HTMLResponse(_err(
                    "No route found. Systems may be unreachable or try different route settings."
                ))
        except Exception:
            return HTMLResponse(_err("Route calculation failed. Try again."))

    systems = await _check_route_systems(route, db)

    total_kills = sum(s["pvp_kills"] for s in systems)
    dangerous = sum(1 for s in systems if s["threat"] in ("dangerous", "smartbomb"))
    caution = sum(1 for s in systems if s["threat"] == "caution")

    # Map ESI flag (shortest/secure/insecure) to the star map's preference vocab
    # so the "Open in Star Map" link pre-selects the equivalent preference.
    starmap_pref = {
        "secure": "highsec",
        "insecure": "lowsec",
    }.get(flag, "shortest")

    return templates.TemplateResponse("partials/gatecheck_route.html", {
        "request": request,
        "systems": systems,
        "total_jumps": len(route),
        "total_kills": total_kills,
        "dangerous_count": dangerous,
        "caution_count": caution,
        "origin": origin,
        "destination": dest,
        "avoid_ids": avoid_ids,
        "starmap_pref": starmap_pref,
    })


@router.get("/intel/gatecheck/finder", response_class=HTMLResponse)
async def gatecamp_finder(request: Request, db: AsyncSession = Depends(get_db)):
    """Find active gatecamps across EVE — now backed by the killmail.stream
    rolling 1h buffer (app/intel/killmail_stream.py::get_recent_kills) instead
    of per-request zKB polls. No external API call at page-load, and coverage
    is continuous rather than capped at zKB's 2-page window."""
    from app.intel.killmail_stream import get_recent_kills

    recent = get_recent_kills(window_seconds=3600)

    # Gate kills only (non-NPC, location_id in stargate range)
    gate_kills = []
    for km in recent:
        zkb = km.get("zkb", {})
        if zkb.get("npc"):
            continue
        loc_id = zkb.get("locationID") or 0
        if _is_gate_location(loc_id):
            gate_kills.append(km)

    # Group by system; 3+ gate kills = potential camp
    by_sys: dict[int, list] = defaultdict(list)
    for km in gate_kills:
        sid = km.get("solar_system_id")
        if sid:
            by_sys[sid].append(km)
    camp_systems = {sid: kms for sid, kms in by_sys.items() if len(kms) >= 3}

    if not camp_systems:
        return templates.TemplateResponse("partials/gatecheck_finder.html", {
            "request": request, "camps": [],
        })

    # Stream buffer already has full ESI shape (victim + attackers + zkb),
    # so no ESI hydration step needed.
    type_names, group_ids = await _resolve_type_ids(db, camp_systems)

    camps = []
    for sid, kms in sorted(camp_systems.items(), key=lambda x: -len(x[1])):
        info = await sde.system_info(db, sid)
        if not info:
            continue
        sec = info.get("security", 0)
        analysis = _analyze_kills(kms, type_names, group_ids)
        camps.append({
            "system_id": sid,
            "system_name": info.get("system_name", str(sid)),
            "security": sec,
            "sec_color": _sec_color(sec),
            "region": info.get("region", "?"),
            **analysis,
        })

    return templates.TemplateResponse("partials/gatecheck_finder.html", {
        "request": request, "camps": camps,
    })


@router.post("/intel/gatecheck/wartargets", response_class=HTMLResponse)
async def war_targets(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = form.get("entity_name", "").strip()
    hint = form.get("entity_type", "auto")

    if not name:
        return HTMLResponse(_err("Enter a character, corporation, or alliance name."))

    client = ESIClient("", db=db)
    try:
        result = await client.post_public("/universe/ids/", [name])
    except Exception:
        return HTMLResponse(_err("Name resolution failed. Try again."))

    entity_id = None
    entity_type = None
    resolved = name

    if isinstance(result, dict):
        for etype, key, label in [
            ("characterID", "characters", "character"),
            ("corporationID", "corporations", "corporation"),
            ("allianceID", "alliances", "alliance"),
        ]:
            if hint not in ("auto", label):
                continue
            items = result.get(key, [])
            if items:
                entity_id = items[0]["id"]
                entity_type = etype
                resolved = items[0]["name"]
                break

    if not entity_id:
        return HTMLResponse(_err(f"Could not find: {name}"))

    # Fetch kills + losses in past 3 hours
    kills_data, losses_data = await asyncio.gather(
        _zkb_get(f"/kills/{entity_type}/{entity_id}/pastSeconds/10800/"),
        _zkb_get(f"/losses/{entity_type}/{entity_id}/pastSeconds/10800/"),
    )

    # Fetch full killmail details for all activity
    all_zkb = kills_data + losses_data
    full_kms = await _enrich_kills(all_zkb, max_per_call=30)

    by_sys: dict[int, list] = defaultdict(list)
    for km in full_kms:
        sid = km.get("solar_system_id")
        if sid:
            by_sys[sid].append(km)

    type_names, group_ids = await _resolve_type_ids(db, dict(by_sys))

    systems = []
    for sid, kms in sorted(by_sys.items(), key=lambda x: -len(x[1])):
        info = await sde.system_info(db, sid)
        analysis = _analyze_kills(kms, type_names, group_ids)
        systems.append({
            "system_id": sid,
            "system_name": info.get("system_name", str(sid)) if info else str(sid),
            "security": info.get("security", 0) if info else 0,
            "sec_color": _sec_color(info.get("security", 0) if info else 0),
            "region": info.get("region", "?") if info else "?",
            **analysis,
        })

    return templates.TemplateResponse("partials/gatecheck_wartarget.html", {
        "request": request,
        "entity_name": resolved,
        "entity_type": entity_type.replace("ID", ""),
        "total_kills": len(kills_data),
        "total_losses": len(losses_data),
        "systems": systems,
    })


def _err(msg: str) -> str:
    return f'<div style="padding:0.75rem;color:var(--danger);font-size:11px;">{msg}</div>'
