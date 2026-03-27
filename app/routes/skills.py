"""Skill planner with attribute remap optimizer."""

import asyncio
import math
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, CharacterDashboardCache
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.esi import universe as esi_universe
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])
templates = Jinja2Templates(directory="app/templates")

# ── Constants ─────────────────────────────────────────────────────────────────

# Cumulative SP for each level (index 0 = not trained)
SP_CUMULATIVE = [0, 250, 1415, 8000, 45255, 256000]

# Attribute ID → index and name
ATTR_ID_MAP = {164: 0, 165: 1, 166: 2, 167: 3, 168: 4}
ATTR_NAMES = ["Charisma", "Intelligence", "Memory", "Perception", "Willpower"]
ATTR_KEYS = ["charisma", "intelligence", "memory", "perception", "willpower"]

# Dogma attribute IDs
DGMA_PRIMARY = 180
DGMA_SECONDARY = 181
DGMA_RANK = 275


async def _get_skill_meta(client: ESIClient, db: AsyncSession, type_ids: list[int]) -> dict:
    """Fetch primary/secondary attribute and rank for skill types.
    
    Returns {type_id: {"primary": attr_index, "secondary": attr_index, "rank": int}}
    Uses ESI /universe/types/ (public, cached).
    """
    results = {}
    tasks = []

    async def _fetch_one(tid):
        try:
            data = await esi_universe.get_type(client, tid)
            attrs = {a["attribute_id"]: a["value"] for a in (data.get("dogma_attributes") or [])}
            primary_id = int(attrs.get(DGMA_PRIMARY, 165))
            secondary_id = int(attrs.get(DGMA_SECONDARY, 166))
            rank = int(attrs.get(DGMA_RANK, 1))
            results[tid] = {
                "primary": ATTR_ID_MAP.get(primary_id, 1),
                "secondary": ATTR_ID_MAP.get(secondary_id, 2),
                "rank": rank,
            }
        except Exception:
            results[tid] = {"primary": 1, "secondary": 2, "rank": 1}

    # Fetch in batches of 20 to avoid hammering ESI
    for i in range(0, len(type_ids), 20):
        batch = type_ids[i:i + 20]
        await asyncio.gather(*[_fetch_one(tid) for tid in batch])

    return results


def _sp_for_level(level: int, rank: int) -> int:
    """Cumulative SP to reach a level."""
    if level < 0 or level > 5:
        return 0
    return math.ceil(SP_CUMULATIVE[level] * rank)


def _sp_to_train(from_level: int, to_level: int, rank: int) -> int:
    """SP needed to train from one level to another."""
    return _sp_for_level(to_level, rank) - _sp_for_level(from_level, rank)


def _training_time_minutes(sp: int, primary: float, secondary: float) -> float:
    """Minutes to train given SP amount."""
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
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _optimal_remap(queue_items: list[dict], implants: list[int]) -> tuple[list[int], float]:
    """Brute-force optimal attribute remap for a skill queue.
    
    queue_items: list of {"sp": int, "primary": attr_index, "secondary": attr_index}
    implants: [+bonus for each of the 5 attributes]
    
    Returns (best_attrs[5], best_time_minutes)
    """
    # Group SP by (primary, secondary) pair
    sp_by_pair: dict[tuple[int, int], int] = {}
    for item in queue_items:
        key = (item["primary"], item["secondary"])
        sp_by_pair[key] = sp_by_pair.get(key, 0) + item["sp"]

    if not sp_by_pair:
        return [17, 17, 17, 17, 17], 0.0

    best_time = float("inf")
    best_attrs = [17, 17, 17, 17, 17]

    # Enumerate all valid bonus distributions
    for b0 in range(min(11, 15)):
        for b1 in range(min(11, 15 - b0)):
            for b2 in range(min(11, 15 - b0 - b1)):
                for b3 in range(min(11, 15 - b0 - b1 - b2)):
                    b4 = 14 - b0 - b1 - b2 - b3
                    if b4 < 0 or b4 > 10:
                        continue
                    attrs = [17 + b0, 17 + b1, 17 + b2, 17 + b3, 17 + b4]

                    total_time = 0.0
                    for (pri, sec), sp in sp_by_pair.items():
                        rate = (attrs[pri] + implants[pri]) + (attrs[sec] + implants[sec]) / 2
                        total_time += sp / rate

                    if total_time < best_time:
                        best_time = total_time
                        best_attrs = attrs[:]

    return best_attrs, best_time


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/character/{character_id}/skills", response_class=HTMLResponse)
async def skill_planner(
    request: Request,
    character_id: int,
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return RedirectResponse("/dashboard")

    # Eagerly extract char fields to avoid detached instance errors in templates
    char_info = {
        "character_id": char.character_id,
        "character_name": char.character_name,
        "corporation_name": char.corporation_name,
        "scopes": char.scopes or "",
    }

    scope = "esi-skills.read_skills.v1"
    if scope not in char_info["scopes"]:
        return templates.TemplateResponse("skills.html", {
            "request": request, "char": char_info,
            "error": "Skills scope not available — re-authorize this character.",
            "attributes": None, "queue_items": [], "total_sp": 0,
            "current_time_str": "", "implants": [0]*5,
        })

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        # Fetch attributes, skills, and queue in parallel
        attrs_raw, skills_raw, queue_raw = await asyncio.gather(
            esi_char.get_attributes(client, character_id),
            esi_char.get_skills(client, character_id),
            esi_char.get_skill_queue(client, character_id),
        )

        # Parse attributes
        current_attrs = [
            attrs_raw.get("charisma", 17),
            attrs_raw.get("intelligence", 17),
            attrs_raw.get("memory", 17),
            attrs_raw.get("perception", 17),
            attrs_raw.get("willpower", 17),
        ]
        bonus_remaps = attrs_raw.get("bonus_remaps", 0)
        last_remap = attrs_raw.get("last_remap_date", "")
        next_remap = attrs_raw.get("accrued_remap_cooldown_date", "")

        total_sp = skills_raw.get("total_sp", 0) if isinstance(skills_raw, dict) else 0

        # Build trained skills lookup
        trained = {}
        if isinstance(skills_raw, dict):
            for s in skills_raw.get("skills", []):
                trained[s["skill_id"]] = s.get("active_skill_level", 0)

        # Get current implant bonuses from cached clone data
        cache_result = await db.execute(
            select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
        )
        cache = cache_result.scalar_one_or_none()
        implants = [0, 0, 0, 0, 0]  # cha, int, mem, per, wil

        # Parse queue — include ALL queued skills (active, paused, or not yet started)
        now = datetime.now(timezone.utc)
        active_queue = []
        skill_ids = set()
        for entry in (queue_raw or []):
            finish_str = entry.get("finish_date")
            # If finish_date exists and is in the past, this skill is already done — skip
            if finish_str:
                finish = datetime.fromisoformat(finish_str.replace("Z", "+00:00"))
                if finish <= now:
                    continue
            # Include skills without finish_date (paused queue) and future skills
            skill_ids.add(entry["skill_id"])
            active_queue.append(entry)

        # Resolve skill names and metadata
        names = await sde.type_ids_to_names(db, list(skill_ids)) if skill_ids else {}
        skill_meta = await _get_skill_meta(client, db, list(skill_ids)) if skill_ids else {}

        # Build queue items with training times
        queue_items = []
        total_current_minutes = 0.0
        for entry in active_queue:
            sid = entry["skill_id"]
            meta = skill_meta.get(sid, {"primary": 1, "secondary": 2, "rank": 1})
            level = entry.get("finished_level", 1)
            from_level = level - 1

            sp_needed = entry.get("level_end_sp", 0) - entry.get("training_start_sp", entry.get("start_sp", 0))
            if sp_needed <= 0:
                sp_needed = _sp_to_train(from_level, level, meta["rank"])

            pri_val = current_attrs[meta["primary"]]
            sec_val = current_attrs[meta["secondary"]]
            time_min = _training_time_minutes(sp_needed, pri_val + implants[meta["primary"]],
                                               sec_val + implants[meta["secondary"]])
            total_current_minutes += time_min

            queue_items.append({
                "skill_id": sid,
                "name": names.get(sid, f"Skill {sid}"),
                "level": level,
                "rank": meta["rank"],
                "primary": meta["primary"],
                "secondary": meta["secondary"],
                "primary_name": ATTR_NAMES[meta["primary"]],
                "secondary_name": ATTR_NAMES[meta["secondary"]],
                "sp_needed": sp_needed,
                "time_str": _format_duration(time_min),
                "time_minutes": time_min,
            })

        # Compute optimal remap
        remap_input = [{"sp": q["sp_needed"], "primary": q["primary"], "secondary": q["secondary"]}
                       for q in queue_items]
        optimal_attrs, optimal_time = _optimal_remap(remap_input, implants)
        time_saved = total_current_minutes - optimal_time

    except Exception as exc:
        logger.warning("Skill planner failed for char %s: %s", character_id, exc, exc_info=True)
        return templates.TemplateResponse("skills.html", {
            "request": request, "char": char_info,
            "error": f"Failed to load skill data: {type(exc).__name__}",
            "attributes": None, "queue_items": [], "total_sp": 0,
            "current_time_str": "", "implants": [0]*5,
        })

    return templates.TemplateResponse("skills.html", {
        "request": request,
        "char": char_info,
        "error": None,
        "attributes": current_attrs,
        "attr_names": ATTR_NAMES,
        "attr_keys": ATTR_KEYS,
        "implants": implants,
        "total_sp": total_sp,
        "queue_items": queue_items,
        "total_current_minutes": total_current_minutes,
        "current_time_str": _format_duration(total_current_minutes),
        "optimal_attrs": optimal_attrs,
        "optimal_time": optimal_time,
        "optimal_time_str": _format_duration(optimal_time),
        "time_saved": time_saved,
        "time_saved_str": _format_duration(abs(time_saved)),
        "bonus_remaps": bonus_remaps,
        "last_remap": last_remap[:10] if last_remap else "",
        "next_remap": next_remap[:10] if next_remap else "",
    })


@router.get("/character/{character_id}/skills/remap-calc", response_class=HTMLResponse)
async def remap_calculate(
    request: Request,
    character_id: int,
    cha: int = Query(17), int_: int = Query(17, alias="int"),
    mem: int = Query(17), per: int = Query(17), wil: int = Query(17),
    db: AsyncSession = Depends(get_db),
):
    """Htmx partial: recalculate queue times for proposed attributes."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("")

    proposed = [
        max(17, min(27, cha)),
        max(17, min(27, int_)),
        max(17, min(27, mem)),
        max(17, min(27, per)),
        max(17, min(27, wil)),
    ]
    total_points = sum(proposed)

    char_result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse("")

    try:
        token = await refresh_token(char, db)
        client = ESIClient(token, db=db)

        attrs_raw, queue_raw = await asyncio.gather(
            esi_char.get_attributes(client, character_id),
            esi_char.get_skill_queue(client, character_id),
        )

        current_attrs = [
            attrs_raw.get("charisma", 17),
            attrs_raw.get("intelligence", 17),
            attrs_raw.get("memory", 17),
            attrs_raw.get("perception", 17),
            attrs_raw.get("willpower", 17),
        ]

        implants = [0, 0, 0, 0, 0]

        now = datetime.now(timezone.utc)
        active_queue = []
        for e in (queue_raw or []):
            fd = e.get("finish_date")
            if fd:
                if datetime.fromisoformat(fd.replace("Z", "+00:00")) <= now:
                    continue
            active_queue.append(e)

        skill_ids = list({e["skill_id"] for e in active_queue})
        names = await sde.type_ids_to_names(db, skill_ids) if skill_ids else {}
        skill_meta = await _get_skill_meta(client, db, skill_ids) if skill_ids else {}

        current_total = 0.0
        proposed_total = 0.0
        rows = []

        for entry in active_queue:
            sid = entry["skill_id"]
            meta = skill_meta.get(sid, {"primary": 1, "secondary": 2, "rank": 1})
            level = entry.get("finished_level", 1)
            sp_needed = entry.get("level_end_sp", 0) - entry.get("training_start_sp", entry.get("start_sp", 0))
            if sp_needed <= 0:
                sp_needed = _sp_to_train(level - 1, level, meta["rank"])

            cur_time = _training_time_minutes(
                sp_needed,
                current_attrs[meta["primary"]] + implants[meta["primary"]],
                current_attrs[meta["secondary"]] + implants[meta["secondary"]],
            )
            prop_time = _training_time_minutes(
                sp_needed,
                proposed[meta["primary"]] + implants[meta["primary"]],
                proposed[meta["secondary"]] + implants[meta["secondary"]],
            )

            current_total += cur_time
            proposed_total += prop_time

            diff = cur_time - prop_time
            rows.append({
                "name": names.get(sid, f"Skill {sid}"),
                "level": level,
                "primary_name": ATTR_NAMES[meta["primary"]],
                "secondary_name": ATTR_NAMES[meta["secondary"]],
                "current_time": _format_duration(cur_time),
                "proposed_time": _format_duration(prop_time),
                "diff_minutes": diff,
                "diff_str": _format_duration(abs(diff)),
                "faster": diff > 0,
            })

    except Exception:
        rows = []
        current_total = 0
        proposed_total = 0

    time_diff = current_total - proposed_total

    return templates.TemplateResponse("partials/remap_results.html", {
        "request": request,
        "rows": rows,
        "proposed": proposed,
        "total_points": total_points,
        "valid": total_points == 99,
        "current_total_str": _format_duration(current_total),
        "proposed_total_str": _format_duration(proposed_total),
        "time_diff": time_diff,
        "time_diff_str": _format_duration(abs(time_diff)),
        "is_faster": time_diff > 0,
        "attr_names": ATTR_NAMES,
    })
