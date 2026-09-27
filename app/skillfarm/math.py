"""Pure skill-farm math (T-073) -- no I/O, no ESI, no DB. Unit-testable in
complete isolation from the page/route code.

Vocabulary:
  * "allocated" SP -- SP applied to a trained skill; what a Skill Extractor
    can actually pull from. Equal to ESI's own ``total_sp`` as-is (T-077:
    ``total_sp`` already excludes unallocated SP -- confirmed against live
    data, where it equalled the sum of ``skillpoints_in_skill`` across every
    trained skill -- so it must never be reduced by ``unallocated_sp`` again;
    see app/skillfarm/rows.py:build_pilot_row, which used to do exactly
    that).
  * "unallocated" SP -- SP banked but not yet applied to any skill (e.g. left
    over from a remap). Never extractable, shown separately on the page, and
    never counted toward ``injectors_ready`` -- and never subtracted from
    ``total_sp`` either, since it was never included there to begin with.

Known caveat (report-only, not fixed here): ESI's ``total_sp`` reflects
skills as trained so far; it does not credit progress the character is
partway through on its *currently training* skill. "Next injector in" is
therefore slightly pessimistic -- it doesn't know about SP the pilot has
already banked from an in-progress level that hasn't finished yet.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.skillfarm.constants import (
    HOURS_PER_MONTH,
    LARGE_SKILL_INJECTOR_SP,
    SKILL_FLOOR_SP,
)


def injectors_ready(allocated_sp: int, base_sp: int) -> int:
    """How many Large Skill Injectors' worth of ALLOCATED SP sit above both
    the pilot's own ``base_sp`` floor and the game's hard 5,000,000 floor.

    Callers pass ESI's ``total_sp`` as-is (T-077: it already excludes
    unallocated SP -- do NOT subtract ``unallocated_sp`` from it again, see
    the module docstring).
    """
    floor = max(int(base_sp), SKILL_FLOOR_SP)
    surplus = int(allocated_sp) - floor
    if surplus <= 0:
        return 0
    return surplus // LARGE_SKILL_INJECTOR_SP


def sp_until_next_injector(allocated_sp: int, base_sp: int) -> int:
    """SP still needed, above the current multiple of 500k over the floor,
    before ``injectors_ready`` increases by one.

    Always a positive amount in ``(0, LARGE_SKILL_INJECTOR_SP]`` -- when the
    pilot is sitting exactly on a multiple (including right at the floor,
    where 0 are ready yet), a full injector's worth is still needed to reach
    the *next* one.
    """
    floor = max(int(base_sp), SKILL_FLOOR_SP)
    surplus = max(0, int(allocated_sp) - floor)
    remainder = surplus % LARGE_SKILL_INJECTOR_SP
    return LARGE_SKILL_INJECTOR_SP - remainder


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def current_training_entry(skillqueue: list[dict] | None, now: datetime | None = None) -> dict | None:
    """The skillqueue entry currently training (``start_date <= now <=
    finish_date``), or ``None`` if nothing is -- an empty/absent queue, a
    paused queue, or a queue whose entries don't bracket ``now`` at all.
    """
    if not skillqueue:
        return None
    now = now or datetime.now(timezone.utc)
    for entry in skillqueue:
        start = _parse_dt(entry.get("start_date"))
        finish = _parse_dt(entry.get("finish_date"))
        if start and finish and start <= now <= finish:
            return entry
    return None


def sp_per_hour(skillqueue: list[dict] | None, now: datetime | None = None) -> float:
    """SP/hour from the CURRENTLY TRAINING entry's own rate:
    ``(level_end_sp - training_start_sp) / (finish_date - start_date)``.

    Deliberately no attribute/implant/booster maths -- this is the
    character's ACTUAL observed rate for the skill it is training right now,
    which already bakes in whatever attributes, remaps and boosters produced
    that finish_date. Returns ``0.0`` when nothing is training (empty/paused
    queue, or the field hasn't synced) -- callers show a "not training"
    warning rather than a fabricated rate.
    """
    entry = current_training_entry(skillqueue, now)
    if entry is None:
        return 0.0
    start = _parse_dt(entry.get("start_date"))
    finish = _parse_dt(entry.get("finish_date"))
    level_end_sp = entry.get("level_end_sp")
    training_start_sp = entry.get("training_start_sp")
    if start is None or finish is None or level_end_sp is None or training_start_sp is None:
        return 0.0
    seconds = (finish - start).total_seconds()
    if seconds <= 0:
        return 0.0
    sp_delta = level_end_sp - training_start_sp
    if sp_delta <= 0:
        return 0.0
    return (sp_delta / seconds) * 3600.0


def sp_per_month(rate_per_hour: float) -> float:
    return rate_per_hour * HOURS_PER_MONTH


def injectors_per_month(rate_per_hour: float) -> float:
    return sp_per_month(rate_per_hour) / LARGE_SKILL_INJECTOR_SP


def hours_until_next_injector(sp_needed: int, rate_per_hour: float) -> float | None:
    """Hours until ``sp_needed`` more SP accrue at ``rate_per_hour``, or
    ``None`` when the pilot isn't training (rate <= 0) -- there is no ETA to
    give, and the page shows that as "--" rather than a divide-by-zero."""
    if rate_per_hour is None or rate_per_hour <= 0:
        return None
    return sp_needed / rate_per_hour


def monthly_profit(
    rate_per_hour: float,
    lsi_price: float | None,
    extractor_price: float | None,
    tax_pct: float,
    plex_per_month: int,
    plex_price: float | None,
) -> tuple[float | None, list[str]]:
    """Monthly profit for one farm pilot, and the notes explaining any price
    that had to be excluded.

    ``monthly profit = injectors/month * (LSI price * (1 - tax) - extractor
    price) - plex_per_month * PLEX price``

    Returns ``(None, notes)`` only when there is NOTHING priced to compute at
    all (both the injector revenue line and the PLEX cost line are
    unpriced); a single missing price is instead excluded from its own line
    (extractor cost defaults to 0 if missing, PLEX cost/injector revenue
    dropped entirely if unpriced) with a note, per the T-073 spec.
    """
    notes: list[str] = []
    inj_month = injectors_per_month(rate_per_hour)

    have_revenue = lsi_price is not None
    revenue = 0.0
    if have_revenue:
        tax_mult = max(0.0, 1.0 - (tax_pct / 100.0))
        if extractor_price is None:
            notes.append("Skill Extractor price unavailable — excluded from per-injector cost")
        per_injector = lsi_price * tax_mult - (extractor_price or 0.0)
        revenue = inj_month * per_injector
    else:
        notes.append("Large Skill Injector price unavailable — injector revenue excluded")

    have_cost = plex_price is not None
    cost = plex_per_month * plex_price if have_cost else 0.0
    if not have_cost:
        notes.append("PLEX price unavailable — subscription cost excluded")

    if not have_revenue and not have_cost:
        return None, notes
    return revenue - cost, notes
