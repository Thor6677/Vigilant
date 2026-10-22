"""Blueprint copy packs (/industry/copy-packs).

Pick a product, a quantity and the one character who will both copy and
build. The page lists which copies to make from the BPOs you own and how big
each should be. The planning itself is app/industry/copy_pack.py; this module
only gathers its inputs (SDE rows, the user's blueprints from ESI, the
character's synced skills) and renders the result.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.character_skills import skill_levels, skill_summary
from app.db.models import Character, CharacterDashboardCache, get_db
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.esi.client import ESIClient, refresh_token
from app.industry import copy_pack as cp
from app.industry.copy_pack_form import parse_plan_form
from app.industry.manufacturing import RIGS, SEC_STATUS, STRUCTURES, format_time
from app.routes.blueprints import LOCATION_FLAGS, _fetch_all_pages
from app.routes.corporations import _try_api_call_with_fallback
from app.routes.industry_jobs import NPC_CORP_CEILING, _resolve_location_names
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["copy-packs"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_time


async def _user_characters(db: AsyncSession, user_id: int) -> list[Character]:
    rows = await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )
    return list(rows.scalars().all())


@router.get("/industry/copy-packs", response_class=HTMLResponse)
async def copy_packs_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    chars = await _user_characters(db, user_id)
    return templates.TemplateResponse(request, "copy_packs.html", {
        "characters": [{"id": c.character_id, "name": c.character_name} for c in chars],
        "structures": STRUCTURES,
        "rigs": RIGS,
        "sec_statuses": SEC_STATUS,
    })


@router.get("/industry/copy-packs/search", response_class=HTMLResponse)
async def copy_packs_search(
    request: Request, q: str = Query(""), db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    q = q.strip()
    results = await sde.search_manufacturable_products(db, q) if len(q) >= 2 else []
    return templates.TemplateResponse(request, "partials/copy_pack_search.html", {
        "results": results, "q": q,
    })


def _owned(row: dict, owner_name: str, reachable: bool) -> cp.OwnedBlueprint | None:
    """One ESI blueprint row. Quantity -2 is a copy; -1 is an original, and
    a positive quantity is a stack of unresearched originals."""
    try:
        is_bpc = row.get("quantity") == -2
        flag = row.get("location_flag") or ""
        return cp.OwnedBlueprint(
            item_id=int(row["item_id"]),
            blueprint_type_id=int(row["type_id"]),
            is_bpo=not is_bpc,
            me=int(row.get("material_efficiency") or 0),
            te=int(row.get("time_efficiency") or 0),
            runs=int(row.get("runs") or 0) if is_bpc else None,
            owner_name=owner_name,
            # A corp hangar item sits in an office, whose id is neither a
            # station nor a structure, so it is labelled by division instead.
            location_id=None if flag.startswith("CorpSAG") else row.get("location_id"),
            location_label=LOCATION_FLAGS.get(flag, flag),
            reachable=reachable,
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _owned_blueprints(db: AsyncSession, chars: list[Character],
                            selected: Character) -> tuple[list, list, list]:
    """(BPOs from every character and every readable corp, BPCs the selected
    character can use, notes for the page).

    Sequential on purpose: each ESIClient writes the response cache through
    `db`, and one AsyncSession must never be shared by concurrent
    coroutines. The response cache makes a re-plan cheap anyway."""
    bpos: list[cp.OwnedBlueprint] = []
    bpcs: list[cp.OwnedBlueprint] = []
    notes: list[str] = []

    def add(raw: list, owner: str, own: bool) -> None:
        for row in raw or []:
            ob = _owned(row, owner, reachable=own)
            if ob is None:
                continue
            if ob.is_bpo:
                bpos.append(ob)
            elif own:
                bpcs.append(ob)

    for char in chars:
        own = char.character_id == selected.character_id
        if perms.BLUEPRINTS not in (char.scopes or ""):
            if own:
                notes.append(f"{char.character_name} hasn't shared blueprints with "
                             "Vigilant, so their own BPOs and BPCs aren't counted.")
            continue
        try:
            token = await refresh_token(char, db)
            raw = await _fetch_all_pages(esi_char.get_blueprints,
                                         ESIClient(token, db=db), char.character_id)
        except Exception as exc:
            logger.warning("Copy packs: blueprints for %s failed: %s",
                           char.character_id, exc)
            notes.append(f"Couldn't load {char.character_name}'s blueprints.")
            continue
        add(raw, char.character_name, own)

    corps: dict[int, list[Character]] = {}
    for c in chars:
        if c.corporation_id and c.corporation_id >= NPC_CORP_CEILING:
            corps.setdefault(c.corporation_id, []).append(c)

    async def fetch_corp(client, corp_id):
        return await _fetch_all_pages(esi_corp.get_corporation_blueprints, client, corp_id)

    for corp_id, members in sorted(corps.items()):
        holders = [c for c in members if perms.CORP_BLUEPRINTS in (c.scopes or "")]
        if not holders:
            continue
        corp_name = members[0].corporation_name or f"Corp {corp_id}"
        raw, _err = await _try_api_call_with_fallback(
            "blueprints", {"blueprints": holders}, fetch_corp, corp_id, db)
        if raw is None:
            notes.append(f"{corp_name}'s blueprints couldn't be read by any of "
                         "your characters there.")
            continue
        add(raw, corp_name, corp_id == selected.corporation_id)
    return bpos, bpcs, notes


async def _skills(db: AsyncSession, char: Character, overrides: dict):
    """(planner skill levels, trained levels for the build-skill check or
    None when there are none, rows for the Skills panel). A manual value
    always wins; with no synced skills every field defaults to 5."""
    cache = await db.get(CharacterDashboardCache, char.character_id)
    trained = skill_levels(skill_summary(char, cache)) or None
    levels, rows = {}, []
    for sid, label in cp.PLANNER_SKILLS:
        synced = trained.get(sid, 0) if trained is not None else None
        fallback = synced if synced is not None else 5
        levels[sid] = overrides.get(sid, fallback)
        rows.append({
            "id": sid,
            "label": label,
            "override": overrides.get(sid),
            "placeholder": fallback,
            "source": "from ESI" if sid not in overrides and synced is not None else "manual",
        })
    return levels, trained, rows


def _facility(form) -> cp.Facility:
    s, r, sec = STRUCTURES[form.structure], RIGS[form.rig], SEC_STATUS[form.security]
    return cp.Facility(struct_mat=s["mat"], struct_time=s["time"],
                       rig_mat=r["mat"], rig_time=r["time"], sec_mult=sec["mult"])


async def _location_names(db: AsyncSession, plan: cp.Plan,
                          chars: list[Character]) -> dict[int, str]:
    """Names for the locations the results show: each Copy row's BPO and
    each owned BPC used. Best effort; a failure just leaves flag labels."""
    ids: set[int] = set()
    for ip in plan.items.values():
        if ip.bpo is not None and ip.bpo.location_id:
            ids.add(ip.bpo.location_id)
        ids |= {o.location_id for o, _n in ip.bpcs_used if o.location_id}
    if not ids:
        return {}
    try:
        return await _resolve_location_names(db, ids, {i: list(chars) for i in ids})
    except Exception as exc:
        logger.info("Copy packs: location names failed: %s", exc)
        return {}


@router.post("/industry/copy-packs/plan", response_class=HTMLResponse)
async def copy_packs_plan(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    form = parse_plan_form(await request.form())
    if not form.type_id:
        return HTMLResponse('<div class="b-section"><div class="b-empty">'
                            'Pick a product to plan.</div></div>')

    chars = await _user_characters(db, user_id)
    selected = next((c for c in chars if c.character_id == form.character_id), None)
    if selected is None:
        if form.character_id:
            return HTMLResponse("Character not found", status_code=404)
        return HTMLResponse('<div class="b-section"><div class="b-empty">'
                            'Pick a character.</div></div>')

    rows = await sde.get_blueprint_tree(db, form.type_id, max_depth=cp.MAX_DEPTH)
    if form.type_id not in rows:
        return HTMLResponse('<div class="b-section"><div class="b-empty">'
                            'That item has no manufacturing blueprint.</div></div>')
    blueprints = {t: cp.blueprint_from_row(r) for t, r in rows.items()}

    bpos, bpcs, notes = await _owned_blueprints(db, chars, selected)
    levels, trained, skill_rows = await _skills(db, selected, form.skill_overrides)
    if trained is None:
        notes.append("No synced skills for this character, so skill levels are "
                     "manual and the per-item build-skill check is skipped.")

    # CPU-bound (the split search re-runs the scheduler), so keep it off the
    # event loop.
    plan = await asyncio.to_thread(cp.plan, cp.PlanInput(
        product_type_id=form.type_id,
        quantity=form.quantity,
        blueprints=blueprints,
        bpos=tuple(bpos),
        bpcs=tuple(bpcs),
        build=form.build,
        bpo_choice=form.bpo_choice,
        me_override=form.me_override,
        te_override=form.te_override,
        ignore_bpcs=form.ignore_bpcs,
        skills=levels,
        trained=trained,
        facility=_facility(form),
    ))

    type_ids = {r.product_type_id for r in plan.tree}
    type_ids |= {sid for ip in plan.items.values() for sid, _lvl in ip.missing_skills}
    return templates.TemplateResponse(request, "partials/copy_pack_plan.html", {
        "plan": plan,
        "blueprints": blueprints,
        "names": await sde.type_ids_to_names(db, sorted(type_ids)),
        "location_names": await _location_names(db, plan, chars),
        "copy_loaded": await sde.blueprint_copy_data_loaded(db),
        "notes": notes,
        "skill_rows": skill_rows,
        "ignored": form.ignore_bpcs,
        "product_id": form.type_id,
        "character_name": selected.character_name,
        "copy_items": plan.group(cp.GROUP_COPY),
        "elsewhere_items": plan.group(cp.GROUP_ELSEWHERE),
        "nocopy_items": plan.group(cp.GROUP_NOCOPY),
        "bpc_items": plan.bpc_items(),
    })
