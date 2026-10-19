"""Plausibility window for structure anchor dates.

Used three ways: to refuse a user-submitted anchor date that makes no sense
for its structure ID (app/routes/structure_age.py), to show a reviewer how far
a pending claim is from the expected date (app/routes/admin_structure_age.py),
and to estimate IDs newer than the newest calibration point.

Upwell structure IDs are allocated roughly in time order — only roughly.
Measured on the calibration table in 2026-10: half of all points are within a
day of the median of their 50 nearest neighbours, but 1% are more than 77 days
off and the worst 130. So a claim is compared with the MEDIAN of many
neighbours and allowed months, not days. That still refuses what the check is
for: a recent ID claimed to be years old.

Past the newest point there are no neighbours above, so the expected date is
extrapolated from the recent ID rate (~14M IDs/day through 2026). The rate is
fitted on triff-sourced points only, so approved user points can never move it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StructureAgeCalibration, StructureAgeSubmission

NEIGHBOURS = 25              # nearest calibration points taken on each side
TOLERANCE_DAYS = 120         # allowed distance from the expected date
TAIL_SPREAD = 0.25           # extra allowance per day extrapolated past the newest points
FUTURE_SLACK_DAYS = 60       # an ID extrapolating further past today doesn't exist yet
ESTIMATE_HALF_DAYS = 7       # estimate window half-width at the tail base (triff's tail used ±7)
RATE_WINDOW_DAYS = 180       # triff points used to fit the ID rate
MIN_RATE_POINTS = 20
DEFAULT_RATE = 14_000_000    # IDs/day through 2026; only when the fit has too little data
UPWELL_FLOOR_ID = 1_021_000_000_000
INT64_MAX = 2**63 - 1        # SQLite INTEGER; a bigger int raises when bound
CITADEL_RELEASE = date(2016, 4, 26)
MAX_PENDING_PER_USER = 20

CERTAINTY_DAYS = {"day": 2, "week": 7, "month": 30}
CERTAINTY_LABELS = {"day": "Exact day", "week": "Within a week", "month": "Roughly that month"}

FRONTIER_MSG = "This ID is higher than any structure yet — check it was pasted whole."

# fullmatch, not $ (which allows a trailing newline); [0-9], not \d (which
# matches non-ASCII digits that int() would happily accept).
_ID_RE = re.compile(r"[0-9]{1,19}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

Cal = StructureAgeCalibration
Sub = StructureAgeSubmission


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def fmt_day(d: date) -> str:
    return d.strftime("%d %b %Y")


def parse_structure_id(raw: str | None) -> int | None:
    """A structure ID from a form field, or None. Callers still range-check
    (UPWELL_FLOOR_ID, INT64_MAX) before binding it to SQLite."""
    s = (raw or "").strip()
    return int(s) if _ID_RE.fullmatch(s) else None


def parse_anchor_date(raw: str | None) -> date | None:
    s = (raw or "").strip()
    if not _DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class Window:
    expected: date
    low: date
    high: date
    tail: bool               # extrapolated past the newest points

    def contains(self, d: date) -> bool:
        return self.low <= d <= self.high


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None
    # ok | invalid | frontier | confirmed | pending | limit | empty | outside
    code: str = "ok"
    window: Window | None = None


def _day(d: datetime | date) -> date:
    return d.date() if isinstance(d, datetime) else d


def _median_date(dates) -> date:
    return date.fromordinal(round(median(_day(d).toordinal() for d in dates)))


def interior_window(neighbour_dates, today: date) -> Window:
    """Window around the median of the neighbours' anchor dates."""
    expected = _median_date(neighbour_dates)
    spread = timedelta(days=TOLERANCE_DAYS)
    return Window(expected, max(expected - spread, CITADEL_RELEASE),
                  min(expected + spread, today), tail=False)


def fit_rate(points) -> float:
    """IDs per day from (structure_id, anchor_mid) pairs. Theil–Sen style:
    sorted by ID, each point is paired with the one half the sample further
    on, and the rate is the median of those pairs' slopes — so misdated
    points (common, see the module docstring) can't swing it."""
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) < MIN_RATE_POINTS:
        return DEFAULT_RATE
    half = len(pts) // 2
    slopes = []
    for (id_a, d_a), (id_b, d_b) in zip(pts, pts[half:]):
        days = (_day(d_b) - _day(d_a)).days
        if days > 0:
            slopes.append((id_b - id_a) / days)
    return median(slopes) if len(slopes) >= MIN_RATE_POINTS // 2 else DEFAULT_RATE


def extrapolate(structure_id: int, base_id: int, base_date: date, rate: float,
                today: date) -> tuple[date, float] | None:
    """(expected date, days extrapolated) for an ID past the tail base, or
    None when it lands more than FUTURE_SLACK_DAYS after today. The frontier
    is checked on plain numbers before any timedelta is built, so an absurd
    ID can't overflow one."""
    ahead = max(0.0, (structure_id - base_id) / rate)
    if base_date.toordinal() + ahead > (today + timedelta(days=FUTURE_SLACK_DAYS)).toordinal():
        return None
    return base_date + timedelta(days=round(ahead)), ahead


def tail_window(structure_id: int, base_id: int, base_date: date, rate: float,
                today: date) -> Window | None:
    hit = extrapolate(structure_id, base_id, base_date, rate, today)
    if hit is None:
        return None
    expected, ahead = hit
    spread = timedelta(days=round(TOLERANCE_DAYS + TAIL_SPREAD * ahead))
    return Window(expected, max(expected - spread, CITADEL_RELEASE),
                  min(expected + spread, today), tail=True)


def calibration_values(structure_id: int, anchor_date: date, certainty: str,
                       now: datetime) -> dict:
    """The structure_age_calibration row an approved submission becomes."""
    width = CERTAINTY_DAYS[certainty]
    mid = datetime.combine(anchor_date, time())
    half = timedelta(days=width / 2)
    return {"structure_id": structure_id, "anchor_mid": mid, "anchor_low": mid - half,
            "anchor_high": mid + half, "days_wide": float(width), "fetched_at": now,
            "source": "user"}


# ── Database reads ────────────────────────────────────────────────────────

def _only(stmt, sources):
    return stmt.where(Cal.source.in_(sources)) if sources else stmt


async def _neighbours(db: AsyncSession, structure_id: int, sources):
    below = (await db.execute(
        _only(select(Cal.structure_id, Cal.anchor_mid).where(Cal.structure_id < structure_id),
              sources).order_by(Cal.structure_id.desc()).limit(NEIGHBOURS))).all()
    above = (await db.execute(
        _only(select(Cal.structure_id, Cal.anchor_mid).where(Cal.structure_id > structure_id),
              sources).order_by(Cal.structure_id.asc()).limit(NEIGHBOURS))).all()
    return below, above


async def _tail_base(db: AsyncSession, sources) -> tuple[int, date]:
    """Median ID and median anchor date of the NEIGHBOURS highest-ID points."""
    top = (await db.execute(
        _only(select(Cal.structure_id, Cal.anchor_mid), sources)
        .order_by(Cal.structure_id.desc()).limit(NEIGHBOURS))).all()
    return round(median(r[0] for r in top)), _median_date(r[1] for r in top)


async def tail_rate(db: AsyncSession) -> float:
    """IDs/day over the last RATE_WINDOW_DAYS of triff data. Triff only, so
    approved user points can't move it (spec §7 S5)."""
    newest = await db.scalar(select(func.max(Cal.anchor_mid)).where(Cal.source == "triff"))
    if newest is None:
        return DEFAULT_RATE
    rows = (await db.execute(
        select(Cal.structure_id, Cal.anchor_mid)
        .where(Cal.source == "triff",
               Cal.anchor_mid >= newest - timedelta(days=RATE_WINDOW_DAYS)))).all()
    return fit_rate([(r[0], r[1]) for r in rows])


async def plausible_window(db: AsyncSession, structure_id: int, *, sources=None,
                           today: date | None = None) -> Window | None:
    """The plausible anchor-date window for structure_id, or None when it
    extrapolates past the frontier (FRONTIER_MSG). sources=("triff",) is the
    baseline approved user points can't move. Pending submissions never count:
    they aren't in the calibration table. Raises LookupError when there are
    no points to compare with."""
    today = today or utc_today()
    below, above = await _neighbours(db, structure_id, sources)
    if not below and not above:
        raise LookupError("no calibration points")
    if above:
        return interior_window([r[1] for r in below + above], today)
    base_id, base_date = await _tail_base(db, sources)
    return tail_window(structure_id, base_id, base_date, await tail_rate(db), today)


async def tail_estimate(db: AsyncSession, structure_id: int, *,
                        today: date | None = None) -> dict | None:
    """Estimate for an ID above the newest point: extrapolated at the recent
    rate, window widening with distance, nothing past today (so the age can
    never go negative). None past the frontier. Only called when the table
    has points."""
    today = today or utc_today()
    base_id, base_date = await _tail_base(db, None)
    hit = extrapolate(structure_id, base_id, base_date, await tail_rate(db), today)
    if hit is None:
        return None
    expected, ahead = hit
    mid = min(expected, today)
    half = timedelta(days=round(ESTIMATE_HALF_DAYS + TAIL_SPREAD * ahead))
    low, high = mid - half, min(mid + half, today)
    return {"mid": datetime.combine(mid, time()), "low": datetime.combine(low, time()),
            "high": datetime.combine(high, time()), "days_wide": (high - low).days}


async def pending_for(db: AsyncSession, structure_id: int) -> bool:
    return await db.scalar(
        select(Sub.id).where(Sub.structure_id == structure_id, Sub.status == "pending").limit(1)
    ) is not None


async def check_submission(db: AsyncSession, structure_id: int, anchor_date: date,
                           user_id: int, *, today: date | None = None) -> Verdict:
    """Whether user_id may submit anchor_date for structure_id. The range
    checks run in Python before anything is bound to SQLite (spec §7 S8)."""
    today = today or utc_today()
    if structure_id < UPWELL_FLOOR_ID:
        return Verdict(False, "That isn't an Upwell structure ID.", "invalid")
    if structure_id > INT64_MAX:
        return Verdict(False, FRONTIER_MSG, "frontier")
    if anchor_date > today:
        return Verdict(False, "The anchor date can't be in the future.", "invalid")
    if anchor_date < CITADEL_RELEASE:
        return Verdict(False, "Upwell structures didn't exist before 26 Apr 2016.", "invalid")
    if await db.get(Cal, structure_id) is not None:
        return Verdict(False, "This ID already has a confirmed date.", "confirmed")
    if await pending_for(db, structure_id):
        return Verdict(False, "A date for this ID is already awaiting review.", "pending")
    mine = await db.scalar(select(func.count()).select_from(Sub).where(
        Sub.submitted_by == user_id, Sub.status == "pending"))
    if mine >= MAX_PENDING_PER_USER:
        return Verdict(False, f"You already have {MAX_PENDING_PER_USER} dates awaiting review — "
                              "try again once some have been reviewed.", "limit")
    try:
        window = await plausible_window(db, structure_id, today=today)
    except LookupError:
        return Verdict(False, "There's no calibration data to check this against yet.", "empty")
    if window is None:
        return Verdict(False, FRONTIER_MSG, "frontier")
    if not window.contains(anchor_date):
        return Verdict(False, f"For this ID the plausible anchor date is "
                              f"{fmt_day(window.low)} – {fmt_day(window.high)}.",
                       "outside", window)
    return Verdict(True, window=window)
