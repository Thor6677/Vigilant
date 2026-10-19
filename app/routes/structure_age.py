"""Structure Age Estimator — estimates when an Upwell structure was anchored
from its structure ID, using a local interpolation table built from EVERef
history + triff.tools calibration data, plus admin-approved user points.
Past the newest point it extrapolates at the recent ID rate
(app/intel/structure_age_check.py)."""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StructureAgeCalibration, get_db
from app.intel import structure_age_check as sac

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
        est = await sac.tail_estimate(db, structure_id)
        if est is None:
            return {"error": sac.FRONTIER_MSG}
        return {
            "method": "extrapolate",
            **est,
            "note": "ID is newer than our latest calibration point — "
                    "extrapolated from the recent ID rate",
        }

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

    return ctx


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
    """Result card only — embedded via htmx on the wormhole system page."""
    return templates.TemplateResponse(
        request, "structure_age_result.html", await _build_ctx(request, db, paste))
