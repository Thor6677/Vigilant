"""Market → price-history browser + per-type charts (Phase 4 Task 1).

Three surfaces, all auth-gated:

  * `/market`                          — type search browser (htmx live search).
  * `/market/type/{type_id}`           — chart page for one type in The Forge.
  * `/market/type/{type_id}/history.json` — JSON feed for the chart, range-sliced.

History rows are fetched on demand and cached via `app.market.history` — see
that module for the storage design. The search buckets reuse the palette's
published-SDEType LIKE idiom (small table, small LIMIT, no new indexes).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketHistory, get_db
from app.db.sde_models import SDEGroup, SDEType
from app.market.history import DEFAULT_REGION_ID, get_history

router = APIRouter(tags=["market"])
# MUST be named `templates` — main.py's sys.modules loop pushes the nav globals
# (nav_groups / css_v / …) onto every Jinja2Templates instance found under
# app.routes.*. Rename it and these pages render with no nav chrome.
templates = Jinja2Templates(directory="app/templates")

SEARCH_CAP = 25

# Range toggle → lookback days. `all` means no lower bound.
_RANGES = {"30d": 30, "90d": 90, "1y": 365, "all": None}


async def _search_types(db: AsyncSession, q: str, cap: int = SEARCH_CAP) -> list[dict]:
    """Published SDE types matching `q`; prefix matches sort ahead of substrings.

    Mirrors palette._bucket_items but links to the market type page instead of
    the manufacturing calculator."""
    prefix = f"{q}%"
    sub = f"%{q}%"
    is_prefix = case((SDEType.type_name.ilike(prefix), 0), else_=1)
    rows = (await db.execute(
        select(SDEType.type_id, SDEType.type_name, SDEGroup.group_name)
        .outerjoin(SDEGroup, SDEGroup.group_id == SDEType.group_id)
        .where(SDEType.published.is_(True), SDEType.type_name.ilike(sub))
        .order_by(is_prefix, func.length(SDEType.type_name), SDEType.type_name)
        .limit(cap)
    )).all()
    return [
        {"type_id": tid, "type_name": name, "group": group or "Item"}
        for tid, name, group in rows
    ]


@router.get("/market", response_class=HTMLResponse)
async def market_browser(request: Request, db: AsyncSession = Depends(get_db)):
    """Type-search landing page for market history."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "market.html", {})


@router.get("/market/search", response_class=HTMLResponse)
async def market_search(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    """htmx partial: type results for the browser search box."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    q = (q or "").strip()[:64]
    results = await _search_types(db, q) if len(q) >= 2 else []
    return templates.TemplateResponse(
        request, "partials/market_search_results.html", {"q": q, "results": results}
    )


@router.get("/market/type/{type_id}", response_class=HTMLResponse)
async def market_type(request: Request, type_id: int, db: AsyncSession = Depends(get_db)):
    """Per-type price-history chart page (The Forge)."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")

    row = (await db.execute(
        select(SDEType.type_name, SDEGroup.group_name)
        .outerjoin(SDEGroup, SDEGroup.group_id == SDEType.group_id)
        .where(SDEType.type_id == type_id)
    )).first()
    if row is None:
        return templates.TemplateResponse(
            request, "market_type.html",
            {"type_id": type_id, "type_name": None, "group_name": None, "not_found": True},
            status_code=404,
        )

    type_name, group_name = row
    return templates.TemplateResponse(
        request, "market_type.html",
        {
            "type_id": type_id,
            "type_name": type_name,
            "group_name": group_name or "Item",
            "region_id": DEFAULT_REGION_ID,
            "not_found": False,
        },
    )


@router.get("/market/type/{type_id}/history.json")
async def market_type_history(
    request: Request, type_id: int, range: str = "1y",
    db: AsyncSession = Depends(get_db),
):
    """JSON feed for the chart. Ensures the (region, type) history is cached,
    then returns rows sliced to the requested range IN SQL."""
    if not request.session.get("user_id"):
        return JSONResponse({"error": "auth"}, status_code=401)

    region_id = DEFAULT_REGION_ID
    days = _RANGES.get(range, 365)

    # Populate/refresh the cache (cache-first; one ESI fetch at most per 24h).
    await get_history(region_id, type_id, db)

    stmt = (
        select(
            MarketHistory.date, MarketHistory.average,
            MarketHistory.highest, MarketHistory.lowest, MarketHistory.volume,
        )
        .where(MarketHistory.region_id == region_id, MarketHistory.type_id == type_id)
        .order_by(MarketHistory.date)
    )
    if days is not None:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days))
        stmt = stmt.where(MarketHistory.date >= cutoff)

    rows = (await db.execute(stmt)).all()

    payload = {
        "type_id": type_id,
        "region_id": region_id,
        "range": range if range in _RANGES else "1y",
        "dates": [d.isoformat() for d, _a, _h, _l, _v in rows],
        "average": [a for _d, a, _h, _l, _v in rows],
        "highest": [h for _d, _a, h, _l, _v in rows],
        "lowest": [l for _d, _a, _h, l, _v in rows],
        "volume": [v for _d, _a, _h, _l, v in rows],
    }
    return JSONResponse(payload)
