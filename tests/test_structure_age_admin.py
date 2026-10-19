"""Admin > Structure Age: reviewing user-submitted anchor points
(spec §6–§7). Runs against the synthetic table in tests/_structure_age.py.
Ids and names are invented."""
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db.models import (AdminAuditLog, Character, StructureAgeCalibration,
                           StructureAgeSubmission)
from tests._mobile import source
from tests._structure_age import (RATE, client, make_db, override_db, point_date, point_id,
                                  run)

ADMIN, MANAGER, USER = 4501, 4502, 4503
MID_ID = point_id(100) + RATE // 2


@pytest.fixture
def review_db():
    engine, SL = run(make_db(users=[(ADMIN, "admin"), (MANAGER, "manager"), (USER, "user")]))

    async def seed():
        async with SL() as db:
            db.add(Character(character_id=95000001, character_name="Sample Pilot", user_id=USER,
                             is_main=True, access_token="x", refresh_token="x",
                             token_expiry=datetime(2030, 1, 1)))
            db.add(StructureAgeSubmission(
                id=1, structure_id=MID_ID, anchor_date=point_date(100), certainty="day",
                status="pending", expected_date=point_date(100), submitted_by=USER,
                submitted_at=datetime(2026, 10, 1, 9, 30)))
            await db.commit()
    run(seed())
    undo = override_db(SL)
    yield SL
    undo()
    run(engine.dispose())


def _get(SL, model, key):
    async def go():
        async with SL() as db:
            return await db.get(model, key)
    return run(go())


def test_plain_users_cannot_review(review_db):
    assert client(USER).get("/admin/section/structure-age").status_code == 403
    for action in ("approve", "reject", "remove"):
        assert client(USER).post(f"/admin/structure-age/1/{action}").status_code == 403, action
    assert _get(review_db, StructureAgeSubmission, 1).status == "pending"


def test_the_queue_shows_the_claim_and_who_made_it(review_db):
    r = client(ADMIN).get("/admin/section/structure-age")
    assert r.status_code == 200
    assert str(MID_ID) in r.text and "Sample Pilot" in r.text
    assert 'hx-post="/admin/structure-age/1/approve"' in r.text
    assert "triff baseline" in r.text


def test_approving_adds_a_user_point_with_the_certainty_window(review_db):
    r = client(ADMIN).post("/admin/structure-age/1/approve")
    assert r.status_code == 200 and f"Approved {MID_ID}" in r.text
    row = _get(review_db, StructureAgeCalibration, MID_ID)
    assert row.source == "user" and row.days_wide == 2.0
    assert (row.anchor_high - row.anchor_low).days == 2
    sub = _get(review_db, StructureAgeSubmission, 1)
    assert (sub.status, sub.reviewed_by) == ("approved", ADMIN)


def test_approving_twice_does_nothing_the_second_time(review_db):
    client(ADMIN).post("/admin/structure-age/1/approve")
    r = client(MANAGER).post("/admin/structure-age/1/approve")
    assert "already reviewed" in r.text
    assert _get(review_db, StructureAgeSubmission, 1).reviewed_by == ADMIN


def test_a_manager_can_approve(review_db):
    client(MANAGER).post("/admin/structure-age/1/approve")
    assert _get(review_db, StructureAgeSubmission, 1).status == "approved"


def test_approval_racing_a_triff_point_rejects_instead(review_db):
    async def triff_arrives():
        async with review_db() as db:
            db.add(StructureAgeCalibration(
                structure_id=MID_ID, anchor_mid=datetime(2026, 1, 1),
                anchor_low=datetime(2025, 12, 29), anchor_high=datetime(2026, 1, 4),
                days_wide=6.0, fetched_at=datetime(2026, 10, 2), source="triff"))
            await db.commit()
    run(triff_arrives())
    r = client(ADMIN).post("/admin/structure-age/1/approve")
    assert "already has a calibration point" in r.text
    assert _get(review_db, StructureAgeCalibration, MID_ID).source == "triff"
    sub = _get(review_db, StructureAgeSubmission, 1)
    assert sub.status == "rejected" and sub.reject_reason == "ID already has a calibration point"


def test_rejecting_stores_a_capped_reason(review_db):
    client(ADMIN).post("/admin/structure-age/1/reject", data={"reject_reason": "x" * 500})
    sub = _get(review_db, StructureAgeSubmission, 1)
    assert sub.status == "rejected" and sub.reject_reason == "x" * 200


def test_a_script_in_a_reject_reason_renders_escaped(review_db):
    client(ADMIN).post("/admin/structure-age/1/reject",
                       data={"reject_reason": "<script>alert(1)</script>"})
    r = client(ADMIN).get("/admin/section/audit")
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text


def test_remove_deletes_the_user_point(review_db):
    client(ADMIN).post("/admin/structure-age/1/approve")
    r = client(ADMIN).post("/admin/structure-age/1/remove")
    assert f"Removed {MID_ID}" in r.text
    assert _get(review_db, StructureAgeCalibration, MID_ID) is None
    assert _get(review_db, StructureAgeSubmission, 1).status == "removed"


def test_remove_never_deletes_a_triff_point(review_db):
    async def approved_over_triff():
        async with review_db() as db:
            db.add(StructureAgeSubmission(
                id=2, structure_id=point_id(10), anchor_date=point_date(10), certainty="day",
                status="approved", submitted_by=USER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(approved_over_triff())
    client(ADMIN).post("/admin/structure-age/2/remove")
    assert _get(review_db, StructureAgeCalibration, point_id(10)).source == "triff"
    assert _get(review_db, StructureAgeSubmission, 2).status == "removed"


def test_review_actions_are_audited(review_db):
    client(ADMIN).post("/admin/structure-age/1/approve")
    client(ADMIN).post("/admin/structure-age/1/remove")

    async def events():
        async with review_db() as db:
            return [r[0] for r in (await db.execute(
                select(AdminAuditLog.event_type).order_by(AdminAuditLog.id))).all()]
    assert run(events()) == ["structure_age_approve", "structure_age_remove"]


def test_the_tab_is_listed_and_not_auto_refreshed():
    from app.routes import admin as admin_mod
    assert ("structure-age", "Structure Age") in admin_mod.ADMIN_SECTIONS
    refresh = source("admin.html").split("var refreshSections")[1].split(";")[0]
    assert "structure-age" not in refresh
