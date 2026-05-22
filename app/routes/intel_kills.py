"""Intel → Kill Feed.

Live universe-wide kill feed from killmail.stream's _recent_kills buffer.
Filters: space class (HS/LS/NS/WH + sub-classes + Shattered modifier),
ship search, attacker entity search, victim entity search.

Click a row to expand the detail panel (victim + fitting + ISK + attackers).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/intel/kills", response_class=HTMLResponse)
async def intel_kills_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Page shell. The feed content is loaded via htmx into the container."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(
        "intel_kills.html",
        {"request": request},
    )


@router.get("/intel/kills/feed", response_class=HTMLResponse)
async def intel_kills_feed(request: Request, since: int | None = None):
    """Stub — Task 5 fills this in with the live-tail filter+render."""
    return HTMLResponse('<p style="color:var(--muted);font-size:11px;">Loading…</p>')
