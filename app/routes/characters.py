"""
Characters management page with sorting, drag-and-drop grouping, and skill queue display.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, Character, User
from app.esi.client import ESIClient, refresh_token
from app.esi import character as esi_char
from app.sde import lookup as sde

router = APIRouter(tags=["characters"])
templates = Jinja2Templates(directory="app/templates")


async def _fetch_training(characters: list[Character], db: AsyncSession) -> tuple[dict, dict]:
    """
    Fetch active skill training for each character that has the skill queue scope.
    Returns:
        training   - {character_id: {skill_id, level, queue_finish}}
        skill_names - {skill_id: name}
    """
    training: dict = {}
    skill_ids: set[int] = set()

    for char in characters:
        if "esi-skills.read_skillqueue.v1" not in (char.scopes or ""):
            training[char.character_id] = {}
            continue
        try:
            token = await refresh_token(char, db)
            client = ESIClient(token, db=db)
            queue = await esi_char.get_skill_queue(client, char.character_id)

            if not queue:
                training[char.character_id] = {}
                continue

            # Active skill = first entry with a start_date in the past
            now = datetime.now(timezone.utc)
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

            if active:
                finish_raw = queue[-1].get("finish_date") if queue else None
                queue_finish = None
                if finish_raw:
                    dt = datetime.fromisoformat(finish_raw.replace("Z", "+00:00"))
                    queue_finish = dt.strftime("%Y-%m-%d %H:%M")

                skill_id = active.get("skill_id")
                level = active.get("finished_level")
                training[char.character_id] = {
                    "skill_id": skill_id,
                    "level": level,
                    "queue_finish": queue_finish,
                }
                if skill_id:
                    skill_ids.add(skill_id)
            else:
                training[char.character_id] = {}

        except Exception:
            training[char.character_id] = {}

    skill_names = await sde.type_ids_to_names(db, list(skill_ids))
    return training, skill_names


def _sort_characters(characters: list[Character], sort: str) -> list[Character]:
    if sort == "name":
        return sorted(characters, key=lambda c: c.character_name.lower())
    if sort == "corp":
        return sorted(characters, key=lambda c: (c.corporation_name or "").lower())
    if sort == "custom":
        return sorted(characters, key=lambda c: (c.account_group or "Ungrouped", c.sort_order))
    return list(characters)


def _group_characters(characters: list[Character]) -> dict[str, list[Character]]:
    groups: dict[str, list[Character]] = {}
    for char in characters:
        group = char.account_group or "Ungrouped"
        groups.setdefault(group, []).append(char)
    return groups


@router.get("/characters", response_class=HTMLResponse)
async def characters_page(request: Request, sort: str = "default", db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    active_id = request.session.get("active_character_id")

    # Fetch only this user's characters (enforces isolation)
    result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)

    # Fetch user for main character designation
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    main_character_id = user.main_character_id if user else None

    needs_reauth_count = sum(
        1 for c in characters
        if "esi-skills.read_skillqueue.v1" not in (c.scopes or "")
    )

    sorted_chars = _sort_characters(list(characters), sort)
    groups = _group_characters(sorted_chars) if sort == "custom" else {}

    training, skill_names = {}, {}
    if sort in ("training", "queue", "custom"):
        training, skill_names = await _fetch_training(list(characters), db)

    if sort == "training":
        sorted_chars = sorted(
            sorted_chars,
            key=lambda c: 0 if training.get(c.character_id, {}).get("skill_id") else 1
        )
    elif sort == "queue":
        sorted_chars = sorted(
            sorted_chars,
            key=lambda c: training.get(c.character_id, {}).get("queue_finish") or "9999"
        )

    return templates.TemplateResponse("characters.html", {
        "request": request,
        "characters": sorted_chars,
        "groups": groups,
        "active_char": active_char,
        "main_character_id": main_character_id,
        "sort": sort,
        "training": training,
        "skill_names": skill_names,
        "needs_reauth_count": needs_reauth_count,
    })


@router.post("/characters/reorder")
async def reorder_characters(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()

    for item in data:
        char_id = item.get("character_id")
        # Filter by user_id to prevent modifying other users' characters
        result = await db.execute(
            select(Character).where(
                Character.character_id == char_id,
                Character.user_id == user_id,
            )
        )
        char = result.scalar_one_or_none()
        if char:
            char.sort_order = item.get("sort_order", 0)
            char.account_group = item.get("account_group", "Ungrouped")

    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/characters/rename-group")
async def rename_group(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()

    if not old_name or not new_name:
        return JSONResponse({"error": "Names required"}, status_code=400)

    result = await db.execute(
        select(Character).where(
            Character.user_id == user_id,
            Character.account_group == old_name,
        )
    )
    for char in result.scalars().all():
        char.account_group = new_name

    await db.commit()
    return JSONResponse({"ok": True})
