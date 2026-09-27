"""app/skillfarm/rows.py — build_pilot_row (T-073). Pure, no DB, no ESI.

Focus: the T-077 allocated-SP regression. build_pilot_row is the one place
that turns a synced skills summary (total_sp, unallocated_sp) into the
"allocated" figure the rest of the farm math works in — see
app/skillfarm/math.py's module docstring for why that figure must be
total_sp as-is, never total_sp - unallocated_sp.
"""
from app.skillfarm import rows as farm_rows


def _row(**overrides):
    base = dict(
        pilot_id=1, character_id=100, character_name="Test Pilot", base_sp=5_000_000,
        summary={"total_sp": 231_404_353, "unallocated_sp": 78_971, "levels": {}},
        skillqueue=[{
            "skill_id": 1, "finished_level": 3,
            "start_date": "2026-01-01T00:00:00Z", "finish_date": "2026-01-11T00:00:00Z",
            "training_start_sp": 0, "level_end_sp": 518_400,  # 2,160 SP/h over 10 days
        }],
        lsi_price=None, extractor_price=None, plex_price=None,
        sales_tax_pct=8.0, plex_per_month=500,
    )
    base.update(overrides)
    return base


def test_t077_regression_row_uses_total_sp_as_allocated_not_total_minus_unallocated():
    """Real dev numbers (T-077): total_sp 231,404,353, unallocated_sp 78,971,
    base_sp 5,000,000, a queue training at 2,160 SP/h. The row must report
    452 ready and a ~44.3h ETA (95,647 SP short) to the next one, not the
    pre-fix "3d 8h" (80.8h) that came from subtracting unallocated_sp a
    second time on top of ESI's already-unallocated-excluding total_sp."""
    import datetime as _dt
    from unittest import mock

    now = _dt.datetime(2026, 1, 2, tzinfo=_dt.timezone.utc)
    with mock.patch("app.skillfarm.math.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = _dt.datetime.fromisoformat
        row = farm_rows.build_pilot_row(**_row())

    assert row["status"] == "ok"
    assert row["total_sp"] == 231_404_353
    assert row["unallocated_sp"] == 78_971
    assert row["allocated_sp"] == 231_404_353  # NOT total_sp - unallocated_sp
    assert row["rate_per_hour"] == 2_160.0
    assert row["ready_now"] == 452
    assert row["sp_needed"] == 95_647
    assert round(row["eta_hours"], 1) == 44.3
    assert row["eta_str"] == "1d 20h"
    assert row["eta_str"] != "3d 8h"  # the pre-fix, doubly-subtracted answer


def test_status_no_scope_and_waiting_skip_numeric_fields():
    row = farm_rows.build_pilot_row(**_row(summary="no_scope"))
    assert row["status"] == "no_scope"
    assert "total_sp" not in row

    row2 = farm_rows.build_pilot_row(**_row(summary=None))
    assert row2["status"] == "waiting"
    assert "total_sp" not in row2
