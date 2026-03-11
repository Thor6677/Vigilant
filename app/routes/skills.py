"""
Skill queue monitor — shows training status for all characters grouped by account.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.sde import lookup as sde

router = APIRouter(tags=["skills"])
templates = Jinja2Templates(directory="app/templates")


def skill_warning(queue: list, queue_end: datetime | None) -> str:
    if not queue:
        return "empty"
    if queue_end is None:
        return "empty"
    days = (queue_end - datetime.now(timezone.utc)).days
    if days <= 7:
        return "critical"
    if days <= 14:
        return "warning"
    return "ok"


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "finishing soon"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def _process_skillqueue(
    characters: list[Character],
    raw_data: dict[int, list | None | str],  # cid -> raw queue list, None (error), or "no_scope"
    db: AsyncSession,
) -> list[dict]:
    """
    Process already-fetched raw skill queue data into the display dict list.
    raw_data values:
      - list: the raw ESI queue entries
      - None: fetch failed / not yet cached
      - "no_scope": character lacks the required scope
    """
    skill_ids: set[int] = set()
    for cid, queue in raw_data.items():
        if isinstance(queue, list):
            for entry in queue:
                sid = entry.get("skill_id")
                if sid:
                    skill_ids.add(sid)

    skill_names = await sde.type_ids_to_names(db, list(skill_ids))

    now = datetime.now(timezone.utc)
    results = []

    for char in characters:
        cid = char.character_id
        queue = raw_data.get(cid)

        if queue == "no_scope" or queue is None:
            error_label = "no_scope" if queue == "no_scope" else "error"
            results.append({
                "char": char,
                "warning": error_label,
                "current_skill": None,
                "current_level": None,
                "queue_end": None,
                "queue_end_str": None,
                "days_remaining": None,
                "time_remaining_str": None,
                "queue_length": 0,
                "progress_pct": 0,
            })
            continue

        # Find active skill (first entry with start_date in the past)
        active = None
        for entry in queue:
            start_raw = entry.get("start_date")
            if start_raw:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if start <= now:
                    active = entry
                    break
        if not active and queue:
            active = queue[0]

        # Queue end datetime
        queue_end = None
        queue_end_str = None
        if queue:
            finish_raw = queue[-1].get("finish_date")
            if finish_raw:
                queue_end = datetime.fromisoformat(finish_raw.replace("Z", "+00:00"))
                queue_end_str = queue_end.strftime("%Y-%m-%d %H:%M")

        # Time remaining across whole queue
        days_remaining = None
        time_remaining_str = None
        if queue_end:
            delta = queue_end - now
            days_remaining = delta.days
            time_remaining_str = format_duration(delta.total_seconds())

        # Current skill details + progress within current skill
        current_skill = None
        current_level = None
        progress_pct = 0
        if active:
            sid = active.get("skill_id")
            current_skill = skill_names.get(sid, f"Type {sid}") if sid else None
            current_level = active.get("finished_level")
            start_raw = active.get("start_date")
            finish_raw_a = active.get("finish_date")
            if start_raw and finish_raw_a:
                start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                finish_dt = datetime.fromisoformat(finish_raw_a.replace("Z", "+00:00"))
                total_secs = (finish_dt - start_dt).total_seconds()
                elapsed_secs = (now - start_dt).total_seconds()
                if total_secs > 0:
                    progress_pct = min(100, max(0, int(elapsed_secs / total_secs * 100)))

        results.append({
            "char": char,
            "warning": skill_warning(queue, queue_end),
            "current_skill": current_skill,
            "current_level": current_level,
            "queue_end": queue_end,
            "queue_end_str": queue_end_str,
            "days_remaining": days_remaining,
            "time_remaining_str": time_remaining_str,
            "queue_length": len(queue),
            "progress_pct": progress_pct,
        })

    return results


async def fetch_skill_data(characters: list[Character], db: AsyncSession) -> list[dict]:
    """
    Fetch and process skill queue data for each character.
    Returns list of dicts with processed skill info, preserving input order.
    """
    raw_data: dict[int, list | None | str] = {}

    for char in characters:
        if "esi-skills.read_skillqueue.v1" not in (char.scopes or ""):
            raw_data[char.character_id] = "no_scope"
            continue
        try:
            token = await refresh_token(char, db)
            client = ESIClient(token, db=db)
            queue = await esi_char.get_skill_queue(client, char.character_id)
            raw_data[char.character_id] = queue
        except Exception:
            raw_data[char.character_id] = None

    return await _process_skillqueue(characters, raw_data, db)


def group_skill_data(skill_data: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in skill_data:
        group = item["char"].account_group or "Ungrouped"
        groups.setdefault(group, []).append(item)
    for group in groups:
        groups[group].sort(key=lambda x: x["char"].sort_order)
    return groups


@router.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    active_id = request.session.get("active_character_id")

    # Fetch only this user's characters
    result = await db.execute(
        select(Character).where(Character.user_id == user_id)
    )
    characters = sorted(
        result.scalars().all(),
        key=lambda c: (c.account_group or "Ungrouped", c.sort_order),
    )
    active_char = next((c for c in characters if c.character_id == active_id), None)

    skill_data = await fetch_skill_data(characters, db)
    groups = group_skill_data(skill_data)

    return templates.TemplateResponse("skills.html", {
        "request": request,
        "active_char": active_char,
        "groups": groups,
        "skill_data": skill_data,
    })
