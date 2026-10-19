"""Structure Age Estimator — estimates when an Upwell structure was anchored
from its structure ID, using a local interpolation table built from EVERef
history + triff.tools calibration data, plus admin-approved user points.
Past the newest point it extrapolates at the recent ID rate
(app/intel/structure_age_check.py)."""

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StructureAgeCalibration, StructureAgeSubmission, get_db
from app.intel import structure_age_check as sac
from app.routes.admin import _log_audit

log = logging.getLogger(__name__)

router = APIRouter(tags=["tools"])
templates = Jinja2Templates(directory="app/templates")


def _parse_showinfo(text: str) -> dict:
    """Extract structure_id, jcode, and corp_name from an in-game showinfo paste."""
    text = text.strip()
    m = re.search(r'//([0-9]{10,})>', text) or re.search(r'\b([0-9]{10,})\b', text)
    digits = (m.group(1).lstrip("0") or "0") if m else None
    # Past 19 digits it's beyond INT64 whatever the digits are, and int()
    # refuses strings over 4300 digits — so those aren't converted at all.
    if digits is None:
        structure_id = None
    elif len(digits) > 19:
        structure_id = sac.INT64_MAX + 1
    else:
        structure_id = int(digits)

    m = re.search(r'\b(J\d{6})\b', text, re.IGNORECASE)
    jcode = m.group(1).upper() if m else None

    m = re.search(r'\(([^()]+)\)\s*</url>', text)
    if not m:
        m = re.search(r'\(([^()]+)\)\s*$', text)
    corp_name = m.group(1).strip() if m else None

    return {"structure_id": structure_id, "jcode": jcode, "corp_name": corp_name}


def _capped(est: dict) -> dict:
    """No latest date after today. A recent user point's window ("roughly
    that month" is ±15 days), or an ID interpolated next to one, can reach
    past it; days_wide then follows the span actually shown."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if est["high"] <= now:
        return est
    return {**est, "high": now,
            "days_wide": round((now - est["low"]).total_seconds() / 86400, 1)}


async def _estimate(db: AsyncSession, structure_id: int) -> dict:
    """Interpolate anchor date from the calibration table."""
    if structure_id > sac.INT64_MAX:
        # Too big for SQLite's INTEGER: binding it would raise, not miss.
        return {"error": sac.FRONTIER_MSG}
    if structure_id < sac.UPWELL_FLOOR_ID:
        return {"error": sac.NOT_UPWELL_MSG}

    lower = (await db.execute(
        select(StructureAgeCalibration)
        .where(StructureAgeCalibration.structure_id <= structure_id)
        .order_by(StructureAgeCalibration.structure_id.desc())
        .limit(1)
    )).scalar_one_or_none()

    upper = (await db.execute(
        select(StructureAgeCalibration)
        .where(StructureAgeCalibration.structure_id >= structure_id)
        .order_by(StructureAgeCalibration.structure_id.asc())
        .limit(1)
    )).scalar_one_or_none()

    if lower is None and upper is None:
        return {"error": "Calibration table is empty — run the scraper first."}

    # Exact hit
    if lower and lower.structure_id == structure_id:
        return _capped({
            "method": "exact",
            "mid": lower.anchor_mid,
            "low": lower.anchor_low,
            "high": lower.anchor_high,
            "days_wide": lower.days_wide,
            "source": lower.source,
        })

    # Beyond range
    if lower is None:
        return {
            "method": "extrapolate",
            "mid": upper.anchor_mid,
            "low": upper.anchor_low,
            "high": upper.anchor_high,
            "days_wide": upper.days_wide,
            "note": "ID is older than our earliest calibration point",
        }
    if upper is None:
        est = await sac.neighbour_estimate(db, structure_id)
        if est is None:
            return {"error": sac.FRONTIER_MSG}
        return {
            "method": "extrapolate",
            **est,
            "note": "ID is newer than our latest calibration point — "
                    "extrapolated from the recent ID rate",
        }

    # A user point among the two nearest: no straight line to it. IDs run
    # ~10-45 days noisy, so one honest outlier would drag every estimate
    # between it and its neighbour and leave a jump at its ID. The
    # many-point rule gives it one vote; as more are approved they take
    # over. Stretches of triff points keep the straight line (spec §5).
    if "user" in (lower.source, upper.source):
        return _capped({
            "method": "interpolate",
            **await sac.neighbour_estimate(db, structure_id),
            "note": "Estimated from the nearest points, including user-submitted ones",
        })

    # Interpolate between lower and upper
    lo_id, hi_id = lower.structure_id, upper.structure_id
    frac = (structure_id - lo_id) / (hi_id - lo_id)

    # The columns hold naive UTC; a bare .timestamp() would read them as the
    # host's local time and shift the midpoint by its UTC offset.
    lo_ts = lower.anchor_mid.replace(tzinfo=timezone.utc).timestamp()
    hi_ts = upper.anchor_mid.replace(tzinfo=timezone.utc).timestamp()
    mid_ts = lo_ts + frac * (hi_ts - lo_ts)
    mid = datetime.fromtimestamp(mid_ts, tz=timezone.utc).replace(tzinfo=None)

    # Window: interpolate between the two anchor windows
    lo_window = (lower.anchor_high - lower.anchor_low).total_seconds()
    hi_window = (upper.anchor_high - upper.anchor_low).total_seconds()
    window_secs = lo_window + frac * (hi_window - lo_window)
    days_wide = window_secs / 86400

    low = mid - timedelta(seconds=window_secs / 2)
    high = mid + timedelta(seconds=window_secs / 2)

    return _capped({
        "method": "interpolate",
        "mid": mid,
        "low": low,
        "high": high,
        "days_wide": round(days_wide, 1),
    })


def _age_str(anchor_mid: datetime) -> str:
    """Return a human-readable age string like 'today', '3 days', '≈ 8 months' or '≈ 2y 3m'."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now - anchor_mid
    months = int(delta.days / 30.44)
    if months < 1:
        if delta.days < 1:
            return "today"
        return f"{delta.days} day{'s' if delta.days != 1 else ''}"
    if months < 24:
        return f"≈ {months} month{'s' if months != 1 else ''}"
    years = months // 12
    rem = months % 12
    if rem == 0:
        return f"≈ {years} year{'s' if years != 1 else ''}"
    return f"≈ {years}y {rem}m"


async def _build_ctx(request: Request, db: AsyncSession, paste: str) -> dict:
    """Shared context builder for the full page and the htmx partial."""
    ctx: dict = {"paste": paste, "parsed": None, "estimate": None, "error": None,
                 "submit": None}

    if paste.strip():
        parsed = _parse_showinfo(paste)
        ctx["parsed"] = parsed
        sid = parsed["structure_id"]

        if sid is None:
            ctx["error"] = "Could not find a structure ID in that text. Paste the full showinfo link."
        else:
            est = await _estimate(db, sid)
            if "error" in est:
                ctx["error"] = est["error"]
            else:
                ctx["estimate"] = {
                    **est,
                    "age_str": _age_str(est["mid"]),
                    "mid_str": est["mid"].strftime("%d %b %Y"),
                    "low_str": est["low"].strftime("%d %b %Y"),
                    "high_str": est["high"].strftime("%d %b %Y"),
                }
                if request.session.get("user_id"):
                    ctx["submit"] = await _submit_state(db, sid)

    return ctx


def _form_state(structure_id: int, anchor_date: str = "", certainty: str = "day",
                error: str | None = None) -> dict:
    return {"state": "form", "structure_id": structure_id, "anchor_date": anchor_date,
            "certainty": certainty, "error": error,
            "options": list(sac.CERTAINTY_LABELS.items())}


async def _submit_state(db: AsyncSession, structure_id: int) -> dict:
    """What the "Know when it was anchored?" box shows for this ID."""
    row = await db.get(StructureAgeCalibration, structure_id)
    if row is not None:
        return {"state": "confirmed", "user_point": row.source == "user"}
    if await sac.pending_for(db, structure_id):
        return {"state": "pending"}
    return _form_state(structure_id)


def _box(request: Request, submit: dict):
    return templates.TemplateResponse(
        request, "partials/structure_age_submit.html", {"submit": submit})


@router.get("/tools/structure-age", response_class=HTMLResponse)
async def structure_age_page(request: Request, paste: str = "",
                             db: AsyncSession = Depends(get_db)):
    ctx = await _build_ctx(request, db, paste)
    ctx["point_count"] = await db.scalar(
        select(func.count()).select_from(StructureAgeCalibration))
    return templates.TemplateResponse(request, "structure_age.html", ctx)


@router.get("/tools/structure-age/partial", response_class=HTMLResponse)
async def structure_age_partial(request: Request, paste: str = "",
                                db: AsyncSession = Depends(get_db)):
    """Result card only — embedded via htmx on the wormhole system page and
    the WH Tracker panel."""
    return templates.TemplateResponse(
        request, "structure_age_result.html", await _build_ctx(request, db, paste))


@router.post("/tools/structure-age/submit", response_class=HTMLResponse)
async def structure_age_submit(request: Request, db: AsyncSession = Depends(get_db)):
    """Record a logged-in user's claimed anchor date as a pending submission.

    Refusals answer 200 with the box re-rendered: htmx doesn't swap a 4xx
    body, and base.html's ISS-007 handler would turn it into an error pill.
    Only "no session" is a 401, like the other login-only tool POSTs. A
    refused attempt writes nothing to the database — not even an audit row;
    it's logged at INFO — so retrying can't fill the queue or the audit log
    (spec §7 S10)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    form = await request.form()
    sid = sac.parse_structure_id(str(form.get("structure_id") or ""))
    raw_date = str(form.get("anchor_date") or "").strip()
    echo = raw_date[:32]                 # what goes back into the field; parse the whole thing
    certainty = str(form.get("certainty") or "")
    if sid is None:
        return _box(request, {"state": "error",
                              "error": "That structure ID isn't valid — paste the showinfo link again."})
    keep = certainty if certainty in sac.CERTAINTY_DAYS else "day"
    anchor = sac.parse_anchor_date(raw_date)
    if anchor is None:
        return _box(request, _form_state(sid, echo, keep, "Enter the date as YYYY-MM-DD (UTC)."))
    if certainty not in sac.CERTAINTY_DAYS:
        return _box(request, _form_state(sid, echo, keep, "Pick how sure you are."))

    verdict = await sac.check_submission(db, sid, anchor, user_id)
    if not verdict.ok:
        log.info("structure_age submit refused: user=%s id=%s date=%s: %s",
                 user_id, sid, anchor, verdict.reason)
        if verdict.code in ("confirmed", "pending"):
            return _box(request, await _submit_state(db, sid))
        if verdict.code in ("not_upwell", "frontier", "empty"):
            # About the ID or the data: no date would pass, so no form.
            return _box(request, {"state": "error", "error": verdict.reason})
        return _box(request, _form_state(sid, echo, certainty, verdict.reason))

    db.add(StructureAgeSubmission(
        structure_id=sid, anchor_date=anchor, certainty=certainty, status="pending",
        expected_date=verdict.window.expected, submitted_by=user_id,
        submitted_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    try:
        await db.flush()
    except IntegrityError as e:
        # Another submission for this ID landed between the check and here:
        # the one-pending-per-ID index refused this one. Anything else is a
        # real fault, not a race.
        if "structure_age_submissions.structure_id" not in str(e.orig):
            raise
        await db.rollback()
        log.info("structure_age submit raced: user=%s id=%s already pending", user_id, sid)
        return _box(request, {"state": "pending"})
    # The cap was counted before the insert, so two submits racing from one
    # user could both pass it. The INSERT took SQLite's single write lock,
    # so this recount sees every committed row and holds the cap.
    if await sac.pending_count(db, user_id) > sac.MAX_PENDING_PER_USER:
        await db.rollback()
        return _box(request, _form_state(sid, echo, certainty, sac.LIMIT_MSG))
    # _log_audit commits — the submission and its audit row land together.
    await _log_audit(db, "structure_age_submit", user_id,
                     detail=f"{sid} anchored {anchor.isoformat()} ({certainty})",
                     ip=request.client.host if request.client else None)
    return _box(request, {"state": "sent"})
