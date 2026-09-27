"""Pilot role tags and private notes (T-074) — the htmx partial mounted at
`#char-tags` on the character detail page.

Both routes are `/character/{character_id}/tags`: GET renders the partial
(chips + note + an Edit toggle), POST validates and upserts a submission and
re-renders the same partial for the htmx `outerHTML` swap. Validation and
storage live in app/tags.py; this module is just the route + rendering glue.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import tags as tags_mod
from app.db.models import Character, get_db

router = APIRouter(tags=["character_tags"])
templates = Jinja2Templates(directory="app/templates")


async def _owned_character(db: AsyncSession, character_id: int, user_id: int) -> Character | None:
    result = await db.execute(
        select(Character).where(Character.character_id == character_id, Character.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def _render(
    request: Request, db: AsyncSession, user_id: int, character_id: int,
    error: str | None = None, raw_tags: str | None = None, raw_note: str | None = None,
    edit_open: bool = False,
) -> HTMLResponse:
    existing = (await tags_mod.load_character_tags(db, user_id)).get(character_id, {})
    current_tags = existing.get("tags", [])
    current_note = existing.get("note")

    vocab = await tags_mod.user_tag_vocabulary(db, user_id)
    current_lower = {t.lower() for t in current_tags}
    quick_add: list[str] = []
    seen_lower: set[str] = set()
    for tag in list(tags_mod.SUGGESTED_TAGS) + [t for t, _ in vocab]:
        key = tag.lower()
        if key in current_lower or key in seen_lower:
            continue
        seen_lower.add(key)
        quick_add.append(tag)

    ctx = {
        "character_id": character_id,
        "tags": current_tags,
        "note": current_note,
        "quick_add_tags": quick_add,
        "error": error,
        "tags_input_value": raw_tags if raw_tags is not None else ", ".join(current_tags),
        "note_input_value": raw_note if raw_note is not None else (current_note or ""),
        "edit_open": edit_open,
    }
    return templates.TemplateResponse(request, "partials/character_tags.html", ctx)


@router.get("/character/{character_id}/tags", response_class=HTMLResponse)
async def get_character_tags(request: Request, character_id: int, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    char = await _owned_character(db, character_id, user_id)
    if char is None:
        return HTMLResponse("", status_code=404)
    return await _render(request, db, user_id, character_id)


@router.post("/character/{character_id}/tags", response_class=HTMLResponse)
async def post_character_tags(
    request: Request, character_id: int,
    tags: str = Form(""), note: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    char = await _owned_character(db, character_id, user_id)
    if char is None:
        return HTMLResponse("", status_code=404)

    try:
        tag_list = tags_mod.parse_tags_input(tags)
        note_val = tags_mod.normalize_note(note)
    except tags_mod.TagError as e:
        return await _render(
            request, db, user_id, character_id,
            error=str(e), raw_tags=tags, raw_note=note, edit_open=True,
        )

    await tags_mod.save_character_tags(db, user_id, character_id, tag_list, note_val)
    await db.commit()
    return await _render(request, db, user_id, character_id)
