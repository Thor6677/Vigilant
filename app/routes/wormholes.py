"""Wormhole Reference — System database, connection matrix, effects reference.

Provides searchable/filterable J-space system database, wormhole type lookup,
system detail pages with celestials and zKillboard activity, and system effects.
"""
import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db, AsyncSessionLocal
from app.esi.client import ESIClient
from app.sde import lookup as sde
from app.intel.safety import zkb_get, fetch_killmail

router = APIRouter(tags=["wormholes"])
templates = Jinja2Templates(directory="app/templates")
log = logging.getLogger(__name__)

# Load community wormhole data once at import time
_WH_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "wormholes.json"
_wh_data: dict = {}
try:
    _wh_data = json.loads(_WH_DATA_PATH.read_text())
except Exception as e:
    log.warning("Failed to load wormholes.json: %s", e)


def _class_label(wh_class: int | None) -> str:
    if wh_class is None:
        return "?"
    return _wh_data.get("class_labels", {}).get(str(wh_class), f"C{wh_class}")


def _class_color(wh_class: int | None) -> str:
    if wh_class is None:
        return "var(--muted)"
    return _wh_data.get("class_colors", {}).get(str(wh_class), "var(--text)")


def _effect_label(effect: str | None) -> str:
    if not effect:
        return ""
    effects = _wh_data.get("effects", {})
    info = effects.get(effect)
    return info["name"] if info else effect.replace("_", " ").title()


def _format_mass(kg: float | None) -> str:
    if kg is None:
        return "—"
    if kg >= 1_000_000_000:
        return f"{kg / 1_000_000_000:,.1f}B kg"
    if kg >= 1_000_000:
        return f"{kg / 1_000_000:,.0f}M kg"
    return f"{kg:,.0f} kg"


def _format_time(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    hours = minutes / 60
    if hours == int(hours):
        return f"{int(hours)} hours"
    return f"{hours:.1f} hours"


def _ship_size_hint(max_jump_mass: float | None) -> str:
    if max_jump_mass is None:
        return ""
    m = max_jump_mass
    if m <= 5_000_000:
        return "frigate"
    if m <= 20_000_000:
        return "destroyer"
    if m <= 62_000_000:
        return "battlecruiser"
    if m <= 375_000_000:
        return "battleship"
    if m <= 1_000_000_000:
        return "capital"
    if m <= 1_800_000_000:
        return "capital"
    return "supercapital"


# ── System Database ─────────────────────────────────────────────────────────

EFFECT_COLORS = {
    "pulsar": "#5dffd2",
    "black_hole": "#4a9eda",
    "cataclysmic_variable": "#d4a06a",
    "magnetar": "#cc4433",
    "red_giant": "#e76363",
    "wolf_rayet": "#a8cb70",
}


@router.get("/wormholes", response_class=HTMLResponse)
async def wormhole_systems_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")
    return templates.TemplateResponse("wormholes.html", {
        "request": request,
        "effects_list": list(_wh_data.get("effects", {}).keys()),
        "effects_labels": {k: v["name"] for k, v in _wh_data.get("effects", {}).items()},
        "effect_colors": EFFECT_COLORS,
        "class_colors": _wh_data.get("class_colors", {}),
    })


@router.get("/wormholes/search", response_class=HTMLResponse)
async def wormhole_systems_search(
    request: Request,
    q: str = Query(""),
    wh_class: str = Query(""),
    effect: str = Query(""),
    static_dest: str = Query(""),
    planets: str = Query(""),
    perfect_pi: str = Query(""),
    page: int = Query(1),
    db: AsyncSession = Depends(get_db),
):
    per_page = 50
    offset = (page - 1) * per_page
    search_q = q.strip() if q else ""
    effect_val = effect.strip() if effect.strip() else None

    # Parse multi-select class filter (comma-separated)
    class_list = [int(c) for c in wh_class.split(",") if c.strip().isdigit()] if wh_class.strip() else []

    # Parse static destination filter (comma-separated class IDs)
    static_dest_list = [int(c) for c in static_dest.split(",") if c.strip().isdigit()] if static_dest.strip() else []

    # Parse planet type filter (comma-separated names)
    planet_list = [p.strip() for p in planets.split(",") if p.strip()] if planets.strip() else []

    perfect_pi_on = perfect_pi.strip() == "1"

    # Require at least one filter or 4+ char search
    has_filter = class_list or effect_val or static_dest_list or planet_list or perfect_pi_on
    if not has_filter and len(search_q) < 4:
        return HTMLResponse(
            '<div class="b-empty">Select filters above or type at least 4 characters to search.</div>'
        )

    systems, total = await sde.get_wormhole_systems(
        db,
        class_filter=class_list or None,
        effect_filter=effect_val,
        static_dest_filter=static_dest_list or None,
        planet_filter=planet_list or None,
        perfect_pi=perfect_pi_on,
        search=search_q if search_q else None,
        limit=per_page,
        offset=offset,
        wh_data=_wh_data,
    )

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return templates.TemplateResponse("partials/wormhole_system_list.html", {
        "request": request,
        "systems": systems,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "class_label": _class_label,
        "class_color": _class_color,
        "effect_label": _effect_label,
        "wh_data": _wh_data,
    })


# ── System Detail ───────────────────────────────────────────────────────────

@router.get("/wormholes/system/{name}", response_class=HTMLResponse)
async def wormhole_system_detail(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    sys_detail = await sde.get_wormhole_system_detail(db, name)
    if not sys_detail:
        return templates.TemplateResponse("wormholes.html", {
            "request": request,
            "error": f"System '{name}' not found.",
            "effects_list": list(_wh_data.get("effects", {}).keys()),
            "effects_labels": {k: v["name"] for k, v in _wh_data.get("effects", {}).items()},
        })

    celestials = await sde.get_system_celestials(db, sys_detail["system_id"])

    # Community data
    system_name = sys_detail["system_name"]
    statics = _wh_data.get("system_statics", {}).get(system_name, [])
    effect_key = _wh_data.get("system_effects", {}).get(system_name)
    effect_info = None
    if effect_key:
        effects_data = _wh_data.get("effects", {})
        effect_info = effects_data.get(effect_key)
        if effect_info:
            effect_info = dict(effect_info)
            effect_info["key"] = effect_key

    # Resolve statics to wormhole type info
    static_details = []
    for static_code in statics:
        wh_type = await sde.get_wormhole_type_by_name(db, static_code)
        meta = _wh_data.get("wormhole_meta", {}).get(static_code, {})
        static_details.append({
            "code": static_code,
            "target_class": wh_type["target_class"] if wh_type else None,
            "respawn": meta.get("respawn", "unknown"),
        })

    # Possible wandering connections for this class
    wh_class = sys_detail.get("wh_class")
    wandering = []
    seen_wandering: set[str] = set()
    if wh_class:
        class_key = f"c{wh_class}" if wh_class <= 6 else {7: "hs", 8: "ls", 9: "ns"}.get(wh_class, "")
        matrix = _wh_data.get("connection_matrix", {})
        # Collect all wormhole types that can appear FROM other classes TO this class
        for from_class, destinations in matrix.items():
            for dest_class, codes in destinations.items():
                if dest_class == class_key:
                    for code in codes:
                        if code in seen_wandering:
                            continue
                        meta = _wh_data.get("wormhole_meta", {}).get(code, {})
                        if meta.get("respawn") != "static" or from_class == "?":
                            seen_wandering.add(code)
                            wh_type = await sde.get_wormhole_type_by_name(db, code)
                            wandering.append({
                                "code": code,
                                "target_class": wh_type["target_class"] if wh_type else None,
                                "from_class": from_class,
                            })

    return templates.TemplateResponse("wormhole_system.html", {
        "request": request,
        "system": sys_detail,
        "celestials": celestials,
        "statics": static_details,
        "wandering": wandering,
        "effect": effect_info,
        "wh_class": wh_class,
        "class_label": _class_label,
        "class_color": _class_color,
        "effect_label": _effect_label,
        "wh_data": _wh_data,
    })


@router.get("/wormholes/system/{name}/kills", response_class=HTMLResponse)
async def wormhole_system_kills(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=30, le=90),
):
    """Lazy-loaded kill activity for a wormhole system."""
    sys_detail = await sde.get_wormhole_system_detail(db, name)
    if not sys_detail:
        return HTMLResponse('<div class="b-empty">System not found.</div>')

    system_id = sys_detail["system_id"]
    # Normalise to one of the supported ranges
    if days not in (30, 60, 90):
        days = 30

    # Fetch recent killmails from zKillboard (up to 200 most recent)
    try:
        kills_data = await zkb_get(f"/systemID/{system_id}/")
    except Exception:
        kills_data = []

    if not kills_data:
        return templates.TemplateResponse("partials/wormhole_kills.html", {
            "request": request,
            "kill_count": 0,
            "kills": [],
            "heatmap": [],
            "corps": [],
            "alliances": [],
            "most_recent": None,
            "days": days,
            "system_name": name,
        })

    # Fetch full killmail details from ESI (for timestamps + corp/alliance)
    sem = asyncio.Semaphore(5)

    # Build npc flag lookup from zkb data
    npc_flags: dict[int, bool] = {}
    for km in kills_data:
        kid = km.get("killmail_id")
        if kid:
            npc_flags[kid] = km.get("zkb", {}).get("npc", False)

    async def _fetch_km(km_stub):
        async with sem:
            kid = km_stub.get("killmail_id")
            khash = km_stub.get("zkb", {}).get("hash")
            if kid and khash:
                try:
                    return await fetch_killmail(kid, khash)
                except Exception:
                    pass
        return None

    fetch_limit = 100 if days > 30 else 50
    full_kms = await asyncio.gather(*[_fetch_km(km) for km in kills_data[:fetch_limit]])

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Age bucket boundaries (thirds of the range)
    third = days // 3
    cutoff_recent = now - timedelta(days=third)
    cutoff_mid = now - timedelta(days=third * 2)

    # Build activity heatmap (day-of-week × hour) with kill IDs per cell
    heatmap = [[0] * 24 for _ in range(7)]
    heatmap_ids: dict[str, list[int]] = {}
    heatmap_npc: dict[str, bool] = {}
    heatmap_age: dict[str, int] = {}
    most_recent = None
    recent_kills: list[dict] = []
    victim_type_ids: set[int] = set()
    kill_char_ids: set[int] = set()
    kill_corp_ids: set[int] = set()
    kill_alliance_ids: set[int] = set()

    # Corp/alliance aggregation (player orgs only)
    corp_kills: Counter = Counter()
    alliance_kills: Counter = Counter()

    for km in full_kms:
        if not km:
            continue
        kill_time_str = km.get("killmail_time", "")
        kid = km.get("killmail_id")
        if not kill_time_str:
            continue
        try:
            kill_time = datetime.fromisoformat(kill_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if kill_time < cutoff:
            continue

        d, h = kill_time.weekday(), kill_time.hour
        heatmap[d][h] += 1
        key = f"{d},{h}"

        if kill_time >= cutoff_recent:
            age = 0
        elif kill_time >= cutoff_mid:
            age = 1
        else:
            age = 2
        if key not in heatmap_age or age < heatmap_age[key]:
            heatmap_age[key] = age

        if kid:
            heatmap_ids.setdefault(key, []).append(kid)
            is_npc = npc_flags.get(kid, False)
            if key not in heatmap_npc:
                heatmap_npc[key] = is_npc
            elif not is_npc:
                heatmap_npc[key] = False

        if most_recent is None or kill_time > most_recent:
            most_recent = kill_time

        # Aggregate corp/alliance involvement (player orgs only)
        victim = km.get("victim", {})
        v_corp = victim.get("corporation_id")
        v_alliance = victim.get("alliance_id")
        if v_corp and v_corp >= 98_000_000:
            corp_kills[v_corp] += 1
        if v_alliance and v_alliance >= 99_000_000:
            alliance_kills[v_alliance] += 1
        for att in km.get("attackers", []):
            a_corp = att.get("corporation_id")
            a_alliance = att.get("alliance_id")
            if a_corp and a_corp >= 98_000_000:
                corp_kills[a_corp] += 1
            if a_alliance and a_alliance >= 99_000_000:
                alliance_kills[a_alliance] += 1

        # Build recent kills list (up to 15)
        if len(recent_kills) < 15:
            ship_tid = victim.get("ship_type_id")
            if ship_tid:
                victim_type_ids.add(ship_tid)
            zkb_stub = None
            for stub in kills_data:
                if stub.get("killmail_id") == kid:
                    zkb_stub = stub.get("zkb", {})
                    break
            delta_kill = now - kill_time
            if delta_kill.days >= 1:
                time_ago = f"{delta_kill.days}d ago"
            else:
                hrs = delta_kill.seconds // 3600
                time_ago = f"{hrs}h ago" if hrs > 0 else "just now"
            v_char_id = victim.get("character_id")
            v_corp_id = victim.get("corporation_id")
            v_ally_id = victim.get("alliance_id")
            if v_char_id:
                kill_char_ids.add(v_char_id)
            if v_corp_id:
                kill_corp_ids.add(v_corp_id)
            if v_ally_id:
                kill_alliance_ids.add(v_ally_id)
            recent_kills.append({
                "killmail_id": kid,
                "time": kill_time,
                "time_ago": time_ago,
                "ship_type_id": ship_tid,
                "ship_name": None,
                "character_id": v_char_id,
                "character_name": None,
                "corporation_id": v_corp_id,
                "corporation_name": None,
                "alliance_id": v_ally_id,
                "alliance_name": None,
                "value": zkb_stub.get("totalValue", 0) if zkb_stub else 0,
                "is_npc": npc_flags.get(kid, False),
            })

    # Resolve ship names
    if victim_type_ids:
        ship_names = await sde.type_ids_to_names(db, list(victim_type_ids))
        for kill in recent_kills:
            if kill["ship_type_id"]:
                kill["ship_name"] = ship_names.get(kill["ship_type_id"], f"Type {kill['ship_type_id']}")

    # Resolve names from ESI: top corps/alliances + kill list chars/corps/alliances
    top_corps: list[dict] = []
    top_alliances: list[dict] = []
    top_corp_ids = [cid for cid, _ in corp_kills.most_common(5)]
    top_alliance_ids = [aid for aid, _ in alliance_kills.most_common(5)]

    # Merge IDs: top active + kill list victims
    all_corp_ids = set(top_corp_ids) | kill_corp_ids
    all_alliance_ids = set(top_alliance_ids) | kill_alliance_ids

    name_sem = asyncio.Semaphore(5)
    char_names: dict[int, str] = {}
    corp_names: dict[int, str] = {}
    alliance_names: dict[int, str] = {}

    async def _resolve_char(cid: int):
        async with name_sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/characters/{cid}/")
                    if isinstance(data, dict):
                        char_names[cid] = data.get("name", f"Char {cid}")
                        return
                except Exception:
                    pass
                char_names[cid] = f"Char {cid}"

    async def _resolve_corp(cid: int):
        async with name_sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/corporations/{cid}/")
                    if isinstance(data, dict):
                        corp_names[cid] = data.get("name", f"Corp {cid}")
                        return
                except Exception:
                    pass
                corp_names[cid] = f"Corp {cid}"

    async def _resolve_alliance(aid: int):
        async with name_sem:
            async with AsyncSessionLocal() as sess:
                c = ESIClient("", db=sess)
                try:
                    data = await c.get_public(f"/alliances/{aid}/")
                    if isinstance(data, dict):
                        alliance_names[aid] = data.get("name", f"Alliance {aid}")
                        return
                except Exception:
                    pass
                alliance_names[aid] = f"Alliance {aid}"

    await asyncio.gather(
        *[_resolve_char(cid) for cid in kill_char_ids],
        *[_resolve_corp(cid) for cid in all_corp_ids],
        *[_resolve_alliance(aid) for aid in all_alliance_ids],
    )

    top_corps = [{"id": cid, "name": corp_names.get(cid, f"Corp {cid}"), "count": corp_kills[cid]} for cid in top_corp_ids]
    top_alliances = [{"id": aid, "name": alliance_names.get(aid, f"Alliance {aid}"), "count": alliance_kills[aid]} for aid in top_alliance_ids]

    # Backfill resolved names into recent kills
    for kill in recent_kills:
        if kill["character_id"]:
            kill["character_name"] = char_names.get(kill["character_id"])
        if kill["corporation_id"]:
            kill["corporation_name"] = corp_names.get(kill["corporation_id"])
        if kill["alliance_id"]:
            kill["alliance_name"] = alliance_names.get(kill["alliance_id"])

    filtered_count = sum(sum(row) for row in heatmap)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    max_kills = max(max(row) for row in heatmap) if filtered_count else 1

    days_ago = None
    if most_recent:
        delta = now - most_recent
        days_ago = round(delta.total_seconds() / 86400, 1)

    # Age bucket labels for the legend
    age_labels = [f"<{third}d", f"{third}-{third*2}d", f"{third*2}-{days}d"]

    return templates.TemplateResponse("partials/wormhole_kills.html", {
        "request": request,
        "kill_count": filtered_count,
        "heatmap": heatmap,
        "heatmap_ids": heatmap_ids,
        "heatmap_npc": heatmap_npc,
        "heatmap_age": heatmap_age,
        "recent_kills": recent_kills,
        "day_names": day_names,
        "max_kills": max_kills,
        "most_recent": most_recent,
        "days_ago": days_ago,
        "days": days,
        "age_labels": age_labels,
        "system_name": name,
        "top_corps": top_corps,
        "top_alliances": top_alliances,
    })


# ── Wormhole Types / Connection Matrix ──────────────────────────────────────

@router.get("/wormholes/types", response_class=HTMLResponse)
async def wormhole_types_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    # Load all wormhole types from SDE for the detail lookup
    all_types = await sde.get_all_wormhole_types(db)
    # Build name -> type dict for quick lookup (strip "Wormhole " prefix)
    type_lookup = {}
    for t in all_types:
        short = t["type_name"].replace("Wormhole ", "")
        type_lookup[short] = t

    return templates.TemplateResponse("wormhole_types.html", {
        "request": request,
        "matrix": _wh_data.get("connection_matrix", {}),
        "wh_meta": _wh_data.get("wormhole_meta", {}),
        "type_lookup": type_lookup,
        "class_label": _class_label,
        "class_color": _class_color,
        "format_mass": _format_mass,
        "format_time": _format_time,
        "ship_size_hint": _ship_size_hint,
        "wh_data": _wh_data,
    })


@router.get("/wormholes/types/{code}", response_class=HTMLResponse)
async def wormhole_type_detail(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Detail for a wormhole type. Returns partial for htmx, full page for direct nav."""
    wh_type = await sde.get_wormhole_type_by_name(db, code)
    meta = _wh_data.get("wormhole_meta", {}).get(code, {})

    ctx = {
        "request": request,
        "code": code,
        "wh_type": wh_type,
        "meta": meta,
        "class_label": _class_label,
        "class_color": _class_color,
        "format_mass": _format_mass,
        "format_time": _format_time,
        "ship_size_hint": _ship_size_hint,
        "wh_data": _wh_data,
    }

    # htmx request → return partial; direct navigation → full page
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/wormhole_type_detail.html", ctx)
    return templates.TemplateResponse("wormhole_type_page.html", ctx)


# ── System Effects Reference ────────────────────────────────────────────────

@router.get("/wormholes/effects", response_class=HTMLResponse)
async def wormhole_effects_page(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    return templates.TemplateResponse("wormhole_effects.html", {
        "request": request,
        "effects": _wh_data.get("effects", {}),
    })
