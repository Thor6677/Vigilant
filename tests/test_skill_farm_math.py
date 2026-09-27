"""Pure skill-farm math (T-073) — app/skillfarm/math.py. No DB, no ESI."""
from datetime import datetime, timedelta, timezone

from app.skillfarm import math as farm_math
from app.skillfarm.constants import LARGE_SKILL_INJECTOR_SP, SKILL_FLOOR_SP


# ── injectors_ready / sp_until_next_injector ────────────────────────────────

def test_below_the_extraction_floor_has_no_injectors_ready():
    # 5.4M SP, default floor 5,000,000 — nowhere near a full 500k surplus.
    assert farm_math.injectors_ready(5_400_000, SKILL_FLOOR_SP) == 0


def test_exactly_at_five_point_five_million_is_zero_ready():
    """5.5M SP is the minimum to USE a Skill Extractor, but the pilot's
    ALLOCATED sp here (5.5M) minus the 5.0M floor is only 500k — that's
    exactly one injector's worth, so this is the boundary between 0 and 1."""
    assert farm_math.injectors_ready(5_500_000, SKILL_FLOOR_SP) == 1


def test_just_under_five_point_five_million_is_zero_ready():
    assert farm_math.injectors_ready(5_499_999, SKILL_FLOOR_SP) == 0


def test_a_full_extra_500k_surplus_is_one_injector():
    assert farm_math.injectors_ready(SKILL_FLOOR_SP + 500_000, SKILL_FLOOR_SP) == 1
    assert farm_math.injectors_ready(SKILL_FLOOR_SP + 999_999, SKILL_FLOOR_SP) == 1
    assert farm_math.injectors_ready(SKILL_FLOOR_SP + 1_000_000, SKILL_FLOOR_SP) == 2


def test_base_sp_above_the_game_floor_is_respected():
    """A pilot who wants to keep more than the bare 5M banked (e.g. 20M) never
    counts SP below their own base_sp as extractable, even though the game
    itself would allow extracting further."""
    base_sp = 20_000_000
    assert farm_math.injectors_ready(20_400_000, base_sp) == 0
    assert farm_math.injectors_ready(20_500_000, base_sp) == 1
    # The game's own 5M floor never overrides a HIGHER base_sp.
    assert farm_math.injectors_ready(6_000_000, base_sp) == 0


def test_base_sp_below_the_game_floor_never_lowers_it():
    """A base_sp of 0 (or anything under 5M) still can't be extracted past
    the game's own hard floor."""
    assert farm_math.injectors_ready(5_000_000, 0) == 0
    assert farm_math.injectors_ready(5_500_000, 0) == 1


def test_sp_until_next_injector_at_the_floor_needs_a_full_injector():
    assert farm_math.sp_until_next_injector(SKILL_FLOOR_SP, SKILL_FLOOR_SP) == LARGE_SKILL_INJECTOR_SP


def test_sp_until_next_injector_partway_through():
    assert farm_math.sp_until_next_injector(SKILL_FLOOR_SP + 300_000, SKILL_FLOOR_SP) == 200_000


def test_sp_until_next_injector_right_after_one_ready():
    """Sitting exactly on a multiple (one ready) still needs a full 500k for
    the NEXT one, not 0."""
    assert farm_math.sp_until_next_injector(SKILL_FLOOR_SP + 500_000, SKILL_FLOOR_SP) == LARGE_SKILL_INJECTOR_SP


def test_t077_regression_allocated_sp_is_total_sp_not_total_minus_unallocated():
    """Real dev numbers (T-077): total_sp 231,404,353, unallocated_sp 78,971,
    base_sp 5,000,000, training at 2,160 SP/h.

    ESI's total_sp ALREADY excludes unallocated SP, so the allocated figure
    passed to injectors_ready/sp_until_next_injector must be total_sp AS-IS
    -- never total_sp - unallocated_sp. Subtracting unallocated_sp again
    (the bug) understated the surplus and reported "3d 8h" to the next
    injector instead of the correct ~44.3h (95,647 SP short at 2,160 SP/h)."""
    total_sp = 231_404_353
    unallocated_sp = 78_971
    base_sp = 5_000_000
    rate_per_hour = 2_160

    # The fix: pass total_sp as-is.
    assert farm_math.injectors_ready(total_sp, base_sp) == 452
    sp_needed = farm_math.sp_until_next_injector(total_sp, base_sp)
    assert sp_needed == 95_647
    eta_hours = farm_math.hours_until_next_injector(sp_needed, rate_per_hour)
    assert round(eta_hours, 1) == 44.3

    # The bug this guards against: subtracting unallocated_sp a second time
    # (it's already excluded from total_sp) understates the surplus and
    # reports the wrong, longer ETA -- ~80.8h ("3d 8h") instead of ~44.3h.
    buggy_allocated = total_sp - unallocated_sp
    buggy_sp_needed = farm_math.sp_until_next_injector(buggy_allocated, base_sp)
    buggy_eta_hours = farm_math.hours_until_next_injector(buggy_sp_needed, rate_per_hour)
    assert buggy_sp_needed != sp_needed
    assert round(buggy_eta_hours, 1) != round(eta_hours, 1)
    assert round(buggy_eta_hours, 1) == 80.8


# ── sp_per_hour / training state ────────────────────────────────────────────

def _entry(start, finish, training_start_sp, level_end_sp):
    return {
        "start_date": start.isoformat().replace("+00:00", "Z"),
        "finish_date": finish.isoformat().replace("+00:00", "Z"),
        "training_start_sp": training_start_sp,
        "level_end_sp": level_end_sp,
        "skill_id": 1,
        "finished_level": 3,
    }


def test_sp_per_hour_from_the_active_entry():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    start = now - timedelta(hours=2)
    finish = now + timedelta(hours=2)
    # 400,000 SP over a 4h window = 100,000 SP/hour.
    queue = [_entry(start, finish, 100_000, 500_000)]
    assert farm_math.sp_per_hour(queue, now=now) == 100_000.0


def test_sp_per_hour_is_zero_when_nothing_is_training():
    assert farm_math.sp_per_hour(None) == 0.0
    assert farm_math.sp_per_hour([]) == 0.0


def test_sp_per_hour_is_zero_between_queue_entries():
    """A queue whose entries don't bracket `now` at all (e.g. a stale cached
    payload after the whole queue finished) reads as not training, not a
    crash."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    finished = now - timedelta(hours=5)
    queue = [_entry(finished - timedelta(hours=2), finished, 0, 100_000)]
    assert farm_math.sp_per_hour(queue, now=now) == 0.0


def test_hours_until_next_injector_none_when_not_training():
    assert farm_math.hours_until_next_injector(200_000, 0.0) is None
    assert farm_math.hours_until_next_injector(200_000, -5.0) is None


def test_hours_until_next_injector_divides_cleanly():
    assert farm_math.hours_until_next_injector(200_000, 100_000) == 2.0


# ── monthly_profit ───────────────────────────────────────────────────────────

def test_monthly_profit_with_every_price_known():
    # rate 100k/hr -> 73,000,000 SP/month -> 146 injectors/month.
    profit, notes = farm_math.monthly_profit(
        rate_per_hour=100_000, lsi_price=800_000_000, extractor_price=600_000_000,
        tax_pct=8.0, plex_per_month=500, plex_price=5_000_000,
    )
    assert notes == []
    inj_month = farm_math.injectors_per_month(100_000)
    expected = inj_month * (800_000_000 * 0.92 - 600_000_000) - 500 * 5_000_000
    assert profit == expected


def test_monthly_profit_excludes_a_missing_extractor_price_but_keeps_going():
    profit, notes = farm_math.monthly_profit(
        rate_per_hour=100_000, lsi_price=800_000_000, extractor_price=None,
        tax_pct=8.0, plex_per_month=500, plex_price=5_000_000,
    )
    assert profit is not None
    assert any("Extractor" in n for n in notes)


def test_monthly_profit_excludes_a_missing_plex_price():
    profit, notes = farm_math.monthly_profit(
        rate_per_hour=100_000, lsi_price=800_000_000, extractor_price=600_000_000,
        tax_pct=8.0, plex_per_month=500, plex_price=None,
    )
    inj_month = farm_math.injectors_per_month(100_000)
    expected = inj_month * (800_000_000 * 0.92 - 600_000_000)  # no PLEX cost subtracted
    assert profit == expected
    assert any("PLEX" in n for n in notes)


def test_monthly_profit_is_none_when_nothing_is_priced():
    profit, notes = farm_math.monthly_profit(
        rate_per_hour=100_000, lsi_price=None, extractor_price=None,
        tax_pct=8.0, plex_per_month=500, plex_price=None,
    )
    assert profit is None
    assert len(notes) == 2


def test_monthly_profit_handles_zero_rate():
    """Not training: injectors/month is 0, so with a priced LSI the revenue
    line is legitimately 0 rather than excluded."""
    profit, notes = farm_math.monthly_profit(
        rate_per_hour=0.0, lsi_price=800_000_000, extractor_price=600_000_000,
        tax_pct=8.0, plex_per_month=500, plex_price=5_000_000,
    )
    assert profit == -500 * 5_000_000
