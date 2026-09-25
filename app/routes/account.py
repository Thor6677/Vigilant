"""Account › Characters & permissions.

Where a user sees, per character, exactly which permissions its token carries
— and changes them. Changing goes back through EVE SSO (app/auth/routes.py,
intent "update"): widening needs EVE's consent, and narrowing is done with a
fresh, smaller token plus revocation of the old one, so the promise holds at
EVE and not only inside Vigilant.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.auth import status as perm_status
from app.auth.routes import UPDATE, picker_context
from app.db.models import Character, CharacterDashboardCache, get_db

router = APIRouter(tags=["account"])
templates = Jinja2Templates(directory="app/templates")


def _char_view(char: Character, warnings: dict | None) -> dict:
    """Plain dict for the template (no lazy loads on a detached row)."""
    return {
        "character_id": char.character_id,
        "character_name": char.character_name,
        "corporation_name": char.corporation_name,
        "alliance_name": char.alliance_name,
        "is_main": bool(char.is_main),
        "scopes": char.scopes or "",
        "declined_scopes": char.declined_scopes or "",
        "token_failed": perm_status.token_failed(warnings),
        **perm_status.summary(char),
    }


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)
    chars = (await db.execute(
        select(Character).where(Character.user_id == user_id)
        .order_by(Character.is_main.desc(), Character.character_name))).scalars().all()
    caches = {c.character_id: c for c in (await db.execute(
        select(CharacterDashboardCache).where(
            CharacterDashboardCache.character_id.in_([c.character_id for c in chars])))).scalars().all()}
    views = []
    for char in chars:
        cache = caches.get(char.character_id)
        try:
            warnings = json.loads(cache.sync_warnings_json) if cache and cache.sync_warnings_json else {}
        except (ValueError, TypeError):
            warnings = {}
        views.append(_char_view(char, warnings))
    return templates.TemplateResponse(request, "account.html", {
        "characters": views,
        "flash": request.session.pop("flash", None),
        "total_permissions": len(perms.PERMISSIONS),
    })


@router.get("/account/permissions/{character_id}", response_class=HTMLResponse)
async def change_permissions(character_id: int, request: Request, welcome: int = 0,
                             db: AsyncSession = Depends(get_db)):
    """The picker, preset to what this character shares now.

    ``?add=<key>`` (repeatable) ticks extra permissions on top — the "share
    it" links in missing-permission notices land here.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)
    char = (await db.execute(select(Character).where(
        Character.character_id == character_id,
        Character.user_id == user_id))).scalar_one_or_none()
    if char is None:
        return RedirectResponse("/account", status_code=303)
    current = perms.keys_for_scopes(char.scopes)
    adding = perms.normalize_keys(request.query_params.getlist("add"))
    if welcome and not current:
        selected = set(perms.PRESETS[perms.DEFAULT_PRESET])
        active_preset = perms.DEFAULT_PRESET
    else:
        selected = set(perms.normalize_keys(current + adding))
        active_preset = None
    return templates.TemplateResponse(request, "permissions_picker.html", {
        "intent": UPDATE,
        "welcome": bool(welcome),
        "character": _char_view(char, None),
        "current": set(current),
        "adding": set(adding),
        "selected": selected,
        "active_preset": active_preset,
        **picker_context(),
    })
