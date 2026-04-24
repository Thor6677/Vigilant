"""Intel → Watchlist.

Per-user system + hunter watchlists that fire via the killmail.stream
consumer (app/intel/killmail_stream.py::_fire_watch_alerts).

Routes:
- GET  /intel/watch                      — render the page
- POST /intel/watch/system/add           — manual system add (by name or ID)
- POST /intel/watch/system/remove/{id}   — remove one
- POST /intel/watch/system/sync-assets   — bulk-add asset-bearing systems
- POST /intel/watch/hunter/add           — manual hunter add (name + kind)
- POST /intel/watch/hunter/remove/{id}   — remove one
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Character,
    CharacterAssetCache,
    KillAlertEvent,
    UserHunterWatch,
    UserSystemWatch,
    get_db,
)
from app.db.sde_models import SDESystem
from app.sde import lookup as sde_helpers

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


_HUNTER_KINDS = {"character": "characters", "corporation": "corporations", "alliance": "alliances"}


async def _resolve_system(db: AsyncSession, name_or_id: str) -> tuple[int | None, str | None]:
    """Resolve a system name (or numeric ID) to (system_id, system_name)."""
    s = (name_or_id or "").strip()
    if not s:
        return None, None
    # Numeric ID path
    if s.isdigit():
        sid = int(s)
        row = (
            await db.execute(
                select(SDESystem.system_name).where(SDESystem.system_id == sid)
            )
        ).first()
        if row:
            return sid, row[0]
        return None, None
    # Name path — exact case-insensitive match first, else best prefix
    row = (
        await db.execute(
            select(SDESystem.system_id, SDESystem.system_name)
            .where(SDESystem.system_name.ilike(s))
            .limit(1)
        )
    ).first()
    if row:
        return row[0], row[1]
    # Fallback to contains
    hits = await sde_helpers.search_systems(db, s, limit=1)
    if hits:
        return hits[0]["system_id"], hits[0]["system_name"]
    return None, None


async def _resolve_hunter(kind: str, name: str) -> tuple[int | None, str | None]:
    """Resolve a character/corp/alliance name to (id, canonical_name) via
    ESI /universe/ids/. Returns (None, None) on failure."""
    if kind not in _HUNTER_KINDS or not name:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://esi.evetech.net/latest/universe/ids/",
                json=[name.strip()],
                headers={"User-Agent": "Vigilant/1.0 EVE Dashboard"},
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
    except Exception as e:
        log.warning("intel_watch: ESI resolve failed for %r: %s", name, e)
        return None, None
    bucket = data.get(_HUNTER_KINDS[kind]) or []
    if not bucket:
        return None, None
    # Prefer exact case-insensitive match
    lowered = name.strip().lower()
    for item in bucket:
        if (item.get("name") or "").lower() == lowered:
            return item.get("id"), item.get("name")
    return bucket[0].get("id"), bucket[0].get("name")


async def _asset_system_ids_for_user(db: AsyncSession, user_id: int) -> set[int]:
    """Extract unique system IDs from every character's asset cache for
    this user. Assets already have system_id resolved."""
    char_ids = [
        r[0]
        for r in (
            await db.execute(
                select(Character.character_id).where(Character.user_id == user_id)
            )
        ).all()
    ]
    if not char_ids:
        return set()
    rows = await db.execute(
        select(CharacterAssetCache.assets_json).where(
            CharacterAssetCache.character_id.in_(char_ids)
        )
    )
    out: set[int] = set()
    for (assets_json,) in rows.all():
        if not assets_json:
            continue
        try:
            assets = json.loads(assets_json)
        except Exception:
            continue
        for a in assets:
            sid = a.get("system_id")
            if isinstance(sid, int):
                out.add(sid)
    return out


@router.get("/intel/watch")
async def intel_watch(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Current system watches
    sys_rows = (
        await db.execute(
            select(UserSystemWatch.id, UserSystemWatch.system_id, UserSystemWatch.label, UserSystemWatch.created_at)
            .where(UserSystemWatch.user_id == user_id)
            .order_by(UserSystemWatch.created_at.desc())
        )
    ).all()
    watched_system_ids = [r[1] for r in sys_rows]
    # Resolve system names
    system_name_map: dict[int, tuple[str, float | None]] = {}
    if watched_system_ids:
        for sid, sname, sec in (
            await db.execute(
                select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
                .where(SDESystem.system_id.in_(watched_system_ids))
            )
        ).all():
            system_name_map[sid] = (sname, sec)

    systems = [
        {
            "id": r[0],
            "system_id": r[1],
            "label": r[2],
            "system_name": system_name_map.get(r[1], (f"#{r[1]}", None))[0],
            "security": system_name_map.get(r[1], (None, None))[1],
        }
        for r in sys_rows
    ]

    # Hunters
    hunter_rows = (
        await db.execute(
            select(UserHunterWatch)
            .where(UserHunterWatch.user_id == user_id)
            .order_by(UserHunterWatch.created_at.desc())
        )
    ).scalars().all()
    hunters = [
        {
            "id": h.id,
            "kind": h.kind,
            "entity_id": h.entity_id,
            "label": h.label,
            "notes": h.notes,
        }
        for h in hunter_rows
    ]

    # Asset-derived system candidates (not yet watched) for the bulk-add prompt
    asset_sids = await _asset_system_ids_for_user(db, user_id)
    unwatched_asset_sids = sorted(asset_sids - set(watched_system_ids))
    unwatched_asset_systems: list[dict] = []
    if unwatched_asset_sids:
        name_rows = (
            await db.execute(
                select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
                .where(SDESystem.system_id.in_(unwatched_asset_sids))
            )
        ).all()
        for sid, sname, sec in name_rows:
            unwatched_asset_systems.append({
                "system_id": sid,
                "system_name": sname,
                "security": sec,
            })
        unwatched_asset_systems.sort(key=lambda x: x["system_name"] or "")

    # Recent alerts (last 72h)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=72)
    alert_rows = (
        await db.execute(
            select(KillAlertEvent)
            .where(KillAlertEvent.user_id == user_id)
            .where(KillAlertEvent.triggered_at >= cutoff)
            .order_by(KillAlertEvent.triggered_at.desc())
            .limit(50)
        )
    ).scalars().all()
    alert_system_ids = {a.system_id for a in alert_rows}
    alert_sys_name_map: dict[int, str] = {}
    if alert_system_ids:
        for sid, sname in (
            await db.execute(
                select(SDESystem.system_id, SDESystem.system_name)
                .where(SDESystem.system_id.in_(alert_system_ids))
            )
        ).all():
            alert_sys_name_map[sid] = sname
    alerts = [
        {
            "id": a.id,
            "kind": a.kind,
            "killmail_id": a.killmail_id,
            "system_id": a.system_id,
            "system_name": alert_sys_name_map.get(a.system_id, f"#{a.system_id}"),
            "matched_label": a.matched_label,
            "matched_entity_id": a.matched_entity_id,
            "triggered_at": a.triggered_at,
        }
        for a in alert_rows
    ]

    return templates.TemplateResponse(
        "intel_watch.html",
        {
            "request": request,
            "systems": systems,
            "hunters": hunters,
            "unwatched_asset_systems": unwatched_asset_systems,
            "asset_system_count": len(unwatched_asset_sids),
            "alerts": alerts,
        },
    )


@router.post("/intel/watch/system/add")
async def intel_watch_system_add(
    request: Request,
    system: str = Form(...),
    label: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    sid, sname = await _resolve_system(db, system)
    if not sid:
        return RedirectResponse("/intel/watch?err=unknown_system", status_code=303)
    entry = UserSystemWatch(
        user_id=user_id,
        system_id=sid,
        label=(label.strip() or None),
    )
    db.add(entry)
    try:
        await db.commit()
    except Exception:
        await db.rollback()  # duplicate — ignore
    return RedirectResponse("/intel/watch", status_code=303)


@router.post("/intel/watch/system/remove/{row_id}")
async def intel_watch_system_remove(
    row_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    row = (
        await db.execute(
            select(UserSystemWatch).where(
                UserSystemWatch.id == row_id, UserSystemWatch.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return RedirectResponse("/intel/watch", status_code=303)


@router.post("/intel/watch/system/sync-assets")
async def intel_watch_sync_assets(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    asset_sids = await _asset_system_ids_for_user(db, user_id)
    if not asset_sids:
        return RedirectResponse("/intel/watch", status_code=303)
    existing = {
        r[0]
        for r in (
            await db.execute(
                select(UserSystemWatch.system_id).where(UserSystemWatch.user_id == user_id)
            )
        ).all()
    }
    to_add = asset_sids - existing
    for sid in to_add:
        db.add(UserSystemWatch(user_id=user_id, system_id=sid, label="assets"))
    if to_add:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
    return RedirectResponse("/intel/watch", status_code=303)


@router.post("/intel/watch/hunter/add")
async def intel_watch_hunter_add(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    if kind not in _HUNTER_KINDS:
        return RedirectResponse("/intel/watch?err=bad_kind", status_code=303)
    entity_id, canonical = await _resolve_hunter(kind, name)
    if not entity_id:
        return RedirectResponse("/intel/watch?err=unknown_entity", status_code=303)
    entry = UserHunterWatch(
        user_id=user_id,
        kind=kind,
        entity_id=entity_id,
        label=canonical,
        notes=(notes.strip() or None),
    )
    db.add(entry)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
    return RedirectResponse("/intel/watch", status_code=303)


@router.post("/intel/watch/hunter/remove/{row_id}")
async def intel_watch_hunter_remove(
    row_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    row = (
        await db.execute(
            select(UserHunterWatch).where(
                UserHunterWatch.id == row_id, UserHunterWatch.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return RedirectResponse("/intel/watch", status_code=303)
