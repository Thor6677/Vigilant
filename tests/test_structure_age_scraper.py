"""Spec §3: a user point blocks a later triff result for the same ID —
_store_exact's on_conflict_do_nothing, against the hermetic test database
that app.db.models' AsyncSessionLocal points at. Ids are invented."""
from datetime import date, datetime

from sqlalchemy import delete

from app.db.models import AsyncSessionLocal, StructureAgeCalibration, engine
from app.intel import structure_age_check as sac
from app.intel.structure_age_scraper import _store_exact
from tests._structure_age import run

SID = 1_055_900_000_017
NEW = 1_055_900_000_019          # in the same batch, not in the table yet


def test_a_triff_result_never_overwrites_a_user_point():
    async def go():
        try:
            async with AsyncSessionLocal() as db:
                db.add(StructureAgeCalibration(**sac.calibration_values(
                    SID, date(2026, 9, 30), "day", datetime(2026, 10, 1))))
                await db.commit()
            # _store_exact swallows every error, so NEW proves the batch was
            # really written and SID's survival isn't just a failed insert.
            await _store_exact([{
                "id": str(sid), "method": "exact", "midISO": "2026-08-01T00:00:00.000Z",
                "lowISO": "2026-07-29T00:00:00.000Z", "highISO": "2026-08-04T00:00:00.000Z",
                "daysWide": "6.0"} for sid in (SID, NEW)])
            async with AsyncSessionLocal() as db:
                kept = await db.get(StructureAgeCalibration, SID)
                added = await db.get(StructureAgeCalibration, NEW)
                return (kept.source, kept.anchor_mid), added and added.source
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(StructureAgeCalibration)
                                 .where(StructureAgeCalibration.structure_id.in_((SID, NEW))))
                await db.commit()
            # Every test drives its own event loop; don't leave a pooled
            # connection pinned to this one.
            await engine.dispose()
    assert run(go()) == (("user", datetime(2026, 9, 30)), "triff")
