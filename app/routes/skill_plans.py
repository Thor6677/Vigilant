"""Skill plan management — create, edit, import, and analyze skill plans."""

import html
import json
import logging
import re
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import selectinload

from app.db.models import get_db, Character, SkillPlan, SkillPlanEntry, SkillPlanACL
from app.esi.client import refresh_token
from app.esi import character as esi_char
from app.sde import lookup as sde
from app.routes import skill_plan_perms as perms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skill-plans", tags=["skill-plans"])
templates = Jinja2Templates(directory="app/templates")

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
ROMAN_REV = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

SP_CUMULATIVE = [0, 250, 1415, 8000, 45255, 256000]


# ── Permission helpers ───────────────────────────────────────────────────────

async def _load_plan_for(db: AsyncSession, plan_id: int, ident: perms.Identities,
                          required: str):
    """Load a plan and confirm the user has the required permission.

    `required` is one of "view" | "edit" | "admin". Returns the plan with
    entries + acl_entries eager-loaded, or None if the plan doesn't exist or
    the user lacks the required permission.
    """
    result = await db.execute(
        select(SkillPlan)
        .where(SkillPlan.id == plan_id)
        .options(selectinload(SkillPlan.entries),
                 selectinload(SkillPlan.acl_entries))
    )
    plan = result.scalar_one_or_none()
    if plan is None:
        return None
    checker = {"view": perms.can_view, "edit": perms.can_edit, "admin": perms.can_admin}[required]
    if not checker(plan, ident):
        return None
    return plan


def _touch_edit(plan: SkillPlan, user_id: int) -> None:
    """Record who last edited the plan and when. Called before commit on mutations."""
    plan.last_edited_by_user_id = user_id
    plan.last_edited_at = datetime.now(timezone.utc)
    plan.updated_at = datetime.now(timezone.utc)


def _sp_for_level(level: int, rank: float) -> int:
    if level < 0 or level > 5:
        return 0
    return int(SP_CUMULATIVE[level] * rank)


def _training_time_minutes(sp: int, primary: float, secondary: float) -> float:
    rate = primary + secondary / 2
    if rate <= 0:
        return float("inf")
    return sp / rate


def _format_duration(minutes: float) -> str:
    if minutes <= 0:
        return "done"
    if minutes == float("inf"):
        return "—"
    total_secs = int(minutes * 60)
    days = total_secs // 86400
    hours = (total_secs % 86400) // 3600
    mins = (total_secs % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# ── Skill injector calculator ─────────────────────────────────────────────────

import math

# Large Skill Injector SP yield depends on the character's CURRENT total SP.
_LSI_BRACKETS = [
    (5_000_000,  500_000),   # <5M SP   → 500k per injector
    (50_000_000, 400_000),   # 5–50M SP → 400k per injector
    (80_000_000, 300_000),   # 50–80M SP → 300k per injector
    (None,       150_000),   # >80M SP  → 150k per injector
]
_SSI_SP = 100_000  # Small Skill Injector — always 100k SP

_LSI_TYPE_ID = 40520
_SSI_TYPE_ID = 45635
_FORGE_REGION = 10000002  # Jita is in The Forge

# Cached Jita sell prices (refreshed every 30 minutes)
_injector_price_cache: dict = {}
_injector_price_ts: datetime | None = None


async def _get_injector_prices() -> tuple[float, float]:
    """Return (large_price, small_price) from Jita lowest sell orders.
    Cached for 30 minutes to avoid hammering ESI on repeated gap checks."""
    global _injector_price_cache, _injector_price_ts
    import httpx

    now = datetime.now(timezone.utc)
    if _injector_price_ts and (now - _injector_price_ts).total_seconds() < 1800:
        return _injector_price_cache.get("large", 0), _injector_price_cache.get("small", 0)

    prices = {"large": 0.0, "small": 0.0}
    async with httpx.AsyncClient(timeout=15) as client:
        for key, type_id in [("large", _LSI_TYPE_ID), ("small", _SSI_TYPE_ID)]:
            try:
                resp = await client.get(
                    f"https://esi.evetech.net/latest/markets/{_FORGE_REGION}/orders/",
                    params={"type_id": type_id, "order_type": "sell"},
                )
                if resp.status_code == 200:
                    orders = resp.json()
                    if orders:
                        prices[key] = min(o["price"] for o in orders)
            except Exception:
                pass

    _injector_price_cache = prices
    _injector_price_ts = now
    return prices["large"], prices["small"]


def _lsi_yield(current_sp: int) -> int:
    for ceiling, sp in _LSI_BRACKETS:
        if ceiling is None or current_sp < ceiling:
            return sp
    return 150_000


def _calc_injectors(sp_needed: int, char_total_sp: int,
                    large_price: float = 0, small_price: float = 0) -> dict:
    """Calculate injector options for covering `sp_needed`.

    Returns three options:
      - large_only: all Large Skill Injectors
      - small_only: all Small Skill Injectors
      - optimal: cheapest mix of large + small (greedy per-injection decision)
    """
    if sp_needed <= 0:
        return {
            "large_only": {"large": 0, "cost": 0},
            "small_only": {"small": 0, "cost": 0},
            "optimal": {"large": 0, "small": 0, "cost": 0},
            "char_sp": char_total_sp,
        }

    # ── Large-only option ────────────────────────────────────────────────
    remaining = sp_needed
    current_sp = char_total_sp
    large_count = 0
    while remaining > 0:
        remaining -= _lsi_yield(current_sp)
        current_sp += _lsi_yield(char_total_sp + (sp_needed - remaining - _lsi_yield(current_sp)))
        large_count += 1
    # Recompute cleanly
    remaining = sp_needed
    current_sp = char_total_sp
    large_count = 0
    while remaining > 0:
        yld = _lsi_yield(current_sp)
        large_count += 1
        current_sp += yld
        remaining -= yld
    large_only_cost = large_count * large_price

    # ── Small-only option ────────────────────────────────────────────────
    small_count = math.ceil(sp_needed / _SSI_SP)
    small_only_cost = small_count * small_price

    # ── Optimal mix (greedy) ─────────────────────────────────────────────
    # At each step: compare ISK/SP of one large vs one small. Pick the
    # cheaper option. This accounts for bracket transitions correctly
    # because we track current_sp as we go.
    remaining = sp_needed
    current_sp = char_total_sp
    opt_large = 0
    opt_small = 0
    if large_price > 0 and small_price > 0:
        while remaining > 0:
            lsi_yld = _lsi_yield(current_sp)
            # How much SP we'd actually USE (don't overshoot for cost comparison)
            lsi_effective = min(lsi_yld, remaining)
            ssi_effective = min(_SSI_SP, remaining)

            lsi_cost_per_sp = large_price / lsi_effective if lsi_effective > 0 else float("inf")
            ssi_cost_per_sp = small_price / ssi_effective if ssi_effective > 0 else float("inf")

            if lsi_cost_per_sp <= ssi_cost_per_sp:
                opt_large += 1
                current_sp += lsi_yld
                remaining -= lsi_yld
            else:
                opt_small += 1
                current_sp += _SSI_SP
                remaining -= _SSI_SP
    elif large_price > 0:
        opt_large = large_count
    elif small_price > 0:
        opt_small = small_count

    opt_cost = opt_large * large_price + opt_small * small_price

    return {
        "large_only":  {"large": large_count, "cost": large_only_cost},
        "small_only":  {"small": small_count, "cost": small_only_cost},
        "optimal":     {"large": opt_large, "small": opt_small, "cost": opt_cost},
        "char_sp":     char_total_sp,
        "large_price": large_price,
        "small_price": small_price,
    }


# ── List all plans ───────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_plans(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)

    # Pre-filter at the DB layer so we don't load every plan in the system
    # into memory. Anything that survives this filter still goes through
    # perms.can_view to handle edge cases (e.g. a corp role check the SQL
    # can't do).
    custom_plan_ids: set[int] = set()
    if ident.character_ids or ident.corp_ids or ident.alliance_ids:
        acl_clauses = []
        if ident.character_ids:
            acl_clauses.append(and_(SkillPlanACL.subject_type == "character",
                                    SkillPlanACL.subject_id.in_(ident.character_ids)))
        if ident.corp_ids:
            acl_clauses.append(and_(SkillPlanACL.subject_type == "corporation",
                                    SkillPlanACL.subject_id.in_(ident.corp_ids)))
        if ident.alliance_ids:
            acl_clauses.append(and_(SkillPlanACL.subject_type == "alliance",
                                    SkillPlanACL.subject_id.in_(ident.alliance_ids)))
        if acl_clauses:
            custom_plan_ids = set((await db.execute(
                select(SkillPlanACL.plan_id).where(or_(*acl_clauses))
            )).scalars().all())

    plan_filters = [SkillPlan.user_id == user_id]
    if ident.corp_ids:
        plan_filters.append(and_(
            SkillPlan.visibility == "corporation",
            SkillPlan.owner_corp_id.in_(ident.corp_ids),
        ))
    if ident.alliance_ids:
        plan_filters.append(and_(
            SkillPlan.visibility == "alliance",
            SkillPlan.owner_alliance_id.in_(ident.alliance_ids),
        ))
    if custom_plan_ids:
        plan_filters.append(SkillPlan.id.in_(custom_plan_ids))

    query = (
        select(SkillPlan)
        .where(or_(*plan_filters))
        .options(
            selectinload(SkillPlan.entries),
            selectinload(SkillPlan.acl_entries),
        )
    )
    all_plans = (await db.execute(query)).scalars().all()
    visible = [p for p in all_plans if perms.can_view(p, ident)]

    # Group by scope for the template
    groups = {"personal": [], "corporation": [], "alliance": [], "custom": []}
    for p in visible:
        vis = p.visibility or "personal"
        # "My Plans" section = ANY plan I own, regardless of scope, goes there.
        # Otherwise scope determines the bucket.
        if p.user_id == user_id:
            groups["personal"].append(p)
        else:
            groups.setdefault(vis, []).append(p)

    for k in groups:
        groups[k].sort(key=lambda p: p.updated_at, reverse=True)

    # Calculate SP and training time for each plan
    all_skill_ids = set()
    for p in visible:
        for e in p.entries:
            all_skill_ids.add(e.skill_type_id)
    skill_infos = await sde.get_skill_infos(db, list(all_skill_ids)) if all_skill_ids else {}

    plan_stats = {}
    editable = {}
    for p in visible:
        total_sp = 0
        total_mins = 0.0
        for e in p.entries:
            info = skill_infos.get(e.skill_type_id, {})
            rank = info.get("rank", 1.0)
            sp = _sp_for_level(e.target_level, rank)
            total_sp += sp
            total_mins += _training_time_minutes(sp, 20, 20)
        plan_stats[p.id] = {
            "total_sp": total_sp,
            "time_str": _format_duration(total_mins) if total_mins > 0 else "—",
        }
        editable[p.id] = perms.can_edit(p, ident)

    # Resolve corp/alliance names for scope badges
    corp_ids = {p.owner_corp_id for p in visible if p.owner_corp_id}
    alliance_ids = {p.owner_alliance_id for p in visible if p.owner_alliance_id}
    corp_names, alliance_names = await _resolve_org_names(db, corp_ids, alliance_ids)

    # Eligibility for create form (corps + alliances where user has the required role)
    eligible_corps = perms.eligible_corps_for_create(ident)
    eligible_alliances = perms.eligible_alliances_for_create(ident)
    user_corp_ids = ident.corp_ids
    user_alliance_ids = ident.alliance_ids

    # Get user's characters for the character selector
    char_result = await db.execute(
        select(Character).where(Character.user_id == user_id).order_by(Character.character_name)
    )
    characters = char_result.scalars().all()
    eligible_corp_options = [
        {"id": cid, "name": corp_names.get(cid, f"Corp {cid}")}
        for cid in sorted(eligible_corps)
    ]
    eligible_alliance_options = [
        {"id": aid, "name": alliance_names.get(aid, f"Alliance {aid}")}
        for aid in sorted(eligible_alliances)
    ]

    return templates.TemplateResponse(request, "skill_plans.html", {"groups": groups,
        "plan_stats": plan_stats,
        "editable": editable,
        "characters": characters,
        "corp_names": corp_names,
        "alliance_names": alliance_names,
        "eligible_corps": eligible_corp_options,
        "eligible_alliances": eligible_alliance_options})


async def _resolve_org_names(db: AsyncSession, corp_ids: set[int], alliance_ids: set[int]) -> tuple[dict, dict]:
    """Pull corp + alliance display names from the Character table (cheap
    cache — every character entry has name pairs kept fresh by sync)."""
    corp_names: dict[int, str] = {}
    alliance_names: dict[int, str] = {}
    if not corp_ids and not alliance_ids:
        return corp_names, alliance_names
    char_rows = (await db.execute(
        select(Character).where(or_(
            Character.corporation_id.in_(corp_ids) if corp_ids else False,
            Character.alliance_id.in_(alliance_ids) if alliance_ids else False,
        ))
    )).scalars().all()
    for c in char_rows:
        if c.corporation_id and c.corporation_id in corp_ids and c.corporation_name:
            corp_names.setdefault(c.corporation_id, c.corporation_name)
        if c.alliance_id and c.alliance_id in alliance_ids and c.alliance_name:
            alliance_names.setdefault(c.alliance_id, c.alliance_name)
    return corp_names, alliance_names


# ── Create plan ──────────────────────────────────────────────────────────────

@router.post("/create", response_class=HTMLResponse)
async def create_plan(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    name = (form.get("name") or "").strip() or "New Skill Plan"
    scope = (form.get("visibility") or "personal").strip()
    target_id_raw = (form.get("target_id") or "").strip()

    plan = SkillPlan(user_id=user_id, name=name, visibility="personal")

    if scope in ("corporation", "alliance"):
        ident = await perms.resolve_identities(db, user_id)
        eligible = (perms.eligible_corps_for_create(ident) if scope == "corporation"
                    else perms.eligible_alliances_for_create(ident))
        try:
            target_id = int(target_id_raw)
        except ValueError:
            target_id = 0
        if target_id in eligible:
            plan.visibility = scope
            if scope == "corporation":
                plan.owner_corp_id = target_id
            else:
                plan.owner_alliance_id = target_id
        # else: silently fall back to personal — safer than 400ing
    elif scope == "custom":
        # Custom scope = owner is admin, ACL starts empty and is filled in
        # from the plan detail page after creation.
        plan.visibility = "custom"

    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    return RedirectResponse(f"/skill-plans/{plan.id}", status_code=302)


# ── View / edit plan ─────────────────────────────────────────────────────────

@router.get("/{plan_id}", response_class=HTMLResponse)
async def view_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "view")
    if plan is None:
        return RedirectResponse("/skill-plans", status_code=302)

    # Resolve skill names
    skill_ids = [e.skill_type_id for e in plan.entries]
    skill_names = await sde.type_ids_to_names(db, skill_ids) if skill_ids else {}
    skill_infos = await sde.get_skill_infos(db, skill_ids) if skill_ids else {}

    entries = []
    for e in plan.entries:
        info = skill_infos.get(e.skill_type_id, {})
        entries.append({
            "id": e.id,
            "skill_type_id": e.skill_type_id,
            "skill_name": skill_names.get(e.skill_type_id, f"Skill {e.skill_type_id}"),
            "target_level": e.target_level,
            "rank": info.get("rank", 1.0),
            "primary_attr_name": info.get("primary_attr_name", "?"),
            "secondary_attr_name": info.get("secondary_attr_name", "?"),
        })

    # Get characters for gap analysis selector
    char_result = await db.execute(
        select(Character).where(Character.user_id == user_id).order_by(Character.character_name)
    )
    characters = char_result.scalars().all()

    # Scope-display context
    corp_names, alliance_names = await _resolve_org_names(
        db,
        {plan.owner_corp_id} if plan.owner_corp_id else set(),
        {plan.owner_alliance_id} if plan.owner_alliance_id else set(),
    )

    # Promote eligibility (only meaningful for personal plans owned by user)
    eligible_corps = perms.eligible_corps_for_create(ident)
    eligible_alliances = perms.eligible_alliances_for_create(ident)
    corp_more_names, alliance_more_names = await _resolve_org_names(
        db, eligible_corps, eligible_alliances,
    )
    corp_names.update(corp_more_names)
    alliance_names.update(alliance_more_names)
    eligible_corp_options = [
        {"id": cid, "name": corp_names.get(cid, f"Corp {cid}")}
        for cid in sorted(eligible_corps)
    ]
    eligible_alliance_options = [
        {"id": aid, "name": alliance_names.get(aid, f"Alliance {aid}")}
        for aid in sorted(eligible_alliances)
    ]

    can_edit = perms.can_edit(plan, ident)
    can_admin = perms.can_admin(plan, ident)
    is_owner = (plan.user_id == user_id)

    # ACL entries for the custom-scope editor (sorted for stable UI)
    acl_entries = sorted(plan.acl_entries or [],
                         key=lambda e: (e.subject_type, e.subject_name.lower()))
    acl_err = request.query_params.get("acl_err")

    return templates.TemplateResponse(request, "skill_plan_detail.html", {"plan": plan,
        "entries": entries,
        "characters": characters,
        "corp_names": corp_names,
        "alliance_names": alliance_names,
        "can_edit": can_edit,
        "can_admin": can_admin,
        "is_owner": is_owner,
        "eligible_corps": eligible_corp_options,
        "eligible_alliances": eligible_alliance_options,
        "acl_entries": acl_entries,
        "acl_err": acl_err})


# ── Delete plan ──────────────────────────────────────────────────────────────

@router.post("/{plan_id}/delete", response_class=HTMLResponse)
async def delete_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if plan:
        await db.delete(plan)
        await db.commit()

    return RedirectResponse("/skill-plans", status_code=302)


# ── Clear all skills from plan ───────────────────────────────────────────────

@router.post("/{plan_id}/clear", response_class=HTMLResponse)
async def clear_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if plan:
        for entry in list(plan.entries):
            await db.delete(entry)
        _touch_edit(plan, user_id)
        await db.commit()

    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Duplicate plan (always forks to the current user's personal scope) ───────

@router.post("/{plan_id}/duplicate", response_class=HTMLResponse)
async def duplicate_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    source = await _load_plan_for(db, plan_id, ident, "view")
    if not source:
        return RedirectResponse("/skill-plans", status_code=302)

    # Always fork into a personal plan under the current user — viewers of a
    # shared plan get their own copy without needing edit access on the source.
    new_plan = SkillPlan(
        user_id=user_id,
        name=f"{source.name} (Copy)",
        visibility="personal",
    )
    db.add(new_plan)
    await db.flush()

    for e in source.entries:
        db.add(SkillPlanEntry(
            plan_id=new_plan.id,
            skill_type_id=e.skill_type_id,
            target_level=e.target_level,
            sort_order=e.sort_order,
        ))

    await db.commit()
    return RedirectResponse(f"/skill-plans/{new_plan.id}", status_code=302)


# ── Rename plan ──────────────────────────────────────────────────────────────

@router.post("/{plan_id}/rename", response_class=HTMLResponse)
async def rename_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return HTMLResponse("", status_code=400)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    plan.name = name
    _touch_edit(plan, user_id)
    await db.commit()
    return HTMLResponse(f'<span class="b-label">{html.escape(name)}</span>')


# ── Add skill to plan ────────────────────────────────────────────────────────

async def _apply_skills_with_prereqs(
    db: AsyncSession,
    plan: SkillPlan,
    targets: list[tuple[int, int]],
) -> tuple[int, int]:
    """For each (skill_id, required_level) in `targets`, expand the full
    transitive prerequisite chain and apply it to `plan`:

      - skills not already in the plan are inserted at their required level
        in training order (prereqs before dependents);
      - skills already in the plan at a lower level are upgraded;
      - skills at or above the required level are left alone (no downgrade).

    Returns (added, upgraded). Caller commits."""
    existing = {e.skill_type_id: e for e in plan.entries}
    max_order = max((e.sort_order for e in plan.entries), default=-1)

    req_lvl: dict[int, int] = {}
    req_order: list[int] = []
    for sid, lvl in targets:
        chain = await _resolve_prereq_chain(db, sid, lvl)
        for csid, clvl in chain:
            if req_lvl.get(csid, 0) < clvl:
                req_lvl[csid] = clvl
            if csid not in req_order:
                req_order.append(csid)

    added = 0
    upgraded = 0
    for sid in req_order:
        lvl = req_lvl[sid]
        e = existing.get(sid)
        if e is None:
            max_order += 1
            db.add(SkillPlanEntry(
                plan_id=plan.id,
                skill_type_id=sid,
                target_level=lvl,
                sort_order=max_order,
            ))
            added += 1
        elif lvl > e.target_level:
            e.target_level = lvl
            upgraded += 1
    return added, upgraded


async def _resolve_prereq_chain(
    db: AsyncSession,
    root_skill_id: int,
    root_level: int,
) -> list[tuple[int, int]]:
    """Return (skill_id, required_level) pairs for the full transitive prereq
    chain of `root_skill_id`, in training order (prereqs before dependents),
    deduplicated at the highest required level seen across the tree. The
    root skill itself is the final entry.

    Example: adding Gallente Battleship V returns roughly
        [(Spaceship Cmd, 4), (Gallente Frigate, 3), (Gallente Destroyer, 3),
         (Gallente Cruiser, 3), (Gallente Battleship, 5)]
    (exact ordering depends on DFS traversal of SDETypeSkillReq)."""
    from app.db.sde_models import SDETypeSkillReq

    seen_level: dict[int, int] = {}
    ordered: list[int] = []
    in_ordered: set[int] = set()
    stack: set[int] = set()

    async def visit(sid: int, lvl: int) -> None:
        if sid in stack:
            # Defensive cycle guard; skill prereq graph is a DAG in practice.
            return
        stack.add(sid)
        try:
            rows = (await db.execute(
                select(
                    SDETypeSkillReq.skill_type_id,
                    SDETypeSkillReq.required_level,
                ).where(SDETypeSkillReq.type_id == sid)
            )).all()
            for req_sid, req_lvl in rows:
                await visit(req_sid, req_lvl)
            if seen_level.get(sid, 0) < lvl:
                seen_level[sid] = lvl
            if sid not in in_ordered:
                ordered.append(sid)
                in_ordered.add(sid)
        finally:
            stack.remove(sid)

    await visit(root_skill_id, root_level)
    return [(sid, seen_level[sid]) for sid in ordered]


@router.post("/{plan_id}/add-skill", response_class=HTMLResponse)
async def add_skill(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    skill_type_id = form.get("skill_type_id", "")
    target_level = form.get("target_level", "1")

    try:
        skill_type_id = int(skill_type_id)
        target_level = int(target_level)
        if target_level < 1 or target_level > 5:
            target_level = 1
    except ValueError:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid skill or level.</div>')

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    # Shared helper expands the prereq chain and applies all adds/upgrades.
    added, upgraded = await _apply_skills_with_prereqs(
        db, plan, [(skill_type_id, target_level)]
    )

    if added == 0 and upgraded == 0:
        name = await sde.type_id_to_name(db, skill_type_id) or f"Skill {skill_type_id}"
        return HTMLResponse(
            f'<div class="b-empty" style="color:var(--warn);">'
            f'{name} {ROMAN_REV.get(target_level, "")} '
            f'and all prerequisites are already in the plan at or above that level.'
            f'</div>'
        )

    _touch_edit(plan, user_id)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Remove skill from plan ───────────────────────────────────────────────────

@router.post("/{plan_id}/remove-skill/{entry_id}", response_class=HTMLResponse)
async def remove_skill(plan_id: int, entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    entry = next((e for e in plan.entries if e.id == entry_id), None)
    if entry:
        await db.delete(entry)
        _touch_edit(plan, user_id)
        await db.commit()

    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Sort skills ──────────────────────────────────────────────────────────────

@router.post("/{plan_id}/sort", response_class=HTMLResponse)
async def sort_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    mode = form.get("mode", "name")  # "name", "optimal", "level"

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan or not plan.entries:
        return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    skill_ids = [e.skill_type_id for e in plan.entries]
    skill_names = await sde.type_ids_to_names(db, skill_ids)
    skill_infos = await sde.get_skill_infos(db, skill_ids)

    entries = list(plan.entries)

    if mode == "name":
        entries.sort(key=lambda e: skill_names.get(e.skill_type_id, ""))
    elif mode == "optimal":
        # Group by primary/secondary attr pair for remap efficiency
        def attr_key(e):
            info = skill_infos.get(e.skill_type_id, {})
            return (info.get("primary_attr", 0), info.get("secondary_attr", 0),
                    skill_names.get(e.skill_type_id, ""))
        entries.sort(key=attr_key)
    elif mode == "level":
        entries.sort(key=lambda e: (e.target_level, skill_names.get(e.skill_type_id, "")))

    for i, e in enumerate(entries):
        e.sort_order = i

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Reorder skills (drag-and-drop) ──────────────────────────────────────────

@router.post("/{plan_id}/reorder", response_class=HTMLResponse)
async def reorder_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    order_json = form.get("order", "[]")
    try:
        entry_ids = json.loads(order_json)
    except (json.JSONDecodeError, TypeError):
        return HTMLResponse("", status_code=400)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    entry_map = {e.id: e for e in plan.entries}
    for i, eid in enumerate(entry_ids):
        if eid in entry_map:
            entry_map[eid].sort_order = i

    _touch_edit(plan, user_id)
    await db.commit()
    return HTMLResponse("")


# ── Share / unshare plan (admin-only — changes plan-level access) ────────────

@router.post("/{plan_id}/share", response_class=HTMLResponse)
async def toggle_share(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return HTMLResponse("", status_code=404)

    if plan.share_token:
        plan.share_token = None
    else:
        plan.share_token = secrets.token_urlsafe(12)

    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Promote plan to corp/alliance scope ──────────────────────────────────────

@router.post("/{plan_id}/promote", response_class=HTMLResponse)
async def promote_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Move a plan from personal scope to corporation or alliance scope.
    Requires admin on the source (owner) AND the user must have permission
    to create plans in the target scope (via eligibility helpers)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    target_scope = (form.get("visibility") or "").strip()
    target_id_raw = (form.get("target_id") or "").strip()
    if target_scope not in ("corporation", "alliance"):
        return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return RedirectResponse("/skill-plans", status_code=302)

    try:
        target_id = int(target_id_raw)
    except ValueError:
        return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    eligible = (perms.eligible_corps_for_create(ident) if target_scope == "corporation"
                else perms.eligible_alliances_for_create(ident))
    if target_id not in eligible:
        return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    plan.visibility = target_scope
    if target_scope == "corporation":
        plan.owner_corp_id = target_id
        plan.owner_alliance_id = None
    else:
        plan.owner_alliance_id = target_id
        plan.owner_corp_id = None
    _touch_edit(plan, user_id)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Demote back to personal scope ────────────────────────────────────────────

@router.post("/{plan_id}/demote", response_class=HTMLResponse)
async def demote_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Return a shared plan to personal scope. Admin only."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return RedirectResponse("/skill-plans", status_code=302)

    plan.visibility = "personal"
    plan.owner_corp_id = None
    plan.owner_alliance_id = None
    _touch_edit(plan, user_id)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Shared plan view (public, read-only) ─────────────────────────────────────

@router.get("/shared/{share_token}", response_class=HTMLResponse)
async def shared_plan(share_token: str, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SkillPlan)
        .where(SkillPlan.share_token == share_token)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse('<div class="b-empty">Plan not found or sharing disabled.</div>', status_code=404)

    skill_ids = [e.skill_type_id for e in plan.entries]
    skill_names = await sde.type_ids_to_names(db, skill_ids) if skill_ids else {}
    skill_infos = await sde.get_skill_infos(db, skill_ids) if skill_ids else {}

    entries = []
    for e in plan.entries:
        info = skill_infos.get(e.skill_type_id, {})
        entries.append({
            "id": e.id,
            "skill_type_id": e.skill_type_id,
            "skill_name": skill_names.get(e.skill_type_id, f"Skill {e.skill_type_id}"),
            "target_level": e.target_level,
            "rank": info.get("rank", 1.0),
            "primary_attr_name": info.get("primary_attr_name", "?"),
            "secondary_attr_name": info.get("secondary_attr_name", "?"),
        })

    # Get viewer's characters for gap analysis (if logged in)
    characters = []
    viewer_id = request.session.get("user_id")
    if viewer_id:
        char_result = await db.execute(
            select(Character).where(Character.user_id == viewer_id).order_by(Character.character_name)
        )
        characters = char_result.scalars().all()

    return templates.TemplateResponse(request, "skill_plan_shared.html", {"plan": plan,
        "entries": entries,
        "characters": characters,
        "share_token": share_token})


# ── Import from EVE text ────────────────────────────────────────────────────

@router.post("/{plan_id}/import", response_class=HTMLResponse)
async def import_skills(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    text = form.get("skill_text", "").strip()
    if not text:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">No skill text provided.</div>')

    # Parse lines like "Skill Name I" or "Skill Name 3"
    not_found = []
    targets: list[tuple[int, int]] = []

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        match = re.match(r'^(.+?)\s+(I{1,3}V?|IV|V|[1-5])$', line)
        if not match:
            not_found.append(line)
            continue

        skill_name = match.group(1).strip()
        level_str = match.group(2)
        level = ROMAN.get(level_str) or int(level_str)

        skill_id = await sde.type_name_to_id(db, skill_name)
        if not skill_id:
            not_found.append(line)
            continue

        targets.append((skill_id, level))

    added, upgraded = await _apply_skills_with_prereqs(db, plan, targets)

    _touch_edit(plan, user_id)
    await db.commit()

    msg = f"Imported {added} skill(s) (including prereqs)."
    if upgraded:
        msg += f" Upgraded {upgraded}."
    if not_found:
        safe_unresolved = ", ".join(html.escape(n) for n in not_found[:5])
        msg += f" Could not resolve: {safe_unresolved}"
        if len(not_found) > 5:
            msg += f" (+{len(not_found) - 5} more)"
    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">{msg}</div>')


# ── Import from EFT fitting ─────────────────────────────────────────────────

def _parse_eft_text(text: str) -> tuple[str | None, list[str]]:
    """Parse EFT fitting text. Returns (ship_name, list_of_module_names).
    EFT format:
      [Ship Name, Fitting Name]
      Module Name
      Module Name
      ...
      Drone Name x5
    """
    lines = text.strip().splitlines()
    if not lines:
        return None, []

    # First line should be [Ship, Name]
    first = lines[0].strip()
    ship_name = None
    if first.startswith("[") and "]" in first:
        inner = first[1:first.index("]")]
        parts = inner.split(",", 1)
        ship_name = parts[0].strip()

    modules = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("["):
            continue
        # Strip "x5" quantity suffix and "[T2]" style offline markers
        name = re.sub(r'\s+x\d+$', '', line)
        name = re.sub(r'\s+\[.*?\]$', '', name)
        # Skip empty slots
        if name.startswith("[Empty ") or name.lower() == "[empty]":
            continue
        if name:
            modules.append(name)

    return ship_name, modules


@router.post("/{plan_id}/from-fitting", response_class=HTMLResponse)
async def import_from_fitting(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    fitting_text = form.get("fitting_text", "").strip()
    if not fitting_text:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Paste a fitting first.</div>')

    ship_name, module_names = _parse_eft_text(fitting_text)

    # Resolve all item names to type_ids
    all_names = list(set(module_names))
    if ship_name:
        all_names.append(ship_name)

    resolved: dict[str, int] = {}
    not_found = []
    for name in all_names:
        type_id = await sde.type_name_to_id(db, name)
        if type_id:
            resolved[name] = type_id
        else:
            not_found.append(name)

    if not resolved:
        msg = "Could not resolve any items."
        if not_found:
            safe_unresolved = ", ".join(html.escape(n) for n in not_found[:5])
            msg += f" Not found: {safe_unresolved}"
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">{msg}</div>')

    # Get all unique type_ids to look up skill requirements
    all_type_ids = list(set(resolved.values()))

    # Gather all skill requirements (recursive) for every item
    needed: dict[int, int] = {}  # skill_type_id -> max required level
    for type_id in all_type_ids:
        reqs = await sde.get_full_skill_tree(db, type_id)
        for r in reqs:
            sid = r["skill_type_id"]
            lvl = r["required_level"]
            if lvl > needed.get(sid, 0):
                needed[sid] = lvl

    if not needed:
        return HTMLResponse('<div class="b-empty" style="color:var(--warn);">No skill requirements found for these items.</div>')

    # Apply via the shared helper so the skill-on-skill prereq chain is
    # expanded for anything get_full_skill_tree didn't already cover.
    added, upgraded = await _apply_skills_with_prereqs(
        db, plan, list(needed.items())
    )

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()

    items_resolved = len(resolved)
    msg = f"Added {added} skill(s) from {items_resolved} item(s)."
    if upgraded:
        msg += f" Upgraded {upgraded}."
    if not_found:
        safe_unresolved = ", ".join(html.escape(n) for n in not_found[:5])
        msg += f" Not found: {safe_unresolved}"
        if len(not_found) > 5:
            msg += f" (+{len(not_found) - 5} more)"
    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">{msg}</div>')


# ── Generate from ship / mastery ─────────────────────────────────────────────

@router.post("/{plan_id}/from-ship", response_class=HTMLResponse)
async def add_from_ship(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "edit")
    if not plan:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    ship_type_id = form.get("ship_type_id", "")
    mastery_level = form.get("mastery_level", "")
    mode = form.get("mode", "requirements")  # "requirements" or "mastery"

    try:
        ship_type_id = int(ship_type_id)
    except ValueError:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid ship.</div>')

    skills = []
    if mode == "mastery" and mastery_level:
        try:
            mastery_level = int(mastery_level)
        except ValueError:
            return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid mastery level.</div>')
        skills = await sde.get_mastery_skills(db, ship_type_id, mastery_level)
    else:
        # Just the direct + recursive skill requirements
        skills = await sde.get_full_skill_tree(db, ship_type_id)

    if not skills:
        return HTMLResponse('<div class="b-empty" style="color:var(--warn);">No skills found for this ship.</div>')

    targets = [(s["skill_type_id"], s["required_level"]) for s in skills]
    added, upgraded = await _apply_skills_with_prereqs(db, plan, targets)

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()

    ship_name = await sde.type_id_to_name(db, ship_type_id) or f"Ship {ship_type_id}"
    extra = f", upgraded {upgraded}" if upgraded else ""
    return HTMLResponse(
        f'<div class="b-empty" style="color:var(--success);">'
        f'Added {added} skill(s) from {ship_name}{extra}.</div>'
    )


# ── Skill search (for add-skill typeahead) ───────────────────────────────────

@router.get("/search/skills", response_class=HTMLResponse)
async def search_skills_api(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return HTMLResponse("")

    results = await sde.search_skills(db, q, limit=10)
    if not results:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No skills found.</div>')

    from html import escape
    html = []
    for r in results:
        safe_name = escape(r["type_name"], quote=True)
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'data-id="{r["type_id"]}" data-name="{safe_name}" '
            f'onclick="selectSkill(+this.dataset.id, this.dataset.name)">'
            f'{safe_name}</div>'
        )
    return HTMLResponse("".join(html))


# ── Ship search (for from-ship typeahead) ────────────────────────────────────

@router.get("/search/ships", response_class=HTMLResponse)
async def search_ships_api(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return HTMLResponse("")

    from app.db.sde_models import SDEType, SDEGroup
    # Ships are in category 6 (Ship)
    result = await db.execute(
        select(SDEType.type_id, SDEType.type_name)
        .join(SDEGroup, SDEType.group_id == SDEGroup.group_id)
        .where(SDEGroup.category_id == 6)
        .where(SDEType.published == True)
        .where(func.lower(SDEType.type_name).contains(q.lower()))
        .order_by(SDEType.type_name)
        .limit(10)
    )
    rows = result.fetchall()
    if not rows:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No ships found.</div>')

    from html import escape
    html = []
    for r in rows:
        safe_name = escape(r.type_name, quote=True)
        html.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'data-id="{r.type_id}" data-name="{safe_name}" '
            f'onclick="selectShip(+this.dataset.id, this.dataset.name)">'
            f'{safe_name}</div>'
        )
    return HTMLResponse("".join(html))


# ── Gap analysis (htmx partial) ──────────────────────────────────────────────

@router.get("/{plan_id}/gap/{character_id}", response_class=HTMLResponse)
async def gap_analysis(plan_id: int, character_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    # View permission: owner, any scope that grants access, or anyone with a
    # valid share_token on the plan (read-only public URL flow).
    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "view")
    if plan is None:
        # Fall back to share_token lookup — a viewer may arrive here from a
        # shared link rather than scope membership.
        result = await db.execute(
            select(SkillPlan)
            .where(SkillPlan.id == plan_id, SkillPlan.share_token.isnot(None))
            .options(selectinload(SkillPlan.entries))
        )
        plan = result.scalar_one_or_none()
    if not plan or not plan.entries:
        return HTMLResponse('<div class="b-empty">No skills in this plan.</div>')

    # Verify character ownership
    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Character not found.</div>')

    # Fetch character's trained skills from ESI
    from app.esi.client import ESIClient
    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        skills_data = await esi_char.get_skills(client, character_id)
        char_total_sp = skills_data.get("total_sp", 0) if isinstance(skills_data, dict) else 0
        trained = {s["skill_id"]: s.get("active_skill_level", 0) for s in skills_data.get("skills", [])}

        # Fetch attributes for training time calculation
        attrs_data = await esi_char.get_attributes(client, character_id)
        char_attrs = [
            attrs_data.get("charisma", 17),
            attrs_data.get("intelligence", 17),
            attrs_data.get("memory", 17),
            attrs_data.get("perception", 17),
            attrs_data.get("willpower", 17),
        ]
        # Get implant bonuses
        implants = [0, 0, 0, 0, 0]
        try:
            implant_ids = await esi_char.get_implants(client, character_id)
            if implant_ids:
                for imp_id in implant_ids:
                    imp_info = await sde.get_skill_info(db, imp_id)  # Won't match, that's ok
                    # Implant attribute bonuses are in dogma attrs 175-179
                    # We'd need typeDogma for this, skip for now — use ESI attrs which include implants
        except Exception:
            pass
    except Exception as e:
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">Failed to fetch skills: {html.escape(str(e))}</div>')

    # Build gap analysis
    skill_ids = [e.skill_type_id for e in plan.entries]
    skill_names = await sde.type_ids_to_names(db, skill_ids)
    skill_infos = await sde.get_skill_infos(db, skill_ids)

    ATTR_INDEX = {164: 0, 165: 1, 166: 2, 167: 3, 168: 4}

    rows = []
    total_sp_needed = 0
    total_time_mins = 0
    completed = 0

    for e in plan.entries:
        current_level = trained.get(e.skill_type_id, 0)
        info = skill_infos.get(e.skill_type_id, {})
        rank = info.get("rank", 1.0)

        if current_level >= e.target_level:
            rows.append({
                "skill_name": skill_names.get(e.skill_type_id, f"Skill {e.skill_type_id}"),
                "target_level": e.target_level,
                "current_level": current_level,
                "completed": True,
                "sp_needed": 0,
                "time_str": "done",
            })
            completed += 1
            continue

        sp_needed = _sp_for_level(e.target_level, rank) - _sp_for_level(current_level, rank)
        primary_idx = ATTR_INDEX.get(info.get("primary_attr", 165), 1)
        secondary_idx = ATTR_INDEX.get(info.get("secondary_attr", 166), 2)
        time_mins = _training_time_minutes(sp_needed, char_attrs[primary_idx], char_attrs[secondary_idx])

        total_sp_needed += sp_needed
        total_time_mins += time_mins

        rows.append({
            "skill_name": skill_names.get(e.skill_type_id, f"Skill {e.skill_type_id}"),
            "target_level": e.target_level,
            "current_level": current_level,
            "completed": False,
            "sp_needed": sp_needed,
            "time_str": _format_duration(time_mins),
        })

    injectors = None
    if total_sp_needed > 0:
        large_price, small_price = await _get_injector_prices()
        injectors = _calc_injectors(total_sp_needed, char_total_sp, large_price, small_price)

    return templates.TemplateResponse(request, "partials/skill_plan_gap.html", {"rows": rows,
        "total_sp": total_sp_needed,
        "total_time": _format_duration(total_time_mins),
        "completed": completed,
        "total": len(plan.entries),
        "char": char,
        "char_total_sp": char_total_sp,
        "injectors": injectors})


# ── Export as text ───────────────────────────────────────────────────────────

@router.get("/{plan_id}/export", response_class=HTMLResponse)
async def export_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "view")
    if not plan:
        return HTMLResponse("", status_code=404)

    skill_ids = [e.skill_type_id for e in plan.entries]
    skill_names = await sde.type_ids_to_names(db, skill_ids) if skill_ids else {}

    lines = []
    for e in plan.entries:
        name = skill_names.get(e.skill_type_id, f"Skill {e.skill_type_id}")
        lines.append(f"{name} {ROMAN_REV.get(e.target_level, str(e.target_level))}")

    from html import escape
    text = escape("\n".join(lines))
    return HTMLResponse(
        '<div style="display:flex;gap:0.4rem;align-items:flex-start;">'
        f'<textarea id="export-text" readonly style="flex:1;width:100%;height:150px;background:var(--bg);color:var(--text);'
        f'border:1px solid var(--border);font-family:inherit;font-size:10px;padding:0.5rem;"'
        f' onclick="this.select()">{text}</textarea>'
        '<button type="button" onclick="copyExportText(this)" class="b-btn"'
        ' style="padding:0.4rem 0.8rem;border:1px solid var(--accent);background:var(--bg);color:var(--accent);font-size:11px;cursor:pointer;white-space:nowrap;">'
        'Copy'
        '</button>'
        '</div>'
        '<div style="font-size:9px;color:var(--muted);margin-top:0.25rem;">Copy to clipboard and paste into EVE skill plans.</div>'
    )


# ── Ship Browser ─────────────────────────────────────────────────────────────

@router.get("/ship/{ship_type_id}", response_class=HTMLResponse)
async def ship_detail(ship_type_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    ship_name = await sde.type_id_to_name(db, ship_type_id)
    if not ship_name:
        return RedirectResponse("/skill-plans", status_code=302)

    # Get ship group info
    from app.db.sde_models import SDEType, SDEGroup
    type_result = await db.execute(
        select(SDEType.group_id).where(SDEType.type_id == ship_type_id)
    )
    group_id = type_result.scalar_one_or_none()
    group_name = None
    if group_id:
        grp = await db.execute(select(SDEGroup.group_name).where(SDEGroup.group_id == group_id))
        group_name = grp.scalar_one_or_none()

    # Direct skill requirements
    direct_reqs = await sde.get_skill_requirements(db, ship_type_id)

    # Full recursive skill tree
    full_tree = await sde.get_full_skill_tree(db, ship_type_id)

    # Mastery data
    mastery_data = await sde.get_ship_mastery(db, ship_type_id)

    # Get user's characters
    char_result = await db.execute(
        select(Character).where(Character.user_id == user_id).order_by(Character.character_name)
    )
    characters = char_result.scalars().all()

    # Get plans the user can edit for the "add to plan" dropdown — scope-aware
    ident = await perms.resolve_identities(db, user_id)
    plan_result = await db.execute(
        select(SkillPlan)
        .options(selectinload(SkillPlan.acl_entries))
        .order_by(SkillPlan.name)
    )
    plans = [p for p in plan_result.scalars().all() if perms.can_edit(p, ident)]

    return templates.TemplateResponse(request, "ship_mastery.html", {"ship_type_id": ship_type_id,
        "ship_name": ship_name,
        "group_name": group_name,
        "direct_reqs": direct_reqs,
        "full_tree": full_tree,
        "mastery_data": mastery_data,
        "characters": characters,
        "plans": plans})


# ── Ship mastery character check (htmx partial) ─────────────────────────────

@router.get("/ship/{ship_type_id}/check/{character_id}", response_class=HTMLResponse)
async def ship_mastery_check(ship_type_id: int, character_id: int, request: Request,
                              db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Character not found.</div>')

    # Fetch trained skills
    from app.esi.client import ESIClient
    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)
        skills_data = await esi_char.get_skills(client, character_id)
        trained = {s["skill_id"]: s.get("active_skill_level", 0) for s in skills_data.get("skills", [])}

        attrs_data = await esi_char.get_attributes(client, character_id)
        char_attrs = [
            attrs_data.get("charisma", 17),
            attrs_data.get("intelligence", 17),
            attrs_data.get("memory", 17),
            attrs_data.get("perception", 17),
            attrs_data.get("willpower", 17),
        ]
    except Exception as e:
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">Failed to fetch skills: {html.escape(str(e))}</div>')

    ATTR_INDEX = {164: 0, 165: 1, 166: 2, 167: 3, 168: 4}

    # Check direct requirements
    direct_reqs = await sde.get_skill_requirements(db, ship_type_id)
    can_fly = all(trained.get(r["skill_type_id"], 0) >= r["required_level"] for r in direct_reqs)

    # Check each mastery level
    mastery_data = await sde.get_ship_mastery(db, ship_type_id)
    mastery_results = []
    achieved_level = -1

    for level in range(5):
        level_skills = await sde.get_mastery_skills(db, ship_type_id, level)
        if not level_skills:
            mastery_results.append({"level": level, "complete": False, "pct": 0, "missing": 0,
                                     "sp_needed": 0, "time_str": "—"})
            continue

        total = len(level_skills)
        done = 0
        sp_needed = 0
        time_mins = 0
        missing_skills = []

        skill_ids = [s["skill_type_id"] for s in level_skills]
        skill_infos = await sde.get_skill_infos(db, skill_ids)

        for s in level_skills:
            current = trained.get(s["skill_type_id"], 0)
            if current >= s["required_level"]:
                done += 1
            else:
                info = skill_infos.get(s["skill_type_id"], {})
                rank = info.get("rank", 1.0)
                sp = _sp_for_level(s["required_level"], rank) - _sp_for_level(current, rank)
                sp_needed += sp
                p_idx = ATTR_INDEX.get(info.get("primary_attr", 165), 1)
                s_idx = ATTR_INDEX.get(info.get("secondary_attr", 166), 2)
                time_mins += _training_time_minutes(sp, char_attrs[p_idx], char_attrs[s_idx])
                missing_skills.append({
                    "skill_name": s["skill_name"],
                    "current": current,
                    "target": s["required_level"],
                })

        complete = done == total
        if complete:
            achieved_level = level

        mastery_results.append({
            "level": level,
            "complete": complete,
            "pct": int(done / total * 100) if total > 0 else 0,
            "done": done,
            "total": total,
            "missing": len(missing_skills),
            "missing_skills": missing_skills[:10],  # Limit to 10 for display
            "sp_needed": sp_needed,
            "time_str": _format_duration(time_mins),
        })

    return templates.TemplateResponse(request, "partials/ship_mastery_check.html", {"char": char,
        "can_fly": can_fly,
        "direct_reqs": direct_reqs,
        "trained": trained,
        "mastery_results": mastery_results,
        "achieved_level": achieved_level})


# ── Custom ACL management (Phase 3) ──────────────────────────────────────────

_ACL_SUBJECT_TYPES = {"character", "corporation", "alliance"}
_ACL_PERMISSIONS = {"view", "edit", "admin"}
_ACL_BUCKETS = {
    "character": "characters",
    "corporation": "corporations",
    "alliance": "alliances",
}


async def _resolve_acl_subject(subject_type: str, name: str) -> tuple[int | None, str | None]:
    """Resolve an EVE character / corporation / alliance name to (id, canonical_name)
    via the public ESI /universe/ids/ endpoint. Returns (None, None) on no match
    or on ESI failure — callers should surface a user-friendly error.
    """
    if subject_type not in _ACL_BUCKETS or not name:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://esi.evetech.net/latest/universe/ids/",
                json=[name],
                headers={"User-Agent": "Vigilant/1.0 (EVE Online personal dashboard)"},
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
    except Exception as e:
        logger.warning("ACL name resolve failed for %r: %s", name, e)
        return None, None
    bucket = data.get(_ACL_BUCKETS[subject_type]) or []
    lowered = name.strip().lower()
    for item in bucket:
        if (item.get("name") or "").lower() == lowered:
            return item.get("id"), item.get("name")
    # Second pass: accept the first result even if case differs (ESI is case-
    # insensitive on matching but returns canonical casing).
    if bucket:
        return bucket[0].get("id"), bucket[0].get("name")
    return None, None


@router.post("/{plan_id}/acl/add", response_class=HTMLResponse)
async def acl_add(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Add a new ACL entry to a custom-scope plan. Admin only."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    subject_type = (form.get("subject_type") or "").strip()
    name = (form.get("subject_name") or "").strip()
    permission = (form.get("permission") or "view").strip()

    if subject_type not in _ACL_SUBJECT_TYPES or permission not in _ACL_PERMISSIONS:
        return RedirectResponse(f"/skill-plans/{plan_id}?acl_err=invalid", status_code=302)
    if not name:
        return RedirectResponse(f"/skill-plans/{plan_id}?acl_err=name_required", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return RedirectResponse("/skill-plans", status_code=302)

    subject_id, canonical_name = await _resolve_acl_subject(subject_type, name)
    if not subject_id:
        return RedirectResponse(f"/skill-plans/{plan_id}?acl_err=not_found", status_code=302)

    # Upsert: if a row for (plan, type, id) exists, update its permission
    existing = next((e for e in plan.acl_entries
                     if e.subject_type == subject_type and e.subject_id == subject_id), None)
    if existing:
        existing.permission = permission
        existing.subject_name = canonical_name or existing.subject_name
    else:
        db.add(SkillPlanACL(
            plan_id=plan.id,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=canonical_name or name,
            permission=permission,
        ))
    _touch_edit(plan, user_id)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


@router.post("/{plan_id}/acl/{acl_id}/delete", response_class=HTMLResponse)
async def acl_delete(plan_id: int, acl_id: int, request: Request,
                     db: AsyncSession = Depends(get_db)):
    """Remove an ACL entry. Admin only."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return RedirectResponse("/skill-plans", status_code=302)

    entry = next((e for e in plan.acl_entries if e.id == acl_id), None)
    if entry:
        await db.delete(entry)
        _touch_edit(plan, user_id)
        await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


@router.post("/{plan_id}/acl/{acl_id}/permission", response_class=HTMLResponse)
async def acl_set_permission(plan_id: int, acl_id: int, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """Change the permission level on an existing ACL entry. Admin only."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    permission = (form.get("permission") or "").strip()
    if permission not in _ACL_PERMISSIONS:
        return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    ident = await perms.resolve_identities(db, user_id)
    plan = await _load_plan_for(db, plan_id, ident, "admin")
    if not plan:
        return RedirectResponse("/skill-plans", status_code=302)

    entry = next((e for e in plan.acl_entries if e.id == acl_id), None)
    if entry:
        entry.permission = permission
        _touch_edit(plan, user_id)
        await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)
