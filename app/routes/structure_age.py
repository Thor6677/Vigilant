"""Structure Age Estimator — estimates when an Upwell structure was anchored
from its structure ID, using a local interpolation table built from EVERef
history + triff.tools calibration data (36k known anchor dates)."""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.db.models import AsyncSessionLocal, StructureAgeCalibration

router = APIRouter(tags=["tools"])
templates = Jinja2Templates(directory="app/templates")


def _parse_showinfo(text: str) -> dict:
    """Extract structure_id, jcode, and corp_name from an in-game showinfo paste."""
    text = text.strip()
    m = re.search(r'//(\d{10,})>', text) or re.search(r'\b(\d{10,})\b', text)
    structure_id = int(m.group(1)) if m else None

    m = re.search(r'\b(J\d{6})\b', text, re.IGNORECASE)
    jcode = m.group(1).upper() if m else None

    m = re.search(r'\(([^()]+)\)\s*</url>', text)
    if not m:
        m = re.search(r'\(([^()]+)\)\s*$', text)
    corp_name = m.group(1).strip() if m else None

    return {"structure_id": structure_id, "jcode": jcode, "corp_name": corp_name}


async def _estimate(structure_id: int) -> dict:
    """Interpolate anchor date from the calibration table."""
    async with AsyncSessionLocal() as db:
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
        return {
            "method": "exact",
            "mid": lower.anchor_mid,
            "low": lower.anchor_low,
            "high": lower.anchor_high,
            "days_wide": lower.days_wide,
        }

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
        return {
            "method": "extrapolate",
            "mid": lower.anchor_mid,
            "low": lower.anchor_low,
            "high": lower.anchor_high,
            "days_wide": lower.days_wide,
            "note": "ID is newer than our latest calibration point",
        }

    # Interpolate between lower and upper
    lo_id, hi_id = lower.structure_id, upper.structure_id
    frac = (structure_id - lo_id) / (hi_id - lo_id)

    lo_ts = lower.anchor_mid.timestamp()
    hi_ts = upper.anchor_mid.timestamp()
    mid_ts = lo_ts + frac * (hi_ts - lo_ts)
    mid = datetime.fromtimestamp(mid_ts, tz=timezone.utc).replace(tzinfo=None)

    # Window: interpolate between the two anchor windows
    lo_window = (lower.anchor_high - lower.anchor_low).total_seconds()
    hi_window = (upper.anchor_high - upper.anchor_low).total_seconds()
    window_secs = lo_window + frac * (hi_window - lo_window)
    days_wide = window_secs / 86400

    from datetime import timedelta
    low = mid - timedelta(seconds=window_secs / 2)
    high = mid + timedelta(seconds=window_secs / 2)

    return {
        "method": "interpolate",
        "mid": mid,
        "low": low,
        "high": high,
        "days_wide": round(days_wide, 1),
    }


def _age_str(anchor_mid: datetime) -> str:
    """Return a human-readable age string like '8 months' or '2 years 3 months'."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = now - anchor_mid
    months = int(delta.days / 30.44)
    if months < 1:
        return f"{delta.days} days"
    if months < 24:
        return f"≈ {months} month{'s' if months != 1 else ''}"
    years = months // 12
    rem = months % 12
    if rem == 0:
        return f"≈ {years} year{'s' if years != 1 else ''}"
    return f"≈ {years}y {rem}m"


@router.get("/tools/structure-age", response_class=HTMLResponse)
async def structure_age_page(request: Request, paste: str = ""):
    ctx: dict = {"paste": paste, "parsed": None, "estimate": None, "error": None}

    if paste.strip():
        parsed = _parse_showinfo(paste)
        ctx["parsed"] = parsed

        if parsed["structure_id"] is None:
            ctx["error"] = "Could not find a structure ID in that text. Paste the full showinfo link."
        else:
            est = await _estimate(parsed["structure_id"])
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

    return templates.TemplateResponse(request, "structure_age.html", ctx)
