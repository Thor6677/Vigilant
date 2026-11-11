"""Market → Hub Scan.

  GET  /market/hub-scan            the page
  GET  /market/hub-scan/count      how many items a list holds (status line text)
  POST /market/hub-scan/start      save the inputs, start (or rejoin) a scan
  GET  /market/hub-scan/status     progress while a job runs; count and result when not
  POST /market/hub-scan/cancel     stop the user's scan
  GET  /market/hub-scan/result     the saved result for a list, filtered and sorted

Maths is in app.market.scan_rules, jobs in scan_runner, strings in scan_view.
Every route needs a logged-in user and only ever reads that user's rows: the
job registry is keyed by user, results are loaded by (user, list), and a
watchlist is only counted when it is the user's.

While the user's job is queued or running, the status line is that job's,
whatever list the page has picked since: one job per user, and a second
start returns the running one. When it ends the status line falls back to
the list it was asked about.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db
from app.market import hub_prefs, hub_view, scan_lists, scan_results, scan_runner, scan_view, watchlists
from app.routes.market_hubs import _fee, _resolve_origin
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market"])
# MUST be named `templates` — see the note in app/routes/market.py.
templates = Jinja2Templates(directory="app/templates")

PICK_A_LIST = "Pick a list to scan."


def _kind_ref(kind: str, ref) -> tuple[str, int]:
    try:
        number = int(ref)
    except (TypeError, ValueError):
        number = 0
    kind = kind if kind in scan_lists.KINDS else "category"
    return kind, 0 if kind == "blueprints" else max(number, 0)


def _has_list(kind: str, ref: int) -> bool:
    return kind == "blueprints" or ref > 0


def _status_ctx(job: scan_runner.Job | None, kind: str, ref: int, note: str = "") -> dict:
    """While the job is active the line is the job's (and polls with its list);
    otherwise it is the asked-for list's, and a failed or cancelled job is only
    mentioned for the list it was scanning."""
    active = job is not None and job.state in scan_runner.ACTIVE
    if active:
        kind, ref = job.kind, job.ref
    return {"job": job, "active": active, "kind": kind, "ref": ref, "note": note,
            "same_list": job is not None and (job.kind, job.ref) == (kind, ref),
            # With no list picked there is nothing to count: say so at once.
            "count_text": "" if _has_list(kind, ref) else PICK_A_LIST,
            "percent": int(job.done * 100 / job.total) if active and job.total else 0}


async def _category_path(db: AsyncSession, group_id: int) -> str:
    if not group_id:
        return ""
    return " › ".join(p["market_group_name"]
                      for p in await sde.get_market_group_path(db, group_id))


async def _watchlists(db: AsyncSession, user_id: int) -> list[dict]:
    """Plain dicts, so nothing in the template can touch an expired row."""
    return [{"id": w.id, "name": w.name} for w in await watchlists.all_for(db, user_id)]


@router.get("/market/hub-scan", response_class=HTMLResponse)
async def hub_scan_page(request: Request, kind: str = "category", ref: str = "0",
                        db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    prefs = await hub_prefs.get_prefs(db, user_id)
    _id, origin_name, _err = await _resolve_origin(db, None, prefs.origin_system_id)
    kind, number = _kind_ref(kind, ref)
    lists = await _watchlists(db, user_id)
    job = scan_runner.get(user_id)
    if job is not None and job.state in scan_runner.ACTIVE:
        kind, number = job.kind, job.ref        # the page shows the running scan's list
    elif kind == "watchlist" and not number and lists:
        number = lists[0]["id"]                 # what the picker shows first
    return templates.TemplateResponse(request, "market_hub_scan.html", {
        "kind": kind, "ref": number,
        "category_path": await _category_path(db, number) if kind == "category" else "",
        "budget": scan_view.fmt_budget(prefs.scan_budget_isk),
        "share_pct": scan_view.fmt_pct(prefs.market_share_pct),
        "sales_tax_pct": scan_view.fmt_pct(prefs.sales_tax_pct),
        "broker_fee_pct": scan_view.fmt_pct(prefs.broker_fee_pct),
        "origin_name": origin_name,
        "watchlists": lists,
        **_status_ctx(job, kind, number),
    })


@router.get("/market/hub-scan/count", response_class=HTMLResponse)
async def hub_scan_count(request: Request, kind: str = "category", ref: str = "0",
                         db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    if not _has_list(kind, number):
        return HTMLResponse(PICK_A_LIST)
    try:
        n = await scan_lists.count(db, user_id, kind, number)
    except scan_lists.ListNotFound:
        return HTMLResponse("That list no longer exists.")
    if n is None:
        return HTMLResponse("Your blueprints are counted when the scan starts.")
    if n > scan_lists.CAP:
        return HTMLResponse(f"{n:,} items: the first {scan_lists.CAP} by name will be "
                            "scanned. Pick a narrower list to cover the rest.")
    return HTMLResponse(f"{n:,} item{'s' if n != 1 else ''}.")


@router.post("/market/hub-scan/start", response_class=HTMLResponse)
async def hub_scan_start(request: Request, kind: str = Form("category"), ref: str = Form("0"),
                         budget: str = Form(""), share: str = Form(""),
                         sales_tax: str = Form(""), broker_fee: str = Form(""),
                         origin: str = Form(""), db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    if not _has_list(kind, number):
        # Nothing starts; the status line says to pick a list (count_text).
        return templates.TemplateResponse(request, "partials/hub_scan_status.html",
                                          _status_ctx(scan_runner.get(user_id), kind, number))
    prefs = await hub_prefs.get_prefs(db, user_id)
    tax, _ = _fee(sales_tax, prefs.sales_tax_pct)
    broker, _ = _fee(broker_fee, prefs.broker_fee_pct)
    origin_id, origin_name, error = await _resolve_origin(db, origin, None)
    note = ""
    if error:
        # Keep the saved system rather than clearing it on a typo, as phase 1 does.
        origin_id, origin_name, _ = await _resolve_origin(db, None, prefs.origin_system_id)
        note = f"{error} Using your saved starting system." if origin_id else error
    budget_isk = hub_prefs.clamp_budget(hub_view.parse_isk(budget), prefs.scan_budget_isk)
    share_pct = hub_prefs.clamp_share(share, prefs.market_share_pct)
    await hub_prefs.save_prefs(db, user_id, tax, broker, origin_id)
    await hub_prefs.save_scan_prefs(db, user_id, budget_isk, share_pct)
    job = scan_runner.start(user_id, kind, number, scan_results.Settings(
        budget_isk, share_pct, tax, broker, origin_id, origin_name))
    if (job.kind, job.ref) != (kind, number):
        note = "Your other scan is still running; start this one when it ends."
    return templates.TemplateResponse(request, "partials/hub_scan_status.html",
                                      _status_ctx(job, kind, number, note))


@router.post("/market/hub-scan/cancel", response_class=HTMLResponse)
async def hub_scan_cancel(request: Request):
    """Cancelling is asynchronous: the job says "cancelled" once its task
    stops, which the next poll shows."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    scan_runner.cancel(user_id)
    job = scan_runner.get(user_id)
    kind, ref = (job.kind, job.ref) if job else ("category", 0)
    return templates.TemplateResponse(request, "partials/hub_scan_status.html",
                                      _status_ctx(job, kind, ref))


@router.get("/market/hub-scan/status", response_class=HTMLResponse)
async def hub_scan_status(request: Request, kind: str = "category", ref: str = "0",
                          was: str = ""):
    """Polled every 2 s while a job is active. `was=1` is sent by a page that
    was polling: with no job left (a restart), it is told the scan was lost."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    job = scan_runner.get(user_id)
    ctx = _status_ctx(job, kind, number)
    ctx["interrupted"] = job is None and was == "1"
    return templates.TemplateResponse(request, "partials/hub_scan_status.html", ctx)


@router.get("/market/hub-scan/result", response_class=HTMLResponse)
async def hub_scan_result(request: Request, kind: str = "category", ref: str = "0",
                          hub: str = "", source: str = "", losers: str = "", under: str = "",
                          sort: str = scan_view.DEFAULT_SORT,
                          direction: str = Query("", alias="dir"),
                          db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    if not _has_list(kind, number):
        return HTMLResponse("")
    result = await scan_results.load(db, user_id, kind, number)
    if result is None:
        return HTMLResponse("")
    filters = scan_view.parse_filters(hub, source, losers, under)
    return templates.TemplateResponse(request, "partials/hub_scan_result.html", {
        "result": result, "filters": filters, "kind": kind, "ref": number,
        "scanned": scan_view.scanned_text(result["scanned_at"]),
        "view": scan_view.build(result, filters, sort, direction),
        "hub_names": scan_view.HUB_NAMES, "footnote": scan_view.FOOTNOTE,
        "copy_only_title": scan_view.COPY_ONLY_TITLE,
        "scan_cap": scan_lists.CAP,
        "watchlists": await _watchlists(db, user_id),
    })
