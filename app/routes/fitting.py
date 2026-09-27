"""Ship fitting tool — build and analyze ship fittings locally."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, Depends, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.db.models import get_db, UserFitting, UserFittingFolder, Character
from app.sde import lookup as sde
from app.fitting.engine import calculate_fitting_stats, get_type_dogma_attrs, dps_cache_key
from app.fitting.compare import build_compare_sections
from app.fitting.constants import ATTR_CPU, ATTR_POWER, ATTR_UPGRADE_COST, ATTR_DRONE_BW_USED
from app.fitting.boosters import ATTR_BOOSTERNESS, get_booster_info
from app.db.sde_models import (
    SDEModuleSlot, SDEType, SDEGroup, SDETypeDogmaAttribute, SDEDogmaAttribute,
    SDETypeSkillReq,
)
from app.esi.client import ESIClient, refresh_token
from app.esi import universe as esi_universe
from app.esi import character as esi_char
from app.esi import market as esi_market

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fitting"])
templates = Jinja2Templates(directory="app/templates")


def _folder_path_map(folders: list[dict]) -> dict[int, str]:
    """Flatten folders into id→'A / B / C' path labels for pickers."""
    by_id = {f["id"]: f for f in folders}
    out: dict[int, str] = {}
    for f in folders:
        parts = []
        cur = f
        while cur is not None:
            parts.append(cur["name"])
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        out[f["id"]] = " / ".join(reversed(parts))
    return out


@router.get("/tools/fitting", response_class=HTMLResponse)
async def fitting_tool(request: Request, db: AsyncSession = Depends(get_db)):
    """Fitting builder. The saved-fits list moved to /tools/fitting/saved;
    the builder now only needs the flat folder list for its 'Save to folder'
    picker."""
    user_id = request.session.get("user_id")
    if not user_id:
        # ISS-044: login-only. base.html loads actions.js only for a session,
        # so a stranger got a builder whose every control was dead.
        return RedirectResponse("/")
    folder_rows = await db.execute(
        select(UserFittingFolder)
        .where(UserFittingFolder.user_id == user_id)
        .order_by(UserFittingFolder.name)
    )
    folders = [
        {"id": f.id, "parent_id": f.parent_id, "name": f.name}
        for f in folder_rows.scalars().all()
    ]
    path_map = _folder_path_map(folders)
    folder_paths = sorted(
        [{"id": fid, "path": p} for fid, p in path_map.items()],
        key=lambda x: x["path"].lower(),
    )
    return templates.TemplateResponse(request, "fitting_tool.html", {"folder_paths": folder_paths})


@router.get("/tools/fitting/saved", response_class=HTMLResponse)
async def saved_fittings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Dedicated list view for saved fittings.

    DPS is computed lazily via /tools/fitting/saved/dps after page render
    (ISS-018 — was ~700ms warm because calculate_fitting_stats ran in
    parallel for every fit before the page returned). Cost stays in
    the synchronous render because it's a single batched ESI call
    (cached by the ESI layer) and a cheap arithmetic loop.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # --- Folders + fits -----------------------------------------------------
    folder_rows = await db.execute(
        select(UserFittingFolder)
        .where(UserFittingFolder.user_id == user_id)
        .order_by(UserFittingFolder.name)
    )
    folders = [
        {"id": f.id, "parent_id": f.parent_id, "name": f.name}
        for f in folder_rows.scalars().all()
    ]
    path_map = _folder_path_map(folders)

    fit_rows = await db.execute(
        select(UserFitting)
        .where(UserFitting.user_id == user_id)
        .order_by(UserFitting.updated_at.desc())
    )
    fits = list(fit_rows.scalars().all())

    # --- Gather all type_ids for name + price resolution -------------------
    items_by_fit: dict[int, list[dict]] = {}
    all_type_ids: set[int] = set()
    for f in fits:
        try:
            items = json.loads(f.items_json) if f.items_json else []
        except Exception:
            items = []
        items_by_fit[f.id] = items
        if f.ship_type_id:
            all_type_ids.add(f.ship_type_id)
        for item in items:
            tid = item.get("type_id")
            if tid:
                all_type_ids.add(int(tid))
            cid = item.get("charge_type_id")
            if cid:
                all_type_ids.add(int(cid))

    ship_names = await sde.type_ids_to_names(db, [f.ship_type_id for f in fits if f.ship_type_id]) if fits else {}

    # --- Prices only (DPS deferred to /saved/dps) -------------------------
    price_map: dict[int, float] = {}
    if all_type_ids:
        try:
            client = ESIClient("", db=db)
            prices = await esi_market.get_market_prices(client)
            for p in prices or []:
                tid = p.get("type_id")
                if tid in all_type_ids:
                    price_map[tid] = float(p.get("average_price") or p.get("adjusted_price") or 0)
        except Exception as e:
            logger.info("market price fetch failed: %s", e)

    # --- Compose rows ------------------------------------------------------
    rows = []
    for f in fits:
        items = items_by_fit.get(f.id, [])
        cost = price_map.get(f.ship_type_id, 0.0)
        for item in items:
            qty = item.get("quantity", 1) or 1
            tid = item.get("type_id")
            if tid:
                cost += price_map.get(int(tid), 0.0) * qty
            cid = item.get("charge_type_id")
            if cid:
                cost += price_map.get(int(cid), 0.0)
        rows.append({
            "id": f.id,
            "name": f.name,
            "ship_type_id": f.ship_type_id,
            "ship_name": ship_names.get(f.ship_type_id, f"Type {f.ship_type_id}"),
            "folder_id": f.folder_id,
            "folder_path": path_map.get(f.folder_id) if f.folder_id else "",
            # DPS is filled in client-side from /tools/fitting/saved/dps —
            # None signals "loading" to the template.
            "dps": None,
            "cost": round(cost, 2),
            "updated_at": f.updated_at,
        })

    # Sort by folder path then name for predictability
    rows.sort(key=lambda r: (r["folder_path"].lower(), r["name"].lower()))

    folder_paths = sorted(
        [{"id": fid, "path": p} for fid, p in path_map.items()],
        key=lambda x: x["path"].lower(),
    )

    return templates.TemplateResponse(request, "fitting_saved.html", {"rows": rows,
        "folders": folders,
        "folder_paths": folder_paths,
        "total": len(rows)})


# Stale fits recomputed per /saved/dps call. ISS-018's original design
# computed EVERY saved fit, concurrently, on every call — fine at a
# handful of fits, but calculate_fitting_stats is not cheap (dogma
# modifier pipeline + stacking penalties per module), and a real 135-fit
# account measured 175s of 100%-CPU recompute on one call. A small
# sequential batch keeps any single call fast; the page polls again while
# "pending" is nonzero to fill in the rest (see fitting_saved.html).
DPS_STALE_BATCH_SIZE = 8


async def _compute_and_cache_dps(
    db: AsyncSession, fit_id: int, ship_type_id: int,
    items_json: str, implants_json: str, boosters_json: str, cache_key: str,
) -> float:
    """Compute one fit's DPS (with its saved implants/boosters, matching
    what the builder and compare view would show) and persist it under
    `cache_key` so the next call can skip it entirely. Never raises —
    a fit that fails to compute (a since-removed type, malformed JSON on
    an old row) caches as 0.0 under its current key rather than being
    retried every single call forever.
    """
    try:
        items = json.loads(items_json) if items_json else []
    except Exception:
        items = []
    try:
        implants = [rec["type_id"] for rec in _sanitize_implants_map(json.loads(implants_json or "{}")).values()]
    except Exception:
        implants = []
    try:
        boosters = _booster_entries(_sanitize_boosters_map(json.loads(boosters_json or "{}")))
    except Exception:
        boosters = []

    dps = 0.0
    if items:
        try:
            stats = await calculate_fitting_stats(db, ship_type_id, items, implants=implants, boosters=boosters)
            dps = float(stats.get("total_dps") or 0.0)
        except Exception as e:
            logger.info("DPS calc failed for fit %s: %s", fit_id, e)
            dps = 0.0
    dps = round(dps, 1)

    await db.execute(
        update(UserFitting).where(UserFitting.id == fit_id)
        .values(dps_cached=dps, dps_cache_key=cache_key)
    )
    await db.commit()
    return dps


@router.get("/tools/fitting/saved/dps")
async def saved_fittings_dps(request: Request, db: AsyncSession = Depends(get_db)):
    """Persistent per-fit DPS cache (T-069 follow-up to ISS-018).

    Returns {"dps": {fit_id: dps, ...}, "pending": <stale fits left>}.
    `dps` carries every fit whose cache is still valid (dps_cache_key
    matches — see app.fitting.engine.dps_cache_key) plus whatever this
    call just (re)computed; `pending` is how many stale fits are left
    after this call's batch, for the page to decide whether to ask again.

    Sequential with the one request-scoped session, not
    asyncio.gather + a session per fit — that concurrency is exactly what
    turned "compute everything" into a CPU spike large enough to stall the
    whole app on a real account's worth of fits. asyncio.sleep(0) between
    fits yields to the event loop so one slow request doesn't starve
    others on it.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    sde_stamp = await sde.get_sde_version_stamp(db)

    fit_rows = await db.execute(
        select(
            UserFitting.id, UserFitting.ship_type_id, UserFitting.items_json,
            UserFitting.implants_json, UserFitting.boosters_json,
            UserFitting.dps_cached, UserFitting.dps_cache_key,
        ).where(UserFitting.user_id == user_id)
    )
    fits = fit_rows.all()
    if not fits:
        return {"dps": {}, "pending": 0}

    out: dict[int, float] = {}
    stale: list[tuple[int, int, str, str, str, str]] = []
    for row in fits:
        items_json = row.items_json or "[]"
        implants_json = row.implants_json or "{}"
        boosters_json = row.boosters_json or "{}"
        key = dps_cache_key(row.ship_type_id, items_json, implants_json, boosters_json, sde_stamp)
        if row.dps_cache_key == key and row.dps_cached is not None:
            out[row.id] = row.dps_cached
        else:
            stale.append((row.id, row.ship_type_id, items_json, implants_json, boosters_json, key))

    batch = stale[:DPS_STALE_BATCH_SIZE]
    for fit_id, ship_type_id, items_json, implants_json, boosters_json, key in batch:
        out[fit_id] = await _compute_and_cache_dps(
            db, fit_id, ship_type_id, items_json, implants_json, boosters_json, key)
        await asyncio.sleep(0)

    return {"dps": out, "pending": len(stale) - len(batch)}


async def _owned_fit_or_none(
    db: AsyncSession, fitting_id: int, user_id: int
) -> UserFitting | None:
    """Return the fitting only if it belongs to ``user_id``; else None.

    Single ownership gate for the compare view — a fit owned by another user
    resolves to None here, which the route maps to a 404 (never leaking that
    the id exists).
    """
    r = await db.execute(
        select(UserFitting)
        .where(UserFitting.id == fitting_id)
        .where(UserFitting.user_id == user_id)
    )
    return r.scalar_one_or_none()


@router.get("/tools/fitting/compare", response_class=HTMLResponse)
async def compare_fittings(
    request: Request,
    a: int = Query(...),
    b: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Side-by-side comparison of two owned saved fits (Phase 5 Task 4b).

    Both fits' stats come from the same ``calculate_fitting_stats`` engine
    call used across the fitting tool — no duplicated stat math. A fit that
    isn't owned by the session user 404s via ``_owned_fit_or_none``. Reads
    only, so the two sequential engine calls share the request session safely.

    Each fit's saved implants (``implants_json``, ISS-016) and boosters
    (``boosters_json``, T-049) go into the same engine call the builder
    makes, so a fit compares with the numbers it was saved with. The header
    says how many implants and boosters each side carries so a lopsided
    comparison is visible for what it is.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    fit_a = await _owned_fit_or_none(db, a, user_id)
    fit_b = await _owned_fit_or_none(db, b, user_id)
    if not fit_a or not fit_b:
        raise HTTPException(status_code=404, detail="Fitting not found")

    def _implant_ids(fit: UserFitting) -> list[int]:
        try:
            implants = _sanitize_implants_map(json.loads(fit.implants_json or "{}"))
        except Exception:
            implants = {}
        return [rec["type_id"] for rec in implants.values()]

    def _boosters_for(fit: UserFitting) -> list[dict]:
        try:
            boosters = _sanitize_boosters_map(json.loads(fit.boosters_json or "{}"))
        except Exception:
            boosters = {}
        return _booster_entries(boosters)

    async def _real_booster_slot_count(boosters: list[dict]) -> int:
        """How many of `boosters` the engine will actually apply — one per
        real boosterness slot, not one per the saved map's slot key.

        The map's key is whatever the builder attached at add-time; it is
        not re-derived from the SDE on save, so it can't be trusted to
        match a type's real boosterness (or to even BE a booster at all —
        a crafted save could put any type_id in there). apply_booster_bonuses
        does its own dedup this same way (first entry wins per real slot),
        so the displayed count has to agree with what it will apply, not
        with however many map keys happened to be present.
        """
        type_ids = [b["type_id"] for b in boosters]
        if not type_ids:
            return 0
        info = await get_booster_info(db, type_ids)
        return len({info[tid]["slot"] for tid in type_ids if tid in info})

    async def _stats_for(fit: UserFitting, implants: list[int], boosters: list[dict]) -> dict:
        try:
            items = json.loads(fit.items_json) if fit.items_json else []
        except Exception:
            items = []
        return await calculate_fitting_stats(
            db, fit.ship_type_id, items, implants=implants, boosters=boosters,
        )

    implants_a = _implant_ids(fit_a)
    implants_b = _implant_ids(fit_b)
    boosters_a = _boosters_for(fit_a)
    boosters_b = _boosters_for(fit_b)
    booster_count_a = await _real_booster_slot_count(boosters_a)
    booster_count_b = await _real_booster_slot_count(boosters_b)
    stats_a = await _stats_for(fit_a, implants_a, boosters_a)
    stats_b = await _stats_for(fit_b, implants_b, boosters_b)

    names = await sde.type_ids_to_names(db, [fit_a.ship_type_id, fit_b.ship_type_id])
    sections = build_compare_sections(stats_a, stats_b)

    return templates.TemplateResponse(request, "fitting_compare.html", {
        "fit_a": {
            "id": fit_a.id, "name": fit_a.name,
            "ship_name": names.get(fit_a.ship_type_id, f"Type {fit_a.ship_type_id}"),
            "ship_type_id": fit_a.ship_type_id,
            "implant_count": len(implants_a),
            "booster_count": booster_count_a,
        },
        "fit_b": {
            "id": fit_b.id, "name": fit_b.name,
            "ship_name": names.get(fit_b.ship_type_id, f"Type {fit_b.ship_type_id}"),
            "ship_type_id": fit_b.ship_type_id,
            "implant_count": len(implants_b),
            "booster_count": booster_count_b,
        },
        "sections": sections,
    })


@router.get("/tools/fitting/search/ships", response_class=HTMLResponse)
async def search_ships(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    results = await sde.search_ships(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "ship"})


@router.get("/tools/fitting/search/modules", response_class=HTMLResponse)
async def search_modules(
    request: Request,
    q: str = Query("", min_length=2),
    slot: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    slot_filter = slot if slot in ("high", "mid", "low", "rig", "subsystem") else None
    results = await sde.search_modules(db, q, slot_type=slot_filter, limit=20)

    # Attach slot type and fitting info to each result
    for r in results:
        slot_result = await db.execute(
            select(SDEModuleSlot.slot_type, SDEModuleSlot.is_turret, SDEModuleSlot.is_launcher)
            .where(SDEModuleSlot.type_id == r["type_id"])
        )
        slot_row = slot_result.fetchone()
        r["slot_type"] = slot_row.slot_type if slot_row else "unknown"
        r["is_turret"] = slot_row.is_turret if slot_row else False
        r["is_launcher"] = slot_row.is_launcher if slot_row else False

    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "module"})


@router.get("/tools/fitting/search/drones", response_class=HTMLResponse)
async def search_drones(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    results = await sde.search_drones(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "drone"})


@router.get("/tools/fitting/search/implants")
async def search_implants(
    request: Request,
    q: str = Query("", min_length=2),
    slot: int | None = Query(None, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    """Return implants matching a name query, optionally filtered to a
    single slot (1-10 via implantness dogma attribute 331).

    Returns JSON: [{type_id, name, slot}, ...] — the caller (fitting UI)
    drives a vanilla <input> + <ul> rather than the htmx partial used
    for modules/drones, because each slot picks one item only.
    """
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    # `select`, `SDEType`, `SDETypeDogmaAttribute` are already imported at
    # module scope (sqlalchemy + app.db.sde_models above) — a stray local
    # re-import here used to shadow them with `app.db.models.SDEType`, which
    # doesn't exist (the SDE tables live in app.db.sde_models), so every call
    # raised ImportError before the query ever ran. That's the whole bug
    # behind "implant search doesn't work": the endpoint 500'd unconditionally.
    pattern = f"%{q}%"
    # Implant types have implantness (attr_id 331) set to their slot.
    stmt = (
        select(SDEType.type_id, SDEType.type_name, SDETypeDogmaAttribute.value)
        .join(SDETypeDogmaAttribute,
              (SDETypeDogmaAttribute.type_id == SDEType.type_id)
              & (SDETypeDogmaAttribute.attribute_id == 331))
        .where(SDEType.type_name.like(pattern))
        .where(SDEType.published == True)
        .order_by(SDEType.type_name)
        .limit(30)
    )
    if slot is not None:
        stmt = stmt.where(SDETypeDogmaAttribute.value == float(slot))
    rows = (await db.execute(stmt)).fetchall()
    return JSONResponse([
        {"type_id": r[0], "name": r[1], "slot": int(r[2])}
        for r in rows
    ])


@router.get("/tools/fitting/search/boosters")
async def search_boosters(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    """Return combat boosters matching a name query (T-049).

    Returns JSON: [{type_id, name, slot, side_effects: [{effect_id, label,
    chance}, ...]}, ...] — mirrors search_implants' JSON-endpoint shape
    (vanilla <input> + results list, one item picked at a time), but slot
    here is boosterness (attr 1087), not implantness, and each result
    carries the side-effect catalog the UI needs to draw its checkboxes.

    Name + published filtering happens here against the SDE; slot and
    side_effects come from get_booster_info, the engine-side lookup this
    module doesn't implement. A type_id absent from that lookup (unknown to
    the engine) is simply left out of the results.
    """
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    pattern = f"%{q}%"
    stmt = (
        select(SDEType.type_id)
        .join(SDETypeDogmaAttribute,
              (SDETypeDogmaAttribute.type_id == SDEType.type_id)
              & (SDETypeDogmaAttribute.attribute_id == ATTR_BOOSTERNESS))
        .where(SDEType.type_name.like(pattern))
        .where(SDEType.published == True)
        .order_by(SDEType.type_name)
        .limit(30)
    )
    rows = (await db.execute(stmt)).fetchall()
    type_ids = [r[0] for r in rows]
    if not type_ids:
        return JSONResponse([])

    info = await get_booster_info(db, type_ids)
    results = []
    for type_id in type_ids:
        rec = info.get(type_id)
        if not rec:
            continue
        results.append({
            "type_id": rec["type_id"],
            "name": rec["name"],
            "slot": rec["slot"],
            "side_effects": rec.get("side_effects", []),
        })
    return JSONResponse(results)


@router.get("/tools/fitting/search/charges", response_class=HTMLResponse)
async def search_charges(
    request: Request,
    q: str = Query("", min_length=2),
    db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    results = await sde.search_charges(db, q, limit=15)
    return templates.TemplateResponse(request, "partials/fitting_search_results.html", {"results": results,
        "search_type": "charge"})


@router.post("/tools/fitting/stats", response_class=HTMLResponse)
async def fitting_stats(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Calculate and return fitting stats as an HTML partial."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    ship_type_id = body.get("ship_type_id")
    if not ship_type_id:
        return HTMLResponse("<div class='b-empty'>Select a ship to see stats</div>")

    items = body.get("items", [])
    damage_profile = body.get("damage_profile", "uniform")
    # Custom damage profile: 4 raw EM/Therm/Kin/Exp weights from the slider
    # UI. Normalized in resolve_damage_profile; when present it overrides the
    # named preset for EHP weighting only (see engine.resolve_damage_profile).
    damage_profile_custom = body.get("damage_profile_custom")
    if damage_profile_custom is not None:
        try:
            damage_profile_custom = [float(x) for x in damage_profile_custom]
            if len(damage_profile_custom) != 4:
                damage_profile_custom = None
        except (TypeError, ValueError):
            damage_profile_custom = None
    # Profile selector in the UI drives BOTH defensive EHP weighting
    # (damage_profile = incoming damage type fractions) AND offensive
    # effective DPS (target_resist_profile = the target's resists). One
    # selector, both calcs — matches what most fitting tools do.
    target_resist_profile = body.get("target_resist_profile", damage_profile)
    # Implant type IDs (slots 1-10) — slots 1-5 are no-ops; 6-10 are combat
    # hardwirings whose modifiers apply via _apply_implant_bonuses.
    implants_raw = body.get("implants", []) or []
    implants = [int(x) for x in implants_raw if x]
    # Active boosters (T-049) — engine entry shape {"type_id", "side_effects"}
    # per app/fitting/boosters.py. Malformed entries drop silently, same
    # tolerance as the implants line above; count capped defensively. The
    # body's "boosters" isn't necessarily a list at all (a crafted request
    # can send anything JSON allows), so that has to be checked before
    # slicing it — a bare int or dict there used to raise TypeError.
    boosters_raw = body.get("boosters", []) or []
    if not isinstance(boosters_raw, list):
        boosters_raw = []
    boosters: list[dict] = []
    for b in boosters_raw[:MAX_BOOSTERS_PER_REQUEST]:
        if not isinstance(b, dict):
            continue
        type_id = _bounded_int(b.get("type_id"))
        if type_id is None:
            continue
        boosters.append({
            "type_id": type_id,
            "side_effects": _clean_side_effects(b.get("side_effects")),
        })

    # Optional: scale by a specific character's trained skills instead of All V.
    user_id = request.session.get("user_id")
    character_id = body.get("character_id")
    skill_levels: dict[int, int] | None = None
    character_name: str | None = None
    if character_id and user_id:
        r = await db.execute(
            select(Character)
            .where(Character.character_id == int(character_id))
            .where(Character.user_id == user_id)
        )
        char = r.scalar_one_or_none()
        if char and _SKILLS_SCOPE in (char.scopes or ""):
            try:
                skill_levels = await _character_skills_map(db, char)
                character_name = char.character_name
            except Exception as e:
                logger.info("fitting_stats: skills fetch failed for %s: %s", character_id, e)

    stats = await calculate_fitting_stats(
        db, int(ship_type_id), items, damage_profile,
        skill_levels=skill_levels,
        target_resist_profile=target_resist_profile,
        implants=implants,
        damage_profile_custom=damage_profile_custom,
        boosters=boosters,
    )

    # Get ship name
    ship_name = await sde.type_id_to_name(db, int(ship_type_id))

    return templates.TemplateResponse(request, "partials/fitting_stats.html", {"stats": stats,
        "ship_name": ship_name or f"Ship {ship_type_id}",
        "ship_type_id": ship_type_id,
        "character_name": character_name})


@router.get("/tools/fitting/ship-slots/{ship_type_id}")
async def ship_slots(
    request: Request,
    ship_type_id: int,
    subsystems: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Return slot counts for a ship type, including subsystem modifiers."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    attrs = await get_type_dogma_attrs(db, ship_type_id)
    from app.fitting.constants import (
        ATTR_HI_SLOTS, ATTR_MED_SLOTS, ATTR_LOW_SLOTS,
        ATTR_RIG_SLOTS, ATTR_TURRET_SLOTS, ATTR_LAUNCHER_SLOTS,
        ATTR_HI_SLOT_MODIFIER, ATTR_MED_SLOT_MODIFIER, ATTR_LOW_SLOT_MODIFIER,
        ATTR_TURRET_HARDPOINT_MODIFIER, ATTR_LAUNCHER_HARDPOINT_MODIFIER,
    )
    hi = attrs.get(ATTR_HI_SLOTS, 0)
    med = attrs.get(ATTR_MED_SLOTS, 0)
    low = attrs.get(ATTR_LOW_SLOTS, 0)
    rig = attrs.get(ATTR_RIG_SLOTS, 0)
    turret = attrs.get(ATTR_TURRET_SLOTS, 0)
    launcher = attrs.get(ATTR_LAUNCHER_SLOTS, 0)

    # Apply subsystem slot modifiers if provided
    sub_ids = [int(x) for x in subsystems.split(",") if x.strip()] if subsystems else []
    for sub_id in sub_ids:
        sub_attrs = await get_type_dogma_attrs(db, sub_id)
        hi += sub_attrs.get(ATTR_HI_SLOT_MODIFIER, 0)
        med += sub_attrs.get(ATTR_MED_SLOT_MODIFIER, 0)
        low += sub_attrs.get(ATTR_LOW_SLOT_MODIFIER, 0)
        turret += sub_attrs.get(ATTR_TURRET_HARDPOINT_MODIFIER, 0)
        launcher += sub_attrs.get(ATTR_LAUNCHER_HARDPOINT_MODIFIER, 0)

    # Detect T3C (Strategic Cruiser, group 963) for subsystem slot count
    GROUP_STRATEGIC_CRUISER = 963
    ship_result = await db.execute(
        select(SDEType.group_id).where(SDEType.type_id == ship_type_id)
    )
    group_id = ship_result.scalar_one_or_none()
    subsystem_slots = 4 if group_id == GROUP_STRATEGIC_CRUISER else 0

    return {
        "high": int(hi),
        "med": int(med),
        "low": int(low),
        "rig": int(rig),
        "turret": int(turret),
        "launcher": int(launcher),
        "subsystem": subsystem_slots,
    }


def _autoload_cargo_charges(items: list[dict], compat_by_weapon: dict[int, set[int]]) -> list[dict]:
    """Load a compatible cargo charge onto each empty weapon, then drop the
    cargo stacks that got used — shared by import_eft (a hand-pasted EFT fit
    can carry suggested ammo in its Cargo section) and the character
    bulk-import conversion path (an ESI fitting's suggested ammo/cap
    boosters/nanite paste sit in the Cargo flag the same way). Pulled out of
    import_eft so the two paths can't drift apart on this step.

    `compat_by_weapon` is {weapon_type_id: {compatible charge type_id, ...}}
    — callers batch this once via sde.get_compatible_charges_bulk rather
    than querying per weapon (see import_eft and _prepare_bulk_import_cache
    below).

    Pure — no DB access here, so it's cheap to call once per fit even
    inside a loop over 173 of them.
    """
    cargo_charges = [i for i in items if i["slot"] == "cargo"]
    weapon_items = [
        i for i in items
        if i["slot"] in ("high", "med") and not i.get("charge_type_id")
    ]
    if cargo_charges and weapon_items:
        for weapon in weapon_items:
            compat_ids = compat_by_weapon.get(weapon["type_id"], set())
            for cargo in cargo_charges:
                if cargo["type_id"] in compat_ids:
                    weapon["charge_type_id"] = cargo["type_id"]
                    weapon["charge_name"] = cargo["type_name"]
                    break

    loaded_charge_ids = {i.get("charge_type_id") for i in items if i.get("charge_type_id")}
    return [i for i in items if not (i["slot"] == "cargo" and i["type_id"] in loaded_charge_ids)]


@router.post("/tools/fitting/import-eft")
async def import_eft(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Parse EFT format text and return fitting state as JSON."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    eft_text = body.get("eft", "").strip()
    if not eft_text:
        return {"error": "No EFT text provided"}

    # Aggressively normalize Unicode that breaks name matching.
    # EVE client, Discord, and browsers inject invisible chars on copy/paste.
    import unicodedata
    cleaned = []
    for ch in eft_text:
        if ch in ('\n', '\r', '\t'):
            cleaned.append(ch)
        elif ch == '\u2019' or ch == '\u2018':
            cleaned.append("'")
        elif ch == '\u201c' or ch == '\u201d':
            cleaned.append('"')
        elif ch == '\u2013' or ch == '\u2014':
            cleaned.append('-')
        elif ch == '\u00a0':
            cleaned.append(' ')  # non-breaking space → space
        elif unicodedata.category(ch).startswith('C') and ch not in ('\n', '\r', '\t'):
            continue  # strip all control/format chars (ZWSP, BOM, etc.)
        else:
            cleaned.append(ch)
    eft_text = ''.join(cleaned)

    lines = eft_text.split("\n")
    if not lines:
        return {"error": "Empty EFT text"}

    # Parse header: [Ship Name, Fitting Name]
    header = lines[0].strip()
    match = re.match(r'^\[(.+?),\s*(.+?)\]$', header)
    if not match:
        return {"error": "Invalid EFT header — expected [Ship, Name]"}

    ship_name = match.group(1).strip()
    fitting_name = match.group(2).strip()

    # Resolve ship type
    ship_type_id = await sde.type_name_to_id(db, ship_name)
    if not ship_type_id:
        return {"error": f"Unknown ship: {ship_name}"}

    # Parse items. Slot resolution is deferred to one batched pass below
    # (_slots below) rather than a per-line await, so a fit with dozens of
    # lines costs a handful of queries total, not one (or three) per line —
    # the same helper the character bulk-import path uses (T-069). A
    # comma-separated inline charge ("Module Name, Charge Name") can't be
    # resolved to high/med until then either, so its raw name rides along
    # on a parallel list keyed by index rather than living on the item dict
    # itself — nothing about the item's own shape should depend on how far
    # through parsing we are.
    items = []
    charge_names_eft: list[str | None] = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("["):
            continue

        # Handle quantity suffix: "Module Name x5"
        qty_match = re.match(r'^(.+?)\s+x(\d+)$', line)
        if qty_match:
            item_name = qty_match.group(1).strip()
            quantity = int(qty_match.group(2))
        else:
            item_name = line
            quantity = 1

        # Handle comma-separated charge: "Module Name, Charge Name"
        charge_name_eft = None
        if ", " in item_name and not item_name.startswith("["):
            parts = item_name.rsplit(", ", 1)
            item_name = parts[0].strip()
            charge_name_eft = parts[1].strip()

        # Skip empty slot markers
        if item_name.startswith("[Empty ") or item_name.startswith("[empty "):
            continue

        # Resolve type
        type_id = await sde.type_name_to_id(db, item_name)
        if not type_id:
            logger.warning("EFT import: unresolved name '%s' (hex: %s)",
                           item_name, item_name.encode('unicode_escape').decode())
            continue

        items.append({
            "type_id": type_id,
            "type_name": item_name,
            "quantity": quantity,
        })
        charge_names_eft.append(charge_name_eft)

    # Batched slot resolution (SDEModuleSlot, with the drone/cargo fallback
    # via group -> category) for every distinct type_id parsed above.
    slot_map = await sde.get_module_slot_types_bulk(db, [i["type_id"] for i in items])
    for item in items:
        item["slot"] = slot_map.get(item["type_id"], "cargo")

    # Resolve inline charges now that slot is known. NOTE: "med", not
    # "mid" — SDEModuleSlot and the fitting tool's own slot keys both say
    # "med" (app/sde/loader.py's SLOT_EFFECT_MAP, fitting_tool.html's own
    # slotTypes list). Comparing against "mid" here used to make this a
    # silent no-op for every mid-slot weapon — a Capacitor Injector typed
    # as "Capacitor Injector II, Cap Booster 400" never got the charge.
    for item, charge_name_eft in zip(items, charge_names_eft):
        if charge_name_eft and item["slot"] in ("high", "med"):
            charge_id = await sde.type_name_to_id(db, charge_name_eft)
            if charge_id:
                item["charge_type_id"] = charge_id
                item["charge_name"] = charge_name_eft

    # Auto-load charges from cargo onto compatible weapons — one fit's worth
    # of weapon types, batched in the same fixed handful of queries the
    # character bulk-import path uses for all of them at once.
    weapon_type_ids = [
        i["type_id"] for i in items
        if i["slot"] in ("high", "med") and not i.get("charge_type_id")
    ]
    compat_by_weapon = await sde.get_compatible_charges_bulk(db, weapon_type_ids)
    compat_ids_by_weapon = {tid: {c["type_id"] for c in chs} for tid, chs in compat_by_weapon.items()}
    items = _autoload_cargo_charges(items, compat_ids_by_weapon)

    return {
        "ship_type_id": ship_type_id,
        "ship_name": ship_name,
        "fitting_name": fitting_name,
        "items": items,
    }


@router.post("/tools/fitting/export-eft")
async def export_eft(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Generate EFT format text from fitting state."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("")

    ship_type_id = body.get("ship_type_id")
    fitting_name = body.get("name", "Unnamed")
    items = body.get("items", [])

    if not ship_type_id:
        return PlainTextResponse("")

    ship_name = await sde.type_id_to_name(db, int(ship_type_id))
    if not ship_name:
        ship_name = f"Ship {ship_type_id}"

    # Resolve item names
    type_ids = list({i["type_id"] for i in items})
    type_names = await sde.type_ids_to_names(db, type_ids)

    lines = [f"[{ship_name}, {fitting_name}]"]

    for slot in ["low", "med", "high", "rig", "subsystem"]:
        slot_items = [i for i in items if i.get("slot") == slot]
        for item in slot_items:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            lines.append(name)
        lines.append("")

    drones = [i for i in items if i.get("slot") == "drone"]
    if drones:
        for item in drones:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            qty = item.get("quantity", 1)
            if qty > 1:
                lines.append(f"{name} x{qty}")
            else:
                lines.append(name)
        lines.append("")

    cargo = [i for i in items if i.get("slot") == "cargo"]
    if cargo:
        for item in cargo:
            name = type_names.get(item["type_id"], f"Type {item['type_id']}")
            qty = item.get("quantity", 1)
            if qty > 1:
                lines.append(f"{name} x{qty}")
            else:
                lines.append(name)

    return PlainTextResponse("\n".join(lines).rstrip())


def _sanitize_implants_map(raw) -> dict:
    """Validate an implant loadout map to {"1".."10": {type_id, name}}.

    Accepts untrusted JSON (save body or a stored row); drops anything
    malformed rather than erroring — an implant map is never worth failing
    a fit save over. `from_clone` marks slots imported from the active
    jump clone (ISS-016) so the UI can badge them.
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for slot, rec in raw.items():
        try:
            s = int(slot)
        except (TypeError, ValueError):
            continue
        if not (1 <= s <= 10) or not isinstance(rec, dict):
            continue
        try:
            type_id = int(rec.get("type_id"))
        except (TypeError, ValueError):
            continue
        entry = {"type_id": type_id, "name": str(rec.get("name") or f"Type {type_id}")[:128]}
        if rec.get("from_clone"):
            entry["from_clone"] = True
        out[str(s)] = entry
    return out


# Boosterness (the booster slot) is not a small fixed range like implantness
# (1-10) — the SDE runs 1 into the hundreds, since accelerators and event
# boosters each get a slot of their own. Bound generously rather than to a
# tight real-world range. See app/fitting/boosters.py for the full contract.
MAX_BOOSTER_SLOT = 1000
# A booster's SDE side-effect list is short — 12 booster*Penalty effects
# spread over 24 classic combat boosters, so no single booster has more than
# a handful. Capped well above that as a defensive bound, not a real limit.
MAX_BOOSTER_SIDE_EFFECTS = 20
# Defensive cap on how many boosters one /tools/fitting/stats request, or
# one saved fit's boosters_map, may carry — comfortably above any real
# loadout (one booster per boosterness slot; a handful of slots exist at
# all).
MAX_BOOSTERS_PER_REQUEST = 50

# Upper bound for any booster-related integer (type ID, effect ID, slot).
# Not a real-world limit — the SDE never gets close to it — it's a defense
# against a crafted request whose number int() converts without error but
# that later breaks something downstream: a JSON integer literal like
# 99999999999999999999999 parses fine as an arbitrary-precision Python int
# and would round-trip through save/load right up until a query tried to
# bind it as a SQLite parameter (SQLite integers are 64-bit), at which point
# every load of that fit started raising OverflowError from the DB driver.
_INT_UPPER_BOUND = 2 ** 31


def _bounded_int(value, upper: int = _INT_UPPER_BOUND) -> int | None:
    """int(value), or None if it isn't a clean positive int under `upper`.

    Three ways untrusted JSON breaks a bare ``int(x)`` call: a non-numeric
    value (TypeError/ValueError, already handled everywhere this used to be
    written inline); a huge literal like ``1e400`` that ``json.loads``
    parses as the float ``inf`` before it ever reaches here, and
    ``int(inf)`` raises OverflowError rather than returning anything; and an
    arbitrary-precision integer literal that converts cleanly but is too
    large for a downstream SQLite bind. The range check catches the third
    case; the exception tuple catches the first two.
    """
    try:
        x = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 < x < upper):
        return None
    return x


def _clean_side_effects(raw) -> list[int]:
    """Coerce a side_effects list to deduped, bounded, capped ints.

    Shared by the boosters map sanitizer and the stats route's inline
    request validation — both need the exact same tolerance for junk input.
    """
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    seen: set[int] = set()
    for e in raw:
        e_int = _bounded_int(e)
        if e_int is None or e_int in seen:
            continue
        seen.add(e_int)
        out.append(e_int)
        if len(out) >= MAX_BOOSTER_SIDE_EFFECTS:
            break
    return out


def _sanitize_boosters_map(raw) -> dict:
    """Validate a booster loadout map to
    {"<slot>": {type_id, name, side_effects}}, mirroring
    _sanitize_implants_map above.

    Accepts untrusted JSON (save body, or a stored row on load/compare —
    all three call this, so a bound enforced here holds everywhere); drops
    anything malformed rather than erroring — a booster map is never worth
    failing a fit save over. Slot is boosterness (see MAX_BOOSTER_SLOT
    above, not the 1-10 implantness range). `side_effects` are the
    side-effect dogma effect IDs the user has switched ON for that booster
    (see app/fitting/boosters.py for why the rest stay off by default).
    Entries are capped the same way the stats route caps its list, so a
    saved fit can't carry thousands of slots into compare or load.
    """
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for slot, rec in list(raw.items())[:MAX_BOOSTERS_PER_REQUEST]:
        s = _bounded_int(slot, MAX_BOOSTER_SLOT + 1)
        if s is None or not isinstance(rec, dict):
            continue
        type_id = _bounded_int(rec.get("type_id"))
        if type_id is None:
            continue
        out[str(s)] = {
            "type_id": type_id,
            "name": str(rec.get("name") or f"Type {type_id}")[:128],
            "side_effects": _clean_side_effects(rec.get("side_effects")),
        }
    return out


def _booster_entries(boosters_map: dict) -> list[dict]:
    """Sanitized booster map -> engine entry list [{type_id, side_effects}].

    The shape calculate_fitting_stats(..., boosters=...) takes, per
    app/fitting/boosters.py. Used by the compare view, and available to any
    other caller that already has a sanitized map in hand.
    """
    return [
        {"type_id": rec["type_id"], "side_effects": rec.get("side_effects", [])}
        for rec in boosters_map.values()
    ]


@router.get("/tools/fitting/clone-implants/{character_id}")
async def clone_implants(
    request: Request,
    character_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Active-clone implants for a linked character (ISS-016).

    Fetches the character's currently-plugged implants from ESI
    (GET /characters/{id}/implants/, esi-clones.read_implants.v1), resolves
    each type's slot via the implantness dogma attribute (331), and returns
    the same slot-map shape the fitting UI keeps in state.implants.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    r = await db.execute(
        select(Character)
        .where(Character.character_id == character_id)
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found"}
    if _IMPLANTS_SCOPE not in (char.scopes or ""):
        return {"error": "Character lacks the implants scope — re-add it to grant."}

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        type_ids = await client.get(f"/characters/{character_id}/implants/") or []
    except Exception as e:
        logger.warning("clone-implants ESI fetch failed for %s: %s", character_id, e)
        return {"error": "ESI implant fetch failed — try again shortly."}

    if not type_ids:
        return {"implants": {}}

    rows = (await db.execute(
        select(SDEType.type_id, SDEType.type_name, SDETypeDogmaAttribute.value)
        .join(SDETypeDogmaAttribute,
              (SDETypeDogmaAttribute.type_id == SDEType.type_id)
              & (SDETypeDogmaAttribute.attribute_id == 331))
        .where(SDEType.type_id.in_([int(t) for t in type_ids]))
    )).fetchall()

    implants: dict = {}
    for type_id, name, slot_val in rows:
        try:
            slot = int(slot_val)
        except (TypeError, ValueError):
            continue
        if 1 <= slot <= 10:
            implants[str(slot)] = {"type_id": type_id, "name": name, "from_clone": True}

    return {"implants": implants, "character_name": char.character_name}


@router.post("/tools/fitting/save")
async def save_fitting(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    ship_type_id = body.get("ship_type_id")
    name = body.get("name", "Unnamed").strip()
    description = body.get("description", "").strip()
    items = body.get("items", [])
    implants = _sanitize_implants_map(body.get("implants_map"))
    boosters = _sanitize_boosters_map(body.get("boosters_map"))
    fitting_id = body.get("fitting_id")
    folder_id = body.get("folder_id")
    if folder_id is not None:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            folder_id = None
    if folder_id is not None:
        owner_check = await db.execute(
            select(UserFittingFolder.id)
            .where(UserFittingFolder.id == folder_id)
            .where(UserFittingFolder.user_id == user_id)
        )
        if not owner_check.scalar_one_or_none():
            folder_id = None

    if not ship_type_id:
        return {"error": "No ship selected"}
    if not name:
        return {"error": "Name required"}

    now = datetime.now(timezone.utc)

    if fitting_id:
        result = await db.execute(
            select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
        )
        fitting = result.scalar_one_or_none()
        if fitting:
            fitting.name = name
            fitting.description = description
            fitting.ship_type_id = int(ship_type_id)
            fitting.items_json = json.dumps(items)
            fitting.implants_json = json.dumps(implants)
            fitting.boosters_json = json.dumps(boosters)
            fitting.updated_at = now
            if "folder_id" in body:
                fitting.folder_id = folder_id
            await db.commit()
            return {"id": fitting.id, "status": "updated"}

    fitting = UserFitting(
        user_id=user_id,
        folder_id=folder_id,
        name=name,
        description=description,
        ship_type_id=int(ship_type_id),
        items_json=json.dumps(items),
        implants_json=json.dumps(implants),
        boosters_json=json.dumps(boosters),
        created_at=now,
        updated_at=now,
    )
    db.add(fitting)
    await db.commit()
    await db.refresh(fitting)
    return {"id": fitting.id, "status": "saved"}


@router.get("/tools/fitting/load/{fitting_id}")
async def load_fitting(
    request: Request,
    fitting_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    result = await db.execute(
        select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
    )
    fitting = result.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}

    ship_name = await sde.type_id_to_name(db, fitting.ship_type_id)
    items = json.loads(fitting.items_json)

    # Resolve item names
    type_ids = list({i["type_id"] for i in items})
    type_names = await sde.type_ids_to_names(db, type_ids)
    for item in items:
        if "type_name" not in item:
            item["type_name"] = type_names.get(item["type_id"], f"Type {item['type_id']}")

    try:
        implants = _sanitize_implants_map(json.loads(fitting.implants_json or "{}"))
    except (ValueError, TypeError):
        implants = {}
    try:
        boosters = _sanitize_boosters_map(json.loads(fitting.boosters_json or "{}"))
    except (ValueError, TypeError):
        boosters = {}

    # The saved map only carries the switched-ON side-effect IDs. Rendering
    # the checkboxes for a reloaded fit needs every side effect's label and
    # chance too — including the ones currently off — so hand back the full
    # per-type catalog alongside the sanitized map; the builder merges it in.
    booster_type_ids = [rec["type_id"] for rec in boosters.values()]
    booster_info = await get_booster_info(db, booster_type_ids) if booster_type_ids else {}

    return {
        "id": fitting.id,
        "name": fitting.name,
        "description": fitting.description or "",
        "ship_type_id": fitting.ship_type_id,
        "ship_name": ship_name or f"Ship {fitting.ship_type_id}",
        "items": items,
        "implants": implants,
        "boosters": boosters,
        "booster_info": booster_info,
    }


@router.delete("/tools/fitting/{fitting_id}")
async def delete_fitting(
    request: Request,
    fitting_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}

    result = await db.execute(
        select(UserFitting).where(UserFitting.id == fitting_id, UserFitting.user_id == user_id)
    )
    fitting = result.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}

    await db.delete(fitting)
    await db.commit()
    return {"status": "deleted"}


# ── Folder management ──────────────────────────────────────────────────────

async def _owned_folder(db: AsyncSession, folder_id: int, user_id: int) -> UserFittingFolder | None:
    r = await db.execute(
        select(UserFittingFolder)
        .where(UserFittingFolder.id == folder_id)
        .where(UserFittingFolder.user_id == user_id)
    )
    return r.scalar_one_or_none()


@router.post("/tools/fitting/folders")
async def create_folder(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "Name required"}
    parent_id = body.get("parent_id")
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            return {"error": "Invalid parent"}
        if not await _owned_folder(db, parent_id, user_id):
            return {"error": "Parent folder not found"}
    folder = UserFittingFolder(user_id=user_id, parent_id=parent_id, name=name[:128])
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return {"id": folder.id, "parent_id": folder.parent_id, "name": folder.name}


@router.patch("/tools/fitting/folders/{folder_id}")
async def update_folder(request: Request, folder_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    folder = await _owned_folder(db, folder_id, user_id)
    if not folder:
        return {"error": "Folder not found"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}

    if "name" in body:
        new_name = (body.get("name") or "").strip()
        if not new_name:
            return {"error": "Name required"}
        folder.name = new_name[:128]
    if "parent_id" in body:
        pid = body.get("parent_id")
        if pid is None:
            folder.parent_id = None
        else:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                return {"error": "Invalid parent"}
            if pid == folder.id:
                return {"error": "Cannot parent folder to itself"}
            # Walk up parent chain to detect cycles
            parent = await _owned_folder(db, pid, user_id)
            if not parent:
                return {"error": "Parent folder not found"}
            cursor = parent
            seen = {folder.id}
            while cursor is not None:
                if cursor.id in seen:
                    return {"error": "Would create a folder cycle"}
                seen.add(cursor.id)
                if cursor.parent_id is None:
                    break
                cursor = await _owned_folder(db, cursor.parent_id, user_id)
                if cursor is None:
                    break
            folder.parent_id = pid
    folder.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": folder.id, "parent_id": folder.parent_id, "name": folder.name}


@router.delete("/tools/fitting/folders/{folder_id}")
async def delete_folder(request: Request, folder_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    folder = await _owned_folder(db, folder_id, user_id)
    if not folder:
        return {"error": "Folder not found"}

    # MVP: refuse delete if the folder (or any descendant) contains fits or subfolders.
    sub_count = await db.execute(
        select(UserFittingFolder.id).where(UserFittingFolder.parent_id == folder_id)
    )
    if sub_count.scalar_one_or_none() is not None:
        return {"error": "Folder contains subfolders; empty it first"}
    fit_count = await db.execute(
        select(UserFitting.id)
        .where(UserFitting.folder_id == folder_id)
        .where(UserFitting.user_id == user_id)
    )
    if fit_count.scalar_one_or_none() is not None:
        return {"error": "Folder contains fittings; move or delete them first"}
    await db.delete(folder)
    await db.commit()
    return {"status": "deleted"}


@router.patch("/tools/fitting/{fitting_id}/folder")
async def move_fitting(request: Request, fitting_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    r = await db.execute(
        select(UserFitting)
        .where(UserFitting.id == fitting_id)
        .where(UserFitting.user_id == user_id)
    )
    fitting = r.scalar_one_or_none()
    if not fitting:
        return {"error": "Fitting not found"}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request"}
    folder_id = body.get("folder_id")
    if folder_id is None:
        fitting.folder_id = None
    else:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            return {"error": "Invalid folder"}
        if not await _owned_folder(db, folder_id, user_id):
            return {"error": "Folder not found"}
        fitting.folder_id = folder_id
    fitting.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"id": fitting.id, "folder_id": fitting.folder_id}


# ── Import from character (ESI in-game fittings) ────────────────────────

_FITTINGS_SCOPE = "esi-fittings.read_fittings.v1"

# A character keeps well under this many in-game fits; it's headroom for
# _bounded_int's coercion of the save endpoint's fitting_ids list, not a
# real-world estimate (the owner this shipped for has 173).
MAX_BULK_IMPORT_FITTING_IDS = 1000

# Root of the "Ships" market-group tree. Matched by name (parent_group_id
# IS NULL, market_group_name == "Ships") rather than a hardcoded id: the
# real SDE's id for it is 4 — the same tree FITTING_ROOT_GROUPS below hangs
# off of for the module browser — but a synthetic SDE slice in tests builds
# its own ids, and matching by name is what generalizes to that without
# special-casing test data.
SHIPS_MARKET_ROOT_NAME = "Ships"


async def _ship_class_map(db: AsyncSession, ship_type_ids) -> dict[int, str]:
    """{ship_type_id: class name} for the per-character import folders
    (T-069). The class is the name of the market-group ancestor directly
    under the Ships root, walking sde_market_groups.parent_group_id up from
    the ship's own market_group_id — e.g. a Rifter's market_group_id is
    already "Frigates"; a T3 cruiser's might sit a level or two deeper and
    has to walk up to find it. Falls back to the ship's SDE group name, then
    "Other", for hulls with no Ships-rooted market group (structures) —
    every ship has to land somewhere.
    """
    ship_type_ids = [t for t in set(ship_type_ids) if t]
    if not ship_type_ids:
        return {}

    groups = await sde.get_all_market_groups(db)
    by_id = {g["market_group_id"]: g for g in groups}
    root_id = next(
        (g["market_group_id"] for g in groups
         if g["parent_group_id"] is None and g["market_group_name"] == SHIPS_MARKET_ROOT_NAME),
        None,
    )

    rows = (await db.execute(
        select(SDEType.type_id, SDEType.market_group_id, SDEType.group_id)
        .where(SDEType.type_id.in_(ship_type_ids))
    )).fetchall()
    group_names = await _resolve_group_names(db, [r.group_id for r in rows if r.group_id])

    out: dict[int, str] = {}
    for r in rows:
        name = None
        if root_id is not None and r.market_group_id:
            name = _walk_market_group_to_ships_child(r.market_group_id, by_id, root_id)
        out[r.type_id] = name or group_names.get(r.group_id) or "Other"
    return out


def _walk_market_group_to_ships_child(start_id: int, by_id: dict, root_id: int) -> str | None:
    """Walk parent_group_id up from `start_id` until the node just below the
    Ships root, returning its name — or None if the chain never reaches
    `root_id` (a cycle guard stops an unexpected loop from hanging)."""
    cur = start_id
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        node = by_id.get(cur)
        if node is None:
            return None
        if node["parent_group_id"] == root_id:
            return node["market_group_name"]
        cur = node["parent_group_id"]
    return None


# Canonical ship-class display order (T-069 follow-up) — ESI's own fitting
# order has no relation to hull size, so "Other" (or any class alphabetically
# early) could land second in the character-import checklist. One list, used
# wherever ship classes are grouped for display: import_char_fittings sorts
# its response by this before returning, and the checklist dialog just
# preserves array order, so there's only one place that decides the order
# rather than a copy of this list living in the template's JS too.
SHIP_CLASS_ORDER = (
    "Frigates", "Destroyers", "Cruisers", "Battlecruisers", "Battleships",
    "Capital Ships", "Mining Barges", "Haulers", "Industrial Ships",
)


def _ship_class_sort_key(name: str) -> tuple:
    """(rank, tiebreak) for sorting ship-class groups: the known classes in
    SHIP_CLASS_ORDER first (in that order), then anything else
    alphabetically, then "Other" absolutely last regardless of where it
    would otherwise alphabetize."""
    if name == "Other":
        return (2, "")
    try:
        return (0, SHIP_CLASS_ORDER.index(name))
    except ValueError:
        return (1, name.lower())


async def _find_folder(
    db: AsyncSession, user_id: int, parent_id: int | None, name: str
) -> UserFittingFolder | None:
    """(user, parent, name) lookup with no create-if-missing side effect —
    for a caller that needs to know whether a folder already exists (e.g.
    reporting the character-import root folder's id) without creating it
    just to look. Never touches another user's folders."""
    name = (name or "Other")[:128]
    q = (
        select(UserFittingFolder)
        .where(UserFittingFolder.user_id == user_id)
        .where(UserFittingFolder.name == name)
    )
    q = q.where(UserFittingFolder.parent_id == parent_id) if parent_id is not None \
        else q.where(UserFittingFolder.parent_id.is_(None))
    return (await db.execute(q)).scalar_one_or_none()


async def _get_or_create_folder(
    db: AsyncSession, user_id: int, parent_id: int | None, name: str
) -> UserFittingFolder:
    """Find (user, parent, name) or create it. Shared by the
    character-import root/class folders below and available to any other
    caller that wants get-or-create semantics instead of create_folder's
    always-create endpoint."""
    existing = await _find_folder(db, user_id, parent_id, name)
    if existing:
        return existing
    folder = UserFittingFolder(user_id=user_id, parent_id=parent_id, name=(name or "Other")[:128])
    db.add(folder)
    await db.flush()  # assigns folder.id without committing the whole import
    return folder


def _normalize_fit_name(raw_name) -> str:
    """The one name normalization every already-imported/skip-rule check
    (and the save endpoint's own INSERT) must agree on — a fit whose ESI
    name is None, empty or all-whitespace stores as "Unnamed", same as a
    hand-saved fit's default. Used identically by import_char_save (what
    it stores and skips against), import_char_fittings' already_imported
    flag, and fittings_list's — three separate call sites that used to
    each roll this inline with a subtly different shape (one skipped the
    .strip()), which could make a fit "skip" from one endpoint's point of
    view and not the other's.
    """
    return (raw_name or "Unnamed").strip() or "Unnamed"


async def _already_imported_lookup(
    db: AsyncSession, user_id: int, character_id: int
) -> tuple[set[int], set[tuple[int, str]]]:
    """The two skip-rule sets (T-069), shared by the fittings-list GET
    (already_imported flag) and the save POST (what to skip):
    - source_fitting_ids already recorded for this (user, character) — a
      prior bulk import of the exact same in-game fit.
    - (ship_type_id, name) pairs among this user's fits that carry NO
      source columns at all — a fit saved earlier through the one-at-a-time
      "Import" path, before source tracking existed, or a hand-built fit
      that happens to match by name.
    """
    src_rows = (await db.execute(
        select(UserFitting.source_fitting_id)
        .where(UserFitting.user_id == user_id)
        .where(UserFitting.source_character_id == character_id)
        .where(UserFitting.source_fitting_id.isnot(None))
    )).scalars().all()
    null_rows = (await db.execute(
        select(UserFitting.ship_type_id, UserFitting.name)
        .where(UserFitting.user_id == user_id)
        .where(UserFitting.source_character_id.is_(None))
    )).all()
    return set(src_rows), {(sid, name) for sid, name in null_rows}


async def _prepare_bulk_import_cache(db: AsyncSession, raw_fittings: list[dict]) -> dict:
    """One batched round of SDE lookups covering every fit in
    `raw_fittings` — the whole point of this function is O(distinct types
    across the batch) queries, not O(fits). For the owner's 173 fits this is
    a handful of queries total; see _convert_esi_fit_items below for the
    per-fit (DB-free) conversion that consumes it.
    """
    all_type_ids: set[int] = set()
    for f in raw_fittings:
        if f.get("ship_type_id"):
            all_type_ids.add(f["ship_type_id"])
        for it in f.get("items", []):
            all_type_ids.add(it["type_id"])

    names = await sde.type_ids_to_names(db, list(all_type_ids))
    slots = await sde.get_module_slot_types_bulk(db, list(all_type_ids))
    weapon_type_ids = [tid for tid, slot in slots.items() if slot in ("high", "med")]
    compat = await sde.get_compatible_charges_bulk(db, weapon_type_ids)
    compat_ids = {tid: {c["type_id"] for c in chs} for tid, chs in compat.items()}
    return {"names": names, "slots": slots, "compat": compat_ids}


def _validate_bulk_fit(raw_fit: dict, cache: dict) -> dict | None:
    """Return what's unresolvable in `raw_fit`, else None.

    Checked before anything touches the DB session (validate-then-insert):
    a fit that fails here is simply skipped in the loop below, so it can
    never leave a half-written folder or fitting behind for the ones after
    it to trip over — there is nothing to roll back because nothing was
    written. import_eft tolerates an unresolved line by dropping it; this
    path can't silently drop part of a fit the way that would, since there
    is no user watching to notice a shorter item list, so an unresolvable
    type fails the whole fit instead.

    Returns {"unknown_ship": type_id | None, "unknown_items": [type_id,
    ...]} rather than a finished message — the caller resolves every
    unknown id across the whole batch in one bulk ESI names lookup and
    turns this into "Uses items no longer in the game: X, Y" / "Hull not
    in game data: Z" (real examples from a 173-fit account: 35 fits use
    unpublished/removed items, 8 use hulls the SDE never carried).
    """
    ship_type_id = raw_fit.get("ship_type_id")
    unknown_ship = ship_type_id if ship_type_id not in cache["names"] else None
    unknown_items: list[int] = []
    seen: set[int] = set()
    for it in raw_fit.get("items", []):
        tid = it.get("type_id")
        if tid not in cache["names"] and tid not in seen:
            seen.add(tid)
            unknown_items.append(tid)
    if unknown_ship is None and not unknown_items:
        return None
    return {"unknown_ship": unknown_ship, "unknown_items": unknown_items}


async def _resolve_unknown_type_names(db: AsyncSession, type_ids) -> dict[int, str]:
    """Best-effort display names for type ids missing from the local SDE —
    an item CCP later unpublished/removed, or a hull (GM/test ship) the SDE
    export never carried — via ESI's public /universe/names/, which can
    still resolve many of these even though sde_types can't. Public: no
    scope, no character token (ESIClient("", db=db), same as fitting_info's
    ESI description lookup above).

    One bulk POST for the whole batch (ESI's own cap is 1000 ids) on the
    happy path. But /universe/names/ 404s the WHOLE batch if even one id
    is entirely unknown to ESI (see app/routes/character_detail.py's
    _sender_names, the established pattern for this) — real accounts mix
    genuinely nameable removed items with a GM/test hull ESI has never
    heard of, and treating that 404 as "resolve nothing" would blank out
    every name in the batch over one bad id. On a 404, falls back to
    asking for each id alone (bounded concurrency); any other failure
    (network error, timeout) gives up and returns whatever was resolved
    so far — the caller falls back to "type <id>" per id that's still
    missing either way.
    """
    ids = sorted({int(t) for t in type_ids if t})
    if not ids:
        return {}
    client = ESIClient("", db=db)
    names: dict[int, str] = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        try:
            rows = await client.post_public("/universe/names/", chunk)
            for r in rows or []:
                if r.get("id") is not None and r.get("name"):
                    names[int(r["id"])] = r["name"]
            continue
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                logger.info("import-char save: /universe/names failed: %s", e)
                continue
        except Exception as e:
            logger.info("import-char save: /universe/names failed: %s", e)
            continue

        # 404 = at least one id in this chunk ESI can't name. Ask for each
        # alone rather than losing every name in the chunk over that one.
        sem = asyncio.Semaphore(3)

        async def _one(tid: int) -> tuple[int, str | None]:
            async with sem:
                try:
                    rows = await client.post_public("/universe/names/", [tid])
                    return tid, (rows[0].get("name") if rows else None)
                except Exception:
                    return tid, None

        for tid, name in await asyncio.gather(*[_one(t) for t in chunk]):
            if name:
                names[tid] = name
    return names


def _describe_unresolvable_fit(validation: dict, names: dict[int, str]) -> str:
    """{"unknown_ship", "unknown_items"} + resolved names -> the message
    shown for one failed fit. Falls back to "type <id>" per id ESI (or the
    fallback lookup itself) couldn't name either."""
    def _name(tid: int) -> str:
        return names.get(tid, f"type {tid}")

    parts = []
    if validation.get("unknown_ship"):
        parts.append(f"Hull not in game data: {_name(validation['unknown_ship'])}")
    if validation.get("unknown_items"):
        parts.append(
            "Uses items no longer in the game: "
            + ", ".join(_name(t) for t in validation["unknown_items"])
        )
    return "; ".join(parts)


def _convert_esi_fit_items(raw_items: list[dict], cache: dict) -> list[dict]:
    """ESI fitting items -> the fitting tool's saved-items shape, using the
    batched lookups _prepare_bulk_import_cache already ran for the whole
    request. Deliberately mirrors import_eft: same slot source (SDE module
    slot type, not the ESI flag), same cargo-charge autoload step — see
    tests/test_fitting_char_import.py's equivalence check.
    """
    items = []
    for it in raw_items:
        tid = it["type_id"]
        items.append({
            "type_id": tid,
            "type_name": cache["names"].get(tid, f"Type {tid}"),
            "slot": cache["slots"].get(tid, "cargo"),
            "quantity": it.get("quantity", 1) or 1,
        })
    return _autoload_cargo_charges(items, cache["compat"])


@router.get("/tools/fitting/import-character/characters")
async def import_char_list(request: Request, db: AsyncSession = Depends(get_db)):
    """Return the logged-in user's characters that have the fittings scope."""
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "characters": []}
    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    chars = []
    for c in r.scalars().all():
        if _FITTINGS_SCOPE in (c.scopes or ""):
            chars.append({"id": c.character_id, "name": c.character_name})
    return {"characters": chars}


@router.get("/tools/fitting/import-character/{character_id}/fittings")
async def import_char_fittings(
    request: Request, character_id: int, db: AsyncSession = Depends(get_db),
):
    """Return the character's in-game fittings as EFT strings + metadata."""
    # Local import avoids a circular dep between fitting.py and fittings.py
    from app.routes.fittings import _parse_fitting, _to_eft

    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "fittings": []}
    r = await db.execute(
        select(Character)
        .where(Character.character_id == character_id)
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found", "fittings": []}
    if _FITTINGS_SCOPE not in (char.scopes or ""):
        return {"error": "Fittings scope missing — re-authorize this character", "fittings": []}
    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await esi_char.get_fittings(client, character_id)
    except Exception as e:
        logger.warning("import-char fittings fetch failed: %s", e, exc_info=True)
        return {"error": f"ESI error: {type(e).__name__}", "fittings": []}

    if not raw:
        return {"fittings": []}

    all_ids: set[int] = set()
    for f in raw:
        all_ids.add(f["ship_type_id"])
        for item in f.get("items", []):
            all_ids.add(item["type_id"])
    type_names = await sde.type_ids_to_names(db, list(all_ids))
    ship_classes = await _ship_class_map(db, [f["ship_type_id"] for f in raw])
    already_src, already_saved_pairs = await _already_imported_lookup(db, user_id, character_id)

    fittings = []
    for f in sorted(raw, key=lambda x: (x.get("ship_type_id"), x.get("name", ""))):
        sid = f["ship_type_id"]
        ship_name = type_names.get(sid, f"Ship {sid}")
        parsed = _parse_fitting(f, type_names, ship_name, {})
        fid = f.get("fitting_id")
        name = _normalize_fit_name(f.get("name"))
        already_imported = fid in already_src or (sid, name) in already_saved_pairs
        fittings.append({
            "fitting_id": fid,
            "name": name,
            "ship_type_id": sid,
            "ship_name": ship_name,
            "eft": _to_eft(parsed),
            "ship_class": ship_classes.get(sid, "Other"),
            "module_count": parsed["total_modules"],
            "already_imported": already_imported,
        })
    # Canonical class order (see SHIP_CLASS_ORDER) rather than ESI's own
    # per-fit order — the checklist dialog groups by array order, so
    # deciding the order here is what keeps "Other" from landing wherever
    # ESI happened to put its first fit.
    fittings.sort(key=lambda fit: (_ship_class_sort_key(fit["ship_class"]), fit["ship_name"], fit["name"]))
    return {"fittings": fittings}


@router.post("/tools/fitting/import-character/{character_id}/save")
async def import_char_save(
    request: Request, character_id: int, db: AsyncSession = Depends(get_db),
):
    """Bulk-import a character's in-game fits into Saved Fits (T-069).

    Re-fetches the character's fittings from ESI itself and converts them
    server-side — the client only ever sends which fits to import, never
    fitting data, so a crafted body can't plant arbitrary items. Already-
    imported fits are skipped (never overwritten); a bad type in one fit
    fails only that fit (see _validate_bulk_fit).
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in"}
    r = await db.execute(
        select(Character)
        .where(Character.character_id == character_id)
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found"}
    if _FITTINGS_SCOPE not in (char.scopes or ""):
        return {"error": "Fittings scope missing — re-authorize this character"}

    try:
        body = await request.json()
    except Exception:
        body = {}
    want_all = bool(body.get("all"))
    requested_ids: set[int] | None = None
    if not want_all:
        requested_ids = set()
        raw_ids = body.get("fitting_ids") or []
        if isinstance(raw_ids, list):
            for v in raw_ids[:MAX_BULK_IMPORT_FITTING_IDS]:
                iv = _bounded_int(v)
                if iv is not None:
                    requested_ids.add(iv)

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        raw = await esi_char.get_fittings(client, character_id)
    except Exception as e:
        logger.warning("import-char save: ESI fetch failed for %s: %s", character_id, e, exc_info=True)
        return {"error": f"ESI error: {type(e).__name__}"}

    raw = raw or []
    failed: list[dict] = []
    if want_all:
        selected = raw
    else:
        raw_by_id = {f.get("fitting_id"): f for f in raw}
        selected = [raw_by_id[i] for i in requested_ids if i in raw_by_id]
        for missing_id in sorted(requested_ids - set(raw_by_id.keys())):
            failed.append({"name": f"Fitting {missing_id}", "reason": "Not found on character"})

    if not selected:
        return {"imported": 0, "skipped": 0, "failed": failed, "folder_id": None}

    cache = await _prepare_bulk_import_cache(db, selected)
    already_src, already_saved_pairs = await _already_imported_lookup(db, user_id, character_id)
    ship_classes = await _ship_class_map(db, [f.get("ship_type_id") for f in selected])

    # Only PEEK for the root folder here — don't create it. A request where
    # every fit turns out to be a skip or a failure (e.g. a re-run against
    # an already-imported character) has no reason to leave behind an empty
    # "<character> · in-game" folder. char_folder_id gets set for real, via
    # get-or-create, the first time a fit actually needs it below.
    existing_root = await _find_folder(db, user_id, None, f"{char.character_name} · in-game")
    char_folder_id: int | None = existing_root.id if existing_root else None

    imported = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    # One get-or-create per distinct class actually needed, not one per
    # fit — a 173-fit import might only touch half a dozen ship classes.
    sub_folder_ids: dict[str, int] = {}
    # Raw validation failures, named after one bulk ESI lookup resolves
    # every unknown type id across the whole batch at once — see below.
    unresolvable: list[dict] = []

    for f in selected:
        fid = f.get("fitting_id")
        name = _normalize_fit_name(f.get("name"))
        ship_type_id = f.get("ship_type_id")

        if fid is not None and fid in already_src:
            skipped += 1
            continue
        if (ship_type_id, name) in already_saved_pairs:
            skipped += 1
            continue

        validation = _validate_bulk_fit(f, cache)
        if validation:
            unresolvable.append({"name": name, **validation})
            continue

        items = _convert_esi_fit_items(f.get("items", []), cache)

        if char_folder_id is None:
            root = await _get_or_create_folder(db, user_id, None, f"{char.character_name} · in-game")
            char_folder_id = root.id
        class_name = ship_classes.get(ship_type_id, "Other")
        if class_name not in sub_folder_ids:
            sub_folder = await _get_or_create_folder(db, user_id, char_folder_id, class_name)
            sub_folder_ids[class_name] = sub_folder.id

        fitting = UserFitting(
            user_id=user_id,
            folder_id=sub_folder_ids[class_name],
            name=name[:255],
            description="",
            ship_type_id=int(ship_type_id),
            items_json=json.dumps(items),
            source_character_id=character_id,
            source_fitting_id=fid,
            created_at=now,
            updated_at=now,
        )
        db.add(fitting)
        imported += 1

    await db.commit()

    if unresolvable:
        unknown_ids: set[int] = set()
        for u in unresolvable:
            if u.get("unknown_ship"):
                unknown_ids.add(u["unknown_ship"])
            unknown_ids.update(u.get("unknown_items") or [])
        names = await _resolve_unknown_type_names(db, unknown_ids)
        for u in unresolvable:
            failed.append({"name": u["name"], "reason": _describe_unresolvable_fit(u, names)})

    return {"imported": imported, "skipped": skipped, "failed": failed, "folder_id": char_folder_id}


# ── Module browser endpoints ─────────────────────────────────────────────


# Root market group IDs relevant to ship fitting
FITTING_ROOT_GROUPS = {
    "modules": 9,      # Ship Equipment
    "rigs": 955,        # Ship Modifications
    "drones": 157,      # Drones
    "charges": 11,      # Ammunition & Charges
}


@router.get("/tools/fitting/browse/groups")
async def browse_groups(
    request: Request,
    parent: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get child market groups for the browser tree."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    if parent is None:
        # Return the top-level fitting categories
        groups = []
        for label, gid in FITTING_ROOT_GROUPS.items():
            children = await sde.get_market_group_children(db, gid)
            groups.append({
                "market_group_id": gid,
                "market_group_name": label.replace("_", " ").title(),
                "has_children": len(children) > 0,
            })
        return groups
    children = await sde.get_market_group_children(db, parent)
    return children


@router.get("/tools/fitting/browse/items/{market_group_id}")
async def browse_items(
    request: Request,
    market_group_id: int,
    ship_type_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get items in a market group with fit restriction info."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    items = await sde.get_market_group_items(db, market_group_id)

    # Check fit restrictions if a ship is selected
    if ship_type_id:
        for item in items:
            item["can_fit"] = await sde.can_module_fit_ship(
                db, item["type_id"], ship_type_id
            )
    else:
        for item in items:
            item["can_fit"] = True

    return items


@router.get("/tools/fitting/browse/path/{market_group_id}")
async def browse_path(
    request: Request,
    market_group_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get breadcrumb path for a market group."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    return await sde.get_market_group_path(db, market_group_id)


@router.get("/tools/fitting/check-fit")
async def check_module_fit(
    request: Request,
    module_type_id: int = Query(...),
    ship_type_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Check if a module can fit a specific ship."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    can_fit = await sde.can_module_fit_ship(db, module_type_id, ship_type_id)
    restrictions = await sde.get_module_fit_restrictions(db, module_type_id)
    return {"can_fit": can_fit, "restrictions": restrictions}


OVERLOAD_ATTR_IDS = [1210, 1205, 1223, 1208, 1230, 1231, 1206, 1222]


@router.get("/tools/fitting/can-overheat")
async def can_overheat(
    request: Request,
    type_ids: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    """Check which module types can be overheated."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    if not type_ids:
        return {}
    ids = [int(x) for x in type_ids.split(",") if x.strip()]
    if not ids:
        return {}
    result = await db.execute(
        select(SDETypeDogmaAttribute.type_id)
        .where(SDETypeDogmaAttribute.type_id.in_(ids))
        .where(SDETypeDogmaAttribute.attribute_id.in_(OVERLOAD_ATTR_IDS))
        .distinct()
    )
    overheatable = {row[0] for row in result.fetchall()}
    return {str(tid): tid in overheatable for tid in ids}


# ── Character skills + fit skill-check ──────────────────────────────────

_SKILLS_SCOPE = "esi-skills.read_skills.v1"
# Referenced by clone_implants() above too — defined here alongside
# _SKILLS_SCOPE since Python resolves module-level names at call time, not
# definition order, and this keeps the two scope constants together.
_IMPLANTS_SCOPE = "esi-clones.read_implants.v1"


async def _character_skills_map(db: AsyncSession, char: Character) -> dict[int, int]:
    """Return {skill_type_id: active_skill_level} for this character."""
    token = await refresh_token(char, db)
    client = ESIClient(token, db=db)
    data = await esi_char.get_skills(client, char.character_id)
    return {
        int(s["skill_id"]): int(s.get("active_skill_level", 0))
        for s in (data or {}).get("skills", [])
    }


@router.get("/tools/fitting/characters")
async def list_fitting_characters(request: Request, db: AsyncSession = Depends(get_db)):
    """Character-picker source, shared by the skill-check selector and the
    implant character picker: every character linked to this user, each
    tagged with the two scopes those pickers care about. One query backs
    both UIs instead of each running its own — see fetchFittingCharacters()
    in fitting_tool.html, which caches this response for both callers.

    Returning every character (not just scope-holders) lets the implant
    picker show characters missing the clones scope as a disabled option
    with a reason, rather than omitting them silently. The skill-check
    selector still only wants scope-holders, so it filters has_skills_scope
    client-side — this endpoint no longer pre-filters that for it.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"characters": []}
    r = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    return {
        "characters": [
            {
                "id": c.character_id,
                "name": c.character_name,
                "has_skills_scope": _SKILLS_SCOPE in (c.scopes or ""),
                "has_implants_scope": _IMPLANTS_SCOPE in (c.scopes or ""),
            }
            for c in r.scalars().all()
        ],
    }


@router.post("/tools/fitting/skill-check")
async def fit_skill_check(request: Request, db: AsyncSession = Depends(get_db)):
    """For a ship + list of items + character, return missing skills per type_id.

    Response shape:
        {
          "missing": {
            "<type_id>": [{"skill_id", "skill_name", "need", "have"}, ...],
            ...
          },
          "skills": {<skill_id>: level, ...}
        }

    A type_id only appears in `missing` if the character is short on at
    least one of its required skills.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Not logged in", "missing": {}}
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid request", "missing": {}}

    character_id = body.get("character_id")
    ship_type_id = body.get("ship_type_id")
    items = body.get("items", []) or []
    if not character_id:
        return {"missing": {}, "skills": {}}

    r = await db.execute(
        select(Character)
        .where(Character.character_id == int(character_id))
        .where(Character.user_id == user_id)
    )
    char = r.scalar_one_or_none()
    if not char:
        return {"error": "Character not found", "missing": {}}
    if _SKILLS_SCOPE not in (char.scopes or ""):
        return {
            "error": "Character is missing esi-skills.read_skills.v1 — re-authorize it.",
            "missing": {},
        }

    try:
        skills = await _character_skills_map(db, char)
    except Exception as e:
        logger.warning("skills fetch failed for char %s: %s", character_id, e)
        return {"error": f"Could not load skills: {type(e).__name__}", "missing": {}}

    # Collect every type_id whose requirements we need to check
    type_ids: set[int] = set()
    if ship_type_id:
        type_ids.add(int(ship_type_id))
    for item in items:
        tid = item.get("type_id")
        if tid:
            type_ids.add(int(tid))
        # Drones and charges are also items the char needs skills to use;
        # skip cargo since it doesn't affect "can I fly this" materially.
        if item.get("charge_type_id"):
            type_ids.add(int(item["charge_type_id"]))

    if not type_ids:
        return {"missing": {}, "skills": skills}

    req_rows = await db.execute(
        select(
            SDETypeSkillReq.type_id,
            SDETypeSkillReq.skill_type_id,
            SDETypeSkillReq.required_level,
        ).where(SDETypeSkillReq.type_id.in_(type_ids))
    )

    missing_by_type: dict[int, list[dict]] = {}
    missing_skill_ids: set[int] = set()
    for row in req_rows.fetchall():
        have = int(skills.get(row.skill_type_id, 0))
        if have < int(row.required_level):
            missing_by_type.setdefault(row.type_id, []).append({
                "skill_id": int(row.skill_type_id),
                "need": int(row.required_level),
                "have": have,
            })
            missing_skill_ids.add(row.skill_type_id)

    skill_names = await sde.type_ids_to_names(db, list(missing_skill_ids)) if missing_skill_ids else {}
    for tid, missing in missing_by_type.items():
        missing.sort(key=lambda m: (-m["need"], m["skill_id"]))
        for m in missing:
            m["skill_name"] = skill_names.get(m["skill_id"], f"Skill {m['skill_id']}")

    # Serialize keys as strings for JSON friendliness (JS uses type_id as key)
    return {
        "missing": {str(k): v for k, v in missing_by_type.items()},
        "skills_trained": len(skills),
    }


@router.get("/tools/fitting/charges/{module_type_id}")
async def get_charges(
    request: Request,
    module_type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get compatible charges for a module."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    return await sde.get_compatible_charges(db, module_type_id)


# ── Module info modal ────────────────────────────────────────────────────────

# unit_id → (suffix, optional value transform)
# Values are per CCP's dogmaUnits table.
_UNIT_FMT: dict[int, tuple[str, callable]] = {
    1:   (" m",      lambda v: f"{v:,.1f}"),          # length
    2:   (" kg",     lambda v: f"{v:,.0f}"),          # mass
    3:   (" s",      lambda v: f"{v:,.1f}"),          # time (seconds)
    9:   ("",        lambda v: f"{v:g}"),             # enum
    101: (" s",      lambda v: f"{v/1000:,.2f}"),     # milliseconds → s
    102: (" mm",     lambda v: f"{v:,.0f}"),          # mm
    104: (" AU",     lambda v: f"{v:,.2f}"),          # AU
    105: ("%",       lambda v: f"{v:+,.1f}"),         # percent
    106: (" tf",     lambda v: f"{v:,.0f}"),          # CPU
    107: (" MW",     lambda v: f"{v:,.0f}"),          # PG
    108: (" x",      lambda v: f"{v:,.3f}"),          # inverse-absolute-percent multiplier
    109: (" x",      lambda v: f"{v:,.3f}"),          # multiplier
    111: ("%",       lambda v: f"{v*100:+,.1f}"),     # inverse percent 0.05 → 5%
    113: (" HP",     lambda v: f"{v:,.0f}"),          # HP
    114: (" GJ",     lambda v: f"{v:,.2f}"),          # GJ
    119: ("",        lambda v: f"{int(v)}"),          # level
    120: ("",        lambda v: f"{int(v)}"),          # slot
    121: ("",        lambda v: f"{int(v)}"),          # item
    122: (" ISK",    lambda v: f"{v:,.2f}"),          # ISK
    123: ("%",       lambda v: f"{v:+,.1f}"),         # abs percent
    124: ("%",       lambda v: f"{(1-v)*100:+,.1f}"), # inverse resonance
    125: (" m³/hr",  lambda v: f"{v:,.0f}"),
    126: ("%",       lambda v: f"{v:+,.1f}"),         # speed %
    127: ("%",       lambda v: f"{v:+,.1f}"),
    128: ("%",       lambda v: f"{v:+,.1f}"),
    129: ("%",       lambda v: f"{v:+,.1f}"),
    130: (" rad/s",  lambda v: f"{v:,.3f}"),
    137: ("",        lambda v: "yes" if v else "no"), # bool
    139: ("",        lambda v: f"{v:,.0f}"),          # units
    140: ("",        lambda v: f"{int(v)}"),          # level
    141: ("",        lambda v: f"{int(v)}"),          # hardpoints
    143: ("",        lambda v: f"{v:g}"),             # datetime-ish
    204: (" rad/s²", lambda v: f"{v:,.4f}"),
}


def _format_attr(value: float, unit_id: int | None) -> str:
    fmt = _UNIT_FMT.get(unit_id or 0)
    if fmt:
        suffix, conv = fmt
        try:
            return f"{conv(value)}{suffix}"
        except Exception:
            pass
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


# Attributes we always hide even if CCP gave them a display_name
_INFO_ATTR_BLACKLIST = {
    182, 183, 184, 1285, 1289, 1290,      # required skill IDs (we don't resolve names here)
    277, 278, 279, 1286, 1287, 1288,      # required skill levels
    1768,                                   # typeColorScheme
    633,                                    # metaLevelOld (duplicate of 1692)
}


async def _resolve_type_names(db: AsyncSession, type_ids: list[int]) -> dict[int, str]:
    if not type_ids:
        return {}
    r = await db.execute(
        select(SDEType.type_id, SDEType.type_name).where(SDEType.type_id.in_(type_ids))
    )
    return {row.type_id: row.type_name for row in r.fetchall()}


async def _resolve_group_names(db: AsyncSession, group_ids: list[int]) -> dict[int, str]:
    if not group_ids:
        return {}
    r = await db.execute(
        select(SDEGroup.group_id, SDEGroup.group_name).where(SDEGroup.group_id.in_(group_ids))
    )
    return {row.group_id: row.group_name for row in r.fetchall()}


@router.get("/tools/fitting/info/{type_id}", response_class=HTMLResponse)
async def fitting_info(
    request: Request,
    type_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Show essential info about a ship/module/charge — modal body partial."""
    if not request.session.get("user_id"):   # ISS-044: login-only tool
        return HTMLResponse("", status_code=401)
    # Type + group
    t = (await db.execute(
        select(SDEType).where(SDEType.type_id == type_id)
    )).scalar_one_or_none()
    if not t:
        return HTMLResponse(
            "<div style='padding:1rem;color:var(--muted);font-size:11px;'>Type not found.</div>"
        )
    group_name = None
    if t.group_id:
        g = (await db.execute(
            select(SDEGroup.group_name).where(SDEGroup.group_id == t.group_id)
        )).scalar_one_or_none()
        group_name = g

    # Description — best-effort via public ESI (cached)
    description = ""
    try:
        esi = ESIClient("", db=db)
        info = await esi_universe.get_type(esi, type_id)
        description = (info or {}).get("description", "") or ""
    except Exception as e:
        logger.info("fitting_info: ESI description fetch failed for %d: %s", type_id, e)

    # Dogma attrs with display_name only — plus a few meta attrs collected for ref
    rows = (await db.execute(
        select(
            SDETypeDogmaAttribute.attribute_id,
            SDETypeDogmaAttribute.value,
            SDEDogmaAttribute.display_name,
            SDEDogmaAttribute.attribute_name,
            SDEDogmaAttribute.unit_id,
        )
        .join(SDEDogmaAttribute,
              SDEDogmaAttribute.attribute_id == SDETypeDogmaAttribute.attribute_id)
        .where(SDETypeDogmaAttribute.type_id == type_id)
    )).fetchall()

    meta_level = None
    for r in rows:
        if r.attribute_id == 1692:  # metaLevel
            meta_level = r.value

    # Collect group/type IDs referenced in unit_id 115/116 so we can resolve to names
    referenced_groups: list[int] = []
    referenced_types: list[int] = []
    for r in rows:
        if r.unit_id == 115:
            referenced_groups.append(int(r.value))
        elif r.unit_id == 116:
            referenced_types.append(int(r.value))
    group_map = await _resolve_group_names(db, referenced_groups)
    type_map = await _resolve_type_names(db, referenced_types)

    attrs = []
    for r in rows:
        if r.attribute_id in _INFO_ATTR_BLACKLIST:
            continue
        if not r.display_name:
            continue
        if r.value == 0:
            continue
        # Resolve IDs-as-values into names
        if r.unit_id == 115:
            value_display = group_map.get(int(r.value), f"Group {int(r.value)}")
        elif r.unit_id == 116:
            value_display = type_map.get(int(r.value), f"Type {int(r.value)}")
        else:
            value_display = _format_attr(r.value, r.unit_id)
        attrs.append({
            "label": r.display_name,
            "value_display": value_display,
            "_sort": r.display_name.lower(),
        })
    attrs.sort(key=lambda a: a["_sort"])

    return templates.TemplateResponse(request, "partials/fitting_info.html", {"type_id": type_id,
        "type_name": t.type_name,
        "group_name": group_name,
        "meta_level": meta_level,
        "volume": t.volume,
        "description": description,
        "attrs": attrs})
