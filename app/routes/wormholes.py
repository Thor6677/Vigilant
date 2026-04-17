"""Wormhole Reference — System database, connection matrix, effects reference.

Provides searchable/filterable J-space system database, wormhole type lookup,
system detail pages with celestials and zKillboard activity, and system effects.
"""
import asyncio
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
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
    })


@router.get("/wormholes/search", response_class=HTMLResponse)
async def wormhole_systems_search(
    request: Request,
    q: str = Query(""),
    wh_class: str = Query(""),
    effect: str = Query(""),
    static: str = Query(""),
    page: int = Query(1),
    db: AsyncSession = Depends(get_db),
):
    per_page = 50
    offset = (page - 1) * per_page
    search_q = q.strip() if q else ""
    class_int = int(wh_class) if wh_class.strip().isdigit() else None
    effect_val = effect.strip() if effect.strip() else None

    # Require at least 4 chars in search OR a class/effect filter selected
    has_filter = (class_int and class_int > 0) or effect_val
    if not has_filter and len(search_q) < 4:
        return HTMLResponse(
            '<div class="b-empty">Type at least 4 characters (e.g. J114) to search, or select a class/effect filter.</div>'
        )

    systems, total = await sde.get_wormhole_systems(
        db,
        class_filter=class_int if class_int and class_int > 0 else None,
        effect_filter=effect_val,
        static_filter=static.strip() if static.strip() else None,
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
async def wormhole_system_kills(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Lazy-loaded kill activity for a wormhole system."""
    sys_detail = await sde.get_wormhole_system_detail(db, name)
    if not sys_detail:
        return HTMLResponse('<div class="b-empty">System not found.</div>')

    system_id = sys_detail["system_id"]

    # Fetch kills from zKillboard (last 30 days = 2592000 seconds)
    try:
        kills_data = await zkb_get(f"/kills/systemID/{system_id}/pastSeconds/2592000/")
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
        })

    # Fetch full killmail details from ESI (for timestamps)
    # Limit to 30 kills to avoid overloading ESI
    sem = asyncio.Semaphore(5)

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

    full_kms = await asyncio.gather(*[_fetch_km(km) for km in kills_data[:30]])

    # Build activity heatmap (day-of-week × hour) with kill IDs per cell
    heatmap = [[0] * 24 for _ in range(7)]  # 7 days × 24 hours
    heatmap_ids: dict[str, list[int]] = {}  # "d,h" -> [killmail_id, ...]
    most_recent = None

    for km in full_kms:
        if not km:
            continue
        kill_time_str = km.get("killmail_time", "")
        kid = km.get("killmail_id")
        if kill_time_str:
            try:
                kill_time = datetime.fromisoformat(kill_time_str.replace("Z", "+00:00"))
                d, h = kill_time.weekday(), kill_time.hour
                heatmap[d][h] += 1
                if kid:
                    heatmap_ids.setdefault(f"{d},{h}", []).append(kid)
                if most_recent is None or kill_time > most_recent:
                    most_recent = kill_time
            except (ValueError, TypeError):
                pass

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    max_kills = max(max(row) for row in heatmap) if kills_data else 1

    now = datetime.now(timezone.utc)
    days_ago = None
    if most_recent:
        delta = now - most_recent
        days_ago = round(delta.total_seconds() / 86400, 1)

    return templates.TemplateResponse("partials/wormhole_kills.html", {
        "request": request,
        "kill_count": len(kills_data),
        "heatmap": heatmap,
        "heatmap_ids": heatmap_ids,
        "day_names": day_names,
        "max_kills": max_kills,
        "most_recent": most_recent,
        "days_ago": days_ago,
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
