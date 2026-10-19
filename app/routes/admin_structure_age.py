"""Admin > Structure Age: review user-submitted anchor points.

Submissions arrive pending from app/routes/structure_age.py. Approving copies
the point into structure_age_calibration with source='user'; Remove deletes
that row again — and only a source='user' row, never a triff one. Every
transition is a guarded UPDATE … WHERE status=<expected>, so a double click or
two reviewers at once can't apply it twice. Admins and managers can review,
their own submissions included (decided 2026-10-18; audited).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (Character, StructureAgeCalibration, StructureAgeSubmission, User,
                           get_db)
from app.intel import structure_age_check as sac
from app.routes.admin import _log_audit, require_admin

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")

Cal = StructureAgeCalibration
Sub = StructureAgeSubmission
LIST_LIMIT = 200
ALREADY_HAS_POINT = "ID already has a calibration point"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _transition(sub_id: int, expected: str, **values):
    return (update(Sub).where(Sub.id == sub_id, Sub.status == expected).values(**values)
            .execution_options(synchronize_session=False))


async def _user_labels(db: AsyncSession, user_ids) -> dict[int, str]:
    """Each user's main character name, else their first by name."""
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    rows = (await db.execute(
        select(Character.user_id, Character.character_name, Character.is_main)
        .where(Character.user_id.in_(ids)).order_by(Character.character_name))).all()
    labels: dict[int, str] = {}
    for uid, name, is_main in rows:
        if is_main or uid not in labels:
            labels[uid] = name
    return labels


def _who(labels: dict[int, str], uid: int | None) -> str:
    if uid is None:
        return "(removed user)"
    return labels.get(uid, f"user #{uid}")


async def _gap(db: AsyncSession, sub, rate: float, sources=None) -> dict | None:
    """How far the claim sits from the expected date right now."""
    try:
        window = await sac.plausible_window(db, sub.structure_id, sources=sources, rate=rate)
    except LookupError:
        return None
    if window is None:
        return None
    return {"expected": sac.fmt_day(window.expected),
            "days": (sub.anchor_date - window.expected).days,
            "in_window": window.contains(sub.anchor_date)}


async def _render(request: Request, db: AsyncSession, flash: str | None = None,
                  flash_ok: bool = True):
    pending = (await db.execute(select(Sub).where(Sub.status == "pending")
                                .order_by(Sub.submitted_at).limit(LIST_LIMIT))).scalars().all()
    approved = (await db.execute(select(Sub).where(Sub.status == "approved")
                                 .order_by(Sub.reviewed_at.desc()).limit(LIST_LIMIT))).scalars().all()
    labels = await _user_labels(db, [s.submitted_by for s in pending + approved]
                                + [s.reviewed_by for s in approved])
    rate = await sac.tail_rate(db)        # once per render, not once per row
    pending_rows = [{"sub": s, "by": _who(labels, s.submitted_by),
                     "check": await _gap(db, s, rate),
                     "baseline": await _gap(db, s, rate, ("triff",))}
                    for s in pending]
    approved_rows = [{"sub": s, "by": _who(labels, s.submitted_by),
                      "reviewer": _who(labels, s.reviewed_by)} for s in approved]
    newest = await db.scalar(select(func.max(Cal.anchor_mid)))
    triff_last = await db.scalar(select(func.max(Cal.fetched_at)).where(Cal.source == "triff"))
    stats = {
        "total": await db.scalar(select(func.count()).select_from(Cal)),
        "from_users": await db.scalar(select(func.count()).select_from(Cal)
                                      .where(Cal.source == "user")),
        "newest": newest.strftime("%d %b %Y") if newest else None,
        "triff_last": triff_last.strftime("%d %b %Y") if triff_last else None,
    }
    return templates.TemplateResponse(request, "partials/admin_structure_age.html", {
        "flash": flash, "flash_ok": flash_ok, "stats": stats, "pending": pending_rows,
        "approved": approved_rows, "certainty_labels": sac.CERTAINTY_LABELS})


@router.get("/section/structure-age", response_class=HTMLResponse)
async def admin_structure_age(request: Request, db: AsyncSession = Depends(get_db),
                              admin: User = Depends(require_admin)):
    return await _render(request, db)


@router.post("/structure-age/{sub_id}/approve", response_class=HTMLResponse)
async def admin_structure_age_approve(sub_id: int, request: Request,
                                      db: AsyncSession = Depends(get_db),
                                      admin: User = Depends(require_admin)):
    now = _now()
    admin_id = admin.id        # the rollback below would expire `admin` too
    res = await db.execute(_transition(sub_id, "pending", status="approved",
                                       reviewed_by=admin_id, reviewed_at=now))
    if res.rowcount != 1:
        await db.rollback()
        return await _render(request, db, "That submission was already reviewed.", False)
    sub = await db.get(Sub, sub_id)
    # Copied out now: the rollback below would expire `sub`.
    sid, anchor, certainty = sub.structure_id, sub.anchor_date, sub.certainty
    ins = (sqlite_insert(Cal).values(**sac.calibration_values(sid, anchor, certainty, now))
           .on_conflict_do_nothing(index_elements=["structure_id"]))
    if (await db.execute(ins)).rowcount != 1:
        # A calibration point for this ID appeared after the submission.
        await db.rollback()
        await db.execute(_transition(sub_id, "pending", status="rejected", reviewed_by=admin_id,
                                     reviewed_at=now, reject_reason=ALREADY_HAS_POINT))
        await _log_audit(db, "structure_age_reject", admin_id,
                         detail=f"#{sub_id} {sid}: {ALREADY_HAS_POINT}", ip=_ip(request))
        return await _render(request, db, f"{sid}: {ALREADY_HAS_POINT} — submission rejected.",
                             False)
    await _log_audit(db, "structure_age_approve", admin_id,
                     detail=f"#{sub_id} {sid} anchored {anchor.isoformat()} ({certainty})",
                     ip=_ip(request))
    return await _render(request, db, f"Approved {sid}.")


@router.post("/structure-age/{sub_id}/reject", response_class=HTMLResponse)
async def admin_structure_age_reject(sub_id: int, request: Request,
                                     db: AsyncSession = Depends(get_db),
                                     admin: User = Depends(require_admin)):
    form = await request.form()
    reason = str(form.get("reject_reason") or "").strip()[:200] or None
    res = await db.execute(_transition(sub_id, "pending", status="rejected", reviewed_by=admin.id,
                                       reviewed_at=_now(), reject_reason=reason))
    if res.rowcount != 1:
        await db.rollback()
        return await _render(request, db, "That submission was already reviewed.", False)
    await _log_audit(db, "structure_age_reject", admin.id,
                     detail=f"#{sub_id}" + (f": {reason}" if reason else ""), ip=_ip(request))
    return await _render(request, db, "Rejected.")


@router.post("/structure-age/{sub_id}/remove", response_class=HTMLResponse)
async def admin_structure_age_remove(sub_id: int, request: Request,
                                     db: AsyncSession = Depends(get_db),
                                     admin: User = Depends(require_admin)):
    sub = await db.get(Sub, sub_id)
    if sub is None or sub.status != "approved":
        return await _render(request, db, "Only an approved point can be removed.", False)
    sid = sub.structure_id
    res = await db.execute(_transition(sub_id, "approved", status="removed",
                                       reviewed_by=admin.id, reviewed_at=_now()))
    if res.rowcount != 1:
        await db.rollback()
        return await _render(request, db, "That point was already removed.", False)
    # source='user' only: Remove can never delete a triff point (spec §7 S7).
    await db.execute(delete(Cal).where(Cal.structure_id == sid, Cal.source == "user"))
    await _log_audit(db, "structure_age_remove", admin.id, detail=f"#{sub_id} {sid}",
                     ip=_ip(request))
    return await _render(request, db, f"Removed {sid}.")
