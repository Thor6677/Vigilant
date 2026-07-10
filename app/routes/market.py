"""Market → price-history browser + per-type charts (Phase 4 Task 1/2/3).

Surfaces, all auth-gated:

  * `/market`                          — type search browser (htmx live search).
  * `/market/type/{type_id}`           — chart page for one type in The Forge.
  * `/market/type/{type_id}/history.json` — JSON feed for the chart, range-sliced.
  * `/market/type/{type_id}/orders`    — htmx partial: hub order-book (Task 2).
  * `/market/lp`                       — LP store ROI calculator landing (Task 3).
  * `/market/lp/corps-tree`            — htmx partial: faction -> corp tree (Task 3).
  * `/market/lp/offers`                — htmx partial: ranked offers for one corp.

History rows are fetched on demand and cached via `app.market.history` — see
that module for the storage design. The order book is a separate, much
shorter-lived cache — see `app.market.orders`. LP store offers + the NPC corp
roster live in `app.market.lp` — see that module for the ISK/LP formula and
its guards. The search buckets reuse the palette's published-SDEType LIKE
idiom (small table, small LIMIT, no new indexes).
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
from app.market import lp as market_lp
from app.market.history import DEFAULT_REGION_ID, get_history
from app.market.orders import (
    STATION_ID_CEILING,
    build_order_book,
    get_orders,
    location_ids_in_book,
    location_name,
)
from app.sde import lookup as sde

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


def _fmt_price(v: float | None) -> str:
    """ISK price formatter for order-book rows/header stats. Order prices are
    per-unit (can be sub-1-ISK for minerals or hundreds of millions for
    capital modules), so we keep 2 decimals below 1B rather than always
    abbreviating — matches the precision players actually price orders at."""
    if v is None:
        return "—"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    return f"{v:,.2f}"


def _fmt_qty(v: int | None) -> str:
    if v is None:
        return "—"
    return f"{v:,}"


@router.get("/market/type/{type_id}/orders", response_class=HTMLResponse)
async def market_type_orders(request: Request, type_id: int, db: AsyncSession = Depends(get_db)):
    """htmx partial: hub order-book section (The Forge) for a type page.

    Lazy-loaded via `hx-trigger="load"` on `market_type.html` so the chart page
    itself never blocks on an ESI round trip. Auth-gated like every other
    partial in this router (empty body + 401, not a redirect — htmx swaps the
    body in place)."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)

    region_id = DEFAULT_REGION_ID
    raw_orders = await get_orders(region_id, type_id)
    book = build_order_book(raw_orders)

    # Resolve names only for the ≤30 rows actually displayed, not every order
    # in the region for this type.
    station_ids = [
        lid for lid in location_ids_in_book(book) if lid < STATION_ID_CEILING
    ]
    station_map = await sde.stations_by_ids(db, station_ids) if station_ids else {}
    station_names = {sid: info["station_name"] for sid, info in station_map.items()}

    def _display_row(row: dict) -> dict:
        return {
            "price_str": _fmt_price(row["price"]),
            "volume_str": _fmt_qty(row["volume_remain"]),
            "location_name": location_name(row["location_id"], station_names),
        }

    return templates.TemplateResponse(
        request, "partials/market_order_book.html",
        {
            "type_id": type_id,
            "sell_orders": [_display_row(r) for r in book["sell_orders"]],
            "buy_orders": [_display_row(r) for r in book["buy_orders"]],
            "best_sell_str": _fmt_price(book["best_sell"]),
            "best_buy_str": _fmt_price(book["best_buy"]),
            "spread_str": _fmt_price(book["spread"]),
            "spread_pct_str": (
                f"{book['spread_pct']:.1f}%" if book["spread_pct"] is not None else "—"
            ),
        },
    )


# ── LP store ROI (Task 3) ──────────────────────────────────────────────────────

@router.get("/market/lp", response_class=HTMLResponse)
async def market_lp_page(request: Request):
    """LP store ROI calculator landing — a faction -> corp tree picker (see
    `market_lp_corps_tree` below); the offers table loads via htmx once a
    corp is picked (see `market_lp_offers` below). The tree itself loads
    lazily (`hx-trigger="load"` in the template) so this route never blocks
    page render on the ~270-corp ESI pass."""
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "market_lp.html", {})


@router.get("/market/lp/corps-tree", response_class=HTMLResponse)
async def market_lp_corps_tree(request: Request):
    """htmx partial: the whole faction -> corp tree in one fragment (~270
    corps, no lazy per-level loading — see `app.market.lp.get_corps_by_faction`
    for the grouping + caching discipline). Corp click sets the page's
    existing hidden `corporation_id` input and triggers the offers load
    exactly as the old flat `<select>` did — this endpoint only supplies the
    tree data; wiring the click handlers is a follow-up task."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    factions = await market_lp.get_corps_by_faction()
    degraded = any(f.get("degraded") for f in factions)
    return templates.TemplateResponse(
        request, "partials/lp_corp_tree.html",
        {"factions": factions, "degraded": degraded},
    )


def _fmt_isk_per_lp(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


@router.get("/market/lp/offers", response_class=HTMLResponse)
async def market_lp_offers(
    request: Request, corporation_id: int = 0, db: AsyncSession = Depends(get_db),
):
    """htmx partial: one corp's LP store offers, ranked by ISK/LP.

    `corporation_id=0` (nothing selected yet, or a cleared dropdown) renders
    an empty body rather than erroring — the select's own default option
    round-trips here on first render in some browsers."""
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    if not corporation_id:
        return HTMLResponse("")

    raw_offers = await market_lp.get_offers(corporation_id)
    price_map = await market_lp.get_price_map(db)
    ranked = market_lp.rank_offers(raw_offers, price_map)

    type_ids = {r["type_id"] for r in ranked if r["type_id"] is not None}
    name_map = await sde.type_ids_to_names(db, list(type_ids))

    def _display_row(r: dict) -> dict:
        return {
            "item_name": name_map.get(r["type_id"], f"Type {r['type_id']}"),
            "quantity_str": _fmt_qty(r["quantity"]),
            "lp_cost_str": _fmt_qty(r["lp_cost"]),
            "isk_cost_str": _fmt_price(r["isk_cost"]),
            "materials_cost_str": _fmt_price(r["materials_cost"]),
            "unit_price_str": _fmt_price(r["unit_price"]),
            "isk_per_lp_str": _fmt_isk_per_lp(r["isk_per_lp"]),
            "priced": r["priced"],
        }

    return templates.TemplateResponse(
        request, "partials/market_lp_offers.html",
        {"corporation_id": corporation_id, "rows": [_display_row(r) for r in ranked]},
    )
