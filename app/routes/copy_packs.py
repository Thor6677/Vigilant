"""Blueprint copy packs (/industry/copy-packs).

Pick a product, a quantity and the one character who will both copy and
build. The page lists which copies to make from the BPOs you own and how big
each should be. The planning itself is app/industry/copy_pack.py; this module
only gathers its inputs (SDE rows, the user's blueprints from ESI, the
character's synced skills) and renders the result.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms, status as perm_status
from app.character_skills import skill_levels, skill_summary
from app.db.models import AsyncSessionLocal, Character, CharacterDashboardCache, get_db
from app.esi import assets as esi_assets
from app.esi import character as esi_char
from app.esi import corporation as esi_corp
from app.esi.client import ESIClient, refresh_token
from app.industry import copy_pack as cp
from app.industry import copy_pack_view as view
from app.industry.copy_pack_form import parse_plan_form
from app.industry.manufacturing import RIGS, SEC_STATUS, STRUCTURES, format_time
from app.routes.blueprints import LOCATION_FLAGS, _fetch_all_pages
from app.routes.corporations import _build_office_to_structure_map, _try_api_call_with_fallback
from app.routes.industry_jobs import NPC_CORP_CEILING, _resolve_location_names
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

# _fetch_all_pages stops after 10 pages of 1000; a list that long may be cut off.
_PAGE_CAP = 10 * 1000

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
    request: Request, q: str = Query("", max_length=100), db: AsyncSession = Depends(get_db),
):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    q = q.strip()
    results = await sde.search_manufacturable_products(db, q) if len(q) >= 2 else []
    return templates.TemplateResponse(request, "partials/copy_pack_search.html", {
        "results": results, "q": q,
    })


_PLACE_FLAGS = frozenset({"Hangar", "Deliveries", "CorpDeliveries"})
# Corp hangar divisions: location_id is the office's item id or, for some
# structures, the structure id itself; either way it needs the office map.
_OFFICE_FLAGS = frozenset(f"CorpSAG{n}" for n in range(1, 8))
# Flags meaning "inside another item": location_id is that item's id (a ship
# for Cargo, a container for Unlocked/Locked), not a place. Only the owner's
# assets say what the item is and where it sits.
_INSIDE_ITEM_FLAGS = frozenset(
    {"Cargo", "Unlocked", "Locked", "AutoFit", "ShipHangar", "FleetHangar"})
_HOLDER_DEPTH = 6   # ship in a container in a ship... is as deep as it gets


def _inside_item(row: dict) -> bool:
    return row.get("location_flag") in _INSIDE_ITEM_FLAGS and bool(row.get("location_id"))


def resolve_holder(holder_id: int, assets_by_id: dict) -> tuple[int | None, int | None]:
    """(place id, type id of the item the blueprint is directly inside) from
    the owner's assets. Walks up through nested containers to the station or
    structure; the place is None when the item is in space or isn't in the
    assets (undocked and unlisted, or another owner's)."""
    first = assets_by_id.get(holder_id)
    if first is None:
        return None, None
    cur = first
    for _ in range(_HOLDER_DEPTH):
        kind = cur.get("location_type")
        if kind in ("station", "other"):
            return cur.get("location_id"), first.get("type_id")
        if kind != "item":
            break
        cur = assets_by_id.get(cur.get("location_id"))
        if cur is None:
            break
    return None, first.get("type_id")


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
            # location_id is a station or structure only for these flags. A
            # corp hangar item sits in an office, a container's id is an item
            # id and a ship's cargo is the ship, so everything else is
            # labelled by its flag instead of being sent for name lookup.
            location_id=row.get("location_id") if flag in _PLACE_FLAGS else None,
            location_label=LOCATION_FLAGS.get(flag, flag),
            reachable=reachable,
            holder_id=row.get("location_id") if flag in _INSIDE_ITEM_FLAGS else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _dead_token_ids(chars: list[Character]) -> set[int]:
    """Characters whose last sync found their authorization rejected. Own
    short session, like corporations._rank_for_scope."""
    try:
        async with AsyncSessionLocal() as dead_db:
            return await perm_status.dead_token_ids(
                dead_db, [c.character_id for c in chars])
    except Exception as exc:
        logger.info("Copy packs: dead-token lookup failed: %s", exc)
        return set()


async def _owned_blueprints(db: AsyncSession, chars: list[Character],
                            selected: Character) -> tuple[list, list, list, dict]:
    """(BPOs from every character and every readable corp, BPCs the selected
    character can use, notes for the page, {location_id: [characters whose
    token may resolve it]}).

    Sequential on purpose: refresh_token(char, db) commits on the request's
    session, which must never be shared by concurrent coroutines. (The ESI
    response cache writes through its own session.) The response cache makes
    a re-plan cheap anyway."""
    bpos: list[cp.OwnedBlueprint] = []
    bpcs: list[cp.OwnedBlueprint] = []
    notes: list[str] = []
    candidates: dict[int, list[Character]] = {}

    unlocated: dict[str, int] = {}

    def add(raw: list, owner: str, own: bool, resolvers: list[Character],
            office_map: dict[int, int] | None = None,
            assets_by_id: dict | None = None) -> None:
        for row in raw or []:
            ob = _owned(row, owner, reachable=own)
            if ob is None:
                continue
            if row.get("location_flag") in _OFFICE_FLAGS and office_map:
                lid = row.get("location_id")
                # ESI gives an office's item id for some hangars and the
                # structure's own id for others.
                structure_id = office_map.get(lid) or (
                    lid if lid in office_map.values() else None)
                if structure_id:
                    ob = dataclasses.replace(ob, location_id=structure_id)
            if ob.holder_id:
                place, holder_type = resolve_holder(ob.holder_id, assets_by_id or {})
                if holder_type:
                    ob = dataclasses.replace(ob, location_id=place, holder_type_id=holder_type)
                else:
                    unlocated[owner] = unlocated.get(owner, 0) + 1
            if ob.is_bpo:
                bpos.append(ob)
            elif own:
                bpcs.append(ob)
            else:
                continue
            if ob.location_id:
                known = candidates.setdefault(ob.location_id, [])
                known.extend(c for c in resolvers if c not in known)

    dead = await _dead_token_ids(chars)
    for char in chars:
        own = char.character_id == selected.character_id
        if perms.BLUEPRINTS not in (char.scopes or ""):
            if own:
                notes.append(f"{char.character_name} hasn't shared blueprints with "
                             "Vigilant, so their own BPOs and BPCs aren't counted.")
            continue
        if char.character_id in dead:
            notes.append(f"{char.character_name} needs to sign in again, so "
                         "their blueprints aren't counted.")
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
        if len(raw) >= _PAGE_CAP:
            notes.append(f"{char.character_name}'s blueprint list may be incomplete "
                         "(over 10,000 blueprints).")
        assets_by_id = await _char_assets_by_id(raw, char, token, db)
        add(raw, char.character_name, own, [char], assets_by_id=assets_by_id)

    corps: dict[int, list[Character]] = {}
    for c in chars:
        if c.corporation_id and c.corporation_id >= NPC_CORP_CEILING:
            corps.setdefault(c.corporation_id, []).append(c)

    async def fetch_corp(client, corp_id):
        return await _fetch_all_pages(esi_corp.get_corporation_blueprints, client, corp_id)

    for corp_id, members in sorted(corps.items()):
        holders = [c for c in members if perms.CORP_BLUEPRINTS in (c.scopes or "")]
        corp_name = members[0].corporation_name or f"Corp {corp_id}"
        if not holders:
            if corp_id == selected.corporation_id:
                notes.append(f"No character in {corp_name} shares corp blueprints, "
                             "so its hangars aren't counted.")
            continue
        raw, _err = await _try_api_call_with_fallback(
            "blueprints", {"blueprints": holders}, fetch_corp, corp_id, db)
        if raw is None:
            notes.append(f"{corp_name}'s blueprints couldn't be loaded.")
            continue
        if len(raw) >= _PAGE_CAP:
            notes.append(f"{corp_name}'s blueprint list may be incomplete "
                         "(over 10,000 blueprints).")
        office_map, asset_holders, assets_by_id = await _office_map(raw, members, corp_id, db)
        add(raw, corp_name, corp_id == selected.corporation_id,
            holders + [c for c in asset_holders if c not in holders], office_map,
            assets_by_id)

    bpos, bpcs = await _name_holders(db, bpos, bpcs)
    for owner, n in sorted(unlocated.items()):
        notes.append(f"{owner} has {n} blueprint{'s' if n != 1 else ''} inside a ship or "
                     "container that couldn't be located (assets not shared or not "
                     "readable), so they show as Cargo or the container flag.")
    return bpos, bpcs, notes, candidates


async def _char_assets_by_id(raw: list, char: Character, token: str,
                             db: AsyncSession) -> dict[int, dict]:
    """{item id: asset} for a character with blueprints inside ships or
    containers, else {} without a fetch. Best effort: no assets scope or a
    failed fetch leaves the blueprints labelled by their flag."""
    if not any(_inside_item(r) for r in raw or []):
        return {}
    if perms.ASSETS not in (char.scopes or ""):
        return {}
    try:
        assets = await esi_assets.get_character_assets(
            ESIClient(token, db=db), char.character_id)
    except Exception as exc:
        logger.info("Copy packs: assets for %s failed: %s", char.character_id, exc)
        return {}
    return {a["item_id"]: a for a in assets or [] if "item_id" in a}


async def _name_holders(db: AsyncSession, bpos: list, bpcs: list) -> tuple[list, list]:
    """Fill in holder_label (the ship or container's type name) in one lookup."""
    ids = {o.holder_type_id for o in bpos + bpcs if o.holder_type_id}
    if not ids:
        return bpos, bpcs
    try:
        names = await sde.type_ids_to_names(db, list(ids))
    except Exception as exc:
        logger.info("Copy packs: holder names failed: %s", exc)
        return bpos, bpcs

    def label(o):
        name = names.get(o.holder_type_id) if o.holder_type_id else None
        return dataclasses.replace(o, holder_label=name) if name else o

    return [label(o) for o in bpos], [label(o) for o in bpcs]


async def _office_map(raw: list, members: list[Character], corp_id: int,
                      db: AsyncSession) -> tuple[dict[int, int], list[Character], dict[int, dict]]:
    """({office item id: structure id}, characters that could read the corp's
    assets, {item id: asset}) for a corp with blueprints sitting in hangar
    offices (BPOs) or inside ships and containers. Corp assets are the only
    place an office is tied to its structure and a ship to its location. Best
    effort: with no asset scope or a failed fetch the blueprints keep their
    hangar-division or flag label."""
    wants_offices = any(r.get("quantity") != -2 and r.get("location_flag") in _OFFICE_FLAGS
                        for r in raw or [])
    wants_holders = any(_inside_item(r) for r in raw or [])
    if not (wants_offices or wants_holders):
        return {}, [], {}
    holders = [c for c in members if perms.CORP_ASSETS in (c.scopes or "")]
    if not holders:
        return {}, [], {}
    assets, _err = await _try_api_call_with_fallback(
        "assets", {"assets": holders}, esi_corp.get_corporation_assets, corp_id, db)
    by_id = {a["item_id"]: a for a in assets or [] if "item_id" in a}
    return _build_office_to_structure_map(assets or []), holders, by_id


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
                          candidates: dict[int, list[Character]]) -> dict[int, str]:
    """Names for the locations the results show: the BPO of each Copy row, each
    owned BPC used and each BPO in a picker. Best effort; a failure just leaves flag labels."""
    ids: set[int] = set()
    for ip in plan.group(cp.GROUP_COPY):
        if ip.bpo is not None and ip.bpo.location_id:
            ids.add(ip.bpo.location_id)
    for ip in plan.bpc_items():
        ids |= {o.location_id for o, _n in ip.bpcs_used if o.location_id}
    # BPOs offered in a picker are told apart by where they sit.
    for ip in plan.items.values():
        if len(ip.bpo_options) > 1:
            ids |= {o.location_id for o in ip.bpo_options if o.location_id}
    if not ids:
        return {}
    try:
        return await _resolve_location_names(
            db, ids, {i: list(candidates.get(i, [])) for i in ids})
    except Exception as exc:
        logger.info("Copy packs: location names failed: %s", exc)
        return {}


def _message(request: Request, text: str):
    """An early-exit message that also clears the left column."""
    return templates.TemplateResponse(request, "partials/copy_pack_message.html",
                                      {"message": text})


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
        return _message(request, "Pick a character.")

    rows = await sde.get_blueprint_tree(db, form.type_id, max_depth=cp.MAX_DEPTH)
    if form.type_id not in rows:
        return _message(request, "That item has no manufacturing blueprint.")
    blueprints = {t: cp.blueprint_from_row(r) for t, r in rows.items()}

    bpos, bpcs, notes, candidates = await _owned_blueprints(db, chars, selected)
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
    names = await sde.type_ids_to_names(db, sorted(type_ids))
    location_names = await _location_names(db, plan, candidates)
    collect = view.group_bpcs(plan, location_names, names)
    return templates.TemplateResponse(request, "partials/copy_pack_plan.html", {
        "plan": plan,
        "blueprints": blueprints,
        "names": names,
        "location_names": location_names,
        "copy_loaded": await sde.blueprint_copy_data_loaded(db),
        "notes": notes,
        "skill_rows": skill_rows,
        "ignored": form.ignore_bpcs,
        "product_id": form.type_id,
        "structure": form.structure,
        "rig": form.rig,
        "security": form.security,
        "character_name": selected.character_name,
        "copy_items": plan.group(cp.GROUP_COPY),
        "elsewhere_items": plan.group(cp.GROUP_ELSEWHERE),
        "nocopy_items": plan.group(cp.GROUP_NOCOPY),
        "pack": view.build_pack(plan),
        "collect": collect,
        "collect_total": sum(len(g.rows) for g in collect),
    })
