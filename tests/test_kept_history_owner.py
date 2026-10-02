"""ISS-070: kept history goes back only to the EVE owner it came from.

Self-removal with the history box unticked keeps a character's wallet,
industry, mining and net-worth history, keyed by character_id alone. When the
character is linked again (a NEW characters row: an identity login of a
character Vigilant doesn't know, a signup, or an add), that history is
deleted, in batches and before the link, unless the re-add provably comes from
the same owner:

1. the kept-history record's owner_hash equals the new login's (both known);
2. either side's owner is unknown and the same Vigilant account re-adds it.

Anything else deletes it: another owner, an unknown owner on another account,
history from before owner tracking (no record at all), and history a pending
purge was already due to delete.

Reuses the SSO-stubbed environment from test_permissions_flow.py.
"""
import ast
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text

from app.auth import purge
from app.auth import scopes as cat
from app.db.models import AdminAuditLog, Character, User
from tests.test_character_owner_change import _jwt, _set_owner, _sso
from tests.test_character_removal import _counts, _only, _seed, no_pause  # noqa: F401 — fixture
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    ALT_ID, CSRF, MAIN_ID, OTHER_ID, STRANGER_ID, USER_ID, _pending, _scalar, env,
)

HISTORY = purge.HISTORY_TABLES
NEW_ID = 90000077          # a character Vigilant has never seen


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded(env, no_pause):
    """ALT (removed and re-added below) with 10 wallet snapshots; MAIN and
    STRANGER as controls whose rows must never move."""
    async def go(db):
        await _seed(db, ALT_ID, USER_ID, snapshots=10)
        await _seed(db, MAIN_ID, USER_ID)
        await _seed(db, STRANGER_ID, OTHER_ID)
    env.q(go)
    return env


def _history(env, cid=ALT_ID) -> dict:
    return _only(env.q(lambda db: _counts(db, cid)), HISTORY)


def _full(cid=ALT_ID, snapshots=10) -> dict:
    return {t: (snapshots if t == "wallet_snapshots" else 1) for t in HISTORY}


def _none() -> dict:
    return {t: 0 for t in HISTORY}


def _controls_untouched(env):
    for cid in (MAIN_ID, STRANGER_ID):
        assert _history(env, cid) == _full(cid, snapshots=1), cid


def _self_remove(env, *, tick=False):
    data = {"csrf_token": CSRF}
    if tick:
        data["delete_history"] = "1"
    r = env.user().post(f"/auth/remove/{ALT_ID}", data=data)
    assert r.status_code == 303
    assert env.char(ALT_ID) is None


def _readd(env, monkeypatch, intent, *, owner=None, verify_owner=None, as_user=None,
           cid=ALT_ID):
    """Link the character again through the real callback. `as_user` is the
    logged-in account for an add; login and signup make a new account."""
    _sso(monkeypatch, cid, "Alt Pilot", owner=owner, verify_owner=verify_owner,
         scopes=[cat.WALLET])
    extra = {"user_id": as_user} if as_user is not None else {}
    keys = [] if intent == "login" else ["wallet"]
    return env.client(_pending(intent, keys, **extra)).get("/auth/callback?code=x&state=S")


def _record(env, cid=ALT_ID):
    from app.db.models import KeptCharacterHistory

    async def go(db):
        return await _scalar(db, select(KeptCharacterHistory).where(
            KeptCharacterHistory.character_id == cid))
    return env.q(go)


def _kept_audit(env, cid=ALT_ID) -> list[tuple[str, int | None, str]]:
    async def go(db):
        return [(r.event_type, r.user_id, r.detail) for r in (await db.execute(
            select(AdminAuditLog).where(AdminAuditLog.character_id == cid,
                                        AdminAuditLog.event_type.like("kept_history%"))
            .order_by(AdminAuditLog.id))).scalars().all()]
    return env.q(go)


def _users(env) -> int:
    return env.q(lambda db: _scalar(db, text("SELECT count(*) FROM users")))


def _sql_log(env) -> list[str]:
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).upper())
    event.listen(env.engine.sync_engine, "before_cursor_execute", record)
    return seen


def _linked_to(env, cid=ALT_ID):
    c = env.char(cid)
    return c.user_id if c is not None else None


# ── Back to the same owner: kept ─────────────────────────────────────────────

@pytest.mark.parametrize("intent,as_user", [
    ("add", USER_ID),       # the same account adds it back
    ("add", OTHER_ID),      # the same EVE owner, under another Vigilant account
    ("signup", None),
    ("login", None),
])
def test_the_same_eve_owner_gets_the_history_back(seeded, monkeypatch, intent, as_user):
    env = seeded
    _set_owner(env, ALT_ID, "owner-A")
    _self_remove(env)
    assert _history(env) == _full()

    r = _readd(env, monkeypatch, intent, owner="owner-A", as_user=as_user)

    assert r.status_code == 303
    assert _linked_to(env) is not None
    assert _history(env) == _full()
    assert _record(env) is None                       # gone with the link
    [(event_type, by, detail)] = _kept_audit(env)
    assert event_type == "kept_history_restored"
    assert by == _linked_to(env)
    assert "owner-A" not in detail
    _controls_untouched(env)


# ── Another owner: deleted, in batches, before the link ──────────────────────

@pytest.mark.parametrize("intent,as_user", [
    ("add", OTHER_ID),      # the new owner adds it to their account
    ("add", USER_ID),       # the same account, but EVE says another owner
    ("signup", None),
    ("login", None),
])
def test_another_eve_owner_never_sees_it(seeded, monkeypatch, intent, as_user):
    env = seeded
    monkeypatch.setattr(purge, "PURGE_BATCH_ROWS", 4)
    _set_owner(env, ALT_ID, "owner-A")
    _self_remove(env)
    users_before = _users(env)
    sql = _sql_log(env)

    r = _readd(env, monkeypatch, intent, owner="owner-B", as_user=as_user)

    assert r.status_code == 303
    assert _linked_to(env) is not None
    assert _history(env) == _none()
    _controls_untouched(env)
    # 10 snapshots, 4 at a time, each batch its own statement...
    deletes = [i for i, s in enumerate(sql) if s.startswith("DELETE FROM WALLET_SNAPSHOTS")]
    assert len(deletes) == 3, [sql[i] for i in deletes]
    # ...all of them before the account or the character row is written.
    links = [i for i, s in enumerate(sql)
             if s.startswith("INSERT INTO CHARACTERS") or s.startswith("INSERT INTO USERS")]
    assert links and max(deletes) < min(links)
    assert _users(env) == users_before + (0 if intent == "add" else 1)
    assert _record(env) is None
    [(event_type, by, detail)] = _kept_audit(env)
    assert event_type == "kept_history_deleted"
    assert by == _linked_to(env)
    assert "wallet_snapshots=10" in detail
    assert "owner-A" not in detail and "owner-B" not in detail


# ── Owner unknown on one side: the same Vigilant account decides ─────────────

@pytest.mark.parametrize("removed_owner,login_owner,intent,as_user,kept", [
    (None, "owner-A", "add", USER_ID, True),       # record without owner, same account
    (None, "owner-A", "add", OTHER_ID, False),     # record without owner, another account
    (None, "owner-A", "signup", None, False),      # ...or a brand-new one
    ("owner-A", None, "add", USER_ID, True),       # the login reports no owner, same account
    ("owner-A", None, "add", OTHER_ID, False),
    (None, None, "add", USER_ID, True),
])
def test_an_unknown_owner_falls_back_to_the_same_account(
        seeded, monkeypatch, removed_owner, login_owner, intent, as_user, kept):
    env = seeded
    _set_owner(env, ALT_ID, removed_owner)    # the stored token has no owner claim
    _self_remove(env)

    r = _readd(env, monkeypatch, intent, owner=login_owner, as_user=as_user)

    assert r.status_code == 303
    assert _linked_to(env) is not None
    assert _history(env) == (_full() if kept else _none())
    assert _record(env) is None
    assert [e for e, _, _ in _kept_audit(env)] == (
        ["kept_history_restored"] if kept else ["kept_history_deleted"])
    _controls_untouched(env)


def test_the_owner_comes_from_the_stored_token_when_the_row_has_none(seeded, monkeypatch):
    env = seeded

    async def token_with_owner(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.owner_hash = None
        c.access_token = _jwt(owner="owner-T")
        await db.commit()
    env.q(token_with_owner)
    _self_remove(env)

    rec = _record(env)
    assert (rec.owner_hash, rec.user_id) == ("owner-T", USER_ID)
    # So the same EVE owner gets it back even under another account...
    r = _readd(env, monkeypatch, "add", owner="owner-T", as_user=OTHER_ID)
    assert r.status_code == 303
    assert _history(env) == _full()


def test_an_unreadable_stored_token_records_no_owner(seeded):
    env = seeded

    async def opaque(db):
        c = await _scalar(db, select(Character).where(Character.character_id == ALT_ID))
        c.access_token = "not-a-jwt"
        await db.commit()
    env.q(opaque)
    _self_remove(env)
    rec = _record(env)
    assert (rec.owner_hash, rec.user_id) == (None, USER_ID)


# ── No proof at all: deleted ─────────────────────────────────────────────────

def test_history_from_before_owner_tracking_is_deleted_on_any_readd(seeded, monkeypatch):
    """Legacy: a self-removal made before this fix left history and no record."""
    env = seeded

    async def removed_long_ago(db):
        await db.execute(text("DELETE FROM characters WHERE character_id = :c"), {"c": ALT_ID})
        await db.commit()
    env.q(removed_long_ago)
    assert _history(env) == _full()

    r = _readd(env, monkeypatch, "add", owner="owner-A", as_user=USER_ID)

    assert r.status_code == 303
    assert _linked_to(env) == USER_ID
    assert _history(env) == _none()
    [(event_type, _, detail)] = _kept_audit(env)
    assert event_type == "kept_history_deleted"
    assert "kept before Vigilant recorded" in detail   # says why: no record of its owner
    _controls_untouched(env)


def test_a_readd_inside_the_purge_delay_does_not_keep_history_due_for_deletion(
        seeded, monkeypatch):
    env = seeded
    _set_owner(env, ALT_ID, "owner-A")
    _self_remove(env, tick=True)
    assert _history(env) == _full()              # the background purge hasn't run yet

    r = _readd(env, monkeypatch, "add", owner="owner-A", as_user=USER_ID)

    assert r.status_code == 303
    assert _linked_to(env) == USER_ID
    assert _history(env) == _none()
    [(event_type, _, detail)] = _kept_audit(env)
    assert event_type == "kept_history_deleted"
    assert "pending" in detail


# ── A brand-new character costs nothing ──────────────────────────────────────

def test_a_brand_new_character_is_a_noop(seeded, monkeypatch):
    env = seeded
    sql = _sql_log(env)
    r = _readd(env, monkeypatch, "add", owner="owner-N", as_user=USER_ID, cid=NEW_ID)
    assert r.status_code == 303
    assert _linked_to(env, NEW_ID) == USER_ID
    assert not [s for s in sql if s.startswith("DELETE")]
    assert _kept_audit(env, NEW_ID) == []
    _controls_untouched(env)


def test_the_decision_for_a_brand_new_character_is_indexed_reads_only(seeded):
    env = seeded
    seen: list[tuple[str, tuple]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, parameters))

    async def go(db):
        event.listen(env.engine.sync_engine, "before_cursor_execute", record)
        try:
            outcome = await purge.settle_history_for_relink(
                db, NEW_ID, owner_hash="owner-N", user_id=USER_ID)
        finally:
            event.remove(env.engine.sync_engine, "before_cursor_execute", record)
        plans = []
        async with env.engine.connect() as conn:
            for stmt, params in seen:
                rows = (await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + stmt, params)).fetchall()
                plans.append((stmt, " | ".join(r[3] for r in rows)))
        return outcome, plans

    outcome, plans = env.q(go)
    assert outcome.noop
    assert plans and all(stmt.lstrip().upper().startswith("SELECT") for stmt, _ in plans)
    for stmt, plan in plans:
        assert "SCAN" not in plan, (stmt, plan)


# ── Crash safety ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("intent,as_user", [("signup", None), ("add", OTHER_ID)])
def test_a_crash_between_batches_leaves_it_unlinked_and_a_retry_finishes(
        seeded, monkeypatch, intent, as_user):
    env = seeded
    monkeypatch.setattr(purge, "PURGE_BATCH_ROWS", 4)
    _set_owner(env, ALT_ID, "owner-A")
    _self_remove(env)
    users_before = _users(env)

    async def crash():
        raise RuntimeError("process killed")
    monkeypatch.setattr(purge, "_between_batches", crash)
    r = _readd(env, monkeypatch, intent, owner="owner-B", as_user=as_user)

    assert r.status_code == 500
    assert env.char(ALT_ID) is None              # not linked...
    assert _users(env) == users_before            # ...and no half-made account
    assert _history(env)["wallet_snapshots"] == 6  # one batch of 4 went
    assert _record(env) is not None               # so a retry decides the same way

    async def instant():
        return None
    monkeypatch.setattr(purge, "_between_batches", instant)
    r = _readd(env, monkeypatch, intent, owner="owner-B", as_user=as_user)

    assert r.status_code == 303
    assert _linked_to(env) is not None
    assert _history(env) == _none()
    assert _record(env) is None
    _controls_untouched(env)


# ── Removals that delete history leave no record ─────────────────────────────

def _stale_record(env, cid):
    from app.db.models import KeptCharacterHistory

    async def go(db):
        db.add(KeptCharacterHistory(character_id=cid, owner_hash=None, user_id=USER_ID,
                                    removed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        await db.commit()
    env.q(go)


def _make_admin(env):
    async def go(db):
        u = (await db.execute(select(User).where(User.id == USER_ID))).scalar_one()
        u.role, u.is_admin = "admin", True
        await db.commit()
    env.q(go)


def test_self_removal_keeping_history_records_its_owner(seeded):
    env = seeded
    _set_owner(env, ALT_ID, "owner-A")
    _self_remove(env)
    rec = _record(env)
    assert (rec.owner_hash, rec.user_id) == ("owner-A", USER_ID)
    assert rec.removed_at is not None


def test_self_removal_with_the_box_ticked_leaves_no_record(seeded):
    env = seeded
    _stale_record(env, ALT_ID)
    _self_remove(env, tick=True)
    assert _record(env) is None


@pytest.mark.parametrize("route", [f"/admin/action/remove-character/{STRANGER_ID}",
                                   f"/admin/action/remove-user/{OTHER_ID}"])
def test_admin_removals_leave_no_record(seeded, route):
    env = seeded
    _make_admin(env)
    _stale_record(env, STRANGER_ID)
    assert env.user().post(route).status_code == 200
    assert env.char(STRANGER_ID) is None
    assert _record(env, STRANGER_ID) is None


def test_admin_remove_user_keeps_their_records_owner_bound(seeded):
    """Records of characters the account removed earlier survive the account
    with user_id nulled (NULLABLE_FKS), so they bind by owner alone."""
    env = seeded
    _make_admin(env)

    async def stranger_alt_removed_earlier(db):
        from app.db.models import KeptCharacterHistory
        db.add(KeptCharacterHistory(character_id=NEW_ID, owner_hash="owner-S", user_id=OTHER_ID,
                                    removed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        await db.commit()
    env.q(stranger_alt_removed_earlier)
    assert env.user().post(f"/admin/action/remove-user/{OTHER_ID}").status_code == 200
    rec = _record(env, NEW_ID)
    assert (rec.owner_hash, rec.user_id) == ("owner-S", None)


def test_a_transfer_leaves_no_record(seeded, monkeypatch):
    env = seeded
    _set_owner(env, ALT_ID, "owner-A")
    _stale_record(env, ALT_ID)
    _sso(monkeypatch, ALT_ID, "Alt Pilot", owner="owner-B")
    r = env.client(_pending("login")).get("/auth/callback?code=x&state=S")
    assert r.status_code == 303
    assert _linked_to(env) not in (None, USER_ID)
    assert _record(env) is None
    # Dropped by the transfer's removal, not by the re-link: nothing to restore.
    assert _kept_audit(env) == []


# ── The Account page and the code paths ──────────────────────────────────────

def test_account_page_says_kept_history_stays_with_the_eve_account(env):
    r = env.user().get("/account")
    assert r.status_code == 200
    text_ = " ".join(r.text.split())
    assert "brings the history back" in text_
    assert "deleted before the new owner sees it" in text_
    assert "by a future owner" not in text_


def test_every_place_that_links_a_character_settles_its_history_first():
    """A new characters row anywhere in app/ must go through the decision."""
    found = 0
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
                     for n in ast.walk(fn) if isinstance(n, ast.Call)]
            built = calls.count("Character")
            if not built:
                continue
            found += built
            assert calls.count("settle_history_for_relink") >= built, f"{path}:{fn.name}"
            assert calls.count("finish_relink") >= built, f"{path}:{fn.name}"
    assert found >= 2


def test_the_decision_rules():
    verdict = purge.kept_history_verdict
    rec = lambda owner, uid: SimpleNamespace(owner_hash=owner, user_id=uid)  # noqa: E731
    assert verdict(rec("o1", 1), pending_delete=False, owner_hash="o1", user_id=2) == (True, "same_owner")
    assert verdict(rec("o1", 1), pending_delete=False, owner_hash="o2", user_id=1) == (False, "other_owner")
    assert verdict(rec(None, 1), pending_delete=False, owner_hash="o2", user_id=1) == (True, "same_account")
    assert verdict(rec("o1", 1), pending_delete=False, owner_hash=None, user_id=1) == (True, "same_account")
    assert verdict(rec(None, 1), pending_delete=False, owner_hash="o2", user_id=2) == (False, "unknown_owner")
    assert verdict(rec(None, None), pending_delete=False, owner_hash=None, user_id=None) == (False, "unknown_owner")
    assert verdict(None, pending_delete=False, owner_hash="o1", user_id=1) == (False, "legacy")
    assert verdict(rec("o1", 1), pending_delete=True, owner_hash="o1", user_id=1) == (False, "pending_purge")
