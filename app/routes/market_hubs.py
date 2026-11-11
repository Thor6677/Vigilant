"""Market → hub comparison (item page section).

  * `GET  /market/type/{type_id}/hubs` — htmx partial: the five trade hubs
    compared for one item, quantity and cost.
  * `POST /market/hub-prefs`           — save fees and starting system.

The item page loads the whole partial once; after that the form's own
requests (and the retry button) ask for the same partial but swap in only its
`#hc-results` block, so the inputs are never re-rendered under the user.

All maths is in `app.market.hub_analysis`; all strings in
`app.market.hub_view`. This module only gathers data: regional history and
hub orders through the existing cached fetchers, at most ten upstream requests
for a first view and none while the caches are warm.

History is read on the request's own session, one hub after another, because
`get_history` writes — sharing a session across concurrent writers is unsafe.
Orders have no database side and are fetched concurrently.

Upstream failures: both fetchers remember a failure for 90 seconds and do not
retry that (region, type) meanwhile, so input changes during an ESI outage
cost nothing upstream. Within one request, once a history fetch fails the
remaining hubs that would need the network are not attempted (they are marked
failed and show as unavailable); hubs with fresh stored history still render.
So an outage costs at most one slow history attempt per request, not five.
The retry button sends `retry=1`, which forgets this type's remembered
failures at all five hubs first; ordinary input changes never send it.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
from app.esi.market import APPRAISAL_HUBS
from app.market import history as market_history
from app.market import hub_cost, hub_prefs, hub_route, hub_view
from app.market import orders as market_orders
from app.market.hub_analysis import analyse_hub, best_picks
from app.routes.market import _is_market_type
from app.sde import lookup as sde

router = APIRouter(tags=["market"])
# MUST be named `templates` — see the note in app/routes/market.py.
templates = Jinja2Templates(directory="app/templates")

HUB_ORDER = ("jita", "amarr", "dodixie", "hek", "rens")
HUB_NAMES = {"jita": "Jita", "amarr": "Amarr", "dodixie": "Dodixie",
             "hek": "Hek", "rens": "Rens"}
MAX_QTY = 1_000_000


def _today() -> date:
    """Clock seam — monkeypatched in tests."""
    return datetime.now(timezone.utc).date()


def _parse_qty(raw) -> int:
    """1 – MAX_QTY. Too large (including 'inf', '1e999' and very long digit
    strings, which float() reads as infinity) clamps to MAX_QTY; blank, junk,
    'nan' and anything below 1 give 1."""
    try:
        v = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return 1
    if math.isnan(v) or v < 1:
        return 1
    if v >= MAX_QTY:
        return MAX_QTY
    return int(v)


async def _resolve_origin(db: AsyncSession, raw: str | None, saved_id: int | None):
    """(system_id | None, display name, error). `raw` None means the field was
    not submitted (first load), so the saved system applies; '' means none."""
    if raw is None:
        if saved_id is None:
            return None, "", ""
        names = await sde.system_ids_to_names(db, [saved_id])
        return saved_id, names.get(saved_id, ""), ""
    name = raw.strip()[:64]
    if not name:
        return None, "", ""
    system_id = await sde.system_name_to_id(db, name)
    if system_id is None:
        return None, name, f"Unknown system: {name}."
    names = await sde.system_ids_to_names(db, [system_id])
    return system_id, names.get(system_id, name), ""


def _age_str(type_id: int, latest_day: date | None) -> str:
    stamps = [market_orders.fetched_at(APPRAISAL_HUBS[k]["region_id"], type_id)
              for k in HUB_ORDER]
    stamps = [s for s in stamps if s is not None]
    parts = []
    if stamps:
        minutes = int((market_orders._now() - min(stamps)).total_seconds() // 60)
        parts.append(f"Orders {minutes} min old" if minutes >= 1 else "Orders just fetched")
    if latest_day is not None:
        parts.append(f"history to {latest_day.isoformat()}")
    return " · ".join(parts)


@router.get("/market/type/{type_id}/hubs", response_class=HTMLResponse)
async def market_type_hubs(
    request: Request,
    type_id: int,
    qty: str = "1",
    cost_mode: str = "",
    cost: str = "",
    origin: str | None = None,
    sales_tax: str | None = None,
    broker_fee: str | None = None,
    retry: str = "",
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    # Not a market item: nothing to compare, and no upstream calls (ISS-178).
    if not await _is_market_type(db, type_id):
        return HTMLResponse("")

    prefs = await hub_prefs.get_prefs(db, user_id)
    quantity = _parse_qty(qty)
    tax_pct = hub_prefs.clamp_pct(sales_tax, prefs.sales_tax_pct)
    broker_pct = hub_prefs.clamp_pct(broker_fee, prefs.broker_fee_pct)
    origin_id, origin_name, origin_error = await _resolve_origin(
        db, origin, prefs.origin_system_id)

    # One cost field: a readable figure is used as given (to the 0.01 ISK the
    # game prices in, so the echoed field value is exactly the cost used);
    # blank or unreadable falls back to the benchmark, if there is one.
    bench, bench_me = await hub_cost.benchmark(db, type_id)
    typed_cost = hub_view.parse_isk(cost)
    if typed_cost is not None:
        unit_cost = round(typed_cost, 2)
        source = "build" if cost_mode == "build" else "typed"
    elif bench is not None:
        unit_cost, source = bench, "benchmark"
    else:
        unit_cost, source = None, None
    notes = [hub_view.cost_note(unit_cost, source, bench_me)]
    if typed_cost is None and cost.strip():
        notes.append(hub_view.UNREADABLE_COST_BENCHMARK if bench is not None
                     else hub_view.UNREADABLE_COST_NONE)
    if origin_error:
        notes.append(origin_error)

    hubs = [APPRAISAL_HUBS[k] for k in HUB_ORDER]
    if retry == "1":
        for hub in hubs:
            market_orders.clear_failure(hub["region_id"], type_id)
            market_history.clear_failure(hub["region_id"], type_id)
    order_lists = await asyncio.gather(
        *(market_orders.get_orders(h["region_id"], type_id) for h in hubs))

    today = _today()
    results: dict[str, dict | None] = {}
    latest_day = None
    history_down = False
    for key, hub, raw_orders in zip(HUB_ORDER, hubs, order_lists):
        region = hub["region_id"]
        if history_down and not await market_history.is_fresh(db, region, type_id):
            # ESI failed for another hub a moment ago: don't spend another slow
            # attempt on this one now; stored rows (if any) are still served.
            market_history.mark_failed(region, type_id)
        failing_before = market_history.recently_failed(region, type_id)
        rows = await market_history.get_history(region, type_id, db)
        if not failing_before and market_history.recently_failed(region, type_id):
            history_down = True  # this request's fetch for this hub just failed
        history_ok = await market_history.fetched_at(db, region, type_id) is not None
        orders_ok = market_orders.fetched_at(region, type_id) is not None
        if not (history_ok and orders_ok):
            results[key] = None
            continue
        days = [{"date": r.date, "volume": r.volume, "average": r.average} for r in rows]
        if days:
            newest = max(d["date"] for d in days)
            latest_day = newest if latest_day is None else max(latest_day, newest)
        results[key] = analyse_hub(
            days, raw_orders, hub["station_id"], quantity, unit_cost,
            tax_pct / 100, broker_pct / 100, today)

    routes: dict[str, dict | None] = {}
    if origin_id is not None:
        stations = await sde.stations_by_ids(db, [h["station_id"] for h in hubs])
        hub_systems = {
            key: stations[hub["station_id"]]["system_id"]
            for key, hub in zip(HUB_ORDER, hubs) if hub["station_id"] in stations
        }
        routes = await hub_route.hub_routes(db, origin_id, hub_systems)

    picks = best_picks(results, HUB_ORDER)
    return templates.TemplateResponse(request, "partials/market_hub_comparison.html", {
        "type_id": type_id,
        "qty": quantity,
        # Only the first load renders the form (later responses replace just
        # #hc-results), so these echo what an entry link carried.
        "cost_mode": "build" if cost_mode == "build" else "",
        "cost_value": hub_view.fmt_cost_input(unit_cost) if typed_cost is not None else "",
        "cost_placeholder": hub_view.cost_placeholder(bench),
        "notes": notes,
        "origin_name": origin_name,
        "sales_tax_pct": tax_pct,
        "broker_fee_pct": broker_pct,
        "age_str": _age_str(type_id, latest_day),
        "show_route": origin_id is not None,
        "picks": hub_view.pick_boxes(picks, results, HUB_NAMES),
        "rows": [hub_view.hub_row(k, HUB_NAMES[k], results[k], routes.get(k), quantity,
                                  origin_set=origin_id is not None)
                 for k in HUB_ORDER],
    })


@router.post("/market/hub-prefs", response_class=HTMLResponse)
async def market_hub_prefs_save(
    request: Request,
    sales_tax: str = Form(""),
    broker_fee: str = Form(""),
    origin: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Save fees and starting system. Returns a short status line that htmx
    swaps in beside the button."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    prefs = await hub_prefs.get_prefs(db, user_id)
    origin_id, _name, error = await _resolve_origin(db, origin, None)
    if error:
        # Keep the previously saved system rather than clearing it on a typo.
        origin_id = prefs.origin_system_id
    await hub_prefs.save_prefs(
        db, user_id,
        hub_prefs.clamp_pct(sales_tax, prefs.sales_tax_pct),
        hub_prefs.clamp_pct(broker_fee, prefs.broker_fee_pct),
        origin_id,
    )
    return HTMLResponse("Fees saved; starting system not recognised." if error else "Saved.")
