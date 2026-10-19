"""Submitting a known anchor date from the Structure Age result card
(spec §6–§7). Runs against the synthetic table in tests/_structure_age.py.
Ids and names are invented."""
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, StructureAgeSubmission
from app.intel import structure_age_check as sac
from tests._mobile import SITE_CSS
from tests._structure_age import (NEWEST_ID, POINTS, RATE, client, make_db, override_db,
                                  point_date, point_id, run, showinfo, today)

USER = 4401
OTHER = 4402
MID_ID = point_id(100) + RATE // 2
TAIL_ID = NEWEST_ID + 30 * RATE
FORM = 'hx-post="/tools/structure-age/submit"'


@pytest.fixture
def sa_db():
    engine, SL = run(make_db(users=[(USER, "user"), (OTHER, "user")]))
    undo = override_db(SL)
    yield SL
    undo()
    run(engine.dispose())


def _rows(SL):
    async def go():
        async with SL() as db:
            return (await db.execute(select(StructureAgeSubmission))).scalars().all()
    return run(go())


def _submit(structure_id, anchor_date, certainty="day"):
    return client(USER).post("/tools/structure-age/submit", data={
        "structure_id": str(structure_id), "anchor_date": anchor_date, "certainty": certainty})


def test_a_post_without_the_csrf_token_is_refused(sa_db):
    """Spec S2: the app-wide CSRF middleware covers this route too."""
    c = client(USER)
    del c.headers["X-CSRF-Token"]
    r = c.post("/tools/structure-age/submit", data={
        "structure_id": str(MID_ID), "anchor_date": point_date(100).isoformat(),
        "certainty": "day"})
    assert r.status_code == 403
    assert _rows(sa_db) == []


def test_the_box_keeps_the_phone_tap_floor(sa_db):
    """.sa-input carries a 70px textarea floor that outranks the global 44px
    phone rule. The box's single-line fields reset it and the phone block
    puts 44px back; an inline min-height would beat both."""
    page = client(USER).get("/tools/structure-age/partial",
                            params={"paste": showinfo(MID_ID)}).text
    box = page[page.index('class="sa-submit-box"'):]
    assert "min-height" not in box
    css = open(SITE_CSS, encoding="utf-8").read()
    rules = css[css.index(".sa-submit-box .sa-input, .sa-reject .sa-input { min-height: 0;"):]
    assert re.match(r"[^\n]*\n@media \(max-width: 640px\) \{\s*"
                    r"\.sa-submit-box \.sa-input, \.sa-reject \.sa-input \{ min-height: 44px; \}",
                    rules)


def test_strangers_get_401_and_nothing_is_written(sa_db):
    r = client().post("/tools/structure-age/submit", data={
        "structure_id": str(MID_ID), "anchor_date": point_date(100).isoformat(),
        "certainty": "day"})
    assert r.status_code == 401 and r.text == ""
    assert _rows(sa_db) == []


def test_the_form_shows_for_a_session_only(sa_db):
    paste = {"paste": showinfo(MID_ID)}
    anon = client().get("/tools/structure-age", params=paste).text
    mine = client(USER).get("/tools/structure-age", params=paste).text
    assert FORM not in anon and FORM in mine
    date_input = re.search(r'<input[^>]*name="anchor_date"[^>]*>', mine).group(0)
    assert 'type="text"' in date_input and "datetime-local" not in mine
    # The wormhole page and WH Tracker embed the partial; same rule there.
    assert FORM in client(USER).get("/tools/structure-age/partial", params=paste).text
    assert FORM not in client().get("/tools/structure-age/partial", params=paste).text


def test_a_sensible_date_is_queued_with_its_expected_date(sa_db):
    r = _submit(MID_ID, point_date(100).isoformat())
    assert r.status_code == 200 and "Sent for review" in r.text
    [row] = _rows(sa_db)
    assert (row.structure_id, row.status, row.certainty, row.submitted_by) == (
        MID_ID, "pending", "day", USER)
    assert abs((row.expected_date - point_date(100)).days) <= 1


def test_four_years_ago_is_refused_with_the_window(sa_db):
    r = _submit(MID_ID, (today() - timedelta(days=4 * 365)).isoformat())
    assert r.status_code == 200 and "plausible anchor date is" in r.text
    assert FORM in r.text                      # the form comes back to fix
    assert _rows(sa_db) == []


def test_a_new_structure_with_a_recent_date_is_accepted(sa_db):
    r = _submit(TAIL_ID, (point_date(POINTS - 1) + timedelta(days=28)).isoformat(), "week")
    assert "Sent for review" in r.text and len(_rows(sa_db)) == 1


@pytest.mark.parametrize("sid,date_,certainty,needle", [
    ("9" * 25, "2026-01-01", "day", "structure ID isn"),
    (str(MID_ID), "2026-13-45", "day", "YYYY-MM-DD"),
    (str(MID_ID), "yesterday", "day", "YYYY-MM-DD"),
    (str(MID_ID), "__DATE__" + " " * 22 + "garbage", "day", "YYYY-MM-DD"),   # parsed whole, not cut
    (str(MID_ID), "__DATE__", "forever", "how sure"),
    (str(sac.INT64_MAX + 1), "2026-01-01", "day", "higher than any structure yet"),
])
def test_malformed_input_gets_a_message_not_a_500(sa_db, sid, date_, certainty, needle):
    date_ = date_.replace("__DATE__", point_date(100).isoformat())
    r = client(USER).post("/tools/structure-age/submit",
                          data={"structure_id": sid, "anchor_date": date_, "certainty": certainty})
    assert r.status_code == 200 and needle in r.text
    assert _rows(sa_db) == []


def test_ids_already_known_or_waiting_show_a_note_instead(sa_db):
    r = _submit(point_id(50), point_date(50).isoformat())
    assert "has a confirmed date" in r.text and FORM not in r.text
    assert "Sent for review" in _submit(MID_ID, point_date(100).isoformat()).text
    r = _submit(MID_ID, point_date(100).isoformat())
    assert "awaiting review" in r.text and len(_rows(sa_db)) == 1
    # And the result card shows the note rather than the form.
    page = client(USER).get("/tools/structure-age", params={"paste": showinfo(MID_ID)}).text
    assert "awaiting review" in page and FORM not in page


def test_the_twenty_first_pending_is_refused(sa_db):
    async def seed():
        async with sa_db() as db:
            for i in range(sac.MAX_PENDING_PER_USER):
                db.add(StructureAgeSubmission(
                    structure_id=point_id(i) + 1, anchor_date=point_date(i), certainty="day",
                    status="pending", submitted_by=USER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(seed())
    r = _submit(MID_ID, point_date(100).isoformat())
    assert f"You already have {sac.MAX_PENDING_PER_USER}" in r.text
    assert len(_rows(sa_db)) == sac.MAX_PENDING_PER_USER


def test_the_cap_holds_even_when_the_first_count_was_stale(sa_db, monkeypatch):
    """Two submits racing from one user can both pass check_submission's
    count; the recount after the insert is what holds the cap."""
    async def seed():
        async with sa_db() as db:
            for i in range(sac.MAX_PENDING_PER_USER):
                db.add(StructureAgeSubmission(
                    structure_id=point_id(i) + 1, anchor_date=point_date(i), certainty="day",
                    status="pending", submitted_by=USER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(seed())

    async def stale_check(db, structure_id, anchor_date, user_id, **kw):
        return sac.Verdict(True, window=await sac.plausible_window(db, structure_id))
    monkeypatch.setattr(sac, "check_submission", stale_check)
    r = _submit(MID_ID, point_date(100).isoformat())
    assert f"You already have {sac.MAX_PENDING_PER_USER}" in r.text
    assert len(_rows(sa_db)) == sac.MAX_PENDING_PER_USER


def test_the_twentieth_pending_is_accepted(sa_db):
    async def seed():
        async with sa_db() as db:
            for i in range(sac.MAX_PENDING_PER_USER - 1):
                db.add(StructureAgeSubmission(
                    structure_id=point_id(i) + 1, anchor_date=point_date(i), certainty="day",
                    status="pending", submitted_by=USER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(seed())
    assert "Sent for review" in _submit(MID_ID, point_date(100).isoformat()).text
    assert len(_rows(sa_db)) == sac.MAX_PENDING_PER_USER


def test_a_race_on_the_same_id_lands_on_the_unique_index(sa_db, monkeypatch):
    """Two users submitting one ID at once can both pass check_submission;
    the partial unique index refuses the second, which reads as "awaiting
    review" — not a 500, and without naming the other submitter."""
    async def seed():
        async with sa_db() as db:
            db.add(StructureAgeSubmission(
                structure_id=MID_ID, anchor_date=point_date(100), certainty="day",
                status="pending", submitted_by=OTHER, submitted_at=datetime(2026, 10, 1)))
            await db.commit()
    run(seed())

    async def stale_check(db, structure_id, anchor_date, user_id, **kw):
        return sac.Verdict(True, window=await sac.plausible_window(db, structure_id))
    monkeypatch.setattr(sac, "check_submission", stale_check)
    r = _submit(MID_ID, point_date(100).isoformat())
    assert r.status_code == 200 and "awaiting review" in r.text
    assert str(OTHER) not in r.text
    assert len(_rows(sa_db)) == 1

    async def audit_rows():
        async with sa_db() as db:
            return (await db.execute(select(AdminAuditLog))).scalars().all()
    assert run(audit_rows()) == []


def test_an_id_no_date_could_fix_gets_a_message_not_a_form(sa_db):
    r = _submit(999_999_999_999, point_date(100).isoformat())
    assert "an Upwell structure ID" in r.text and FORM not in r.text


def test_echoed_values_are_escaped(sa_db):
    r = _submit(MID_ID, "<script>alert(1)</script>")
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_a_submission_is_audited_and_a_refusal_is_not(sa_db):
    _submit(MID_ID, (today() - timedelta(days=4 * 365)).isoformat())
    _submit(MID_ID, point_date(100).isoformat())

    async def events():
        async with sa_db() as db:
            return (await db.execute(
                select(AdminAuditLog.event_type, AdminAuditLog.user_id))).all()
    assert [tuple(e) for e in run(events())] == [("structure_age_submit", USER)]
