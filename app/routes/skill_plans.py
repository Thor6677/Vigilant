"""Skill plan management — create, edit, import, and analyze skill plans."""

import json
import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.db.models import get_db, Character, SkillPlan, SkillPlanEntry
from app.esi.client import refresh_token
from app.esi import character as esi_char
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skill-plans", tags=["skill-plans"])
templates = Jinja2Templates(directory="app/templates")

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
ROMAN_REV = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

SP_CUMULATIVE = [0, 250, 1415, 8000, 45255, 256000]


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


# ── List all plans ───────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_plans(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    result = await db.execute(
        select(SkillPlan)
        .where(SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
        .order_by(SkillPlan.updated_at.desc())
    )
    plans = result.scalars().all()

    # Calculate SP and training time for each plan
    all_skill_ids = set()
    for p in plans:
        for e in p.entries:
            all_skill_ids.add(e.skill_type_id)
    skill_infos = await sde.get_skill_infos(db, list(all_skill_ids)) if all_skill_ids else {}

    plan_stats = {}
    for p in plans:
        total_sp = 0
        total_mins = 0.0
        for e in p.entries:
            info = skill_infos.get(e.skill_type_id, {})
            rank = info.get("rank", 1.0)
            sp = _sp_for_level(e.target_level, rank)
            total_sp += sp
            # Estimate time at 20 primary / 20 secondary (base 17 + 3 implants)
            total_mins += _training_time_minutes(sp, 20, 20)
        plan_stats[p.id] = {
            "total_sp": total_sp,
            "time_str": _format_duration(total_mins) if total_mins > 0 else "—",
        }

    # Get user's characters for the character selector
    char_result = await db.execute(
        select(Character).where(Character.user_id == user_id).order_by(Character.character_name)
    )
    characters = char_result.scalars().all()

    return templates.TemplateResponse("skill_plans.html", {
        "request": request,
        "plans": plans,
        "plan_stats": plan_stats,
        "characters": characters,
    })


# ── Create plan ──────────────────────────────────────────────────────────────

@router.post("/create", response_class=HTMLResponse)
async def create_plan(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        name = "New Skill Plan"

    plan = SkillPlan(user_id=user_id, name=name)
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

    result = await db.execute(
        select(SkillPlan)
        .where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if not plan:
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

    return templates.TemplateResponse("skill_plan_detail.html", {
        "request": request,
        "plan": plan,
        "entries": entries,
        "characters": characters,
    })


# ── Delete plan ──────────────────────────────────────────────────────────────

@router.post("/{plan_id}/delete", response_class=HTMLResponse)
async def delete_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
    )
    plan = result.scalar_one_or_none()
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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if plan:
        for entry in list(plan.entries):
            await db.delete(entry)
        plan.updated_at = datetime.now(timezone.utc)
        await db.commit()

    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Duplicate plan ───────────────────────────────────────────────────────────

@router.post("/{plan_id}/duplicate", response_class=HTMLResponse)
async def duplicate_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=302)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    source = result.scalar_one_or_none()
    if not source:
        return RedirectResponse("/skill-plans", status_code=302)

    new_plan = SkillPlan(user_id=user_id, name=f"{source.name} (Copy)")
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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse("", status_code=404)

    plan.name = name
    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return HTMLResponse(f'<span class="b-label">{name}</span>')


# ── Add skill to plan ────────────────────────────────────────────────────────

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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse("", status_code=404)

    # Check if skill already in plan at same or higher level
    for e in plan.entries:
        if e.skill_type_id == skill_type_id:
            if e.target_level >= target_level:
                name = await sde.type_id_to_name(db, skill_type_id) or f"Skill {skill_type_id}"
                return HTMLResponse(f'<div class="b-empty" style="color:var(--warn);">{name} {ROMAN_REV.get(e.target_level, "")} already in plan.</div>')
            # Update to higher level
            e.target_level = target_level
            plan.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)

    # Add new entry
    max_order = max((e.sort_order for e in plan.entries), default=-1)
    db.add(SkillPlanEntry(
        plan_id=plan_id,
        skill_type_id=skill_type_id,
        target_level=target_level,
        sort_order=max_order + 1,
    ))
    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return RedirectResponse(f"/skill-plans/{plan_id}", status_code=302)


# ── Remove skill from plan ───────────────────────────────────────────────────

@router.post("/{plan_id}/remove-skill/{entry_id}", response_class=HTMLResponse)
async def remove_skill(plan_id: int, entry_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(SkillPlanEntry)
        .join(SkillPlan)
        .where(SkillPlanEntry.id == entry_id, SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
    )
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
        # Update plan timestamp
        plan_result = await db.execute(select(SkillPlan).where(SkillPlan.id == plan_id))
        plan = plan_result.scalar_one_or_none()
        if plan:
            plan.updated_at = datetime.now(timezone.utc)
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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse("", status_code=404)

    entry_map = {e.id: e for e in plan.entries}
    for i, eid in enumerate(entry_ids):
        if eid in entry_map:
            entry_map[eid].sort_order = i

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return HTMLResponse("")


# ── Share / unshare plan ─────────────────────────────────────────────────────

@router.post("/{plan_id}/share", response_class=HTMLResponse)
async def toggle_share(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse("", status_code=404)

    if plan.share_token:
        plan.share_token = None
    else:
        plan.share_token = secrets.token_urlsafe(12)

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

    return templates.TemplateResponse("skill_plan_shared.html", {
        "request": request,
        "plan": plan,
        "entries": entries,
        "characters": characters,
        "share_token": share_token,
    })


# ── Import from EVE text ────────────────────────────────────────────────────

@router.post("/{plan_id}/import", response_class=HTMLResponse)
async def import_skills(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
    if not plan:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    text = form.get("skill_text", "").strip()
    if not text:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">No skill text provided.</div>')

    # Parse lines like "Skill Name I" or "Skill Name 3"
    added = 0
    not_found = []
    existing_skills = {e.skill_type_id: e for e in plan.entries}
    max_order = max((e.sort_order for e in plan.entries), default=-1)

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Try to match "Skill Name <roman numeral>" or "Skill Name <digit>"
        match = re.match(r'^(.+?)\s+(I{1,3}V?|IV|V|[1-5])$', line)
        if not match:
            not_found.append(line)
            continue

        skill_name = match.group(1).strip()
        level_str = match.group(2)
        level = ROMAN.get(level_str) or int(level_str)

        # Look up skill by name
        skill_id = await sde.type_name_to_id(db, skill_name)
        if not skill_id:
            not_found.append(line)
            continue

        # Add or update
        if skill_id in existing_skills:
            if existing_skills[skill_id].target_level < level:
                existing_skills[skill_id].target_level = level
                added += 1
        else:
            max_order += 1
            entry = SkillPlanEntry(
                plan_id=plan_id, skill_type_id=skill_id,
                target_level=level, sort_order=max_order,
            )
            db.add(entry)
            existing_skills[skill_id] = entry
            added += 1

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()

    msg = f"Imported {added} skill(s)."
    if not_found:
        msg += f" Could not resolve: {', '.join(not_found[:5])}"
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

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
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
            msg += f" Not found: {', '.join(not_found[:5])}"
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

    # Add to plan
    existing = {e.skill_type_id: e for e in plan.entries}
    max_order = max((e.sort_order for e in plan.entries), default=-1)
    added = 0

    for sid, lvl in needed.items():
        if sid in existing:
            if existing[sid].target_level < lvl:
                existing[sid].target_level = lvl
                added += 1
        else:
            max_order += 1
            entry = SkillPlanEntry(
                plan_id=plan_id, skill_type_id=sid,
                target_level=lvl, sort_order=max_order,
            )
            db.add(entry)
            existing[sid] = entry
            added += 1

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()

    items_resolved = len(resolved)
    msg = f"Added {added} skill(s) from {items_resolved} item(s)."
    if not_found:
        msg += f" Not found: {', '.join(not_found[:5])}"
        if len(not_found) > 5:
            msg += f" (+{len(not_found) - 5} more)"
    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">{msg}</div>')


# ── Generate from ship / mastery ─────────────────────────────────────────────

@router.post("/{plan_id}/from-ship", response_class=HTMLResponse)
async def add_from_ship(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
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

    existing = {e.skill_type_id: e for e in plan.entries}
    max_order = max((e.sort_order for e in plan.entries), default=-1)
    added = 0

    for s in skills:
        sid = s["skill_type_id"]
        lvl = s["required_level"]
        if sid in existing:
            if existing[sid].target_level < lvl:
                existing[sid].target_level = lvl
                added += 1
        else:
            max_order += 1
            entry = SkillPlanEntry(
                plan_id=plan_id, skill_type_id=sid,
                target_level=lvl, sort_order=max_order,
            )
            db.add(entry)
            existing[sid] = entry
            added += 1

    plan.updated_at = datetime.now(timezone.utc)
    await db.commit()

    ship_name = await sde.type_id_to_name(db, ship_type_id) or f"Ship {ship_type_id}"
    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">Added {added} skill(s) from {ship_name}.</div>')


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

    # Verify plan ownership or shared access
    result = await db.execute(
        select(SkillPlan).where(
            SkillPlan.id == plan_id,
            (SkillPlan.user_id == user_id) | (SkillPlan.share_token.isnot(None))
        ).options(selectinload(SkillPlan.entries))
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
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">Failed to fetch skills: {e}</div>')

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

    return templates.TemplateResponse("partials/skill_plan_gap.html", {
        "request": request,
        "rows": rows,
        "total_sp": total_sp_needed,
        "total_time": _format_duration(total_time_mins),
        "completed": completed,
        "total": len(plan.entries),
        "char": char,
    })


# ── Export as text ───────────────────────────────────────────────────────────

@router.get("/{plan_id}/export", response_class=HTMLResponse)
async def export_plan(plan_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    result = await db.execute(
        select(SkillPlan).where(SkillPlan.id == plan_id, SkillPlan.user_id == user_id)
        .options(selectinload(SkillPlan.entries))
    )
    plan = result.scalar_one_or_none()
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
        f'<textarea readonly style="width:100%;height:150px;background:var(--bg);color:var(--text);'
        f'border:1px solid var(--border);font-family:inherit;font-size:10px;padding:0.5rem;"'
        f' onclick="this.select()">{text}</textarea>'
        f'<div style="font-size:9px;color:var(--muted);margin-top:0.25rem;">Click to select, then copy to clipboard. Paste into EVE skill plans.</div>'
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

    # Get user's plans for "add to plan" dropdown
    plan_result = await db.execute(
        select(SkillPlan).where(SkillPlan.user_id == user_id).order_by(SkillPlan.name)
    )
    plans = plan_result.scalars().all()

    return templates.TemplateResponse("ship_mastery.html", {
        "request": request,
        "ship_type_id": ship_type_id,
        "ship_name": ship_name,
        "group_name": group_name,
        "direct_reqs": direct_reqs,
        "full_tree": full_tree,
        "mastery_data": mastery_data,
        "characters": characters,
        "plans": plans,
    })


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
        return HTMLResponse(f'<div class="b-empty" style="color:var(--danger);">Failed to fetch skills: {e}</div>')

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

    return templates.TemplateResponse("partials/ship_mastery_check.html", {
        "request": request,
        "char": char,
        "can_fly": can_fly,
        "direct_reqs": direct_reqs,
        "trained": trained,
        "mastery_results": mastery_results,
        "achieved_level": achieved_level,
    })
