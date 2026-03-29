"""Gate Check — Route safety checker, gatecamp finder, and war target intel.

Checks kill activity along your route using zKillboard data and ESI route planning.
All features included: detailed kill intel, gatecamp finder, war target tracking.
"""

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsyncSessionLocal, get_db
from app.esi.client import ESIClient, get_http_client
from app.sde import lookup as sde

router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger(__name__)

# ── zKillboard client with caching ──────────────────────────────────────────

ZKB_BASE = "https://zkillboard.com/api"
ZKB_HEADERS = {
    "Accept-Encoding": "gzip",
    "User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)",
    "Accept": "application/json",
}

_zkb_cache: dict[str, tuple[list, float]] = {}
_ZKB_CACHE_TTL = 300  # 5 minutes
_zkb_sem = asyncio.Semaphore(5)


async def _zkb_get(path: str) -> list:
    """Fetch from zKillboard API with 5-minute cache and rate limiting."""
    url = f"{ZKB_BASE}{path}"
    now = time.time()
    cached = _zkb_cache.get(url)
    if cached and cached[1] > now:
        return cached[0]

    async with _zkb_sem:
        # Re-check after acquiring semaphore
        cached = _zkb_cache.get(url)
        if cached and cached[1] > now:
            return cached[0]

        client = get_http_client()
        try:
            resp = await client.get(url, headers=ZKB_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    data = []
                _zkb_cache[url] = (data, now + _ZKB_CACHE_TTL)
                # Lazy cache cleanup
                if len(_zkb_cache) > 500:
                    expired = [k for k, v in _zkb_cache.items() if v[1] < now]
                    for k in expired:
                        del _zkb_cache[k]
                return data
            if resp.status_code == 429:
                await asyncio.sleep(2)
            return []
        except Exception as e:
            log.warning("zKB %s: %s", path, e)
            return []


# ── Ship group constants for threat classification ──────────────────────────

DICTOR_GROUPS = {541}   # Interdictor
HIC_GROUPS = {894}      # Heavy Interdictor


def _sec_color(sec: float) -> str:
    """CSS color for security status value."""
    if sec >= 0.9:
        return "#33aa55"
    if sec >= 0.7:
        return "#55cc55"
    if sec >= 0.5:
        return "#88bb44"
    if sec >= 0.3:
        return "#cc8844"
    if sec >= 0.1:
        return "#cc5533"
    return "#cc3333"


def _format_isk(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def _time_ago(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h {mins % 60}m ago"
    except Exception:
        return "?"


# ── Kill analysis ────────────────────────────────────────────────────────────

def _analyze_kills(
    kills: list,
    type_names: dict[int, str],
    group_ids: dict[int, int | None],
) -> dict:
    """Analyze a list of killmails for threat indicators."""
    if not kills:
        return {
            "kill_count": 0, "pvp_kills": 0, "threat": "safe",
            "has_smartbombs": False, "has_dictors": False, "has_hics": False,
            "total_value": 0, "total_value_str": "0", "kills": [],
        }

    has_sb = has_dic = has_hic = False
    total_val = 0
    analyzed = []

    for km in kills:
        victim = km.get("victim", {})
        attackers = km.get("attackers", [])
        zkb = km.get("zkb", {})
        is_npc = zkb.get("npc", False)

        v_ship_id = victim.get("ship_type_id", 0)
        v_ship = type_names.get(v_ship_id, f"Unknown ({v_ship_id})")
        kill_val = zkb.get("totalValue", 0)
        total_val += kill_val

        att_ships = Counter()
        att_weapons = Counter()

        for att in attackers:
            sid = att.get("ship_type_id", 0)
            wid = att.get("weapon_type_id", 0)
            gid = group_ids.get(sid)

            if sid:
                att_ships[type_names.get(sid, f"Unknown ({sid})")] += 1
                if gid in DICTOR_GROUPS:
                    has_dic = True
                if gid in HIC_GROUPS:
                    has_hic = True

            if wid:
                wname = type_names.get(wid, "")
                if wname:
                    att_weapons[wname] += 1
                    if "smartbomb" in wname.lower():
                        has_sb = True

        analyzed.append({
            "killmail_id": km.get("killmail_id"),
            "time_str": _time_ago(km.get("killmail_time", "")),
            "victim_ship": v_ship,
            "victim_ship_id": v_ship_id,
            "victim_char_id": victim.get("character_id"),
            "attacker_count": len(attackers),
            "attacker_ships": dict(att_ships.most_common(8)),
            "attacker_weapons": dict(att_weapons.most_common(8)),
            "value": kill_val,
            "value_str": _format_isk(kill_val),
            "is_npc": is_npc,
        })

    pvp = [k for k in analyzed if not k["is_npc"]]
    n = len(pvp)
    threat = "safe" if n == 0 else ("caution" if n <= 2 else "dangerous")
    if has_sb:
        threat = "smartbomb"

    return {
        "kill_count": len(kills),
        "pvp_kills": n,
        "threat": threat,
        "has_smartbombs": has_sb,
        "has_dictors": has_dic,
        "has_hics": has_hic,
        "total_value": total_val,
        "total_value_str": _format_isk(total_val),
        "kills": analyzed,
    }


# ── Bulk type resolution helper ─────────────────────────────────────────────

async def _resolve_type_ids(
    db: AsyncSession, kills_by_key: dict[int, list],
) -> tuple[dict[int, str], dict[int, int | None]]:
    """Collect all type IDs from grouped kills and resolve names + groups."""
    tids: set[int] = set()
    for kills in kills_by_key.values():
        for km in kills:
            v = km.get("victim", {})
            if v.get("ship_type_id"):
                tids.add(v["ship_type_id"])
            for a in km.get("attackers", []):
                if a.get("ship_type_id"):
                    tids.add(a["ship_type_id"])
                if a.get("weapon_type_id"):
                    tids.add(a["weapon_type_id"])
    tid_list = list(tids)
    names = await sde.type_ids_to_names(db, tid_list) if tid_list else {}
    groups = await sde.get_type_group_ids(db, tid_list) if tid_list else {}
    return names, groups


# ── Route checking ───────────────────────────────────────────────────────────

async def _check_route_systems(route: list[int], db: AsyncSession) -> list[dict]:
    """Fetch kills for each system on route and build enriched system list."""
    # Fetch system info from SDE
    sys_info: dict[int, dict] = {}
    for sid in route:
        info = await sde.system_info(db, sid)
        if info:
            sys_info[sid] = info

    # Fetch kills from zKillboard in parallel
    async def fetch(sid: int):
        return sid, await _zkb_get(f"/kills/systemID/{sid}/pastSeconds/3600/")

    results = await asyncio.gather(*[fetch(sid) for sid in route])
    kills_map: dict[int, list] = dict(results)

    # Resolve all type IDs in one batch
    type_names, group_ids = await _resolve_type_ids(db, kills_map)

    # Analyze each system
    out = []
    for i, sid in enumerate(route):
        info = sys_info.get(sid, {})
        sec = info.get("security", 0)
        analysis = _analyze_kills(kills_map.get(sid, []), type_names, group_ids)
        out.append({
            "waypoint": i + 1,
            "system_id": sid,
            "system_name": info.get("system_name", str(sid)),
            "security": sec,
            "sec_color": _sec_color(sec),
            "region": info.get("region", "?"),
            **analysis,
        })
    return out


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/intel/gatecheck", response_class=HTMLResponse)
async def gatecheck_page(request: Request):
    return templates.TemplateResponse("gatecheck.html", {"request": request})


@router.get("/intel/gatecheck/systems", response_class=JSONResponse)
async def system_autocomplete(q: str = Query(""), db: AsyncSession = Depends(get_db)):
    """System name autocomplete — returns JSON list."""
    if len(q) < 2:
        return []
    return await sde.search_systems(db, q, limit=8)


@router.post("/intel/gatecheck/check", response_class=HTMLResponse)
async def check_route(request: Request, db: AsyncSession = Depends(get_db)):
    """Check route for gate kills. Returns HTMX partial."""
    form = await request.form()
    origin = form.get("origin", "").strip()
    dest = form.get("destination", "").strip()
    flag = form.get("flag", "shortest")
    avoid_text = form.get("avoid", "").strip()

    if not origin or not dest:
        return HTMLResponse(_err("Enter both origin and destination systems."))

    # Resolve system names to IDs via SDE
    origin_id = await sde.system_name_to_id(db, origin)
    dest_id = await sde.system_name_to_id(db, dest)
    if not origin_id:
        return HTMLResponse(_err(f"Unknown system: {origin}"))
    if not dest_id:
        return HTMLResponse(_err(f"Unknown system: {dest}"))

    # Resolve avoid list
    avoid_ids = []
    if avoid_text:
        for line in avoid_text.splitlines():
            name = line.strip()
            if name:
                aid = await sde.system_name_to_id(db, name)
                if aid:
                    avoid_ids.append(aid)

    # Get route from ESI
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

    # Check each system for kills
    systems = await _check_route_systems(route, db)

    total_kills = sum(s["pvp_kills"] for s in systems)
    dangerous = sum(1 for s in systems if s["threat"] in ("dangerous", "smartbomb"))
    caution = sum(1 for s in systems if s["threat"] == "caution")

    return templates.TemplateResponse("partials/gatecheck_route.html", {
        "request": request,
        "systems": systems,
        "total_jumps": len(route),
        "total_kills": total_kills,
        "dangerous_count": dangerous,
        "caution_count": caution,
        "origin": origin,
        "destination": dest,
    })


@router.get("/intel/gatecheck/finder", response_class=HTMLResponse)
async def gatecamp_finder(request: Request, db: AsyncSession = Depends(get_db)):
    """Find active gatecamps across EVE. Returns HTMX partial."""
    # Fetch recent kills (2 pages = up to 400 kills)
    page1, page2 = await asyncio.gather(
        _zkb_get("/kills/pastSeconds/3600/"),
        _zkb_get("/kills/pastSeconds/3600/page/2/"),
    )
    all_kills = page1 + page2

    # Group non-NPC kills by system
    by_sys: dict[int, list] = defaultdict(list)
    for km in all_kills:
        if km.get("zkb", {}).get("npc"):
            continue
        sid = km.get("solar_system_id")
        if sid:
            by_sys[sid].append(km)

    # Systems with 3+ PvP kills = potential camps
    camp_systems = {sid: kms for sid, kms in by_sys.items() if len(kms) >= 3}

    if not camp_systems:
        return templates.TemplateResponse("partials/gatecheck_finder.html", {
            "request": request, "camps": [],
        })

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
    """Search for war target activity in past 3 hours. Returns HTMX partial."""
    form = await request.form()
    name = form.get("entity_name", "").strip()
    hint = form.get("entity_type", "auto")

    if not name:
        return HTMLResponse(_err("Enter a character, corporation, or alliance name."))

    # Resolve name via ESI
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

    all_kms = kills_data + losses_data
    by_sys: dict[int, list] = defaultdict(list)
    for km in all_kms:
        sid = km.get("solar_system_id")
        if sid:
            by_sys[sid].append(km)

    type_names, group_ids = await _resolve_type_ids(db, by_sys)

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
