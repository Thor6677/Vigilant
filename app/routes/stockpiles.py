"""Tools -> Stockpile watchlists (Phase 5 Task 3).

Surfaces, all auth-gated:

  * `GET  /tools/stockpiles`         — watchlist page: targets table (current vs
                                       target with deficit highlighted) + add form.
  * `GET  /tools/stockpiles/search`  — htmx partial: type-search results for the
                                       add form (variant of the market search
                                       idiom — rows populate the form instead of
                                       navigating away).
  * `POST /tools/stockpiles`         — htmx: add a target, returns the rows partial.
  * `DELETE /tools/stockpiles/{id}`  — htmx: remove a target, returns the rows partial.

Current holdings are summed from the synced asset caches across the user's
active characters (see `app/stockpiles/holdings.py` for the included/excluded
rationale). The daily/periodic `_background_scheduler` tick runs
`app/stockpiles/alerts.run_stockpile_check` to emit `stockpile_low` alerts.

CSRF: the POST/DELETE are state-mutating; htmx auto-attaches the `X-CSRF-Token`
header site-wide (base.html's `htmx:configRequest` wiring), so no per-form token
plumbing is needed here — an unauthenticated caller is stopped by the CSRF
middleware (403) before the handler's own `user_id` gate (401) is reached.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
from app.routes.market import _search_types  # reuse the published-SDEType search
from app.sde import lookup as sde
from app.stockpiles.holdings import (
    add_target,
    build_rows,
    delete_target,
    holdings_for_user,
    list_targets,
)

router = APIRouter(tags=["stockpiles"])
# MUST be named `templates` — main.py's sys.modules loop pushes the nav globals
# onto every Jinja2Templates instance named `templates` under app.routes.*.
templates = Jinja2Templates(directory="app/templates")

SEARCH_CAP = 25


async def _render_rows(db: AsyncSession, user_id: int) -> list[dict]:
    """Build the current watchlist rows (targets joined to summed holdings).

    Under-stocked rows sort to the top (largest deficit first) so the user sees
    what needs restocking without scanning; ties fall back to type name.
    """
    targets = await list_targets(db, user_id)
    holdings = await holdings_for_user(db, user_id)
    names = await sde.type_ids_to_names(db, [t.type_id for t in targets])
    rows = build_rows(targets, holdings, names)
    rows.sort(key=lambda r: (-r["deficit"], r["type_name"].lower()))
    return rows


@router.get("/tools/stockpiles", response_class=HTMLResponse)
async def stockpiles_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Stockpile watchlist landing page."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    rows = await _render_rows(db, user_id)
    return templates.TemplateResponse(
        request, "stockpiles.html", {"rows": rows}
    )


@router.get("/tools/stockpiles/search", response_class=HTMLResponse)
async def stockpiles_search(
    request: Request, q: str = "", db: AsyncSession = Depends(get_db)
):
    """htmx partial: type-search results for the add form."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    q = (q or "").strip()[:64]
    results = await _search_types(db, q, cap=SEARCH_CAP) if len(q) >= 2 else []
    return templates.TemplateResponse(
        request, "partials/stockpile_search_results.html",
        {"q": q, "results": results},
    )


@router.post("/tools/stockpiles", response_class=HTMLResponse)
async def stockpiles_add(
    request: Request,
    type_id: int = Form(...),
    target_qty: int = Form(...),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """htmx POST: add a target, return the refreshed rows partial."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    await add_target(db, user_id, type_id, target_qty, note)
    rows = await _render_rows(db, user_id)
    return templates.TemplateResponse(
        request, "partials/stockpile_rows.html", {"rows": rows}
    )


@router.delete("/tools/stockpiles/{target_id}", response_class=HTMLResponse)
async def stockpiles_delete(
    request: Request, target_id: int, db: AsyncSession = Depends(get_db)
):
    """htmx DELETE: remove a target, return the refreshed rows partial."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    await delete_target(db, user_id, target_id)
    rows = await _render_rows(db, user_id)
    return templates.TemplateResponse(
        request, "partials/stockpile_rows.html", {"rows": rows}
    )
