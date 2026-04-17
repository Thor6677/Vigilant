"""Wormhole Reference — System database, connection matrix, effects reference.

Provides searchable/filterable J-space system database, wormhole type lookup,
system detail pages with celestials and zKillboard activity, and system effects.
"""
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
from app.intel.safety import zkb_get

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
    wh_class: int | None = Query(None),
    effect: str | None = Query(None),
    static: str | None = Query(None),
    page: int = Query(1),
    db: AsyncSession = Depends(get_db),
):
    per_page = 50
    offset = (page - 1) * per_page
    systems, total = await sde.get_wormhole_systems(
        db,
        class_filter=wh_class if wh_class and wh_class > 0 else None,
        effect_filter=effect if effect else None,
        static_filter=static if static else None,
        search=q if q else None,
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
    if wh_class:
        class_key = f"c{wh_class}" if wh_class <= 6 else {7: "hs", 8: "ls", 9: "ns"}.get(wh_class, "")
        matrix = _wh_data.get("connection_matrix", {})
        # Collect all wormhole types that can appear FROM other classes TO this class
        for from_class, destinations in matrix.items():
            for dest_class, codes in destinations.items():
                if dest_class == class_key:
                    for code in codes:
                        meta = _wh_data.get("wormhole_meta", {}).get(code, {})
                        if meta.get("respawn") != "static" or from_class == "?":
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

    # Fetch kills from zKillboard (last 7 days)
    try:
        kills_data = await zkb_get(f"/kills/systemID/{system_id}/pastSeconds/604800/")
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

    # Build activity heatmap (day-of-week × hour)
    heatmap = [[0] * 24 for _ in range(7)]  # 7 days × 24 hours
    corp_counter: Counter = Counter()
    alliance_counter: Counter = Counter()
    most_recent = None

    for km in kills_data:
        zkb = km.get("zkb", {})
        kill_time_str = km.get("killmail_time", "")
        if kill_time_str:
            try:
                kill_time = datetime.fromisoformat(kill_time_str.replace("Z", "+00:00"))
                heatmap[kill_time.weekday()][kill_time.hour] += 1
                if most_recent is None or kill_time > most_recent:
                    most_recent = kill_time
            except (ValueError, TypeError):
                pass

    # Find most_recent from zkb data if not from killmail_time
    if most_recent is None and kills_data:
        # Try to use killID ordering as a proxy
        pass

    # Resolve corp/alliance names from kill data (simplified — use zkb data)
    # For a full implementation we'd fetch killmail details, but for now
    # just report counts
    for km in kills_data:
        zkb = km.get("zkb", {})
        # zKillboard basic data doesn't include corp names directly
        # We'll show the count and timestamp info instead

    # Flatten heatmap for template
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
    """Detail page/partial for a specific wormhole type."""
    wh_type = await sde.get_wormhole_type_by_name(db, code)
    meta = _wh_data.get("wormhole_meta", {}).get(code, {})

    return templates.TemplateResponse("partials/wormhole_type_detail.html", {
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
    })


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
