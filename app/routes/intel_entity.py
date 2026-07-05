"""Intel → Entity combat stats (Phase 5 Task 2).

Killboard-grade stats for ANY character / corporation / alliance, computed
from the LOCAL killmail archive (see `app/intel/entity_stats.py` for the query
design + the CARDINAL RULE that every query carries a `killmail_time >=`
window bound).

Surfaces, all auth-gated:

  * `/intel/entity/{kind}/{id}`          — page shell; sections load via htmx.
  * `/intel/entity/{kind}/{id}/summary`  — kills/losses/danger/solo tiles.
  * `/intel/entity/{kind}/{id}/heatmap`  — hour-of-day × day-of-week CSS grid.
  * `/intel/entity/{kind}/{id}/ships`    — top 5 ships used.
  * `/intel/entity/{kind}/{id}/systems`  — top 5 systems.

`kind` is validated against `entity_stats.VALID_KINDS`; anything else 404s.
`window` (days) is validated against `VALID_WINDOWS`, defaulting to 90 — a
parameterized page, so it is NOT in the nav registry (the dead-link test only
checks registry URLs).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
from app.intel import entity_stats
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import system_ids_to_names, type_ids_to_names

router = APIRouter(tags=["intel-entity"])
# MUST be named `templates` — main.py's sys.modules loop pushes nav globals onto
# every Jinja2Templates instance named `templates` under app.routes.*.
templates = Jinja2Templates(directory="app/templates")

_KIND_LABEL = {"character": "Character", "corporation": "Corporation", "alliance": "Alliance"}


def _validate_kind(kind: str) -> None:
    if kind not in entity_stats.VALID_KINDS:
        raise HTTPException(status_code=404, detail="unknown entity kind")


def _window(window: int | None) -> int:
    return window if window in entity_stats.VALID_WINDOWS else entity_stats.DEFAULT_WINDOW


async def _entity_name(kind: str, entity_id: int) -> str:
    """Resolve the entity's display name (ESI universe/names covers all three
    kinds). Falls back to the numeric id if ESI is unreachable."""
    names = await resolve_entity_names([entity_id])
    return names.get(entity_id) or str(entity_id)


@router.get("/intel/entity/{kind}/{entity_id}", response_class=HTMLResponse)
async def entity_page(request: Request, kind: str, entity_id: int):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    _validate_kind(kind)
    name = await _entity_name(kind, entity_id)
    return templates.TemplateResponse(
        request,
        "intel_entity.html",
        {
            "kind": kind,
            "kind_label": _KIND_LABEL[kind],
            "entity_id": entity_id,
            "entity_name": name,
            "windows": entity_stats.VALID_WINDOWS,
            "default_window": entity_stats.DEFAULT_WINDOW,
        },
    )


@router.get("/intel/entity/{kind}/{entity_id}/summary", response_class=HTMLResponse)
async def entity_summary_panel(
    request: Request, kind: str, entity_id: int, window: int | None = None
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    _validate_kind(kind)
    days = _window(window)
    stats = await entity_stats.entity_summary(kind, entity_id, days)
    return templates.TemplateResponse(
        request,
        "partials/entity_summary.html",
        {"stats": stats, "days": days},
    )


@router.get("/intel/entity/{kind}/{entity_id}/heatmap", response_class=HTMLResponse)
async def entity_heatmap_panel(
    request: Request, kind: str, entity_id: int, window: int | None = None
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    _validate_kind(kind)
    days = _window(window)
    cells = await entity_stats.entity_heatmap(kind, entity_id, days)
    grid = {f"{c['dow']}-{c['hour']}": c["count"] for c in cells}
    peak = max((c["count"] for c in cells), default=0)
    return templates.TemplateResponse(
        request,
        "partials/entity_heatmap.html",
        {
            "grid": grid,
            "peak": peak,
            "days": days,
            # dow: 0=Sunday .. 6=Saturday (SQLite strftime('%w'))
            "dow_labels": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
            "hours": list(range(24)),
        },
    )


@router.get("/intel/entity/{kind}/{entity_id}/ships", response_class=HTMLResponse)
async def entity_ships_panel(
    request: Request, kind: str, entity_id: int, window: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    _validate_kind(kind)
    days = _window(window)
    rows = await entity_stats.entity_top_ships(kind, entity_id, days, limit=5)
    names = await type_ids_to_names(db, [r["ship_type_id"] for r in rows])
    items = [
        {"ship_type_id": r["ship_type_id"],
         "name": names.get(r["ship_type_id"], str(r["ship_type_id"])),
         "count": r["count"]}
        for r in rows
    ]
    return templates.TemplateResponse(
        request, "partials/entity_ships.html", {"items": items, "days": days},
    )


@router.get("/intel/entity/{kind}/{entity_id}/systems", response_class=HTMLResponse)
async def entity_systems_panel(
    request: Request, kind: str, entity_id: int, window: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    _validate_kind(kind)
    days = _window(window)
    rows = await entity_stats.entity_top_systems(kind, entity_id, days, limit=5)
    names = await system_ids_to_names(db, [r["system_id"] for r in rows])
    items = [
        {"system_id": r["system_id"],
         "name": names.get(r["system_id"], str(r["system_id"])),
         "count": r["count"]}
        for r in rows
    ]
    return templates.TemplateResponse(
        request, "partials/entity_systems.html", {"items": items, "days": days},
    )
