"""Intel → Kill Feed → Advanced Search.

Sibling of /intel/kills. Full filter UI + cursor pagination + optional live
polling. Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md.

Plan 1 (this MVP):
  - Page route (this file, Task 1)
  - Filter compiler + /search/results endpoint (Task 2)
  - Results partial + NPC badge surfacing (Task 3 modifies the shared partial)
  - Frontend wiring (Task 4-5 in intel_kills_search.html)
  - Live polling (Task 6)

Plan 2 (later) adds heuristic flags (Awox/Padding/HighSec Gank) and AT Ships
category.
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


@router.get("/intel/kills/search", response_class=HTMLResponse)
async def intel_kills_search_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Page shell. Filters + empty results container, JS handles the rest."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse("intel_kills_search.html", {"request": request})
