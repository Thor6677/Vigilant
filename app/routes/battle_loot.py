"""Battle report loot finder (T-082): which wrecks of a fight are worth looting.

Paste a battle link (zKillboard / killmail.app / evetools 'related', or a saved
evetools report) and get one row per wreck with what dropped and what was
destroyed, sorted by dropped value. The data comes from the local killmail
archive; see app/intel/battle_loot.py for the link rules and the valuation, and
app/intel/battle_loot_prices.py for the Jita pricing job.

Routes (all login-gated; the partials answer 401 without a session):
  GET /intel/loot                       the form; `?br=<pasted link>` redirects
  GET /intel/loot/related/{sys}/{stamp} canonical page for a related link
  GET /intel/loot/windows               canonical page for explicit windows
  GET /intel/loot/results               the totals + wreck list (htmx)
  GET /intel/loot/wreck/{killmail_id}   one wreck's items (htmx)
  GET /intel/loot/prices/status         Jita pricing progress poll (htmx)

Every partial carries the whole scope in its query string (`w=sys:start:end`,
repeated), so nothing depends on in-process state for correctness. A request
makes no outbound call except the evetools resolve at paste time, and the
entity-name lookups that every kill page does; Jita prices are fetched by the
background job only.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail, get_db
from app.db.sde_models import SDESystem, SDEType
from app.intel import battle_loot as bl
from app.intel import battle_loot_prices as prices
from app.intel import evetools_br
from app.intel.battle_loot import BrError, Scope
from app.intel.recent_battles import resolve_entity_names
from app.market.lp import get_price_map
from app.sde import lookup as sde

log = logging.getLogger(__name__)
router = APIRouter(tags=["intel"])
templates = Jinja2Templates(directory="app/templates")

DEFAULT_MIN_ISK = 10_000_000
MAX_MIN_MILLIONS = 1_000_000
ROW_CAP = 300
CONTEXT_CACHE_ENTRIES = 8
FRESH_FIGHT_SECONDS = 2 * 3600
TTL_FRESH, TTL_OLD = 60, 3600
TTL_NO_AVERAGES = 10      # the EVE average list failed to load: don't pin that
BLUEPRINT_CATEGORY_ID = 9

_GROUP_RE = re.compile(r"^[acn]:\d{1,10}$")
_STAMP_RE = re.compile(r"^\d{12}$")

SOURCE_LABELS = {
    "jita": "Jita", "avg": "avg", "avg_no_jita": "avg · no Jita sell",
    "avg_thin": "avg · thin Jita ask", "avg_fetch_failed": "avg · fetch failed",
    "avg_budget": "avg · Jita paused", "blueprint": "blueprint", "unpriced": "unpriced",
}


def fmt_isk(v: float | None) -> str:
    v = v or 0.0
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= limit:
            return f"{v / limit:,.2f}{suffix}"
    return f"{v:,.0f}"


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _stamp_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


templates.env.filters["bl_isk"] = fmt_isk
templates.env.globals["bl_source_label"] = lambda s: SOURCE_LABELS.get(s, s)


def _now() -> float:
    return time.time()


# ── Scope helpers ───────────────────────────────────────────────────────

def _scope_query(scope: Scope, src: str = "") -> str:
    pairs = [("w", w) for w in bl.window_params(scope)]
    if src:
        pairs.append(("src", src))
    return urlencode(pairs)


def _zkill_related_url(system_id: int, stamp: str, systems: list[int], hb: int, ha: int) -> str:
    """zKill's own link for the same set: options go in its `/o/` suffix."""
    base = f"https://zkillboard.com/related/{system_id}/{stamp[:10]}00/"
    if systems == [system_id] and (hb, ha) == (bl.DEFAULT_HOURS_BEFORE, bl.DEFAULT_HOURS_AFTER):
        return base
    doc = {"systems": systems, "hoursBefore": hb, "hoursAfter": ha}
    return base + "o/" + quote(json.dumps(doc, separators=(",", ":")), safe="") + "/"


def _related_url(ref: bl.BrRef) -> str:
    primary = ref.system_ids[0]
    stamp = datetime.fromtimestamp(ref.stamp, timezone.utc).strftime("%Y%m%d%H00")
    extra = {}
    if len(ref.system_ids) > 1:
        extra["systems"] = ",".join(str(s) for s in ref.system_ids)
    if ref.hours_before != bl.DEFAULT_HOURS_BEFORE:
        extra["hb"] = ref.hours_before
    if ref.hours_after != bl.DEFAULT_HOURS_AFTER:
        extra["ha"] = ref.hours_after
    qs = f"?{urlencode(extra, safe=',')}" if extra else ""
    return f"/intel/loot/related/{primary}/{stamp}{qs}"


def _query_scope(request: Request) -> tuple[Scope, str]:
    """The scope a partial was asked about. Raises BrError."""
    scope = bl.parse_windows(request.query_params.getlist("w"))
    src = request.query_params.get("src", "")
    return scope, (src if bl.SRC_RE.match(src) else "")


async def _known_systems(db: AsyncSession, ids: list[int]) -> set[int]:
    rows = await db.execute(select(SDESystem.system_id).where(SDESystem.system_id.in_(ids)))
    return set(rows.scalars().all())


def _min_isk(request: Request) -> float:
    raw = request.query_params.get("min")
    if raw is None:
        return float(DEFAULT_MIN_ISK)
    try:
        millions = float(raw)
    except ValueError:
        return float(DEFAULT_MIN_ISK)
    if millions != millions:  # NaN
        return float(DEFAULT_MIN_ISK)
    return max(0.0, min(millions, MAX_MIN_MILLIONS)) * 1_000_000


def _group_param(request: Request) -> str | None:
    g = request.query_params.get("group", "")
    return g if _GROUP_RE.match(g) else None


# ── Computed context (valuation inputs, never HTML) ─────────────────────

_ctx_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


async def _build_context(db: AsyncSession, scope: Scope, src: str) -> dict:
    kills, truncated = await bl.load_scope(db, scope)
    items = await bl.load_items(db, [k["killmail_id"] for k in kills])
    avg = await get_price_map(db)
    item_types = {row[0] for rows in items.values() for row in rows}
    blueprints = await _blueprint_ids(db, item_types)
    return {
        "kills": kills, "items": items, "avg": avg, "blueprints": blueprints,
        "truncated": truncated, "avg_ok": bool(avg),
        "cands": prices.rank_candidates(items, avg, blueprints),
        "key": bl.scope_key(scope, src),
    }


async def _blueprint_ids(db: AsyncSession, type_ids) -> set[int]:
    out: set[int] = set()
    ids = sorted(type_ids)
    for i in range(0, len(ids), bl.ITEM_CHUNK):
        rows = await db.execute(
            select(SDEType.type_id).where(SDEType.type_id.in_(ids[i:i + bl.ITEM_CHUNK]),
                                          SDEType.category_id == BLUEPRINT_CATEGORY_ID))
        out.update(rows.scalars().all())
    return out


async def get_context(db: AsyncSession, scope: Scope, src: str) -> dict:
    """LRU of context dicts. A fight that ended under 2 h ago is still being
    filled by the stream, so it lives 60 s; an old one an hour."""
    key = bl.scope_key(scope, src)
    hit = _ctx_cache.get(key)
    if hit and hit[0] > _now():
        _ctx_cache.move_to_end(key)
        return hit[1]
    ctx = await _build_context(db, scope, src)
    fresh = _now() - max(w.end for w in scope) < FRESH_FIGHT_SECONDS
    ttl = TTL_NO_AVERAGES if not ctx["avg_ok"] else (TTL_FRESH if fresh else TTL_OLD)
    for k in [k for k, (until, _c) in _ctx_cache.items() if until <= _now()]:
        del _ctx_cache[k]
    _ctx_cache[key] = (_now() + ttl, ctx)
    _ctx_cache.move_to_end(key)
    while len(_ctx_cache) > CONTEXT_CACHE_ENTRIES:
        _ctx_cache.popitem(last=False)
    return ctx


# ── View ────────────────────────────────────────────────────────────────

def _entity_id(group: str) -> int:
    return int(group.split(":")[1])


async def _build_view(db: AsyncSession, ctx: dict, scope: Scope, src: str, *,
                      min_isk: float, group: str | None, show_empty: bool, job) -> dict:
    cands = ctx["cands"]
    jita = prices.cached_prices(cands.types)
    v = bl.value_wrecks(ctx["kills"], ctx["items"], ctx["avg"], jita, ctx["blueprints"], min_isk)

    shown = [r for r in v["rows"] if (group is None or r["group"] == group)]
    hidden_empty = sum(1 for r in shown if r["zero_drop"])
    if not show_empty:
        shown = [r for r in shown if not r["zero_drop"]]
    more = max(0, len(shown) - ROW_CAP)
    shown = shown[:ROW_CAP]

    entity_ids = {_entity_id(g["key"]) for g in v["groups"] if g["key"] != "n:0"}
    type_ids = set()
    for r in shown:
        k = r["kill"]
        entity_ids.update(x for x in (k["char_id"], k["corp_id"], k["alliance_id"]) if x)
        type_ids.add(k["ship_type_id"])
        type_ids.update(t for t, _q, _v in r["top"])
    entity_names = await resolve_entity_names(sorted(entity_ids))
    type_names = await sde.type_ids_to_names(db, sorted(type_ids))
    system_names = await sde.system_ids_to_names(db, sorted({w.system_id for w in scope}))

    rows = []
    for r in shown:
        k = r["kill"]
        rows.append({
            "id": k["killmail_id"], "ship": type_names.get(k["ship_type_id"]) or f"Type {k['ship_type_id']}",
            "pilot": entity_names.get(k["char_id"]) if k["char_id"] else None,
            "corp": entity_names.get(k["corp_id"]) if k["corp_id"] else None,
            "alliance": entity_names.get(k["alliance_id"]) if k["alliance_id"] else None,
            "system": system_names.get(k["system_id"]) or str(k["system_id"]),
            "time": k["time"].strftime("%H:%M"), "dropped": r["dropped"], "destroyed": r["destroyed"],
            "worth": r["worth"], "approx": r["approx"], "zero_drop": r["zero_drop"],
            "unknown": r["unknown"],
            "top": [(type_names.get(t) or f"Type {t}", fmt_isk(val)) for t, _q, val in r["top"]],
        })

    groups = []
    for g in v["groups"]:
        kind, ident = g["key"].split(":")
        label = entity_names.get(int(ident)) if kind != "n" else None
        groups.append({**g, "label": label or ("Unknown" if kind == "n" else f"{'Alliance' if kind == 'a' else 'Corp'} {ident}"),
                       "url": "/intel/loot/results?" + _view_query(
                           scope, src, min_isk, None if g["key"] == group else g["key"], show_empty),
                       "active": g["key"] == group})

    # price-source summary: counts are by distinct dropped type
    by_type = v["by_type"]
    type_counts: dict[str, int] = {}
    for t in by_type.values():
        type_counts[t["source"]] = type_counts.get(t["source"], 0) + 1
    jita_value = sum(t["value"] for t in by_type.values() if t["source"] == "jita")
    pending = [t for t in cands.types if t not in jita]
    active = bool(job and job.state != "done")
    as_of = prices.priced_at(cands.types)
    ev = evetools_br.cached_resolution(evetools_br.src_cache_key(src)) if src else None
    return {
        **v, "rows": rows, "groups": groups, "group": group, "show_empty": show_empty,
        "hidden_empty": hidden_empty, "more_rows": more, "min_millions": min_isk / 1e6,
        "truncated": ctx["truncated"], "kill_count": len(ctx["kills"]),
        "evetools_count": ev.kms_count if ev else None,
        "type_counts": {s: type_counts[s] for s in SOURCE_LABELS if type_counts.get(s)},
        "dropped_type_total": sum(1 for t in by_type.values() if t["source"] != "blueprint"), "jita_types": type_counts.get("jita", 0),
        "jita_share": (jita_value / v["dropped"]) if v["dropped"] else 0.0,
        "cand_total": cands.total, "cand_cap": len(cands.types), "cand_coverage": cands.coverage,
        # "provisional" only while a job that will replace the numbers is
        # running; types still unpriced with no job are said so plainly.
        "provisional": active, "poll": active, "job": job if active else None,
        "avg_ok": ctx["avg_ok"], "pending_types": len(pending),
        "as_of": _hhmm(int(as_of)) if as_of else None,
        "scope_q": _scope_query(scope, src), "src": src,
        "scope_pairs": [("w", w) for w in bl.window_params(scope)] + ([("src", src)] if src else []),
    }


def _view_query(scope: Scope, src: str, min_isk: float, group: str | None, show_empty: bool) -> str:
    pairs = [("w", w) for w in bl.window_params(scope)]
    if src:
        pairs.append(("src", src))
    pairs.append(("min", f"{min_isk / 1e6:g}"))
    if group:
        pairs.append(("group", group))
    if show_empty:
        pairs.append(("empty", "1"))
    return urlencode(pairs)


def _scope_text(scope: Scope, system_names: dict[int, str]) -> str:
    first = scope[0]
    systems = sorted({w.system_id for w in scope})
    name = system_names.get(first.system_id) or str(first.system_id)
    text = f"{name} {_stamp_text(first.start)}–{_hhmm(first.end)} EVE"
    if len(systems) > 1:
        text += f" (+{len(systems) - 1} system{'s' if len(systems) > 2 else ''})"
    return text


def _no_store(resp):
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ── Routes ──────────────────────────────────────────────────────────────

def _form(request: Request, error: str | None = None, pasted: str = "", status: int = 200):
    return templates.TemplateResponse(
        request, "battle_loot.html", {"scope": None, "error": error, "pasted": pasted[:bl.MAX_URL_LEN]},
        status_code=status)


@router.get("/intel/loot", response_class=HTMLResponse)
async def loot_form(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    pasted = request.query_params.get("br")
    if pasted is None:
        return _form(request)
    try:
        ref = bl.parse_br_link(pasted)
        if ref.kind == "related":
            if set(ref.system_ids) - await _known_systems(db, list(ref.system_ids)):
                raise BrError("That link names a system this instance doesn't know.")
            return RedirectResponse(_related_url(ref), status_code=303)
    except BrError as e:
        return _form(request, str(e), pasted)
    known = lambda ids: _known_systems(db, ids)
    if ref.kind == "evetools_battle":
        result = await evetools_br.resolve_battle(ref.battle_id, user_id, known)
        src = f"evetools-battle:{ref.battle_id}"
    else:
        result = await evetools_br.resolve_br(ref.br_id, user_id, known)
        src = f"evetools:{ref.br_id}"
    if isinstance(result, evetools_br.EvetoolsError):
        return _form(request, result.message, pasted)
    return RedirectResponse(f"/intel/loot/windows?{_scope_query(result.windows, src)}", status_code=303)


async def _render_page(request: Request, db: AsyncSession, scope: Scope, src: str, *,
                       external_url: str = "", min_isk: float, group: str | None, show_empty: bool):
    ctx = await get_context(db, scope, src)
    job = prices.ensure_job(ctx["key"], ctx["cands"].types)
    view = await _build_view(db, ctx, scope, src, min_isk=min_isk, group=group,
                             show_empty=show_empty, job=job)
    names = await sde.system_ids_to_names(db, sorted({w.system_id for w in scope}))
    return templates.TemplateResponse(request, "battle_loot.html", {
        "scope": scope, "scope_text": _scope_text(scope, names), "v": view, "error": None,
        "pasted": "", "external_url": external_url,
    })


def _digits(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 6):
        raise ValueError(raw)
    return int(raw)


@router.get("/intel/loot/related/{system_id}/{stamp}", response_class=HTMLResponse)
async def loot_related(system_id: int, stamp: str, request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    if not _STAMP_RE.match(stamp):
        return _form(request, "That link's timestamp isn't valid.", status=400)
    try:
        if not 0 < system_id <= bl.MAX_SYSTEM_ID:
            raise BrError("bad system")
        ts = bl.parse_stamp(stamp)
        systems = [system_id]
        listed = request.query_params.get("systems")
        if listed:      # like zKill's options, a listed set IS the selection
            chosen: list[int] = []
            for part in listed.split(",")[:bl.MAX_WINDOWS]:
                if not (part.isascii() and part.isdigit() and len(part) <= 10):
                    raise ValueError(part)
                if int(part) not in chosen:
                    chosen.append(int(part))
            systems = chosen or systems
        hb = _digits(request.query_params.get("hb"), bl.DEFAULT_HOURS_BEFORE)
        ha = _digits(request.query_params.get("ha"), bl.DEFAULT_HOURS_AFTER)
        scope = bl.scope_from_related(systems, ts, hb, ha)
    except (BrError, ValueError):
        return _form(request, "That link isn't valid.", status=400)
    if {w.system_id for w in scope} - await _known_systems(db, [w.system_id for w in scope]):
        return _form(request, "That link names a system this instance doesn't know.", status=404)
    return await _render_page(
        request, db, scope, "", min_isk=_min_isk(request), group=_group_param(request),
        show_empty=request.query_params.get("empty") == "1",
        external_url=_zkill_related_url(
            system_id, stamp, systems,
            max(bl.HOURS_MIN, min(bl.HOURS_MAX, hb)), max(bl.HOURS_MIN, min(bl.HOURS_MAX, ha))))


@router.get("/intel/loot/windows", response_class=HTMLResponse)
async def loot_windows(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return RedirectResponse("/")
    try:
        scope, src = _query_scope(request)
    except BrError as e:
        return _form(request, str(e), status=400)
    if {w.system_id for w in scope} - await _known_systems(db, [w.system_id for w in scope]):
        return _form(request, "That link names a system this instance doesn't know.", status=404)
    return await _render_page(
        request, db, scope, src, min_isk=_min_isk(request), group=_group_param(request),
        show_empty=request.query_params.get("empty") == "1")


@router.get("/intel/loot/results", response_class=HTMLResponse)
async def loot_results(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    try:
        scope, src = _query_scope(request)
    except BrError:
        return HTMLResponse("", status_code=400)
    ctx = await get_context(db, scope, src)
    # Idempotent: joins a running job, or starts one for prices that aged out
    # or failed since the page was drawn (the poll then reappears in the result).
    job = prices.ensure_job(ctx["key"], ctx["cands"].types)
    view = await _build_view(db, ctx, scope, src, min_isk=_min_isk(request), group=_group_param(request),
                             show_empty=request.query_params.get("empty") == "1", job=job)
    return _no_store(templates.TemplateResponse(request, "partials/battle_loot_results.html", {"v": view}))


@router.get("/intel/loot/prices/status", response_class=HTMLResponse)
async def loot_price_status(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    try:
        scope, src = _query_scope(request)
    except BrError:
        return HTMLResponse("", status_code=400)
    ctx = await get_context(db, scope, src)
    job = prices.ensure_job(ctx["key"], ctx["cands"].types) or prices.get_job(ctx["key"])
    active = job is not None and job.state != "done"
    resume = None
    if job and job.budget_resume_at:
        resume = _hhmm(int(job.budget_resume_at))
    resp = templates.TemplateResponse(request, "partials/battle_loot_price_status.html", {
        "job": job, "poll": active, "scope_q": _scope_query(scope, src), "resume": resume})
    if not active:
        resp.headers["HX-Trigger"] = "loot-prices-ready"
    return _no_store(resp)


@router.get("/intel/loot/wreck/{killmail_id}", response_class=HTMLResponse)
async def loot_wreck(killmail_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    try:
        scope, src = _query_scope(request)
    except BrError:
        return HTMLResponse("", status_code=400)
    if not 0 < killmail_id < 2 ** 62:
        return HTMLResponse("", status_code=404)
    kill = (await db.execute(
        select(Killmail.killmail_id, Killmail.solar_system_id, Killmail.killmail_time)
        .where(Killmail.killmail_id == killmail_id))).first()
    if kill is None or not bl.in_scope(scope, kill.solar_system_id, kill.killmail_time):
        return HTMLResponse("", status_code=404)
    rows = (await bl.load_items(db, [killmail_id])).get(killmail_id, [])
    ctx = await get_context(db, scope, src)
    blueprints = await _blueprint_ids(db, {r[0] for r in rows})
    jita = prices.cached_prices(ctx["cands"].types)
    avg = ctx["avg"]
    names = await sde.type_ids_to_names(db, sorted({r[0] for r in rows}))
    dropped, destroyed = [], []
    for type_id, q_destroyed, q_dropped, _single, _flag in rows:
        name = names.get(type_id) or f"Type {type_id}"
        if q_dropped:
            unit, source, alt = bl.price_for_drop(type_id, avg, jita, blueprints)
            dropped.append({"name": name, "qty": q_dropped, "unit": unit, "total": unit * q_dropped,
                            "source": source, "alt": alt})
        if q_destroyed and type_id not in blueprints:
            unit = avg.get(type_id) or 0.0
            destroyed.append({"name": name, "qty": q_destroyed, "unit": unit, "total": unit * q_destroyed})
    dropped.sort(key=lambda x: -x["total"])
    destroyed.sort(key=lambda x: -x["total"])
    return _no_store(templates.TemplateResponse(request, "partials/battle_loot_wreck_items.html", {
        "kill_id": killmail_id, "dropped": dropped, "destroyed": destroyed[:15],
        "destroyed_more": max(0, len(destroyed) - 15), "no_items": not rows}))
