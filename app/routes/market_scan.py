"""Market → Hub Scan.

  GET  /market/hub-scan            the page
  GET  /market/hub-scan/count      how many items a list holds (status line text)
  POST /market/hub-scan/start      save the inputs, start (or rejoin) a scan
  GET  /market/hub-scan/status     progress while a job runs; count and result when not
  POST /market/hub-scan/cancel     stop the user's scan
  GET  /market/hub-scan/result     the saved result for a list, filtered and sorted

Watchlists (app.market.watchlists holds the rules and limits):

  GET  /market/watchlists                          the manager (?id= picks the list)
  POST /market/watchlists                          create
  POST /market/watchlists/{wid}/rename
  POST /market/watchlists/{wid}/delete             with its items and saved result
  POST /market/watchlists/{wid}/items              add one item (type_id)
  POST /market/watchlists/{wid}/items/{type_id}/delete
  POST /market/watchlists/{wid}/add-result         add a saved result's items (kind, ref)
  GET  /market/watchlists/search                   type search for the manager (wid, q)
  POST /market/watchlists/add-item                 the item page's control (watchlist_id, type_id)

Maths is in app.market.scan_rules, jobs in scan_runner, strings in scan_view.
Every route needs a logged-in user (401 without one) and only ever reads that
user's rows: the job registry is keyed by user, results are loaded by (user,
list), and a watchlist is only counted or changed when it is the user's.
Every watchlist route answers 404 for a list id that is not the user's, or
that is not an id at all; ids are bounded (watchlists.parse_id) so none can
overflow SQLite.

The manager (partials/hub_scan_watchlist.html) is drawn into the page on
load and replaced by every manager route's reply, which also carries the
page's list picker (partials/hub_scan_wl_pick.html) as an out-of-band swap so
the picker always shows the lists as they now are.

While the user's job is queued or running, the status line is that job's,
whatever list the page has picked since: one job per user, and a second
start returns the running one. When it ends the status line falls back to
the list it was asked about.
"""
from __future__ import annotations

import logging
import math

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
    """A known kind and a list reference in 0..MAX_ID (0: none). Anything
    else is 0, so an over-long number can't overflow a SQLite bind."""
    number = watchlists.parse_id(ref) or 0
    kind = kind if kind in scan_lists.KINDS else "category"
    return kind, 0 if kind == "blueprints" else number


def _has_list(kind: str, ref: int) -> bool:
    return kind == "blueprints" or ref > 0


async def _count_text(db: AsyncSession, user_id: int, kind: str, ref: int) -> str:
    """The count line: how many items the list holds, or why that is not known."""
    if not _has_list(kind, ref):
        return PICK_A_LIST
    try:
        n = await scan_lists.count(db, user_id, kind, ref)
    except scan_lists.ListNotFound:
        return scan_runner.LIST_GONE
    if n is None:
        return "Your blueprints are counted when the scan starts."
    if n > scan_lists.CAP:
        return (f"{n:,} items: the first {scan_lists.CAP} by name will be scanned. "
                "Pick a narrower list to cover the rest.")
    return f"{n:,} item{'s' if n != 1 else ''}."


async def _status_ctx(db: AsyncSession, user_id: int, job: scan_runner.Job | None,
                      kind: str, ref: int, note: str = "", start_reply: bool = False) -> dict:
    """While the job is active the line is the job's (and polls with its list);
    otherwise it is the asked-for list's, and a failed or cancelled job is only
    mentioned for the list it was scanning.

    One status line at a time: idle, a list with a saved result has that
    result's finished line (label, time, count, Rescan) as its status, so the
    count line is drawn only when there is none. Both are known here, with a
    one-id read for the result and a COUNT for the list, so the line arrives
    whole; the polls of an active job touch the database not at all.
    `start_reply` sends the start's notes (`note`) out of band into the
    page's #hs-start-note, above the status box: the status line itself is
    replaced by the first poll, which would wipe them."""
    active = job is not None and job.state in scan_runner.ACTIVE
    if active:
        kind, ref = job.kind, job.ref
    has_result = (not active and _has_list(kind, ref)
                  and await scan_results.exists(db, user_id, kind, ref))
    return {"job": job, "active": active, "kind": kind, "ref": ref,
            "note": note, "start_reply": start_reply,
            "same_list": job is not None and (job.kind, job.ref) == (kind, ref),
            "has_result": has_result,
            "count_text": ("" if active or has_result
                           else await _count_text(db, user_id, kind, ref)),
            "percent": int(job.done * 100 / job.total) if active and job.total else 0}


def _as_typed(raw: str, used: float) -> bool:
    """Whether a number field's text was used as typed (read as the clamps
    read it): False for blank, junk, and anything clamped."""
    try:
        typed = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(typed) and typed == used


async def _category_path(db: AsyncSession, group_id: int) -> str:
    if not group_id:
        return ""
    return " › ".join(p["market_group_name"]
                      for p in await sde.get_market_group_path(db, group_id))


async def _watchlists(db: AsyncSession, user_id: int) -> list[dict]:
    """Plain dicts, so nothing in the template can touch an expired row."""
    return [{"id": w.id, "name": w.name} for w in await watchlists.all_for(db, user_id)]


NOT_MARKET = "That is not a market item."


async def _manager_ctx(db: AsyncSession, lists: list[dict], selected: int,
                       message: str = "", q: str = "") -> dict:
    """The manager's context (`m` in partials/hub_scan_watchlist.html): the
    user's lists and the one being edited, `selected` when it is theirs, else
    the first. `q` keeps the item search and its matches across a reply."""
    current = next((w for w in lists if w["id"] == selected), lists[0] if lists else None)
    entries = await watchlists.items(db, current["id"]) if current else []
    q = watchlists.clean_query(q) if current else ""
    return {
        "lists": lists, "current": current, "items": entries, "message": message,
        "q": q, "found": await watchlists.search_types(db, q) if q else [],
        "on_list": {t for t, _name in entries},
        "max_items": watchlists.MAX_ITEMS, "max_lists": watchlists.MAX_LISTS,
        "max_name": watchlists.MAX_NAME, "search_min": watchlists.SEARCH_MIN,
        "search_max": watchlists.SEARCH_MAX,
    }


async def _manager(request: Request, db: AsyncSession, user_id: int, selected: int = 0,
                   message: str = "", q: str = "") -> HTMLResponse:
    """The manager fragment, with the page's list picker out of band."""
    m = await _manager_ctx(db, await _watchlists(db, user_id), selected, message, q)
    return templates.TemplateResponse(request, "partials/hub_scan_watchlist.html",
                                      {"m": m, "oob": True})


def _no_list() -> HTMLResponse:
    return HTMLResponse("", status_code=404)


async def _own_list(db: AsyncSession, user_id: int, raw) -> int | None:
    """The id in `raw` when it names one of the user's lists, else None."""
    wid = watchlists.parse_id(raw)
    if wid is None or await watchlists.get(db, user_id, wid) is None:
        return None
    return wid


def _cancel_scan_of(user_id: int, wid: int) -> None:
    """Cancel the user's scan only if it is a queued or running scan of this list."""
    job = scan_runner.get(user_id)
    if (job is not None and job.state in scan_runner.ACTIVE
            and (job.kind, job.ref) == ("watchlist", wid)):
        scan_runner.cancel(user_id)


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
    elif kind == "watchlist" and number not in {w["id"] for w in lists}:
        # None picked, or one that is gone: what the picker shows first.
        number = lists[0]["id"] if lists else 0
    # The manager edits the list the picker shows (the first when the page
    # opens on another kind).
    manager = await _manager_ctx(db, lists, number if kind == "watchlist" else 0)
    return templates.TemplateResponse(request, "market_hub_scan.html", {
        "kind": kind, "ref": number,
        "category_path": await _category_path(db, number) if kind == "category" else "",
        "budget": scan_view.fmt_budget(prefs.scan_budget_isk),
        "share_pct": scan_view.fmt_pct(prefs.market_share_pct),
        "sales_tax_pct": scan_view.fmt_pct(prefs.sales_tax_pct),
        "broker_fee_pct": scan_view.fmt_pct(prefs.broker_fee_pct),
        "origin_name": origin_name,
        "watchlists": lists,
        "manager": manager,
        **(await _status_ctx(db, user_id, job, kind, number)),
    })


@router.get("/market/hub-scan/count", response_class=HTMLResponse)
async def hub_scan_count(request: Request, kind: str = "category", ref: str = "0",
                         db: AsyncSession = Depends(get_db)):
    """The count line's text on its own. The status line works it out itself
    (no second request); this answers the same for any other caller."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    return HTMLResponse(await _count_text(db, user_id, kind, number))


@router.post("/market/hub-scan/start", response_class=HTMLResponse)
async def hub_scan_start(request: Request, kind: str = Form("category"), ref: str = Form("0"),
                         budget: str = Form(""), share: str = Form(""),
                         sales_tax: str = Form(""), broker_fee: str = Form(""),
                         origin: str = Form(""), db: AsyncSession = Depends(get_db)):
    """A number field not used as typed (blank, unreadable, or clamped) is
    named in the reply with the value used, as phase 1 does: "Budget used:
    100B." A blank field falls back to the saved value, so it is named too
    (FastAPI reads a blank form field as absent; the page always sends
    every field). The notes go above the status line (see _status_ctx)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    if not _has_list(kind, number):
        # Nothing starts; the status line says to pick a list (count_text).
        return templates.TemplateResponse(
            request, "partials/hub_scan_status.html",
            await _status_ctx(db, user_id, scan_runner.get(user_id), kind, number,
                              start_reply=True))
    prefs = await hub_prefs.get_prefs(db, user_id)
    tax, tax_adjusted = _fee(sales_tax, prefs.sales_tax_pct)
    broker, broker_adjusted = _fee(broker_fee, prefs.broker_fee_pct)
    origin_id, origin_name, error = await _resolve_origin(db, origin, None)
    typed_budget = hub_view.parse_isk(budget)
    budget_isk = hub_prefs.clamp_budget(typed_budget, prefs.scan_budget_isk)
    share_pct = hub_prefs.clamp_share(share, prefs.market_share_pct)
    notes = []
    if budget_isk != typed_budget:
        notes.append(f"Budget used: {scan_view.fmt_budget(budget_isk)}.")
    if not _as_typed(share, share_pct):
        notes.append(f"Market share used: {scan_view.fmt_pct(share_pct)}%.")
    if tax_adjusted:
        notes.append(hub_view.fee_note("Sales tax", tax))
    if broker_adjusted:
        notes.append(hub_view.fee_note("Broker fee", broker))
    if error:
        # Keep the saved system rather than clearing it on a typo, as phase 1 does.
        origin_id, origin_name, _ = await _resolve_origin(db, None, prefs.origin_system_id)
        notes.append(f"{error} Using your saved starting system." if origin_id else error)
    await hub_prefs.save_prefs(db, user_id, tax, broker, origin_id)
    await hub_prefs.save_scan_prefs(db, user_id, budget_isk, share_pct)
    job = scan_runner.start(user_id, kind, number, scan_results.Settings(
        budget_isk, share_pct, tax, broker, origin_id, origin_name))
    if (job.kind, job.ref) != (kind, number):
        notes = ["Your other scan is still running; start this one when it ends."]
    return templates.TemplateResponse(
        request, "partials/hub_scan_status.html",
        await _status_ctx(db, user_id, job, kind, number, " ".join(notes), start_reply=True))


@router.post("/market/hub-scan/cancel", response_class=HTMLResponse)
async def hub_scan_cancel(request: Request, db: AsyncSession = Depends(get_db)):
    """Cancelling is asynchronous: the job says "cancelled" once its task
    stops, which the next poll shows."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    scan_runner.cancel(user_id)
    job = scan_runner.get(user_id)
    kind, ref = (job.kind, job.ref) if job else ("category", 0)
    return templates.TemplateResponse(request, "partials/hub_scan_status.html",
                                      await _status_ctx(db, user_id, job, kind, ref))


@router.get("/market/hub-scan/status", response_class=HTMLResponse)
async def hub_scan_status(request: Request, kind: str = "category", ref: str = "0",
                          was: str = "", db: AsyncSession = Depends(get_db)):
    """Polled every 2 s while a job is active. `was=1` is sent by a page that
    was polling: with no job left (a restart), it is told the scan was lost."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    kind, number = _kind_ref(kind, ref)
    job = scan_runner.get(user_id)
    ctx = await _status_ctx(db, user_id, job, kind, number)
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


# ── watchlists ─────────────────────────────────────────────────────────────

@router.get("/market/watchlists", response_class=HTMLResponse)
async def watchlist_manager(request: Request, wl_id: str = Query("", alias="id"),
                            db: AsyncSession = Depends(get_db)):
    """No id (or 0): the first list. An id that is not one of the user's: 404."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    selected = 0
    if wl_id.strip() not in ("", "0"):
        selected = await _own_list(db, user_id, wl_id)
        if selected is None:
            return _no_list()
    return await _manager(request, db, user_id, selected)


@router.post("/market/watchlists", response_class=HTMLResponse)
async def watchlist_create(request: Request, name: str = Form(""), current: str = Form(""),
                           db: AsyncSession = Depends(get_db)):
    """`current` is the list the manager was showing: a refused name leaves
    the manager on it."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    try:
        wid = await watchlists.create(db, user_id, name)
    except watchlists.WatchlistError as exc:
        return await _manager(request, db, user_id, watchlists.parse_id(current) or 0, str(exc))
    return await _manager(request, db, user_id, wid, "List created.")


@router.post("/market/watchlists/{wid}/rename", response_class=HTMLResponse)
async def watchlist_rename(request: Request, wid: str, name: str = Form(""),
                           db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = watchlists.parse_id(wid)
    if number is None:
        return _no_list()
    try:
        renamed = await watchlists.rename(db, user_id, number, name)
    except watchlists.WatchlistError as exc:
        return await _manager(request, db, user_id, number, str(exc))
    if renamed is None:
        return _no_list()
    return await _manager(request, db, user_id, number, "Renamed.")


@router.post("/market/watchlists/{wid}/delete", response_class=HTMLResponse)
async def watchlist_delete(request: Request, wid: str, db: AsyncSession = Depends(get_db)):
    """A queued or running scan of this list is cancelled first, so it
    cannot save a result for a list that is gone."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = await _own_list(db, user_id, wid)
    if number is None:
        return _no_list()
    _cancel_scan_of(user_id, number)
    if not await watchlists.remove(db, user_id, number):
        return _no_list()
    return await _manager(request, db, user_id, 0, "List deleted.")


@router.post("/market/watchlists/{wid}/items", response_class=HTMLResponse)
async def watchlist_add_item(request: Request, wid: str, type_id: str = Form(""),
                             q: str = Form(""), db: AsyncSession = Depends(get_db)):
    """Add one item. `q` is the search the button came from, kept in the reply."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = await _own_list(db, user_id, wid)
    if number is None:
        return _no_list()
    type_number = watchlists.parse_id(type_id)
    if not await watchlists.is_market_type(db, type_number):
        return await _manager(request, db, user_id, number, NOT_MARKET, q)
    outcome = await watchlists.add_items(db, user_id, number, [type_number])
    if outcome is None:
        return _no_list()
    added, full = outcome
    message = ("Added." if added else
               f"The list is full ({watchlists.MAX_ITEMS} items)." if full else
               "Already on the list.")
    return await _manager(request, db, user_id, number, message, q)


@router.post("/market/watchlists/{wid}/items/{type_id}/delete", response_class=HTMLResponse)
async def watchlist_remove_item(request: Request, wid: str, type_id: str, q: str = Form(""),
                                db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number, type_number = watchlists.parse_id(wid), watchlists.parse_id(type_id)
    if number is None or type_number is None:
        return _no_list()
    if not await watchlists.remove_item(db, user_id, number, type_number):
        return _no_list()
    return await _manager(request, db, user_id, number, "Removed.", q)


@router.post("/market/watchlists/{wid}/add-result", response_class=HTMLResponse)
async def watchlist_add_result(request: Request, wid: str, kind: str = Form(""),
                               ref: str = Form(""), db: AsyncSession = Depends(get_db)):
    """Add every priced row of the user's own saved result for (kind, ref),
    up to the list's limit. Replies with one line of plain text."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = await _own_list(db, user_id, wid)
    if number is None:
        return _no_list()
    kind, list_ref = _kind_ref(kind, ref)
    result = (await scan_results.load(db, user_id, kind, list_ref)
              if _has_list(kind, list_ref) else None)
    if result is None:
        return HTMLResponse("There is no saved result to add.")
    ids = [r["type_id"] for r in result["rows"] if r.get("status") == "ok"]
    outcome = await watchlists.add_items(db, user_id, number, ids)
    if outcome is None:
        return _no_list()
    added, full = outcome
    if full:
        return HTMLResponse(f"Added {added}; {full} did not fit "
                            f"({watchlists.MAX_ITEMS} items per list).")
    if not added:
        return HTMLResponse("Nothing new to add: they are on the list already.")
    return HTMLResponse(f"Added {added}.")


@router.get("/market/watchlists/search", response_class=HTMLResponse)
async def watchlist_search(request: Request, wid: str = "", q: str = "",
                           db: AsyncSession = Depends(get_db)):
    """Up to 15 market types for the manager's search box, each with an Add
    button for list `wid` (or a note that it is on the list already)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = await _own_list(db, user_id, wid)
    if number is None:
        return _no_list()
    q = watchlists.clean_query(q)
    found = await watchlists.search_types(db, q)
    return templates.TemplateResponse(request, "partials/hub_scan_wl_found.html", {
        "found": found, "found_q": q, "found_wid": number,
        "on_list": set(await watchlists.item_ids(db, number)) if found else set(),
        "search_min": watchlists.SEARCH_MIN,
    })


@router.post("/market/watchlists/add-item", response_class=HTMLResponse)
async def watchlist_add_from_item_page(request: Request, watchlist_id: str = Form(""),
                                       type_id: str = Form(""),
                                       db: AsyncSession = Depends(get_db)):
    """The item page's "Add to watchlist". Replies with one line of plain text."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    number = await _own_list(db, user_id, watchlist_id)
    if number is None:
        return _no_list()
    type_number = watchlists.parse_id(type_id)
    if not await watchlists.is_market_type(db, type_number):
        return HTMLResponse(NOT_MARKET)
    outcome = await watchlists.add_items(db, user_id, number, [type_number])
    if outcome is None:
        return _no_list()
    added, full = outcome
    if added:
        return HTMLResponse("Added.")
    if full:
        return HTMLResponse(f"That list is full ({watchlists.MAX_ITEMS} items).")
    return HTMLResponse("Already on that list.")
