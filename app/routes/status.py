from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, ESIRateLimitEvent
from app.esi.rate_limit import rate_limit_tracker

router = APIRouter(tags=["status"])
templates = Jinja2Templates(directory="app/templates")


def _age_str(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


templates.env.filters["age_str"] = _age_str


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, db: AsyncSession = Depends(get_db)):
    tracker = rate_limit_tracker
    groups = list(tracker.groups.values())
    legacy = tracker.legacy
    request_log = list(reversed(tracker.request_log))
    overall = tracker.overall_status()

    result = await db.execute(
        select(ESIRateLimitEvent)
        .order_by(ESIRateLimitEvent.occurred_at.desc())
        .limit(50)
    )
    recent_events = result.scalars().all()

    return templates.TemplateResponse("status.html", {
        "request": request,
        "groups": groups,
        "legacy": legacy,
        "request_log": request_log,
        "recent_events": recent_events,
        "overall_status": overall,
    })


@router.get("/status/banner", response_class=HTMLResponse)
async def status_banner(request: Request):
    overall = rate_limit_tracker.overall_status()

    if overall == "ok":
        return HTMLResponse(
            '<div id="esi-banner" '
            'hx-get="/status/banner" hx-trigger="every 30s" hx-swap="outerHTML"></div>'
        )
    elif overall == "warning":
        colour = "bg-yellow-900/60 border-yellow-600/50 text-yellow-200"
        icon = "⚠"
        msg = "ESI rate limit warning — approaching token limit on one or more route groups."
    else:
        colour = "bg-red-900/60 border-red-600/50 text-red-200"
        icon = "✕"
        msg = "ESI rate limit critical — heavily throttled. Check <a href='/status' class='underline'>ESI Status</a> for details."

    return HTMLResponse(
        f'<div id="esi-banner" '
        f'hx-get="/status/banner" hx-trigger="every 30s" hx-swap="outerHTML" '
        f'class="border-b {colour} px-4 py-2 text-sm text-center font-mono">'
        f'{icon} {msg}'
        f'</div>'
    )
