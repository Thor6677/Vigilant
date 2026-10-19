"""Admin > Structure Age: reviewing user-submitted anchor points
(spec §6–§7). Runs against the synthetic table in tests/_structure_age.py.
Ids and names are invented."""
import re
from datetime import datetime, time, timedelta

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
    r = client(ADMIN).post("/admin/structure-age/2/remove")
    assert "no user point for it was in the table" in r.text
    assert _get(review_db, StructureAgeCalibration, point_id(10)).source == "triff"
    assert _get(review_db, StructureAgeSubmission, 2).status == "removed"


def test_review_actions_are_audited_with_the_claim(review_db):
    """Spec S14: every review action names the structure ID, date and
    certainty, so the audit log can be searched by structure."""
    other = point_id(130) + RATE // 2

    async def second():
        async with review_db() as db:
            db.add(StructureAgeSubmission(
                id=4, structure_id=other, anchor_date=point_date(130), certainty="week",
                status="pending", submitted_by=USER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(second())
    client(ADMIN).post("/admin/structure-age/1/approve")
    client(ADMIN).post("/admin/structure-age/1/remove")
    client(ADMIN).post("/admin/structure-age/4/reject", data={"reject_reason": "Sample reason"})

    async def events():
        async with review_db() as db:
            return (await db.execute(select(AdminAuditLog.event_type, AdminAuditLog.detail)
                                     .order_by(AdminAuditLog.id))).all()
    rows = run(events())
    assert [r[0] for r in rows] == ["structure_age_approve", "structure_age_remove",
                                    "structure_age_reject"]
    for (_, detail), sid, when in zip(rows, (MID_ID, MID_ID, other),
                                      (point_date(100), point_date(100), point_date(130))):
        assert str(sid) in detail and when.isoformat() in detail, detail
    assert rows[2][1].endswith(": Sample reason")


def test_an_oversized_submission_id_is_refused_not_a_500(review_db):
    for action in ("approve", "reject", "remove"):
        r = client(ADMIN).post(f"/admin/structure-age/99999999999999999999/{action}")
        assert r.status_code == 422, action


def test_the_triff_baseline_ignores_user_points(review_db):
    """Spec S5: the queue's second gap is measured against triff points only,
    so drift from a run of approved user points still shows there after the
    full table has moved with them."""
    async def drift():
        async with review_db() as db:
            # More than NEIGHBOURS user points around MID_ID, all 40 days late.
            for i in range(70, 131):
                mid = datetime.combine(point_date(i) + timedelta(days=40), time())
                db.add(StructureAgeCalibration(
                    structure_id=point_id(i) + RATE // 4, anchor_mid=mid, anchor_low=mid,
                    anchor_high=mid, days_wide=2.0, fetched_at=datetime(2026, 10, 2),
                    source="user"))
            await db.commit()
    run(drift())
    text = " ".join(client(ADMIN).get("/admin/section/structure-age").text.split())
    full = int(re.search(r"expected \d\d \w{3} \d{4} \(([+-]\d+) d\)", text).group(1))
    baseline = int(re.search(r"triff baseline ([+-]\d+) d", text).group(1))
    assert full <= -10                     # the full table has moved with the user points
    assert abs(baseline) <= 1              # the triff-only baseline has not


def test_who_is_shown_main_character_first_or_removed_user(review_db):
    async def more():
        async with review_db() as db:
            # "Alt Pilot" sorts first, but the main character is the label.
            db.add(Character(character_id=95000002, character_name="Alt Pilot", user_id=USER,
                             is_main=False, access_token="x", refresh_token="x",
                             token_expiry=datetime(2030, 1, 1)))
            db.add(StructureAgeSubmission(
                id=3, structure_id=point_id(120) + RATE // 2, anchor_date=point_date(120),
                certainty="day", status="pending", submitted_by=None,
                submitted_at=datetime(2026, 10, 1, 10, 0)))
            await db.commit()
    run(more())
    text = client(ADMIN).get("/admin/section/structure-age").text
    assert "by Sample Pilot" in text and "Alt Pilot" not in text
    assert "by (removed user)" in text


def test_the_tab_is_listed_and_not_auto_refreshed():
    from app.routes import admin as admin_mod
    assert ("structure-age", "Structure Age") in admin_mod.ADMIN_SECTIONS
    refresh = source("admin.html").split("var refreshSections")[1].split(";")[0]
    assert "structure-age" not in refresh
