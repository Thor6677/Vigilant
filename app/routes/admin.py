"""Admin dashboard — system health, user management, and operational tools."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from app.db.models import (
    get_db, User, Character, CharacterDashboardCache, WalletSnapshot,
    MiningLedgerEntry, DScanResult, CharacterAssetCache, CorpInventoryThreshold,
    AdminAuditLog, RegistrationAllowlist, AsyncSessionLocal,
)
from app.db.cache import cache_stats, ESICache
from app.esi.client import get_etag_cache_stats
from app.esi.rate_limit import rate_limit_tracker
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


# ── Auth dependency ──────────────────────────────────────────────────────────

async def require_admin(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Require admin or manager role."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.role not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def _log_audit(db: AsyncSession, event_type: str, user_id: int = None,
                     character_id: int = None, detail: str = None, ip: str = None):
    db.add(AdminAuditLog(
        user_id=user_id, event_type=event_type,
        detail=detail, character_id=character_id, ip_address=ip,
    ))
    await db.commit()


def _format_bytes(size: int) -> str:
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    if size >= 1e6:
        return f"{size / 1e6:.1f} MB"
    if size >= 1e3:
        return f"{size / 1e3:.1f} KB"
    return f"{size} B"


def _format_age(dt: datetime | None) -> str:
    if not dt:
        return "Never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


# ── Main page ────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    return templates.TemplateResponse("admin.html", {"request": request})


# ── Section endpoints ────────────────────────────────────────────────────────

@router.get("/section/overview", response_class=HTMLResponse)
async def admin_overview(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    from app.routes.dashboard import get_scheduler_state

    sched = get_scheduler_state()
    uptime_secs = (datetime.now(timezone.utc) - sched["app_start_time"]).total_seconds()

    # DB file size
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    try:
        db_size = os.path.getsize(db_path)
    except Exception:
        db_size = 0

    # SDE age
    sde_row = await db.execute(text("SELECT value FROM sde_meta WHERE key = 'last_updated'"))
    sde_last = sde_row.scalar_one_or_none()
    sde_age = None
    if sde_last:
        try:
            sde_dt = datetime.fromisoformat(sde_last)
            sde_age = (datetime.now(timezone.utc) - sde_dt.replace(tzinfo=timezone.utc)).days
        except Exception:
            pass

    # Counts
    user_count = (await db.execute(text("SELECT COUNT(*) FROM users"))).scalar()
    char_count = (await db.execute(text("SELECT COUNT(*) FROM characters"))).scalar()

    esi_status = rate_limit_tracker.overall_status()

    return templates.TemplateResponse("partials/admin_overview.html", {
        "request": request,
        "uptime": _format_duration(uptime_secs),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "db_size": _format_bytes(db_size),
        "sde_age_days": sde_age,
        "esi_status": esi_status,
        "user_count": user_count,
        "char_count": char_count,
        "queue_depth": sched["queue_depth"],
        "active_syncs": sched["sync_concurrency"] - sched["semaphore_available"],
        "sync_concurrency": sched["sync_concurrency"],
    })


@router.get("/section/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    # Users with character counts. Single bulk query each for users/chars/caches
    # then join in Python — was N+1 (one chars query per user, one cache query
    # per character).
    users_result = await db.execute(
        select(User).order_by(User.last_login.desc())
    )
    users = users_result.scalars().all()

    user_ids = [u.id for u in users]
    chars_by_user: dict[int, list[Character]] = {uid: [] for uid in user_ids}
    char_ids: list[int] = []
    if user_ids:
        all_chars = (await db.execute(
            select(Character).where(Character.user_id.in_(user_ids))
            .order_by(Character.character_name)
        )).scalars().all()
        for c in all_chars:
            chars_by_user.setdefault(c.user_id, []).append(c)
            char_ids.append(c.character_id)

    cache_by_cid: dict[int, CharacterDashboardCache] = {}
    if char_ids:
        all_caches = (await db.execute(
            select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(char_ids))
        )).scalars().all()
        cache_by_cid = {c.character_id: c for c in all_caches}

    now = datetime.now(timezone.utc)
    user_data = []
    for u in users:
        chars = chars_by_user.get(u.id, [])
        main_char = next((c for c in chars if c.is_main), chars[0] if chars else None)

        char_list = []
        for c in chars:
            expiry = c.token_expiry
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry:
                hours_left = (expiry - now).total_seconds() / 3600
                token_status = "ok" if hours_left > 24 else "expiring" if hours_left > 0 else "expired"
            else:
                token_status = "unknown"

            cache = cache_by_cid.get(c.character_id)
            char_list.append({
                "character_id": c.character_id,
                "character_name": c.character_name,
                "corporation_name": c.corporation_name or "—",
                "token_status": token_status,
                "sync_status": cache.sync_status if cache else "never",
                "last_synced": cache.last_synced if cache else None,
                "scope_count": len((c.scopes or "").split()) if c.scopes else 0,
            })

        user_data.append({
            "user": u,
            "char_count": len(chars),
            "characters": char_list,
            "display_name": main_char.character_name if main_char else f"User {u.id}",
            "main_character_id": main_char.character_id if main_char else None,
        })

    # Registration allowlist
    allow_result = await db.execute(
        select(RegistrationAllowlist).order_by(RegistrationAllowlist.entry_type, RegistrationAllowlist.name)
    )
    allowlist = allow_result.scalars().all()

    # Check if allowlist is enabled (has any entries)
    allowlist_enabled = len(allowlist) > 0

    return templates.TemplateResponse("partials/admin_users.html", {
        "request": request,
        "users": user_data,
        "allowlist": allowlist,
        "allowlist_enabled": allowlist_enabled,
        "format_age": _format_age,
        "admin_id": admin.id,
        "admin_role": admin.role or "admin",
    })


@router.get("/section/esi", response_class=HTMLResponse)
async def admin_esi(request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    from app.routes.status import _build_context, _compute_chart_data
    from app.db.models import ESIRateLimitEvent

    etag = get_etag_cache_stats()
    db_cache = await cache_stats(db)

    # Reuse the full status page context
    ctx = await _build_context(db, admin.id)

    # Significant events from DB — split into active (unarchived) and archive.
    ev_result = await db.execute(
        select(ESIRateLimitEvent)
        .where(ESIRateLimitEvent.archived_at.is_(None))
        .order_by(ESIRateLimitEvent.occurred_at.desc())
        .limit(50)
    )
    recent_events = ev_result.scalars().all()
    archived_result = await db.execute(
        select(ESIRateLimitEvent)
        .where(ESIRateLimitEvent.archived_at.is_not(None))
        .order_by(ESIRateLimitEvent.archived_at.desc())
        .limit(50)
    )
    archived_events = archived_result.scalars().all()

    return templates.TemplateResponse("partials/admin_esi.html", {
        "request": request,
        "etag_cache": etag,
        "db_cache": db_cache,
        "esi_status": ctx["overall_status"],
        "total_requests": ctx["total_requests"],
        "ok_count": ctx["ok_count"],
        "cached_count": ctx["cached_count"],
        "rejected_count": ctx["rejected_count"],
        "ok_rate": ctx["ok_rate"],
        "groups": list(rate_limit_tracker.groups.values()),
        "legacy": rate_limit_tracker.legacy,
        "request_log": list(reversed(rate_limit_tracker.request_log))[:100],
        "recent_events": recent_events,
        "archived_events": archived_events,
        "chart_data_json": json.dumps(_compute_chart_data()),
    })


@router.post("/esi/events/{event_id}/dismiss", response_class=HTMLResponse)
async def admin_esi_dismiss_event(
    event_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Soft-archive a single rate-limit event. Rerenders the ESI section."""
    from app.db.models import ESIRateLimitEvent
    row = await db.get(ESIRateLimitEvent, event_id)
    if row is not None and row.archived_at is None:
        row.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
    return await admin_esi(request, db, admin)


@router.post("/esi/events/dismiss-all", response_class=HTMLResponse)
async def admin_esi_dismiss_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Archive every currently-active event. Rerenders the ESI section."""
    from app.db.models import ESIRateLimitEvent
    from sqlalchemy import update
    await db.execute(
        update(ESIRateLimitEvent)
        .where(ESIRateLimitEvent.archived_at.is_(None))
        .values(archived_at=datetime.now(timezone.utc).replace(tzinfo=None))
    )
    await db.commit()
    return await admin_esi(request, db, admin)


@router.get("/section/scheduler", response_class=HTMLResponse)
async def admin_scheduler(request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    from app.routes.dashboard import get_scheduler_state, _queued_sync
    from app.routes.status import _stale_field_counts, _age_str

    sched = get_scheduler_state()
    now = datetime.now(timezone.utc)

    # Detect stuck characters (in queue > 3 min)
    stuck = []
    for cid, queued_at in sched["queued_characters"].items():
        qa = queued_at if queued_at.tzinfo else queued_at.replace(tzinfo=timezone.utc)
        age = (now - qa).total_seconds()
        if age > 180:
            stuck.append({"character_id": cid, "queued_for": _format_duration(age)})

    # Character sync table (from status page)
    char_result = await db.execute(select(Character))
    characters = list(char_result.scalars().all())
    cids = [c.character_id for c in characters]
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id.in_(cids))
    ) if cids else None
    char_caches = {c.character_id: c for c in (cache_result.scalars().all() if cache_result else [])}

    import json as _json
    char_sync_rows = []
    for char in characters:
        cache = char_caches.get(char.character_id)
        stale, total_fields = _stale_field_counts(char, cache)
        sync_warnings = {}
        if cache and cache.sync_warnings_json:
            try:
                sync_warnings = _json.loads(cache.sync_warnings_json)
            except Exception:
                pass
        char_sync_rows.append({
            "character_id": char.character_id,
            "character_name": char.character_name,
            "last_synced_str": _age_str(cache.last_synced if cache else None),
            "sync_status": cache.sync_status if cache else "idle",
            "queued": char.character_id in _queued_sync,
            "stale_fields": stale,
            "total_fields": total_fields,
            "sync_warnings": sync_warnings,
            "warn_count": len(sync_warnings),
            "sync_error": cache.sync_error if cache else None,
        })

    return templates.TemplateResponse("partials/admin_scheduler.html", {
        "request": request,
        "queue_depth": sched["queue_depth"],
        "active_syncs": sched["sync_concurrency"] - sched["semaphore_available"],
        "sync_concurrency": sched["sync_concurrency"],
        "semaphore_available": sched["semaphore_available"],
        "last_inv_check": _format_age(sched.get("last_inv_check")),
        "notification_queues": sched["notification_queues"],
        "stuck_characters": stuck,
        "char_sync_rows": char_sync_rows,
    })


@router.get("/section/database", response_class=HTMLResponse)
async def admin_database(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    try:
        db_size = os.path.getsize(db_path)
    except Exception:
        db_size = 0

    tables = [
        ("users", "SELECT COUNT(*) FROM users"),
        ("characters", "SELECT COUNT(*) FROM characters"),
        ("character_dashboard_cache", "SELECT COUNT(*) FROM character_dashboard_cache"),
        ("character_asset_cache", "SELECT COUNT(*) FROM character_asset_cache"),
        ("wallet_snapshots", "SELECT COUNT(*) FROM wallet_snapshots"),
        ("esi_cache", "SELECT COUNT(*) FROM esi_cache"),
        ("esi_rate_limit_events", "SELECT COUNT(*) FROM esi_rate_limit_events"),
        ("mining_ledger_entries", "SELECT COUNT(*) FROM mining_ledger_entries"),
        ("dscan_results", "SELECT COUNT(*) FROM dscan_results"),
        ("corp_inventory_thresholds", "SELECT COUNT(*) FROM corp_inventory_thresholds"),
        ("admin_audit_log", "SELECT COUNT(*) FROM admin_audit_log"),
    ]
    sde_tables = [
        ("sde_types", "SELECT COUNT(*) FROM sde_types"),
        ("sde_systems", "SELECT COUNT(*) FROM sde_systems"),
        ("sde_jumps", "SELECT COUNT(*) FROM sde_jumps"),
        ("sde_stations", "SELECT COUNT(*) FROM sde_stations"),
        ("sde_regions", "SELECT COUNT(*) FROM sde_regions"),
        ("sde_constellations", "SELECT COUNT(*) FROM sde_constellations"),
        ("sde_blueprint_materials", "SELECT COUNT(*) FROM sde_blueprint_materials"),
        ("sde_blueprint_info", "SELECT COUNT(*) FROM sde_blueprint_info"),
        ("sde_type_materials", "SELECT COUNT(*) FROM sde_type_materials"),
        ("sde_compressible", "SELECT COUNT(*) FROM sde_compressible"),
    ]

    app_rows = []
    for name, query in tables:
        try:
            count = (await db.execute(text(query))).scalar()
            app_rows.append({"name": name, "count": count})
        except Exception:
            app_rows.append({"name": name, "count": "—"})

    sde_rows = []
    for name, query in sde_tables:
        try:
            count = (await db.execute(text(query))).scalar()
            sde_rows.append({"name": name, "count": count})
        except Exception:
            sde_rows.append({"name": name, "count": "—"})

    db_cache = await cache_stats(db)

    return templates.TemplateResponse("partials/admin_database.html", {
        "request": request,
        "db_size": _format_bytes(db_size),
        "app_tables": app_rows,
        "sde_tables": sde_rows,
        "db_cache": db_cache,
    })


@router.get("/section/sde", response_class=HTMLResponse)
async def admin_sde(request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    sde_row = await db.execute(text("SELECT value FROM sde_meta WHERE key = 'last_updated'"))
    sde_last = sde_row.scalar_one_or_none()
    sde_age_days = None
    sde_last_str = None
    if sde_last:
        try:
            sde_dt = datetime.fromisoformat(sde_last)
            sde_age_days = (datetime.now(timezone.utc) - sde_dt.replace(tzinfo=timezone.utc)).days
            sde_last_str = sde_dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            sde_last_str = sde_last

    return templates.TemplateResponse("partials/admin_sde.html", {
        "request": request,
        "sde_last_updated": sde_last_str,
        "sde_age_days": sde_age_days,
        "needs_update": sde_age_days is not None and sde_age_days >= 30,
    })


@router.get("/section/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, filter: str = "",
                      db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    query = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(100)
    if filter:
        query = query.where(AdminAuditLog.event_type == filter)
    result = await db.execute(query)
    events = result.scalars().all()

    # Resolve character names
    char_ids = {e.character_id for e in events if e.character_id}
    char_names = {}
    if char_ids:
        cr = await db.execute(select(Character.character_id, Character.character_name).where(Character.character_id.in_(char_ids)))
        char_names = {r.character_id: r.character_name for r in cr.fetchall()}

    return templates.TemplateResponse("partials/admin_audit.html", {
        "request": request,
        "events": events,
        "char_names": char_names,
        "filter": filter,
        "format_age": _format_age,
    })


# ── Action handlers ──────────────────────────────────────────────────────────

@router.post("/action/force-sync/{character_id}", response_class=HTMLResponse)
async def admin_force_sync(character_id: int, request: Request,
                           db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    from app.routes.dashboard import _sync_task

    char_result = await db.execute(select(Character).where(Character.character_id == character_id))
    char = char_result.scalar_one_or_none()
    if not char:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Character not found.</div>')

    # Clear field_synced to force all fields stale
    cache_result = await db.execute(
        select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == character_id)
    )
    cache = cache_result.scalar_one_or_none()
    if cache:
        cache.field_synced_json = "{}"
        await db.commit()

    asyncio.create_task(_sync_task(character_id))
    await _log_audit(db, "admin_force_sync", admin.id, character_id,
                     f"Force sync triggered for {char.character_name}",
                     request.client.host if request.client else None)

    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">Sync queued for {char.character_name}.</div>')


@router.post("/action/sync-all", response_class=HTMLResponse)
async def admin_sync_all(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    from app.routes.dashboard import _sync_all_task

    result = await db.execute(select(Character.character_id))
    char_ids = [r[0] for r in result.fetchall()]

    if char_ids:
        # Clear all field_synced timestamps
        await db.execute(
            text("UPDATE character_dashboard_cache SET field_synced_json = '{}'")
        )
        await db.commit()
        asyncio.create_task(_sync_all_task(char_ids))

    await _log_audit(db, "admin_sync_all", admin.id, detail=f"Sync all triggered for {len(char_ids)} characters",
                     ip=request.client.host if request.client else None)

    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">Sync queued for {len(char_ids)} characters.</div>')


@router.post("/action/sde-update", response_class=HTMLResponse)
async def admin_sde_update(request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    from app.sde.loader import ensure_sde_loaded

    # Delete last_updated to force re-download
    await db.execute(text("DELETE FROM sde_meta WHERE key = 'last_updated'"))
    await db.commit()

    asyncio.create_task(ensure_sde_loaded())

    await _log_audit(db, "admin_sde_update", admin.id,
                     detail="SDE force update triggered",
                     ip=request.client.host if request.client else None)

    return HTMLResponse('<div class="b-empty" style="color:var(--success);">SDE update started. This may take a few minutes.</div>')


@router.post("/player-count/backfill")
async def admin_player_count_backfill(
    request: Request,
    source: str = "all",
    mode: str = "fine",
    admin: User = Depends(require_admin),
):
    """Trigger historical PCU backfill from third-party archives.

    Params:
      source: 'all' | 'net' | 'com'  (com not yet implemented)
      mode:   'fine' (chunked, ~50 min, ~8M rows, 1-min resolution; runs in
              background — poll /admin/player-count/status)
              'coarse' (single GET, ~800 rows weekly resolution; synchronous)

    Idempotent — the (source, recorded_at) unique constraint dedups across
    re-runs.
    """
    from fastapi.responses import JSONResponse
    from app.intel.player_count_backfill import run_backfill
    if source not in ("all", "net", "com"):
        return JSONResponse({"error": "source must be 'all' | 'net' | 'com'"}, status_code=400)
    if mode not in ("fine", "coarse"):
        return JSONResponse({"error": "mode must be 'fine' | 'coarse'"}, status_code=400)
    try:
        summary = await run_backfill(source=source, mode=mode)
        return JSONResponse(summary)
    except Exception as e:
        logger.exception("player-count backfill failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/player-count/status")
async def admin_player_count_status(
    request: Request,
    admin: User = Depends(require_admin),
):
    """Poll the fine-backfill task's progress + per-source row counts."""
    from fastapi.responses import JSONResponse
    from app.intel.player_count_backfill import fine_backfill_state
    async with AsyncSessionLocal() as db:
        from app.db.models import PlayerCountSnapshot
        rows = (await db.execute(
            select(
                PlayerCountSnapshot.source,
                func.count(),
                func.min(PlayerCountSnapshot.recorded_at),
                func.max(PlayerCountSnapshot.recorded_at),
            ).group_by(PlayerCountSnapshot.source)
        )).all()
    return JSONResponse({
        "fine_state": fine_backfill_state(),
        "rows_per_source": [
            {
                "source": s,
                "rows": int(n),
                "earliest": str(mn) if mn else None,
                "latest": str(mx) if mx else None,
            }
            for s, n, mn, mx in rows
        ],
    })


@router.post("/action/cache-purge", response_class=HTMLResponse)
async def admin_cache_purge(request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    result = await db.execute(
        text("DELETE FROM esi_cache WHERE expires_at < :now"),
        {"now": datetime.now(timezone.utc).isoformat()},
    )
    await db.commit()
    deleted = result.rowcount

    await _log_audit(db, "admin_cache_purge", admin.id,
                     detail=f"Purged {deleted} expired cache entries",
                     ip=request.client.host if request.client else None)

    return HTMLResponse(f'<div class="b-empty" style="color:var(--success);">Purged {deleted} expired cache entries.</div>')


# ── User management actions ──────────────────────────────────────────────────

@router.post("/action/set-role/{user_id}/{new_role}", response_class=HTMLResponse)
async def admin_set_role(user_id: int, new_role: str, request: Request,
                         db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    if new_role not in ("user", "manager", "admin"):
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid role.</div>')
    if user_id == admin.id:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Cannot change your own role.</div>')

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">User not found.</div>')

    # Managers cannot grant/revoke admin or modify admin users
    if admin.role == "manager":
        if new_role == "admin":
            return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Managers cannot grant admin role.</div>')
        if user.role == "admin":
            return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Managers cannot modify admin users.</div>')

    old_role = user.role
    user.role = new_role
    user.is_admin = new_role in ("admin", "manager")  # Keep is_admin in sync for session/nav
    await db.commit()
    await _log_audit(db, "admin_set_role", admin.id,
                     detail=f"Role changed for user {user_id}: {old_role} -> {new_role}",
                     ip=request.client.host if request.client else None)

    return await admin_users(request, db, admin)


@router.post("/action/remove-user/{user_id}", response_class=HTMLResponse)
async def admin_remove_user(user_id: int, request: Request,
                            db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    if user_id == admin.id:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Cannot remove your own account.</div>')

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">User not found.</div>')

    # Managers cannot delete admin users
    if admin.role == "manager" and user.role == "admin":
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Managers cannot delete admin users.</div>')

    # Delete all characters belonging to this user
    chars_result = await db.execute(select(Character).where(Character.user_id == user_id))
    for char in chars_result.scalars().all():
        # Clean up associated caches
        await db.execute(text("DELETE FROM character_dashboard_cache WHERE character_id = :cid"), {"cid": char.character_id})
        await db.execute(text("DELETE FROM character_asset_cache WHERE character_id = :cid"), {"cid": char.character_id})
        await db.delete(char)

    # Delete user's inventory thresholds
    await db.execute(text("DELETE FROM corp_inventory_thresholds WHERE user_id = :uid"), {"uid": user_id})
    await db.delete(user)
    await db.commit()

    await _log_audit(db, "admin_remove_user", admin.id,
                     detail=f"Removed user {user_id} and all associated characters",
                     ip=request.client.host if request.client else None)

    return await admin_users(request, db, admin)


@router.post("/action/remove-character/{character_id}", response_class=HTMLResponse)
async def admin_remove_character(character_id: int, request: Request,
                                 db: AsyncSession = Depends(get_db),
                                 admin: User = Depends(require_admin)):
    result = await db.execute(select(Character).where(Character.character_id == character_id))
    char = result.scalar_one_or_none()
    if not char:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Character not found.</div>')

    char_name = char.character_name
    await db.execute(text("DELETE FROM character_dashboard_cache WHERE character_id = :cid"), {"cid": character_id})
    await db.execute(text("DELETE FROM character_asset_cache WHERE character_id = :cid"), {"cid": character_id})
    await db.delete(char)
    await db.commit()

    await _log_audit(db, "admin_remove_character", admin.id, character_id,
                     f"Removed character {char_name}",
                     request.client.host if request.client else None)

    return await admin_users(request, db, admin)


# ── Allowlist management ─────────────────────────────────────────────────────

@router.get("/action/allowlist-search", response_class=HTMLResponse)
async def admin_allowlist_search(request: Request, db: AsyncSession = Depends(get_db),
                                 admin: User = Depends(require_admin)):
    """Search ESI for characters/corporations/alliances by name."""
    query = request.query_params.get("q", "").strip()
    category = request.query_params.get("category", "character")

    if len(query) < 3:
        return HTMLResponse("")

    from app.esi.client import ESIClient, get_client
    from html import escape

    categories_map = {"character": "character", "corporation": "corporation", "alliance": "alliance"}
    esi_cat = categories_map.get(category, "character")

    # Try authenticated fuzzy search first (requires esi-search scope)
    search_scope = "esi-search.search_structures.v1"
    char_result = await db.execute(
        select(Character).where(Character.scopes.contains(search_scope)).limit(1)
    )
    char = char_result.scalar_one_or_none()

    results = []
    if char:
        try:
            client = await get_client(char, db)
            search_data = await client.get(
                f"/characters/{char.character_id}/search/",
                params={"categories": esi_cat, "search": query, "strict": "false"},
            )
            ids = search_data.get(esi_cat, [])[:10]
            if ids:
                pub_client = ESIClient("")
                names_data = await pub_client.post_public("/universe/names/", ids)
                results = [{"id": item["id"], "name": item["name"]} for item in names_data]
        except Exception:
            pass  # Fall through to exact-match fallback

    # Fallback: exact match via /universe/ids/ (public, no auth)
    if not results:
        universe_cat = {"character": "characters", "corporation": "corporations", "alliance": "alliances"}
        try:
            pub_client = ESIClient("")
            id_data = await pub_client.post_public("/universe/ids/", [query])
            results = [
                {"id": item["id"], "name": item["name"]}
                for item in id_data.get(universe_cat.get(category, "characters"), [])
            ][:10]
        except Exception:
            return HTMLResponse('<div style="font-size:10px;color:var(--danger);padding:0.25rem;">Search failed.</div>')

    if not results:
        return HTMLResponse('<div style="font-size:10px;color:var(--muted);padding:0.25rem;">No results found.</div>')

    # Render clickable result rows
    html_parts = []
    for r in results:
        safe_name = escape(r["name"], quote=True)
        html_parts.append(
            f'<div style="padding:0.25rem 0.5rem;font-size:10px;color:var(--text);'
            f'cursor:pointer;border-bottom:1px solid var(--border);" '
            f'onmouseover="this.style.background=\'var(--border)\'" '
            f'onmouseout="this.style.background=\'none\'" '
            f'data-id="{r["id"]}" data-name="{safe_name}" '
            f'onclick="selectAllowlistResult(+this.dataset.id, this.dataset.name)">'
            f'{safe_name} <span style="color:var(--muted);">({r["id"]})</span></div>'
        )
    return HTMLResponse("".join(html_parts))


@router.post("/action/allowlist-add", response_class=HTMLResponse)
async def admin_allowlist_add(request: Request, db: AsyncSession = Depends(get_db),
                              admin: User = Depends(require_admin)):
    form = await request.form()
    entry_type = form.get("entry_type", "")
    eve_id = form.get("eve_id", "")
    name = form.get("name", "").strip()

    if entry_type not in ("character", "corporation", "alliance") or not eve_id:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid entry type or ID.</div>')

    try:
        eve_id_int = int(eve_id)
    except ValueError:
        return HTMLResponse('<div class="b-empty" style="color:var(--danger);">Invalid ID.</div>')

    # Resolve name from ESI if not provided
    if not name:
        from app.esi.client import ESIClient
        try:
            client = ESIClient("")
            if entry_type == "character":
                data = await client.get_public(f"/characters/{eve_id_int}/")
                name = data.get("name", f"Character {eve_id_int}")
            elif entry_type == "corporation":
                data = await client.get_public(f"/corporations/{eve_id_int}/")
                name = data.get("name", f"Corporation {eve_id_int}")
            elif entry_type == "alliance":
                data = await client.get_public(f"/alliances/{eve_id_int}/")
                name = data.get("name", f"Alliance {eve_id_int}")
        except Exception:
            name = f"{entry_type.title()} {eve_id_int}"

    # Check for duplicate
    existing = await db.execute(
        select(RegistrationAllowlist).where(
            RegistrationAllowlist.entry_type == entry_type,
            RegistrationAllowlist.eve_id == eve_id_int,
        )
    )
    if existing.scalar_one_or_none():
        return HTMLResponse(f'<div class="b-empty" style="color:var(--warn);">{name} is already on the allowlist.</div>')

    db.add(RegistrationAllowlist(
        entry_type=entry_type, eve_id=eve_id_int, name=name, added_by=admin.id,
    ))
    await db.commit()

    await _log_audit(db, "admin_allowlist_add", admin.id,
                     detail=f"Added {entry_type} {name} ({eve_id_int}) to allowlist",
                     ip=request.client.host if request.client else None)

    return await admin_users(request, db, admin)


@router.post("/action/allowlist-remove/{entry_id}", response_class=HTMLResponse)
async def admin_allowlist_remove(entry_id: int, request: Request,
                                 db: AsyncSession = Depends(get_db),
                                 admin: User = Depends(require_admin)):
    result = await db.execute(select(RegistrationAllowlist).where(RegistrationAllowlist.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry:
        detail = f"Removed {entry.entry_type} {entry.name} ({entry.eve_id}) from allowlist"
        await db.delete(entry)
        await db.commit()
        await _log_audit(db, "admin_allowlist_remove", admin.id, detail=detail,
                         ip=request.client.host if request.client else None)

    return await admin_users(request, db, admin)
