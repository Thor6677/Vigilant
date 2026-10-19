"""Admin > Structure Age: review user-submitted anchor points.

Submissions arrive pending from app/routes/structure_age.py. Approving copies
the point into structure_age_calibration with source='user'; Remove deletes
that row again — and only a source='user' row, never a triff one. Every
transition is a guarded UPDATE … WHERE status=<expected>, so a double click or
two reviewers at once can't apply it twice. Admins and managers can review,
their own submissions included (decided 2026-10-18; audited).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Request
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
# A gap past a month gets flagged: 2025+ triff points sit within 10 days of
# their neighbours 95% of the time and within 46 days 99% of the time.
GAP_FLAG_DAYS = 30
ALREADY_HAS_POINT = "ID already has a calibration point"
ALREADY_REVIEWED = "That submission was already reviewed."
# A submission id from the URL; never bind a bigger int to SQLite.
SUB_ID = Path(ge=1, le=sac.INT64_MAX)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _transition(sub_id: int, expected: str, **values):
    """The one way a submission changes status: only from `expected`, so a
    double click or two reviewers at once can't apply it twice. RETURNING
    hands back the claim for the audit line and the approval, so nothing is
    read off an ORM object a rollback might expire. Keep it the first write
    in its transaction: with the driver's deferred BEGIN, a read inside the
    transaction before it would risk SQLITE_BUSY_SNAPSHOT instead of a wait."""
    return (update(Sub).where(Sub.id == sub_id, Sub.status == expected).values(**values)
            .returning(Sub.structure_id, Sub.anchor_date, Sub.certainty)
            .execution_options(synchronize_session=False))


def _claim(sid: int, anchor, certainty: str) -> str:
    return f"{sid} anchored {anchor.isoformat()} ({certainty})"


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
    """How far the claim sits from the expected date right now; None with
    no points to compare. `beyond` when the ID extrapolates past the
    frontier on these sources — for the triff baseline that is exactly when
    drift from a chain of user points would be largest, so it's flagged,
    not shown as n/a."""
    try:
        window = await sac.plausible_window(db, sub.structure_id, sources=sources, rate=rate)
    except LookupError:
        return None
    if window is None:
        return {"beyond": True}
    days = (sub.anchor_date - window.expected).days
    return {"beyond": False, "expected": sac.fmt_day(window.expected), "days": days,
            "in_window": window.contains(sub.anchor_date),
            "flag": abs(days) > GAP_FLAG_DAYS or not window.contains(sub.anchor_date)}


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
    # The triff side on its own: a recent user point would otherwise hide a
    # stalled upstream behind a fresh "newest anchor".
    newest_triff = await db.scalar(select(func.max(Cal.anchor_mid)).where(Cal.source == "triff"))
    triff_last = await db.scalar(select(func.max(Cal.fetched_at)).where(Cal.source == "triff"))
    stats = {
        "total": await db.scalar(select(func.count()).select_from(Cal)),
        "from_users": await db.scalar(select(func.count()).select_from(Cal)
                                      .where(Cal.source == "user")),
        "pending": await db.scalar(select(func.count()).select_from(Sub)
                                   .where(Sub.status == "pending")),
        "newest": newest.strftime("%d %b %Y") if newest else None,
        "newest_triff": newest_triff.strftime("%d %b %Y") if newest_triff else None,
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
async def admin_structure_age_approve(request: Request, sub_id: int = SUB_ID,
                                      db: AsyncSession = Depends(get_db),
                                      admin: User = Depends(require_admin)):
    now = _now()
    admin_id = admin.id        # a rollback below would expire `admin`
    claim = (await db.execute(_transition(sub_id, "pending", status="approved",
                                          reviewed_by=admin_id, reviewed_at=now))).first()
    if claim is None:
        await db.rollback()
        return await _render(request, db, ALREADY_REVIEWED, flash_ok=False)
    sid, anchor, certainty = claim
    ins = (sqlite_insert(Cal).values(**sac.calibration_values(sid, anchor, certainty, now))
           .on_conflict_do_nothing(index_elements=["structure_id"]))
    if (await db.execute(ins)).rowcount != 1:
        # A calibration point for this ID appeared after the submission.
        await db.rollback()
        rejected = (await db.execute(_transition(
            sub_id, "pending", status="rejected", reviewed_by=admin_id, reviewed_at=now,
            reject_reason=ALREADY_HAS_POINT))).first()
        if rejected is None:
            # Another reviewer acted in the gap the rollback left.
            await db.rollback()
            return await _render(request, db, ALREADY_REVIEWED, flash_ok=False)
        await _log_audit(db, "structure_age_reject", admin_id,
                         detail=f"#{sub_id} {_claim(*claim)}: {ALREADY_HAS_POINT} (automatic)",
                         ip=_ip(request))
        return await _render(request, db, f"{sid}: {ALREADY_HAS_POINT} — submission rejected.",
                             flash_ok=False)
    await _log_audit(db, "structure_age_approve", admin_id,
                     detail=f"#{sub_id} {_claim(*claim)}", ip=_ip(request))
    return await _render(request, db, f"Approved {sid}.")


@router.post("/structure-age/{sub_id}/reject", response_class=HTMLResponse)
async def admin_structure_age_reject(request: Request, sub_id: int = SUB_ID,
                                     db: AsyncSession = Depends(get_db),
                                     admin: User = Depends(require_admin)):
    form = await request.form()
    reason = str(form.get("reject_reason") or "").strip()[:200] or None
    admin_id = admin.id
    claim = (await db.execute(_transition(sub_id, "pending", status="rejected",
                                          reviewed_by=admin_id, reviewed_at=_now(),
                                          reject_reason=reason))).first()
    if claim is None:
        await db.rollback()
        return await _render(request, db, ALREADY_REVIEWED, flash_ok=False)
    await _log_audit(db, "structure_age_reject", admin_id,
                     detail=f"#{sub_id} {_claim(*claim)}" + (f": {reason}" if reason else ""),
                     ip=_ip(request))
    return await _render(request, db, f"Rejected {claim[0]}.")


@router.post("/structure-age/{sub_id}/remove", response_class=HTMLResponse)
async def admin_structure_age_remove(request: Request, sub_id: int = SUB_ID,
                                     db: AsyncSession = Depends(get_db),
                                     admin: User = Depends(require_admin)):
    admin_id = admin.id
    # reviewed_by/at keep the approver; who removed it is in the audit log.
    claim = (await db.execute(_transition(sub_id, "approved", status="removed"))).first()
    if claim is None:
        await db.rollback()
        return await _render(request, db, "Only an approved point can be removed.",
                             flash_ok=False)
    sid = claim[0]
    # source='user' only: Remove can never delete a triff point (spec §7 S7).
    gone = (await db.execute(delete(Cal).where(Cal.structure_id == sid,
                                               Cal.source == "user"))).rowcount
    note = "" if gone else " — no user point for it was in the table"
    await _log_audit(db, "structure_age_remove", admin_id,
                     detail=f"#{sub_id} {_claim(*claim)}{note}", ip=_ip(request))
    return await _render(request, db, f"Removed {sid}{note}.")
