"""Gate Check — Route safety checker, gatecamp finder, and war target intel.

Checks kill activity at stargates along your route using zKillboard + ESI data.
All features included: detailed kill intel, gatecamp finder, war target tracking.
"""

import asyncio
import json
import logging
import time
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


# ── ESI helpers ─────────────────────────────────────────────────────────────

_esi_sem = asyncio.Semaphore(10)


async def _get_system_gates(system_id: int) -> set[int]:
    """Get stargate IDs for a system from ESI (cached permanently)."""
    async with AsyncSessionLocal() as db:
        client = ESIClient("", db=db)
        try:
            data = await client.get_public(f"/universe/systems/{system_id}/")
            if isinstance(data, dict):
                return set(data.get("stargates", []))
        except Exception:
            pass
    return set()


async def _fetch_killmail(killmail_id: int, km_hash: str) -> dict | None:
    """Fetch full killmail from ESI using the hash from zKillboard."""
    async with _esi_sem:
        async with AsyncSessionLocal() as db:
            client = ESIClient("", db=db)
            try:
                data = await client.get_public(
                    f"/killmails/{killmail_id}/{km_hash}/",
                )
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def _is_gate_location(location_id: int) -> bool:
    """Heuristic: stargate IDs are in the 50_000_000 range."""
    return 50_000_000 <= location_id <= 59_999_999


# ── Ship group constants for threat classification ──────────────────────────

DICTOR_GROUPS = {541}   # Interdictor
HIC_GROUPS = {894}      # Heavy Interdictor


def _sec_color(sec: float) -> str:
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
    """Analyze full killmails (ESI format + merged zkb) for threat indicators."""
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

        att_ships: dict[int, int] = {}  # type_id → count
        att_weapons = Counter()

        for att in attackers:
            sid = att.get("ship_type_id", 0)
            wid = att.get("weapon_type_id", 0)
            gid = group_ids.get(sid)

            if sid:
                att_ships[sid] = att_ships.get(sid, 0) + 1
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

        # Build attacker ships as list of dicts with type_id for linking
        top_att = sorted(att_ships.items(), key=lambda x: -x[1])[:8]
        att_ships_list = [
            {"name": type_names.get(tid, f"Unknown ({tid})"), "type_id": tid, "count": cnt}
            for tid, cnt in top_att
        ]

        analyzed.append({
            "killmail_id": km.get("killmail_id"),
            "time_str": _time_ago(km.get("killmail_time", "")),
            "victim_ship": v_ship,
            "victim_ship_id": v_ship_id,
            "victim_char_id": victim.get("character_id"),
            "attacker_count": len(attackers),
            "attacker_ships": att_ships_list,
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


# ── Bulk type resolution ────────────────────────────────────────────────────

async def _resolve_type_ids(
    db: AsyncSession, kills_by_key: dict,
) -> tuple[dict[int, str], dict[int, int | None]]:
    """Collect all type IDs from full killmails and resolve names + groups."""
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


# ── Fetch full killmail data for a set of zKB entries ────────────────────────

async def _enrich_kills(zkb_kills: list, max_per_call: int = 15) -> list[dict]:
    """Fetch full killmail data from ESI for a list of zKB entries.

    Returns merged killmails (ESI data + zkb section). Caps at max_per_call
    to avoid excessive API requests.
    """
    to_fetch = []
    for km in zkb_kills[:max_per_call]:
        km_id = km.get("killmail_id")
        km_hash = km.get("zkb", {}).get("hash", "")
        if km_id and km_hash:
            to_fetch.append((km_id, km_hash, km.get("zkb", {})))

    if not to_fetch:
        return []

    async def fetch_one(km_id: int, km_hash: str, zkb: dict):
        full = await _fetch_killmail(km_id, km_hash)
        if full:
            full["zkb"] = zkb
        return full

    results = await asyncio.gather(
        *[fetch_one(km_id, km_hash, zkb) for km_id, km_hash, zkb in to_fetch]
    )
    return [r for r in results if r]


# ── Route checking ───────────────────────────────────────────────────────────

async def _check_route_systems(route: list[int], db: AsyncSession) -> list[dict]:
    """For each system on route: get stargates, fetch zKB kills, filter to
    gate kills only, then fetch full killmail details from ESI."""

    # Step 1: Parallel fetch — zKB kills AND system stargates for every system
    async def fetch_system(sid: int):
        kills, gates = await asyncio.gather(
            _zkb_get(f"/kills/systemID/{sid}/pastSeconds/3600/"),
            _get_system_gates(sid),
        )
        return sid, kills, gates

    sys_results = await asyncio.gather(*[fetch_system(sid) for sid in route])

    # Step 2: Filter to gate kills (locationID matches a stargate in the system)
    gate_kills_by_sys: dict[int, list] = {}
    for sid, kills, gates in sys_results:
        gate_kills = []
        for km in kills:
            loc_id = km.get("zkb", {}).get("locationID", 0)
            if loc_id in gates:
                gate_kills.append(km)
        gate_kills_by_sys[sid] = gate_kills

    # Step 3: Fetch full killmail data from ESI for gate kills
    full_kms_by_sys: dict[int, list] = {}
    fetch_tasks = []
    for sid in route:
        gk = gate_kills_by_sys.get(sid, [])
        if gk:
            fetch_tasks.append((sid, _enrich_kills(gk, max_per_call=10)))
    if fetch_tasks:
        enriched = await asyncio.gather(*[t for _, t in fetch_tasks])
        for (sid, _), full_kms in zip(fetch_tasks, enriched):
            full_kms_by_sys[sid] = full_kms

    # Step 4: Resolve type IDs from full killmails
    type_names, group_ids = await _resolve_type_ids(db, full_kms_by_sys)

    # Step 5: Build output
    sys_info: dict[int, dict] = {}
    for sid in route:
        info = await sde.system_info(db, sid)
        if info:
            sys_info[sid] = info

    out = []
    for i, sid in enumerate(route):
        info = sys_info.get(sid, {})
        sec = info.get("security", 0)
        analysis = _analyze_kills(
            full_kms_by_sys.get(sid, []), type_names, group_ids,
        )
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
    """Find active gatecamps across EVE."""
    # Fetch recent kills (2 pages)
    page1, page2 = await asyncio.gather(
        _zkb_get("/kills/pastSeconds/3600/"),
        _zkb_get("/kills/pastSeconds/3600/page/2/"),
    )
    all_kills = page1 + page2

    # Pre-filter: keep only non-NPC kills at gate locations (heuristic)
    gate_kills = []
    for km in all_kills:
        zkb = km.get("zkb", {})
        if zkb.get("npc"):
            continue
        loc_id = zkb.get("locationID", 0)
        if _is_gate_location(loc_id):
            gate_kills.append(km)

    # Group by system
    by_sys: dict[int, list] = defaultdict(list)
    for km in gate_kills:
        sid = km.get("solar_system_id")
        if sid:
            by_sys[sid].append(km)

    # Systems with 3+ gate kills = potential camps
    camp_systems = {sid: kms for sid, kms in by_sys.items() if len(kms) >= 3}

    if not camp_systems:
        return templates.TemplateResponse("partials/gatecheck_finder.html", {
            "request": request, "camps": [],
        })

    # Fetch full killmail details for camps (top 5 camps, 10 kills each)
    full_kms_by_sys: dict[int, list] = {}
    top_camps = sorted(camp_systems.items(), key=lambda x: -len(x[1]))[:5]
    if top_camps:
        enriched = await asyncio.gather(
            *[_enrich_kills(kms, max_per_call=10) for _, kms in top_camps]
        )
        for (sid, _), full_kms in zip(top_camps, enriched):
            full_kms_by_sys[sid] = full_kms

    # For remaining camps, use basic zkb data only
    for sid, kms in camp_systems.items():
        if sid not in full_kms_by_sys:
            # Create minimal killmail stubs from zkb data
            full_kms_by_sys[sid] = [
                {"killmail_id": km.get("killmail_id"), "zkb": km.get("zkb", {}),
                 "victim": {}, "attackers": [],
                 "killmail_time": "", "solar_system_id": sid}
                for km in kms
            ]

    type_names, group_ids = await _resolve_type_ids(db, full_kms_by_sys)

    camps = []
    for sid, kms in sorted(camp_systems.items(), key=lambda x: -len(x[1])):
        info = await sde.system_info(db, sid)
        if not info:
            continue
        sec = info.get("security", 0)
        analysis = _analyze_kills(
            full_kms_by_sys.get(sid, []), type_names, group_ids,
        )
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
