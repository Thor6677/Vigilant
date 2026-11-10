"""Structure timers synced from a corporation's ESI structures list (ISS-173).

ESI shows a corporation's structures only to its own members. The sync used
to file them on the shared board with no ACL and no corporation, so every
account on the instance saw them on /structure-timers and in the timer
banners, and a Director or app admin from anywhere could edit or delete them.

Now an 'esi' timer records the corporation it was synced for and is shown
only to accounts that own a character currently in that corporation. A row
from before that (no corporation recorded) is shown to no one, admins
included, until a member's next sync claims it. Editing or deleting one also
needs a character in that corporation, on top of the existing role rule.
Deleting an ACL group that any timer still uses is refused: it used to
unlink the timers and so show them to everyone.

Follow-ups:
- The Director or CEO role counts for an 'esi' timer only when the character
  holding it is in the timer's corporation. A Director of another
  corporation with a plain member character in this one may not.
- A manual timer records no corporation, so the role counts only in a
  corporation the timer's creator has a character in. A Director or CEO of
  any other corporation (anyone can found one) may not, and with no creator
  on record the role counts for nothing.
- A timer may be filed (created, or moved on edit) only under an ACL group
  the user can see. A refused request changes nothing.
- A refused edit or delete, a refused filing and a refused group delete say
  so on the page (?err=<code>), and the page offers Edit and Delete only on
  the timers the user may change, and no delete for a group in use.

Names and ids are invented.
"""
import asyncio
import base64
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from html import unescape

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.esi.client as esi_client
import app.routes.corporations as corps
from app.db.models import (
    Base, Character, StructureTimer, TimerACLEntry, TimerACLGroup, User, get_db,
)
from app.routes.structure_timers import sync_esi_structure_timers

CSRF = "test-csrf-token"
CORP_A, CORP_B, CORP_C = 98200001, 98200002, 98200003

A_USER = 5201        # one character, in A
B_USER = 5202        # one character, in B
BOTH_USER = 5203     # one character in A, one in B
ADMIN_USER = 5204    # app admin, character in C only
A_MANAGER = 5205     # app manager, character in A
B_DIRECTOR = 5206    # in-game Director, character in B only
SPLIT_DIRECTOR = 5207  # plain member in A, in-game Director in B
A_DIRECTOR = 5208    # in-game Director, character in A

SID_ALPHA, SID_BRAVO, SID_LEGACY, SID_BRAVO_OLD = (
    1030000000001, 1030000000002, 1030000000003, 1030000000004)
ALL_NAMES = ("Alpha Keep", "Bravo Keep", "Legacy Keep", "Bravo Archive", "Manual Keep")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat() + "Z"


def _struct(sid, name, corp, state="armor_reinforce", hours=12):
    # No system_id or type_id: the sync then skips the SDE lookups.
    return {"structure_id": sid, "name": name, "corporation_id": corp,
            "state": state, "state_timer_end": _iso(_now() + timedelta(hours=hours))}


def _char(cid, user_id, corp, scopes=""):
    return Character(character_id=cid, character_name=f"Pilot {cid}", user_id=user_id,
                     corporation_id=corp, access_token="a", refresh_token="r",
                     token_expiry=_now() + timedelta(days=1), scopes=scopes)


def _client(user_id):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(
        json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode())).decode()
    # No `with`: the app's startup (and its migrations) must not run here.
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    c.headers.update({"X-CSRF-Token": CSRF})
    return c


@pytest.fixture
def board(monkeypatch):
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add_all([User(id=A_USER), User(id=B_USER), User(id=BOTH_USER),
                        User(id=ADMIN_USER, role="admin"), User(id=A_MANAGER, role="manager"),
                        User(id=B_DIRECTOR), User(id=SPLIT_DIRECTOR), User(id=A_DIRECTOR)])
            db.add_all([
                _char(93000001, A_USER, CORP_A, scopes="esi-corporations.read_structures.v1"),
                _char(93000002, B_USER, CORP_B),
                _char(93000003, BOTH_USER, CORP_A),
                _char(93000004, BOTH_USER, CORP_B),
                _char(93000005, ADMIN_USER, CORP_C),
                _char(93000006, A_MANAGER, CORP_A),
                _char(93000007, B_DIRECTOR, CORP_B,
                      scopes="esi-characters.read_corporation_roles.v1"),
                _char(93000008, SPLIT_DIRECTOR, CORP_A),
                _char(93000009, SPLIT_DIRECTOR, CORP_B,
                      scopes="esi-characters.read_corporation_roles.v1"),
                _char(93000010, A_DIRECTOR, CORP_A,
                      scopes="esi-characters.read_corporation_roles.v1"),
            ])
            await db.commit()
    _run(seed())

    # Every character with the roles scope (B_DIRECTOR's, SPLIT_DIRECTOR's
    # corp B one and A_DIRECTOR's) holds Director in game. Nobody else has
    # the scope, so no other character reaches this.
    async def fake_refresh(char, db):
        return "token"

    class FakeClient:
        def __init__(self, token, db=None):
            pass

        async def get(self, path):
            return {"roles": ["Director"]}

    monkeypatch.setattr(esi_client, "refresh_token", fake_refresh)
    monkeypatch.setattr(esi_client, "ESIClient", FakeClient)

    async def override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = override
    yield SessionLocal
    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


def _sync(SessionLocal, structs, corp):
    async def go():
        async with SessionLocal() as db:
            await sync_esi_structure_timers(db, structs, corporation_id=corp)
    _run(go())


def _seed_esi(SessionLocal):
    """Alpha Keep (A), Bravo Keep (B), Bravo Archive (B, archived) and Legacy
    Keep (an 'esi' row from before ISS-173, no corporation)."""
    _sync(SessionLocal, [_struct(SID_ALPHA, "Alpha Keep", CORP_A)], CORP_A)
    _sync(SessionLocal, [_struct(SID_BRAVO, "Bravo Keep", CORP_B),
                         _struct(SID_BRAVO_OLD, "Bravo Archive", CORP_B, state="hull_reinforce")],
          CORP_B)

    async def go():
        async with SessionLocal() as db:
            await db.execute(update(StructureTimer)
                             .where(StructureTimer.structure_name == "Bravo Archive")
                             .values(is_archived=True, archived_at=_now(),
                                     timer_expires=_now() - timedelta(hours=3)))
            db.add(StructureTimer(
                structure_name="Legacy Keep", structure_type="fortizar", system_name="Sample-01",
                owner_name="Our Corp", disposition="friendly", timer_phase="armor",
                timer_expires=_now() + timedelta(hours=10), priority="critical",
                source="esi", esi_structure_id=SID_LEGACY))
            await db.commit()
    _run(go())


def _row(SessionLocal, name):
    async def go():
        async with SessionLocal() as db:
            return (await db.execute(
                select(StructureTimer).where(StructureTimer.structure_name == name))).scalar_one_or_none()
    return _run(go())


def _shown(html):
    return {n for n in ALL_NAMES if n in html}


def _page(user_id):
    r = _client(user_id).get("/structure-timers", follow_redirects=False)
    assert r.status_code == 200, r.text[:300]
    return _shown(r.text)


def _banners(user_id):
    r = _client(user_id).get("/alerts/timer-banners")
    assert r.status_code == 200
    return _shown(r.text)


def _set_corp(SessionLocal, character_id, corp):
    async def go():
        async with SessionLocal() as db:
            await db.execute(update(Character).where(Character.character_id == character_id)
                             .values(corporation_id=corp))
            await db.commit()
    _run(go())


# ── the sync records where a row came from ──────────────────────────────


def test_sync_stamps_new_rows_with_the_corporation_it_synced_for(board):
    _sync(board, [_struct(SID_ALPHA, "Alpha Keep", CORP_A)], CORP_A)
    row = _row(board, "Alpha Keep")
    assert row.source == "esi"
    assert row.esi_corporation_id == CORP_A


def test_sync_skips_a_structure_listed_for_another_corporation(board):
    _sync(board, [_struct(SID_BRAVO, "Bravo Keep", CORP_B)], CORP_A)
    assert _row(board, "Bravo Keep") is None


def test_corp_detail_hands_its_corporation_to_the_sync(board, monkeypatch, network_unreachable):
    # The call site swallows any exception from the sync, so a missed
    # argument there would only show as rows that never appear.
    async def fake_fallback(scope_name, scope_chars, api_call_func, corp_id, db):
        if scope_name == "structures":
            return [_struct(SID_ALPHA, "Alpha Keep", CORP_A)], None
        return None, None
    monkeypatch.setattr(corps, "_try_api_call_with_fallback", fake_fallback)

    r = _client(A_USER).get(f"/corporations/{CORP_A}/detail")
    assert r.status_code == 200
    row = _row(board, "Alpha Keep")
    assert row is not None and row.esi_corporation_id == CORP_A


def test_resync_claims_a_row_synced_before_the_corporation_was_recorded(board):
    _seed_esi(board)
    assert _row(board, "Legacy Keep").esi_corporation_id is None
    _sync(board, [_struct(SID_LEGACY, "Legacy Keep", CORP_A, hours=8)], CORP_A)
    assert _row(board, "Legacy Keep").esi_corporation_id == CORP_A
    assert "Legacy Keep" in _page(A_USER)
    assert "Legacy Keep" not in _page(B_USER)


def test_a_structure_that_changed_hands_moves_to_its_new_corporation(board):
    # Refreshing the old owner's row in place would show the new owner's
    # timer to the old corporation.
    _seed_esi(board)
    _sync(board, [_struct(SID_ALPHA, "Alpha Keep", CORP_B, hours=20)], CORP_B)
    assert _row(board, "Alpha Keep").esi_corporation_id == CORP_B
    assert "Alpha Keep" not in _page(A_USER)
    assert "Alpha Keep" in _page(B_USER)


# ── who sees an ESI timer ───────────────────────────────────────────────


def test_a_member_sees_their_corporations_timers_and_not_another_corporations(board):
    _seed_esi(board)
    for surface in (_page, _banners):
        assert surface(A_USER) == {"Alpha Keep"}, surface.__name__
        assert surface(B_USER) == {"Bravo Keep"} | ({"Bravo Archive"} if surface is _page else set()), \
            surface.__name__


def test_archived_esi_timers_follow_the_same_rule(board):
    _seed_esi(board)
    assert "Bravo Archive" in _page(B_USER)
    assert "Bravo Archive" not in _page(A_USER)
    assert "Bravo Archive" not in _page(ADMIN_USER)


def test_an_account_with_characters_in_both_corporations_sees_both(board):
    _seed_esi(board)
    assert _page(BOTH_USER) == {"Alpha Keep", "Bravo Keep", "Bravo Archive"}
    assert _banners(BOTH_USER) == {"Alpha Keep", "Bravo Keep"}


def test_a_character_that_left_the_corporation_stops_seeing_its_timers(board):
    _seed_esi(board)
    assert "Alpha Keep" in _page(A_USER)
    _set_corp(board, 93000001, CORP_C)
    assert _page(A_USER) == set()
    assert _banners(A_USER) == set()


def test_an_app_admin_outside_the_corporation_does_not_see_its_timers(board):
    _seed_esi(board)
    assert _page(ADMIN_USER) == set()
    assert _banners(ADMIN_USER) == set()


def test_a_row_with_no_recorded_corporation_is_shown_to_no_one(board):
    _seed_esi(board)
    for user in (A_USER, B_USER, BOTH_USER, ADMIN_USER, A_MANAGER, B_DIRECTOR):
        assert "Legacy Keep" not in _page(user), user
        assert "Legacy Keep" not in _banners(user), user


def test_an_acl_group_narrows_an_esi_timer_but_never_widens_it(board):
    _seed_esi(board)

    async def go():
        async with board() as db:
            # Lists BOTH_USER's corp A character and the whole of corp B.
            db.add(TimerACLGroup(id=20, name="Sample group", created_by=A_MANAGER))
            db.add(TimerACLEntry(group_id=20, entry_type="character", eve_id=93000003))
            db.add(TimerACLEntry(group_id=20, entry_type="corporation", eve_id=CORP_B))
            await db.execute(update(StructureTimer).where(StructureTimer.structure_name == "Alpha Keep")
                             .values(acl_group_id=20))
            await db.commit()
    _run(go())
    assert "Alpha Keep" in _page(BOTH_USER)
    assert "Alpha Keep" not in _page(A_USER)
    assert "Alpha Keep" not in _page(B_USER)
    assert "Alpha Keep" not in _banners(B_USER)


def test_manual_timers_keep_their_board_wide_default(board):
    async def go():
        async with board() as db:
            db.add(StructureTimer(
                structure_name="Manual Keep", structure_type="astrahus", system_name="Sample-02",
                owner_name="Sample Hostile Corp", disposition="hostile", timer_phase="shield",
                timer_expires=_now() + timedelta(hours=6), source="manual", created_by=A_USER))
            await db.commit()
    _run(go())
    for user in (A_USER, B_USER, ADMIN_USER):
        assert "Manual Keep" in _page(user)
        assert "Manual Keep" in _banners(user)


# ── who may edit or delete one ──────────────────────────────────────────


def _edit(user_id, timer_id, name):
    return _client(user_id).post(f"/structure-timers/{timer_id}/edit", follow_redirects=False, data={
        "structure_name": name, "timer_mode": "countdown", "countdown_text": "",
        "acl_group_id": ""})


def _delete(user_id, timer_id):
    return _client(user_id).post(f"/structure-timers/{timer_id}/delete", follow_redirects=False)


@pytest.mark.parametrize("user_id", [B_USER, ADMIN_USER, B_DIRECTOR])
def test_no_one_outside_the_corporation_may_edit_or_delete_its_timer(board, user_id):
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    assert _edit(user_id, tid, "Renamed Keep").status_code == 302
    assert _row(board, "Alpha Keep") is not None
    assert _delete(user_id, tid).status_code == 302
    assert _row(board, "Alpha Keep") is not None


def test_a_member_without_a_role_still_may_not(board):
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    _edit(A_USER, tid, "Renamed Keep")
    _delete(A_USER, tid)
    assert _row(board, "Alpha Keep") is not None


def test_a_member_with_the_role_may_edit_and_delete(board):
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    _edit(A_MANAGER, tid, "Alpha Keep Renamed")
    row = _row(board, "Alpha Keep Renamed")
    assert row is not None and row.esi_corporation_id == CORP_A
    _delete(A_MANAGER, tid)
    assert _row(board, "Alpha Keep Renamed") is None


def test_a_row_with_no_recorded_corporation_may_not_be_modified(board):
    _seed_esi(board)
    tid = _row(board, "Legacy Keep").id
    for user in (ADMIN_USER, A_MANAGER, B_DIRECTOR):
        _edit(user, tid, "Renamed Keep")
        _delete(user, tid)
    assert _row(board, "Legacy Keep") is not None


# ── the role and the corporation belong to the same character ──────────


def _add_manual(SessionLocal, *, created_by, archived=False, acl_group_id=None):
    async def go():
        async with SessionLocal() as db:
            db.add(StructureTimer(
                structure_name="Manual Keep", structure_type="astrahus", system_name="Sample-02",
                region_name="Sample Region", owner_name="Sample Hostile Corp",
                disposition="hostile", timer_phase="shield", notes="Sample note",
                timer_expires=_now() + (timedelta(hours=-3) if archived else timedelta(hours=6)),
                is_archived=archived, archived_at=_now() if archived else None,
                source="manual", acl_group_id=acl_group_id, created_by=created_by))
            await db.commit()
    _run(go())


def test_a_director_elsewhere_with_a_member_here_may_not_modify_this_corporations_timer(board):
    # SPLIT_DIRECTOR sees Alpha Keep through a plain member in A. Their
    # Director is a character in B, and that role is not one in A.
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    assert "Alpha Keep" in _page(SPLIT_DIRECTOR)
    _edit(SPLIT_DIRECTOR, tid, "Renamed Keep")
    _delete(SPLIT_DIRECTOR, tid)
    row = _row(board, "Alpha Keep")
    assert row is not None and row.id == tid


def test_the_same_director_may_modify_their_own_corporations_timer(board):
    _seed_esi(board)
    tid = _row(board, "Bravo Keep").id
    _edit(SPLIT_DIRECTOR, tid, "Bravo Keep Renamed")
    assert _row(board, "Bravo Keep Renamed") is not None
    _delete(SPLIT_DIRECTOR, tid)
    assert _row(board, "Bravo Keep Renamed") is None


def test_a_director_in_the_timers_corporation_may_modify_it(board):
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    _edit(A_DIRECTOR, tid, "Alpha Keep Renamed")
    assert _row(board, "Alpha Keep Renamed") is not None
    _delete(A_DIRECTOR, tid)
    assert _row(board, "Alpha Keep Renamed") is None


def _manual_snapshot(SessionLocal):
    row = _row(SessionLocal, "Manual Keep")
    assert row is not None
    return {c: getattr(row, c) for c in ("id", "structure_name", "region_name", "notes",
                                         "timer_expires", "acl_group_id", "created_by")}


def test_a_director_in_the_creators_corporation_may_modify_a_manual_timer(board):
    _add_manual(board, created_by=A_USER)
    tid = _row(board, "Manual Keep").id
    assert _edit(A_DIRECTOR, tid, "Manual Keep Renamed").headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed") is not None
    assert _delete(A_DIRECTOR, tid).headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed") is None


def test_any_of_the_creators_corporations_counts(board):
    # BOTH_USER has characters in A and B: a Director in B qualifies.
    _add_manual(board, created_by=BOTH_USER)
    tid = _row(board, "Manual Keep").id
    _edit(B_DIRECTOR, tid, "Manual Keep Renamed")
    assert _row(board, "Manual Keep Renamed") is not None


@pytest.mark.parametrize("user_id", [B_DIRECTOR, SPLIT_DIRECTOR])
def test_a_director_of_another_corporation_may_not_modify_a_manual_timer(board, user_id):
    # B_DIRECTOR shares no corporation with the creator. SPLIT_DIRECTOR has
    # a plain member in the creator's corporation, but the role is in B.
    _add_manual(board, created_by=A_USER)
    before = _manual_snapshot(board)
    for r in (_edit(user_id, before["id"], "Manual Keep Renamed"), _delete(user_id, before["id"])):
        assert r.status_code == 302
        assert r.headers["location"] == "/structure-timers?err=not_allowed"
    assert _manual_snapshot(board) == before


@pytest.mark.parametrize("user_id", [A_USER, ADMIN_USER, A_MANAGER])
def test_the_creator_admins_and_managers_still_may_modify_a_manual_timer(board, user_id):
    _add_manual(board, created_by=A_USER)
    tid = _row(board, "Manual Keep").id
    assert _edit(user_id, tid, "Manual Keep Renamed").headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed") is not None
    assert _delete(user_id, tid).headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed") is None


def _drop_characters(SessionLocal, user_id):
    async def go():
        async with SessionLocal() as db:
            for c in (await db.execute(select(Character).where(Character.user_id == user_id))).scalars():
                await db.delete(c)
            await db.commit()
    _run(go())


@pytest.mark.parametrize("creator", ["none", "no_characters", "gone"])
def test_with_no_creator_on_record_the_director_role_counts_for_nothing(board, creator):
    if creator == "none":
        _add_manual(board, created_by=None)
    elif creator == "no_characters":
        _add_manual(board, created_by=A_USER)
        _drop_characters(board, A_USER)
    else:
        _add_manual(board, created_by=5299)  # no such account
    before = _manual_snapshot(board)
    for user_id in (A_DIRECTOR, B_DIRECTOR, SPLIT_DIRECTOR):
        assert _edit(user_id, before["id"], "Manual Keep Renamed").headers["location"] \
            == "/structure-timers?err=not_allowed"
        assert _delete(user_id, before["id"]).headers["location"] == "/structure-timers?err=not_allowed"
    assert _manual_snapshot(board) == before
    # An admin still may.
    _edit(ADMIN_USER, before["id"], "Manual Keep Renamed")
    assert _row(board, "Manual Keep Renamed") is not None


def test_a_director_with_no_corporation_on_record_does_not_match_a_creator_without_one(board):
    # Both accounts' characters have no corporation recorded; None is not a
    # corporation the two share.
    _set_corp(board, 93000001, None)    # A_USER, the creator
    _set_corp(board, 93000010, None)    # A_DIRECTOR
    _add_manual(board, created_by=A_USER)
    before = _manual_snapshot(board)
    _edit(A_DIRECTOR, before["id"], "Manual Keep Renamed")
    _delete(A_DIRECTOR, before["id"])
    assert _manual_snapshot(board) == before


def test_the_page_offers_a_manual_timers_controls_only_to_a_director_in_the_creators_corporation(board):
    _add_manual(board, created_by=A_USER)
    tid = _row(board, "Manual Keep").id
    page = _html(A_DIRECTOR)
    assert _forms(page, f"/structure-timers/{tid}/edit") == 1
    assert _forms(page, f"/structure-timers/{tid}/delete") == 2
    for user_id in (B_DIRECTOR, SPLIT_DIRECTOR):
        page = _html(user_id)
        assert "Manual Keep" in _shown(page), user_id
        assert _forms(page, f"/structure-timers/{tid}/edit") == 0, user_id
        assert _forms(page, f"/structure-timers/{tid}/delete") == 0, user_id
        assert f'data-timer-id="{tid}"' not in page, user_id


def test_the_page_offers_no_director_controls_on_a_timer_with_no_creator(board):
    _add_manual(board, created_by=None)
    tid = _row(board, "Manual Keep").id
    page = _html(A_DIRECTOR)
    assert "Manual Keep" in _shown(page)
    assert _forms(page, f"/structure-timers/{tid}/edit") == 0
    assert _forms(page, f"/structure-timers/{tid}/delete") == 0
    assert _forms(_html(ADMIN_USER), f"/structure-timers/{tid}/edit") == 1


# ── deleting an ACL group ───────────────────────────────────────────────


def _seed_restricted(SessionLocal, archived=False):
    async def go():
        async with SessionLocal() as db:
            db.add(TimerACLGroup(id=30, name="Corp A only", created_by=A_USER))
            db.add(TimerACLEntry(group_id=30, entry_type="corporation", eve_id=CORP_A))
            db.add(StructureTimer(
                structure_name="Manual Keep", structure_type="astrahus", system_name="Sample-02",
                owner_name="Sample Hostile Corp", disposition="hostile", timer_phase="shield",
                timer_expires=_now() + (timedelta(hours=-3) if archived else timedelta(hours=6)),
                is_archived=archived, archived_at=_now() if archived else None,
                source="manual", acl_group_id=30, created_by=BOTH_USER))
            await db.commit()
    _run(go())


def _group(SessionLocal, gid):
    async def go():
        async with SessionLocal() as db:
            return (await db.execute(select(TimerACLGroup).where(TimerACLGroup.id == gid))).scalar_one_or_none()
    return _run(go())


@pytest.mark.parametrize("archived", [False, True])
def test_deleting_a_group_a_timer_still_uses_is_refused(board, archived):
    _seed_restricted(board, archived=archived)
    assert "Manual Keep" not in _page(B_USER)
    r = _client(A_USER).post("/structure-timers/acl/30/delete", follow_redirects=False)
    assert r.status_code == 302
    assert _group(board, 30) is not None
    assert _row(board, "Manual Keep").acl_group_id == 30
    assert "Manual Keep" not in _page(B_USER)
    assert "Manual Keep" in _page(A_USER)


def test_deleting_a_group_no_timer_uses_still_works(board):
    async def go():
        async with board() as db:
            db.add(TimerACLGroup(id=31, name="Unused", created_by=A_USER))
            db.add(TimerACLEntry(group_id=31, entry_type="corporation", eve_id=CORP_A))
            await db.commit()
    _run(go())
    r = _client(A_USER).post("/structure-timers/acl/31/delete", follow_redirects=False)
    assert r.status_code == 302
    assert _group(board, 31) is None


def test_the_delete_confirmation_no_longer_promises_to_publish_timers():
    with open("app/templates/structure_timers.html", encoding="utf-8") as fh:
        src = fh.read()
    assert "will become visible to everyone" not in src


# ── filing a timer under an ACL group ───────────────────────────────────


def _add_group(SessionLocal, gid, corp, *, created_by):
    async def go():
        async with SessionLocal() as db:
            db.add(TimerACLGroup(id=gid, name=f"Group {gid}", created_by=created_by))
            db.add(TimerACLEntry(group_id=gid, entry_type="corporation", eve_id=corp))
            await db.commit()
    _run(go())


def _create(user_id, name, acl):
    return _client(user_id).post("/structure-timers/create", follow_redirects=False, data={
        "structure_name": name, "structure_type": "astrahus", "system_name": "Sample-03",
        "owner_name": "Sample Hostile Corp", "timer_mode": "countdown", "countdown_text": "5h",
        "acl_group_id": acl})


def _edit_acl(user_id, timer_id, name, acl):
    return _client(user_id).post(f"/structure-timers/{timer_id}/edit", follow_redirects=False, data={
        "structure_name": name, "timer_mode": "countdown", "countdown_text": "1h",
        "region_name": "", "notes": "", "acl_group_id": acl})


def _snapshot(row):
    return {c: getattr(row, c) for c in ("structure_name", "region_name", "notes",
                                         "timer_expires", "acl_group_id")}


def test_creating_a_timer_under_a_group_the_user_is_not_in_is_refused(board):
    _add_group(board, 40, CORP_A, created_by=A_USER)
    r = _create(B_USER, "Planted Keep", "40")
    assert r.status_code == 302
    assert r.headers["location"] == "/structure-timers?err=acl_group"
    assert _row(board, "Planted Keep") is None


def test_creating_a_timer_under_a_group_that_does_not_exist_is_refused(board):
    r = _create(A_USER, "Planted Keep", "999")
    assert r.headers["location"] == "/structure-timers?err=acl_group"
    assert _row(board, "Planted Keep") is None


def test_creating_a_timer_under_a_group_the_user_is_in_still_works(board):
    _add_group(board, 40, CORP_A, created_by=A_USER)
    r = _create(A_USER, "Filed Keep", "40")
    assert r.headers["location"] == "/structure-timers"
    row = _row(board, "Filed Keep")
    assert row is not None and row.acl_group_id == 40
    r = _create(B_USER, "Open Keep", "")
    assert r.headers["location"] == "/structure-timers"
    assert _row(board, "Open Keep").acl_group_id is None


def test_moving_a_timer_into_a_group_the_editor_is_not_in_is_refused_and_changes_nothing(board):
    _add_group(board, 40, CORP_A, created_by=A_USER)
    _add_manual(board, created_by=B_USER)
    before = _row(board, "Manual Keep")
    r = _edit_acl(B_USER, before.id, "Manual Keep Renamed", "40")
    assert r.headers["location"] == "/structure-timers?err=acl_group"
    after = _row(board, "Manual Keep")
    assert after is not None and _snapshot(after) == _snapshot(before)


def test_a_director_may_not_move_their_corporations_timer_into_a_group_they_are_not_in(board):
    _seed_esi(board)
    _add_group(board, 41, CORP_B, created_by=B_USER)
    before = _row(board, "Alpha Keep")
    r = _edit_acl(A_DIRECTOR, before.id, "Alpha Keep Renamed", "41")
    assert r.headers["location"] == "/structure-timers?err=acl_group"
    assert _snapshot(_row(board, "Alpha Keep")) == _snapshot(before)


def test_moving_a_timer_into_a_group_the_editor_is_in_still_works(board):
    _add_group(board, 40, CORP_A, created_by=A_USER)
    _add_manual(board, created_by=B_USER)
    tid = _row(board, "Manual Keep").id
    r = _edit_acl(A_MANAGER, tid, "Manual Keep Renamed", "40")
    assert r.headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed").acl_group_id == 40


def test_leaving_a_timer_in_a_group_the_editor_cannot_see_is_not_a_filing(board):
    # An admin may change any timer. Keeping its group as it is files it
    # nowhere new, so the save goes through.
    _seed_restricted(board)
    tid = _row(board, "Manual Keep").id
    r = _edit_acl(ADMIN_USER, tid, "Manual Keep Renamed", "30")
    assert r.headers["location"] == "/structure-timers"
    assert _row(board, "Manual Keep Renamed").acl_group_id == 30


# ── refusals say so, and the controls follow the rules ──────────────────


def _html(user_id, query=""):
    r = _client(user_id).get("/structure-timers" + query, follow_redirects=False)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _banner(html):
    m = re.search(r'<div class="b-banner is-danger" role="alert">(.*?)</div>', html, re.S)
    return unescape(m.group(1)).strip() if m else None


def _forms(html, action):
    return html.count(f'action="{action}"')


def test_a_refused_edit_or_delete_says_so(board):
    _seed_esi(board)
    tid = _row(board, "Alpha Keep").id
    for r in (_edit(A_USER, tid, "Renamed Keep"), _delete(A_USER, tid)):
        assert r.status_code == 302
        assert r.headers["location"] == "/structure-timers?err=not_allowed"
    assert _banner(_html(A_USER, "?err=not_allowed")) == "You can't edit or delete that timer."


def test_a_missing_timer_gets_the_same_answer_as_a_refused_one(board):
    for r in (_edit(A_USER, 999999, "Renamed Keep"), _delete(A_USER, 999999)):
        assert r.headers["location"] == "/structure-timers?err=not_allowed"


def test_a_refused_filing_says_so(board):
    _add_group(board, 40, CORP_A, created_by=A_USER)
    assert _create(B_USER, "Planted Keep", "40").headers["location"].endswith("?err=acl_group")
    assert _banner(_html(B_USER, "?err=acl_group")).startswith("Not saved:")


def test_a_refused_group_delete_says_so(board):
    _seed_restricted(board)
    r = _client(A_USER).post("/structure-timers/acl/30/delete", follow_redirects=False)
    assert r.headers["location"] == "/structure-timers?err=acl_in_use"
    assert _banner(_html(A_USER, "?err=acl_in_use")).startswith("Not deleted:")


def test_no_banner_without_a_known_code(board):
    assert _banner(_html(A_USER)) is None
    page = _html(A_USER, "?err=%3Cb%3ESample%20text%3C%2Fb%3E")
    assert _banner(page) is None
    assert "Sample text" not in page


def test_edit_and_delete_show_only_on_timers_the_user_may_change(board):
    _seed_esi(board)
    alpha, bravo, archive = (_row(board, n).id for n in ("Alpha Keep", "Bravo Keep", "Bravo Archive"))
    page = _html(SPLIT_DIRECTOR)
    assert _shown(page) == {"Alpha Keep", "Bravo Keep", "Bravo Archive"}
    # Alpha Keep: seen through the corp A member, but the Director is in B.
    assert _forms(page, f"/structure-timers/{alpha}/edit") == 0
    assert _forms(page, f"/structure-timers/{alpha}/delete") == 0
    assert f'data-timer-id="{alpha}"' not in page
    # Bravo Keep: desktop and phone delete, one edit form.
    assert _forms(page, f"/structure-timers/{bravo}/edit") == 1
    assert _forms(page, f"/structure-timers/{bravo}/delete") == 2
    assert _forms(page, f"/structure-timers/{archive}/delete") == 1


def test_an_archived_timer_offers_delete_only_to_those_who_may(board):
    _add_manual(board, created_by=A_USER, archived=True)
    tid = _row(board, "Manual Keep").id
    assert "Manual Keep" in _page(B_USER)
    assert _forms(_html(A_USER), f"/structure-timers/{tid}/delete") == 1
    assert _forms(_html(B_USER), f"/structure-timers/{tid}/delete") == 0


def test_a_group_in_use_offers_no_delete_even_when_its_timers_are_hidden(board):
    _seed_restricted(board)                          # group 30 (corp A), used by Manual Keep
    _add_group(board, 31, CORP_B, created_by=B_USER)  # used by nothing
    page = _html(B_USER)
    assert "Manual Keep" not in _shown(page)
    assert _forms(page, "/structure-timers/acl/30/delete") == 0
    assert "Timers still use this group" in page
    assert _forms(page, "/structure-timers/acl/31/delete") == 1


# ── the startup migration ───────────────────────────────────────────────


_OLD_TIMERS = (
    "CREATE TABLE structure_timers ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, structure_name VARCHAR NOT NULL, "
    "structure_type VARCHAR(16) NOT NULL, system_name VARCHAR NOT NULL, region_name VARCHAR, "
    "owner_name VARCHAR NOT NULL, disposition VARCHAR(16) NOT NULL, timer_phase VARCHAR(8) NOT NULL, "
    "timer_expires DATETIME NOT NULL, priority VARCHAR(8) NOT NULL, notes TEXT, "
    "source VARCHAR(8) NOT NULL, esi_structure_id INTEGER, is_archived BOOLEAN NOT NULL, "
    "archived_at DATETIME, acl_group_id INTEGER, created_by INTEGER, created_at DATETIME)"
)


def test_migration_adds_the_column_and_backfills_from_cached_structure_lists():
    from app.db.cache import _cache_key
    from app.db.models import ensure_structure_timer_columns

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    def key(corp, page=None, principal="CHARACTER:EVE:93000001"):
        return _cache_key(f"/corporations/{corp}/structures/", {"page": page} if page else None, principal)

    async def scenario():
        async with SessionLocal() as db:
            await db.execute(text(_OLD_TIMERS))
            await db.execute(text(
                "CREATE TABLE esi_cache (key VARCHAR PRIMARY KEY, data TEXT NOT NULL, "
                "expires_at DATETIME NOT NULL)"))
            # (id, source, esi_structure_id, is_archived)
            for tid, source, sid, archived in [
                (1, "esi", 1, 0),      # listed for A only
                (2, "esi", 2, 0),      # listed for B only (second page)
                (3, "esi", 3, 0),      # listed for both A and B
                (4, "esi", 4, 0),      # not cached anywhere
                (5, "esi", 5, 0),      # under A's path, but ESI says B owns it
                (6, "manual", None, 0),
                (7, "esi", 1, 1),      # archived, another phase of structure 1
            ]:
                await db.execute(text(
                    "INSERT INTO structure_timers (id, structure_name, structure_type, system_name, "
                    "owner_name, disposition, timer_phase, timer_expires, priority, source, "
                    "esi_structure_id, is_archived) VALUES (:id, :n, 'other', 'Sample-01', 'Our Corp', "
                    "'friendly', 'armor', '2030-01-01 00:00:00', 'critical', :src, :sid, :arch)"),
                    {"id": tid, "n": f"Timer {tid}", "src": source, "sid": sid, "arch": archived})
            cached = [
                (key(CORP_A), [{"structure_id": 1, "corporation_id": CORP_A},
                               {"structure_id": 3, "corporation_id": CORP_A},
                               {"structure_id": 5, "corporation_id": CORP_B}]),
                (key(CORP_B, page=2, principal="CHARACTER:EVE:93000002"),
                 [{"structure_id": 2, "corporation_id": CORP_B},
                  {"structure_id": 3, "corporation_id": CORP_B}]),
                # Not a structures list: ignored.
                (_cache_key(f"/corporations/{CORP_C}/", None, None), {"structure_id": 4}),
            ]
            for k, data in cached:
                await db.execute(text("INSERT INTO esi_cache (key, data, expires_at) "
                                      "VALUES (:k, :d, '2030-01-01 00:00:00')"),
                                 {"k": k, "d": json.dumps(data)})
            await db.commit()

        async with SessionLocal() as db:
            await ensure_structure_timer_columns(db)
        # A second start: already migrated, nothing changes and nothing raises.
        async with SessionLocal() as db:
            await ensure_structure_timer_columns(db)

        async with SessionLocal() as db:
            rows = (await db.execute(select(StructureTimer).order_by(StructureTimer.id))).scalars().all()
            return {r.id: r.esi_corporation_id for r in rows}

    got = _run(scenario())
    _run(engine.dispose())
    assert got == {1: CORP_A, 2: CORP_B, 3: None, 4: None, 5: None, 6: None, 7: CORP_A}


def test_a_failed_backfill_leaves_the_rows_hidden_and_the_start_going(monkeypatch):
    import app.db.models as models

    async def broken(db):
        await db.execute(text("UPDATE structure_timers SET esi_corporation_id = 1"))
        raise RuntimeError("sample failure")
    monkeypatch.setattr(models, "_backfill_esi_timer_corporations", broken)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with SessionLocal() as db:
            await db.execute(text(_OLD_TIMERS))
            await db.execute(text(
                "INSERT INTO structure_timers (structure_name, structure_type, system_name, owner_name, "
                "disposition, timer_phase, timer_expires, priority, source, esi_structure_id, is_archived) "
                "VALUES ('Timer 1', 'other', 'Sample-01', 'Our Corp', 'friendly', 'armor', "
                "'2030-01-01 00:00:00', 'critical', 'esi', 1, 0)"))
            await db.commit()
        async with SessionLocal() as db:
            await models.ensure_structure_timer_columns(db)
        async with SessionLocal() as db:
            return (await db.execute(text("SELECT esi_corporation_id FROM structure_timers"))).fetchall()

    got = _run(scenario())
    _run(engine.dispose())
    assert got == [(None,)]


def test_migration_is_a_no_op_on_a_fresh_schema(board):
    from app.db.models import ensure_structure_timer_columns
    _seed_esi(board)

    async def go():
        async with board() as db:
            await ensure_structure_timer_columns(db)
    _run(go())
    assert _row(board, "Alpha Keep").esi_corporation_id == CORP_A
    assert _row(board, "Legacy Keep").esi_corporation_id is None
