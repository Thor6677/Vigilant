"""Plausibility window for structure anchor dates.

Used three ways: to refuse a user-submitted anchor date that makes no sense
for its structure ID (app/routes/structure_age.py), to show a reviewer how far
a pending claim is from the expected date (app/routes/admin_structure_age.py),
and to estimate IDs newer than the newest calibration point.

Upwell structure IDs are allocated roughly in time order — only roughly.
Measured on the calibration table in 2026-10: half of all points are within a
day of the median of their 50 nearest neighbours, but 1% are more than 78 days
off and the worst 130. So a claim is compared with the MEDIAN over many
neighbours and allowed months, not days. That still refuses what the check is
for: a recent ID claimed to be years old.

Each neighbour predicts the date by moving its own along the ID rate (~14M
IDs/day through 2026), and the expected date is the median prediction. Where
neighbours are dense on both sides that moves them a day or so and the median
is simply theirs. Where there's a gap — above the newest point, or between the
stalled triff data and the first user point above it — it extrapolates
instead of sticking to the far side's dates. The rate is fitted on
triff-sourced points only, so approved user points can never move it, and a
user point is one vote among up to fifty.
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
TAIL_SPREAD = 0.25           # extra allowance per day of IDs to the nearest point
FUTURE_SLACK_DAYS = 60       # an ID extrapolating further past today doesn't exist yet
ESTIMATE_HALF_DAYS = 7       # estimate half-width next to the newest point (triff's tail used ±7)
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
LIMIT_MSG = (f"You already have {MAX_PENDING_PER_USER} dates awaiting review — "
             "try again once some have been reviewed.")

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
    tail: bool               # nothing above it: extrapolated past the newest point

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
    if len(slopes) < MIN_RATE_POINTS // 2:
        return DEFAULT_RATE
    rate = median(slopes)
    return rate if rate > 0 else DEFAULT_RATE


def _expected(structure_id: int, neighbours, rate: float) -> tuple[float, float]:
    """(expected anchor day as an ordinal, days of IDs to the nearest
    neighbour) from (structure_id, anchor_mid) neighbours: the median of each
    neighbour's date moved along the ID rate. Plain numbers, so an absurd ID
    can't overflow a date before the caller has checked the frontier."""
    predictions = [_day(d).toordinal() + (structure_id - sid) / rate for sid, d in neighbours]
    gap = min(abs(structure_id - sid) for sid, _ in neighbours) / rate
    return median(predictions), gap


def _past_frontier(expected_ord: float, today: date) -> bool:
    return expected_ord > (today + timedelta(days=FUTURE_SLACK_DAYS)).toordinal()


def window_for(structure_id: int, neighbours, rate: float, today: date, *,
               tail: bool) -> Window | None:
    """The plausible window for structure_id given its (structure_id,
    anchor_mid) calibration neighbours. The allowance is TOLERANCE_DAYS plus
    TAIL_SPREAD per day of IDs to the nearest neighbour, capped at today and
    floored at the Citadel release. None when a tail ID (nothing above it)
    lands more than FUTURE_SLACK_DAYS after today."""
    expected_ord, gap = _expected(structure_id, neighbours, rate)
    if tail and _past_frontier(expected_ord, today):
        return None
    expected = date.fromordinal(round(expected_ord))
    spread = timedelta(days=round(TOLERANCE_DAYS + TAIL_SPREAD * gap))
    return Window(expected, max(expected - spread, CITADEL_RELEASE),
                  min(expected + spread, today), tail=tail)


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
# Every function here binds structure_id, so each refuses one above INT64_MAX
# itself rather than trusting its caller.

def _from_sources(stmt, sources):
    return stmt.where(Cal.source.in_(sources)) if sources else stmt


async def _neighbours(db: AsyncSession, structure_id: int, sources):
    """Up to NEIGHBOURS points either side, nearest first."""
    below = (await db.execute(
        _from_sources(select(Cal.structure_id, Cal.anchor_mid)
                      .where(Cal.structure_id < structure_id), sources)
        .order_by(Cal.structure_id.desc()).limit(NEIGHBOURS))).all()
    above = (await db.execute(
        _from_sources(select(Cal.structure_id, Cal.anchor_mid)
                      .where(Cal.structure_id > structure_id), sources)
        .order_by(Cal.structure_id.asc()).limit(NEIGHBOURS))).all()
    return below, above


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
    """The plausible anchor-date window for structure_id, or None when it is
    past the frontier (FRONTIER_MSG). sources=("triff",) is the baseline that
    approved user points can't move. Pending submissions never count: they
    aren't in the calibration table. Raises LookupError when there are no
    points to compare with."""
    today = today or utc_today()
    if structure_id > INT64_MAX:
        return None
    below, above = await _neighbours(db, structure_id, sources)
    if not below and not above:
        raise LookupError("no calibration points")
    return window_for(structure_id, [*below, *above], await tail_rate(db), today,
                      tail=not above)


async def tail_estimate(db: AsyncSession, structure_id: int, *,
                        today: date | None = None) -> dict | None:
    """Estimate for an ID above the newest point: extrapolated at the recent
    rate, window widening with distance, nothing past today (so the age can
    never go negative). None past the frontier. Raises LookupError when there
    are no points below it."""
    today = today or utc_today()
    if structure_id > INT64_MAX:
        return None
    below, _ = await _neighbours(db, structure_id, None)
    if not below:
        raise LookupError("no calibration points")
    expected_ord, gap = _expected(structure_id, below, await tail_rate(db))
    if _past_frontier(expected_ord, today):
        return None
    mid = min(date.fromordinal(round(expected_ord)), today)
    half = timedelta(days=round(ESTIMATE_HALF_DAYS + TAIL_SPREAD * gap))
    low, high = mid - half, min(mid + half, today)
    return {"mid": datetime.combine(mid, time()), "low": datetime.combine(low, time()),
            "high": datetime.combine(high, time()), "days_wide": (high - low).days}


async def pending_for(db: AsyncSession, structure_id: int) -> bool:
    if structure_id > INT64_MAX:
        return False
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
        return Verdict(False, LIMIT_MSG, "limit")
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
