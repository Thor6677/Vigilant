"""T-071: needs-attention strip — route layer.

Loads a user's characters + their `CharacterDashboardCache` rows (cache
only, no ESI calls), builds pilot dicts in the shape `app.dashboard.
attention.build_attention` documents, and renders the sorted, dismissal-
filtered strip. See app/dashboard/attention.py for the rule engine itself.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.auth import status as perm_status
from app.dashboard.attention import AttentionItem, build_attention
from app.db.models import Character, CharacterDashboardCache, DashboardAttentionDismissal, get_db
from app.routes.dashboard import _queued_sync

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")

_DISMISS_DURATIONS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "change": None}


def _attention_age(since: datetime | None) -> str | None:
    """Display-time age for the partial ('6h', '2d', ...). Recomputed against
    the real clock at render time rather than reusing the `now` the items
    were built with — the gap between the two is microseconds, and this
    keeps the template simple (no datetime math in Jinja)."""
    if since is None:
        return None
    age = max(0.0, (datetime.now(timezone.utc) - since).total_seconds())
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)}m"
    if age < 86400:
        return f"{int(age // 3600)}h"
    return f"{int(age // 86400)}d"


templates.env.globals["attention_age"] = _attention_age


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def _load_pilots(db: AsyncSession, user_id: int) -> tuple[list[dict], datetime]:
    """One join query: every character this user owns, left-joined onto its
    dashboard cache row (characters that never synced still get a pilot
    dict, with everything cache-shaped left None)."""
    now = datetime.now(timezone.utc)

    rows = (await db.execute(
        select(
            Character.character_id, Character.character_name, Character.scopes,
            Character.account_group,
            CharacterDashboardCache.sync_warnings_json,
            CharacterDashboardCache.skillqueue_json,
            CharacterDashboardCache.pi_json,
            CharacterDashboardCache.industry_json,
            CharacterDashboardCache.field_synced_json,
            CharacterDashboardCache.last_synced,
            CharacterDashboardCache.sync_status,
            CharacterDashboardCache.sync_error,
        ).select_from(Character).outerjoin(
            CharacterDashboardCache,
            CharacterDashboardCache.character_id == Character.character_id,
        ).where(Character.user_id == user_id)
        # Same ordering the Dashboard's own "custom" sort uses (app.routes.
        # dashboard.dashboard()) — the account-idle rule picks its target
        # pilot as "the first one", and this is what makes that mean
        # something instead of "whatever order SQLite felt like".
        .order_by(Character.account_group, Character.sort_order)
    )).all()

    pilots: list[dict] = []
    for (cid, name, scopes, account_group, sync_warnings_raw, skillqueue_raw, pi_raw, industry_raw,
         field_synced_raw, last_synced, sync_status, sync_error) in rows:
        scopes = scopes or ""
        sync_warnings = _parse_json(sync_warnings_raw) or {}
        field_synced = _parse_json(field_synced_raw) or {}
        # "Ungrouped" is the column's own default, not a real account — see
        # the "Account grouping" note in app.dashboard.attention's docstring.
        account_group = None if not account_group or account_group == "Ungrouped" else account_group

        industry_synced_at = None
        raw_ts = field_synced.get("industry")
        if raw_ts:
            try:
                industry_synced_at = _aware(datetime.fromisoformat(raw_ts))
            except (ValueError, TypeError):
                industry_synced_at = None

        skillqueue = "no_scope" if perms.SKILLQUEUE not in scopes else _parse_json(skillqueue_raw)
        pi = "no_scope" if perms.PLANETS not in scopes else _parse_json(pi_raw)
        industry_jobs = "no_scope" if perms.JOBS not in scopes else _parse_json(industry_raw)

        # A character queued-but-not-yet-started is shown as "syncing" on the
        # dashboard card too (dashboard.py's own sync_statuses map) — same
        # reasoning: the htmx poller and this strip should agree on what
        # "in progress" means.
        effective_status = "syncing" if cid in _queued_sync else (sync_status or "idle")

        pilots.append({
            "character_id": cid,
            "character_name": name,
            "account_group": account_group,
            "needs_reauth": perm_status.token_failed(sync_warnings),
            "skillqueue": skillqueue,
            "pi": pi,
            "industry_jobs": industry_jobs,
            "industry_synced_at": industry_synced_at,
            "sync_status": effective_status,
            "sync_error": sync_error,
            "last_synced": _aware(last_synced),
        })

    return pilots, now


async def _visible_items(db: AsyncSession, user_id: int, items: list[AttentionItem],
                         now: datetime) -> list[AttentionItem]:
    """Filter `items` down to what isn't actively dismissed, purging any
    dismissal row that has lapsed (past its `dismissed_until`, or whose
    fingerprint no longer matches a freshly recomputed item, or whose item
    no longer exists at all)."""
    by_key = {it.key: it for it in items}
    now_naive = now.replace(tzinfo=None)

    rows = (await db.execute(
        select(DashboardAttentionDismissal).where(DashboardAttentionDismissal.user_id == user_id)
    )).scalars().all()

    active_keys: set[str] = set()
    dirty = False
    for row in rows:
        item = by_key.get(row.item_key)
        lapsed = (
            item is None
            or row.fingerprint != item.fingerprint
            or (row.dismissed_until is not None and row.dismissed_until <= now_naive)
        )
        if lapsed:
            await db.delete(row)
            dirty = True
        else:
            active_keys.add(row.item_key)

    if dirty:
        await db.commit()

    return [it for it in items if it.key not in active_keys]


@router.get("/dashboard/attention", response_class=HTMLResponse)
async def dashboard_attention(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    pilots, now = await _load_pilots(db, user_id)
    items = build_attention(pilots, now)
    visible = await _visible_items(db, user_id, items, now)

    if not visible:
        # outerHTML swap removes the mount entirely — Cards mode looks
        # exactly like it did before this ticket.
        return HTMLResponse("")

    return templates.TemplateResponse(request, "partials/dashboard_attention.html", {"items": visible})


@router.post("/dashboard/attention/dismiss", response_class=HTMLResponse)
async def dismiss_attention_item(
    request: Request,
    key: str = Form(...),
    for_: str = Form(..., alias="for"),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)
    if for_ not in _DISMISS_DURATIONS:
        return HTMLResponse("", status_code=400)

    pilots, now = await _load_pilots(db, user_id)
    items = build_attention(pilots, now)
    # Recomputed here, from THIS user's own pilots only — a key belonging to
    # another user, or one that no longer exists, is simply not among
    # `items`, so it's refused without any separate ownership check.
    item = next((it for it in items if it.key == key), None)
    if item is None:
        return HTMLResponse("", status_code=404)

    visible = await _visible_items(db, user_id, items, now)

    delta = _DISMISS_DURATIONS[for_]
    dismissed_until = (now + delta).replace(tzinfo=None) if delta else None

    existing = (await db.execute(
        select(DashboardAttentionDismissal).where(
            DashboardAttentionDismissal.user_id == user_id,
            DashboardAttentionDismissal.item_key == key,
        )
    )).scalar_one_or_none()

    if existing:
        existing.character_id = item.character_id
        existing.fingerprint = item.fingerprint
        existing.dismissed_until = dismissed_until
    else:
        db.add(DashboardAttentionDismissal(
            user_id=user_id,
            character_id=item.character_id,
            item_key=key,
            fingerprint=item.fingerprint,
            dismissed_until=dismissed_until,
        ))
    await db.commit()

    remaining = [it for it in visible if it.key != key]
    if not remaining:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "partials/dashboard_attention.html", {"items": remaining})
