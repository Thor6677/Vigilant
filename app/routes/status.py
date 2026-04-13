import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import get_db, ESIRateLimitEvent, Character, CharacterDashboardCache
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


def _compute_chart_data() -> dict:
    """Bucket request_log into per-minute counts for the last 30 minutes."""
    now = datetime.now(timezone.utc)
    fetched = [0] * 30
    cached = [0] * 30
    rejected = [0] * 30
    for entry in rate_limit_tracker.request_log:
        age_s = (now - entry.timestamp).total_seconds()
        minute = int(age_s // 60)
        if 0 <= minute < 30:
            idx = 29 - minute
            if entry.status_code == 304:
                cached[idx] += 1
            elif entry.status_code < 400:
                fetched[idx] += 1
            else:
                rejected[idx] += 1
    labels = [f"-{29 - i}m" if i < 29 else "now" for i in range(30)]
    return {"labels": labels, "fetched": fetched, "cached": cached, "rejected": rejected}


def _stale_field_counts(char: Character, cache: CharacterDashboardCache | None) -> tuple[int, int]:
    """Returns (stale_fields, total_scoped_fields)."""
    from app.routes.dashboard import FIELD_CACHE_SECONDS, FIELD_SCOPES
    scopes = char.scopes or ""
    field_synced = json.loads(cache.field_synced_json) if (cache and cache.field_synced_json) else {}
    now = datetime.now(timezone.utc)
    stale, total = 0, 0
    for field, cache_secs in FIELD_CACHE_SECONDS.items():
        scope = FIELD_SCOPES[field]
        if scope and scope not in scopes:
            continue
        total += 1
        last_str = field_synced.get(field)
        if not last_str or (now - datetime.fromisoformat(last_str)).total_seconds() >= cache_secs:
            stale += 1
    return stale, total


async def _build_context(db: AsyncSession, user_id: int) -> dict:
    from app.routes.dashboard import _queued_sync

    tracker = rate_limit_tracker
    request_log = list(reversed(tracker.request_log))
    overall = tracker.overall_status()

    # Summary stats — 304 Not Modified counts as success (ETag cache hit)
    log_all = list(tracker.request_log)
    total_requests = len(log_all)
    ok_count = sum(1 for e in log_all if e.status_code < 400)
    rejected_count = sum(1 for e in log_all if e.status_code >= 400)
    cached_count = sum(1 for e in log_all if e.status_code == 304)
    ok_rate = round(ok_count / total_requests * 100) if total_requests else 100

    # Character sync status — only show characters belonging to this user
    char_result = await db.execute(select(Character).where(Character.user_id == user_id))
    characters = list(char_result.scalars().all())
    cids = [c.character_id for c in characters]
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(cids))
    )
    char_caches = {c.character_id: c for c in cache_result.scalars().all()}

    char_sync_rows = []
    for char in characters:
        cache = char_caches.get(char.character_id)
        stale, total_fields = _stale_field_counts(char, cache)
        sync_warnings = {}
        if cache and cache.sync_warnings_json:
            try:
                sync_warnings = json.loads(cache.sync_warnings_json)
            except Exception:
                pass
        char_sync_rows.append({
            "character_id": char.character_id,
            "character_name": char.character_name,
            "last_synced": cache.last_synced if cache else None,
            "last_synced_str": _age_str(cache.last_synced if cache else None),
            "sync_status": cache.sync_status if cache else "idle",
            "queued": char.character_id in _queued_sync,
            "stale_fields": stale,
            "total_fields": total_fields,
            "sync_warnings": sync_warnings,
            "warn_count": len(sync_warnings),
            "sync_error": cache.sync_error if cache else None,
        })

    syncing_count = sum(1 for r in char_sync_rows if r["sync_status"] == "syncing" or r["queued"])

    # ESI rate limit events
    result = await db.execute(
        select(ESIRateLimitEvent).order_by(ESIRateLimitEvent.occurred_at.desc()).limit(50)
    )
    recent_events = result.scalars().all()

    return dict(
        groups=list(tracker.groups.values()),
        legacy=tracker.legacy,
        request_log=request_log,
        recent_events=recent_events,
        overall_status=overall,
        total_requests=total_requests,
        ok_count=ok_count,
        rejected_count=rejected_count,
        cached_count=cached_count,
        ok_rate=ok_rate,
        char_sync_rows=char_sync_rows,
        syncing_count=syncing_count,
        chart_data=json.dumps(_compute_chart_data()),
    )


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    # Status page is now part of the admin dashboard
    return RedirectResponse("/admin")


@router.get("/status/data", response_class=HTMLResponse)
async def status_data(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX partial — refreshes live sections without touching the chart."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    ctx = await _build_context(db, user_id)
    ctx["request"] = request
    return templates.TemplateResponse("status_data.html", ctx)


@router.get("/status/chart.json")
async def status_chart_json(request: Request):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "Not authenticated"}, status_code=403)
    return JSONResponse(_compute_chart_data())


@router.get("/status/banner", response_class=HTMLResponse)
async def status_banner(request: Request):
    if not request.session.get("user_id"):
        return HTMLResponse('<div id="esi-banner"></div>')
    # Only show ESI rate limit warnings to admins/managers
    if not request.session.get("is_admin"):
        return HTMLResponse(
            '<div id="esi-banner" '
            'hx-get="/status/banner" hx-trigger="every 30s" hx-swap="outerHTML"></div>'
        )
    overall = rate_limit_tracker.overall_status()

    if overall == "ok":
        return HTMLResponse(
            '<div id="esi-banner" '
            'hx-get="/status/banner" hx-trigger="every 30s" hx-swap="outerHTML"></div>'
        )
    elif overall == "warning":
        border_color = "#c8a951"
        text_color = "#c8a951"
        icon = "⚠"
        msg = "ESI rate limit warning — approaching token limit on one or more route groups."
    else:
        border_color = "#cc3333"
        text_color = "#cc3333"
        icon = "✕"
        msg = "ESI rate limit critical — heavily throttled. Check <a href='/admin' style='text-decoration:underline;'>Admin</a> for details."

    style = (
        f"border-bottom:1px solid {border_color};"
        f"color:{text_color};"
        "background:#0e0e0e;"
        "padding:0.4rem 2rem;"
        "font-size:11px;"
        "letter-spacing:0.1em;"
        "text-align:center;"
        "font-family:'JetBrains Mono',monospace;"
        "text-transform:uppercase;"
    )
    return HTMLResponse(
        f'<div id="esi-banner" '
        f'hx-get="/status/banner" hx-trigger="every 30s" hx-swap="outerHTML" '
        f'style="{style}">'
        f'{icon} {msg}'
        f'</div>'
    )
