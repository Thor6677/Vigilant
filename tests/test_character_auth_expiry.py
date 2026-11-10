"""A character whose EVE authorization is no longer shown to work leaves its
account (app/auth/expiry.py).

Two clocks per character: last_authorized_at (a sign-in with it, or a token
refresh EVE accepted) and last_sso_at (a full sign-in). The daily sweep
removes, with history, a character that EVE reports under another owner, one
past the 365-day sign-in cap, and one with no proof for 90 days whose stored
token EVE then refuses; an unknown refresh result removes nobody. The Account
page and the dashboard's needs-attention strip warn ahead of it.

Reuses test_permissions_flow's SSO-stubbed environment; SSO's refresh endpoint
is stubbed at the HTTP client _do_refresh uses, as in
test_signup_keeps_permissions.py, so the real refresh code runs.
"""
import asyncio
import base64
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import expiry, purge
from app.auth import scopes as cat
from app.db.models import (
    AdminAuditLog, Character, CharacterPurge, HeldCharacterRemoval, User, WalletSnapshot,
)
from tests.test_character_owner_change import _sso
from tests.test_character_removal import (  # noqa: F401 — no_pause is a fixture
    EVERYTHING, HISTORY, LIVE, USER_ROWS, _assert_untouched, _counts, _job, _only,
    _run_due, _seed, _zero, no_pause,
)
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    ALT_ID, MAIN_ID, OTHER_ID, STRANGER_ID, USER_ID, _char, _count, _pending, _scalar, env,
)
from tests.test_signup_keeps_permissions import (  # noqa: F401 — sso_refresh is a fixture
    _Resp, rejected, sso_refresh, throttled, unreachable,
)

CALLBACK = "/auth/callback?code=x&state=S"
ALT_SCOPES = [cat.WALLET, cat.ASSETS]
NEW_ID = 90000077


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _ago(days: float) -> datetime:
    return _now() - timedelta(days=days)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _fresh(dt: datetime | None) -> bool:
    return dt is not None and abs(_naive(_now()) - _naive(dt)) < timedelta(minutes=1)


def _same(a: datetime, b: datetime) -> bool:
    return abs(_naive(a) - _naive(b)) < timedelta(seconds=1)


def _token(cid, owner=None, scopes=(), azp="test") -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    claims = {"azp": azp, "sub": f"CHARACTER:EVE:{cid}", "scp": list(scopes)}
    if owner:
        claims["owner"] = owner
    return f"{b64({'alg': 'none'})}.{b64(claims)}.x"


def _refreshes(cid, owner=None, scopes=ALT_SCOPES):
    """SSO accepts the stored refresh token; the new token keeps the scopes
    (so no narrowing) and names `owner`."""
    return lambda data: _Resp(200, {"access_token": _token(cid, owner, scopes),
                                    "refresh_token": "rotated-refresh", "expires_in": 1200})


def _set(env, cid, **values):
    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == cid))
        for k, v in values.items():
            setattr(c, k, v)
        await db.commit()
    env.q(go)


def _sweep(env):
    async def go(db):
        return await expiry.run_expiry_sweep(
            session_factory=async_sessionmaker(db.bind, expire_on_commit=False))
    return env.q(go)


def _events(env, event_type):
    async def go(db):
        rows = (await db.execute(select(AdminAuditLog).where(
            AdminAuditLog.event_type == event_type))).scalars().all()
        return [(r.user_id, r.character_id, r.detail) for r in rows]
    return env.q(go)


def _user(env, uid):
    return env.q(lambda db: _scalar(db, select(User).where(User.id == uid)))


def _chars_of(env, uid):
    async def go(db):
        rows = (await db.execute(select(Character).where(Character.user_id == uid)
                                 .order_by(Character.character_id))).scalars().all()
        return [(c.character_id, bool(c.is_main)) for c in rows]
    return env.q(go)


@pytest.fixture
def seeded(env, no_pause):
    """Every classified table has a row for ALT and MAIN (USER_ID) and for
    STRANGER (OTHER_ID), as linked characters would have accumulated."""
    async def go(db):
        await _seed(db, ALT_ID, USER_ID)
        await _seed(db, MAIN_ID, USER_ID)
        await _seed(db, STRANGER_ID, OTHER_ID)
    env.q(go)
    return env


# ── Data ─────────────────────────────────────────────────────────────────────

def test_new_rows_get_both_stamps_at_creation(env):
    for cid in (MAIN_ID, ALT_ID, STRANGER_ID):
        c = env.char(cid)
        assert _fresh(c.last_authorized_at) and _fresh(c.last_sso_at), cid


def test_backfill_starts_both_clocks_on_legacy_rows(env):
    kept = _ago(10)
    _set(env, MAIN_ID, last_authorized_at=kept, last_sso_at=kept)

    async def legacy(db):
        await db.execute(text("UPDATE characters SET last_authorized_at = NULL, last_sso_at = NULL "
                              "WHERE character_id != :m"), {"m": MAIN_ID})
        await db.commit()
    env.q(legacy)
    assert env.char(ALT_ID).last_authorized_at is None

    assert env.q(expiry.backfill_authorization_stamps) == 2
    for cid in (ALT_ID, STRANGER_ID):
        c = env.char(cid)
        assert _fresh(c.last_authorized_at) and _fresh(c.last_sso_at), cid
    main = env.char(MAIN_ID)
    assert _same(main.last_authorized_at, kept) and _same(main.last_sso_at, kept)
    assert env.q(expiry.backfill_authorization_stamps) == 0      # a no-op once done


def test_startup_adds_the_columns_and_backfills_after_the_rebuild():
    import app.main as main
    src = inspect.getsource(main.startup)
    assert "ALTER TABLE characters ADD COLUMN last_authorized_at DATETIME" in src
    assert "ALTER TABLE characters ADD COLUMN last_sso_at DATETIME" in src
    assert src.index("ensure_characters_autoincrement(db)") < src.index(
        "backfill_authorization_stamps(db)")


def test_settings_defaults():
    from app.config import get_settings
    s = get_settings()
    assert (s.character_auth_expiry_days, s.character_sso_cap_days,
            s.character_sso_warning_days, s.character_expiry_max_share) == (90, 365, 30, 0.25)


# ── Stamping at sign-in ──────────────────────────────────────────────────────

def _age_all(env, days=200):
    for cid in (MAIN_ID, ALT_ID, STRANGER_ID):
        _set(env, cid, last_authorized_at=_ago(days), last_sso_at=_ago(days))


def test_login_stamps_both_without_touching_the_token(env):
    _age_all(env)
    env.sso_returns(ALT_ID, "Alt Pilot", [])
    r = env.client(_pending("login")).get(CALLBACK)
    assert r.status_code == 303
    alt = env.char(ALT_ID)
    assert _fresh(alt.last_authorized_at) and _fresh(alt.last_sso_at)
    assert alt.refresh_token == "old-refresh"
    assert not _fresh(env.char(MAIN_ID).last_sso_at)     # only the one that signed in


def test_login_with_a_new_character_stamps_both(env):
    env.sso_returns(NEW_ID, "Brand New", [])
    env.client(_pending("login")).get(CALLBACK)
    new = env.char(NEW_ID)
    assert _fresh(new.last_authorized_at) and _fresh(new.last_sso_at)


def test_signup_stamps_both(env):
    env.sso_returns(NEW_ID, "Brand New", [cat.WALLET])
    r = env.client(_pending("signup", ["wallet"])).get(CALLBACK)
    assert r.status_code == 303
    new = env.char(NEW_ID)
    assert _fresh(new.last_authorized_at) and _fresh(new.last_sso_at)


def test_add_stamps_both(env):
    env.sso_returns(NEW_ID, "Brand New", [cat.WALLET])
    r = env.client(_pending("add", ["wallet"], user_id=USER_ID)).get(CALLBACK)
    assert r.status_code == 303
    new = env.char(NEW_ID)
    assert new.user_id == USER_ID
    assert _fresh(new.last_authorized_at) and _fresh(new.last_sso_at)


def test_update_stamps_both(env):
    _age_all(env)
    env.sso_returns(ALT_ID, "Alt Pilot", ALT_SCOPES)
    r = env.client(_pending("update", ["wallet", "assets"], ALT_ID, user_id=USER_ID)).get(CALLBACK)
    assert r.status_code == 303
    alt = env.char(ALT_ID)
    assert _fresh(alt.last_authorized_at) and _fresh(alt.last_sso_at)


def test_a_signup_kept_as_a_login_stamps_both(env, sso_refresh):
    """ISS-068: a narrower signup on a registered pilot whose stored token still
    works keeps that token; it is still a sign-in."""
    _age_all(env)
    sso_refresh["respond"] = _refreshes(MAIN_ID, scopes=cat.ALL_SCOPES)
    env.sso_returns(MAIN_ID, "Main Pilot", [])
    r = env.client(_pending("signup", [])).get(CALLBACK)
    assert r.status_code == 303
    main = env.char(MAIN_ID)
    assert main.refresh_token == "rotated-refresh"       # the stored token, kept and refreshed
    assert _fresh(main.last_authorized_at) and _fresh(main.last_sso_at)


def test_an_update_for_the_wrong_character_stamps_nothing(env):
    """Refused before anything is written: nothing about the row changes."""
    _age_all(env)
    env.sso_returns(MAIN_ID, "Main Pilot", ALT_SCOPES)
    env.client(_pending("update", ["wallet"], ALT_ID, user_id=USER_ID)).get(CALLBACK)
    assert not _fresh(env.char(MAIN_ID).last_sso_at)
    assert not _fresh(env.char(ALT_ID).last_sso_at)


# ── Stamping at refresh ──────────────────────────────────────────────────────

def _refresh(env, cid):
    from app.esi import client as client_mod

    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == cid))
        await client_mod._do_refresh(c, db)
    env.q(go)


def test_refresh_stamps_last_authorized_only_and_fills_a_missing_owner(env, sso_refresh):
    old = _ago(40)
    _set(env, ALT_ID, last_authorized_at=old, last_sso_at=old, owner_hash=None)
    sso_refresh["respond"] = _refreshes(ALT_ID, owner="owner-A")
    _refresh(env, ALT_ID)
    alt = env.char(ALT_ID)
    assert _fresh(alt.last_authorized_at)
    assert _same(alt.last_sso_at, old)
    assert alt.owner_hash == "owner-A"


def _token_state(env, cid):
    c = env.char(cid)
    return (c.access_token, c.refresh_token, _naive(c.token_expiry), c.scopes,
            _naive(c.last_authorized_at), c.last_sso_at)


def _refresh_raises(env, cid):
    """_do_refresh on the stored row; returns the in-memory row it left and
    the exception it raised (the session is never committed by the test)."""
    from app.esi import client as client_mod

    async def go(db):
        c = await _scalar(db, select(Character).where(Character.character_id == cid))
        try:
            await client_mod._do_refresh(c, db)
        except Exception as e:
            return c.owner_hash, e
        return c.owner_hash, None
    return env.q(go)


def test_refresh_naming_another_owner_is_never_stored(env, sso_refresh):
    """The new owner's token must not reach the old account: nothing about
    the row changes, and the authorization counts as dead."""
    from app.esi.client import TokenRevoked
    _set(env, ALT_ID, last_authorized_at=_ago(40), owner_hash="owner-A",
         access_token=_token(ALT_ID, "owner-A", ALT_SCOPES))
    before = _token_state(env, ALT_ID)
    # Narrower scopes too, so a narrowing clean-up would show if it ran.
    sso_refresh["respond"] = _refreshes(ALT_ID, owner="owner-B", scopes=[cat.WALLET])
    _owner, err = _refresh_raises(env, ALT_ID)
    assert isinstance(err, TokenRevoked)
    assert _token_state(env, ALT_ID) == before            # tokens, expiry, scopes, stamps
    alt = env.char(ALT_ID)
    assert alt.owner_hash == "owner-A" and alt.user_id == USER_ID


def test_refresh_of_a_legacy_row_keeps_the_old_owner_as_evidence(env, sso_refresh):
    """ISS-162: owner_hash NULL but the stored token names owner-A. The
    stored token stays, so the evidence does too, whether or not the caller
    commits the owner_hash that _do_refresh fills in."""
    from app.esi.client import TokenRevoked
    _set(env, ALT_ID, last_authorized_at=_ago(40), owner_hash=None,
         access_token=_token(ALT_ID, "owner-A", ALT_SCOPES))
    before = _token_state(env, ALT_ID)
    sso_refresh["respond"] = _refreshes(ALT_ID, owner="owner-B")
    filled, err = _refresh_raises(env, ALT_ID)
    assert isinstance(err, TokenRevoked)
    assert filled == "owner-A"                            # recorded on the row it was given
    assert _token_state(env, ALT_ID) == before
    alt = env.char(ALT_ID)
    assert alt.owner_hash is None                         # not committed here...
    assert purge.owner_from_token(alt.access_token) == "owner-A"   # ...and not needed


def test_the_probe_reports_another_owners_refresh_as_revoked(env, sso_refresh):
    from app.auth.routes import PROBE_REVOKED, _probe_stored_token
    _set(env, ALT_ID, owner_hash="owner-A", access_token=_token(ALT_ID, "owner-A", ALT_SCOPES))
    before = _token_state(env, ALT_ID)
    sso_refresh["respond"] = _refreshes(ALT_ID, owner="owner-B")
    verdict, scopes, error = env.q(lambda db: _probe_stored_token(db, ALT_ID))
    assert (verdict, scopes, error) == (PROBE_REVOKED, None, None)
    assert _token_state(env, ALT_ID) == before


def test_refresh_reporting_no_owner_is_not_a_change(env, sso_refresh):
    _set(env, ALT_ID, last_authorized_at=_ago(40), owner_hash="owner-A")
    sso_refresh["respond"] = _refreshes(ALT_ID, owner=None)
    _refresh(env, ALT_ID)
    alt = env.char(ALT_ID)
    assert _fresh(alt.last_authorized_at) and alt.owner_hash == "owner-A"


# ── The sweep: 90-day rule ───────────────────────────────────────────────────

def test_sweep_keeps_a_91_day_old_character_whose_refresh_works(seeded, sso_refresh):
    env = seeded
    _set(env, ALT_ID, last_authorized_at=_ago(91))
    sso_refresh["respond"] = _refreshes(ALT_ID)
    out = _sweep(env)
    assert out == {"held": 0, "checked_ok": 1, "kept": 2}
    alt = env.char(ALT_ID)
    assert alt.user_id == USER_ID and _fresh(alt.last_authorized_at)
    assert len(sso_refresh["calls"]) == 1
    _assert_untouched(env, ALT_ID)
    assert _events(env, "character_expired") == []


def test_sweep_removes_one_whose_token_eve_refuses_with_its_history(seeded, sso_refresh):
    env = seeded
    _set(env, ALT_ID, last_authorized_at=_ago(91))
    sso_refresh["respond"] = rejected
    out = _sweep(env)
    assert out == {"held": 0, "expiry": 1, "kept": 2}
    assert env.char(ALT_ID) is None
    now = env.q(lambda db: _counts(db, ALT_ID))
    assert _only(now, LIVE + USER_ROWS) == _zero(LIVE + USER_ROWS)
    job = env.q(lambda db: _job(db, ALT_ID))
    assert (job.reason, job.delete_history) == ("expired", True)
    [(uid, cid, detail)] = _events(env, "character_expired")
    assert (uid, cid) == (USER_ID, ALT_ID)
    assert "90 days" in detail and "refused" in detail
    assert env.calls["revoked"] == []                    # never revoked at EVE

    [result] = _run_due(env)
    assert result["outcome"] == "done"
    assert env.q(lambda db: _counts(db, ALT_ID)) == _zero(EVERYTHING)
    assert _count(env, WalletSnapshot, character_id=ALT_ID) == 0
    _assert_untouched(env, MAIN_ID)
    _assert_untouched(env, STRANGER_ID)


def test_history_due_for_deletion_is_not_kept_for_a_quick_re_add(seeded, sso_refresh):
    """Signing in again before the background purge runs: the re-link deletes
    the history itself (the pending purge asked for it), so nothing survives."""
    env = seeded
    _set(env, ALT_ID, last_authorized_at=_ago(91))
    sso_refresh["respond"] = rejected
    _sweep(env)
    env.sso_returns(ALT_ID, "Alt Pilot", [])
    env.client(_pending("login")).get(CALLBACK)
    assert env.char(ALT_ID) is not None
    assert _only(env.q(lambda db: _counts(db, ALT_ID)), HISTORY) == _zero(HISTORY)


@pytest.mark.parametrize("respond", [unreachable, throttled,
                                     lambda data: _Resp(503, {"error": "down"})],
                         ids=["network", "429", "503"])
def test_sweep_never_removes_on_an_unknown_result(seeded, sso_refresh, monkeypatch, respond):
    env = seeded

    async def instant(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)       # _do_refresh's 5xx backoff
    _set(env, ALT_ID, last_authorized_at=_ago(200))
    sso_refresh["respond"] = respond
    out = _sweep(env)
    assert out == {"held": 0, "checked_unknown": 1, "kept": 2}
    assert env.char(ALT_ID).user_id == USER_ID
    _assert_untouched(env, ALT_ID)
    assert env.q(lambda db: _job(db, ALT_ID)) is None


def test_a_refused_token_short_of_90_days_is_kept(env, sso_refresh):
    _set(env, ALT_ID, last_authorized_at=_ago(60))
    sso_refresh["respond"] = rejected
    assert _sweep(env) == {"held": 0, "checked_revoked": 1, "kept": 2}
    assert env.char(ALT_ID) is not None


def test_a_sign_in_during_the_refresh_saves_the_character(env, monkeypatch):
    """The decision is made on the row as it is after the refresh."""
    from app.esi import client as client_mod
    _set(env, ALT_ID, last_authorized_at=_ago(91))

    class SignsInThenRefuses:
        async def post(self, url, headers=None, data=None):
            async with async_sessionmaker(env.engine)() as db:
                await db.execute(text("UPDATE characters SET last_authorized_at = :t, "
                                      "last_sso_at = :t WHERE character_id = :c"),
                                 {"t": _naive(_now()).isoformat(" "), "c": ALT_ID})
                await db.commit()
            return _Resp(400, {"error": "invalid_grant"})
    monkeypatch.setattr(client_mod, "get_http_client", lambda: SignsInThenRefuses())

    assert _sweep(env)["checked_revoked"] == 1
    assert env.char(ALT_ID).user_id == USER_ID


def test_a_quiet_character_is_refreshed_early_and_kept(env, sso_refresh):
    """One that shares nothing syncs nothing; the sweep refreshes it after a
    day without a proof, so a working token never reaches the warning."""
    _set(env, ALT_ID, last_authorized_at=_ago(2))
    sso_refresh["respond"] = _refreshes(ALT_ID)
    assert _sweep(env) == {"held": 0, "checked_ok": 1, "kept": 2}
    assert _fresh(env.char(ALT_ID).last_authorized_at)


@pytest.mark.parametrize("local_reason", ["other_application", "old_secret_key"])
def test_a_token_that_fails_for_local_reasons_is_never_tried(env, sso_refresh, local_reason):
    """Not EVE's verdict: a dev copy of production's tokens, or one sealed under
    a SECRET_KEY this instance no longer has."""
    if local_reason == "other_application":
        _set(env, ALT_ID, access_token=_token(ALT_ID, scopes=ALT_SCOPES, azp="prod-client"))
    else:
        _set(env, ALT_ID, refresh_token=Fernet(Fernet.generate_key()).encrypt(b"x").decode())
    _set(env, ALT_ID, last_authorized_at=_ago(200))
    sso_refresh["respond"] = rejected
    assert _sweep(env) == {"held": 0, "kept": 3}
    assert sso_refresh["calls"] == []
    assert env.char(ALT_ID) is not None


# ── The sweep: 365-day cap ───────────────────────────────────────────────────

def test_sweep_removes_past_the_sign_in_cap_even_with_a_working_token(seeded, sso_refresh):
    env = seeded
    _set(env, ALT_ID, last_sso_at=_ago(366))      # last_authorized_at: fresh, the token works
    sso_refresh["respond"] = _refreshes(ALT_ID)
    assert _sweep(env) == {"held": 0, "sso_cap": 1, "kept": 2}
    assert env.char(ALT_ID) is None
    assert sso_refresh["calls"] == []             # no point refreshing it first
    [(uid, cid, detail)] = _events(env, "character_expired")
    assert (uid, cid) == (USER_ID, ALT_ID) and "365 days" in detail
    _run_due(env)
    assert env.q(lambda db: _counts(db, ALT_ID)) == _zero(EVERYTHING)
    _assert_untouched(env, MAIN_ID)


def test_sweep_keeps_one_short_of_the_cap(env):
    _set(env, ALT_ID, last_sso_at=_ago(364))
    assert _sweep(env) == {"held": 0, "kept": 3}


def test_a_row_without_stamps_is_never_removed(env, sso_refresh):
    _set(env, ALT_ID, last_authorized_at=None, last_sso_at=None)
    sso_refresh["respond"] = rejected
    assert _sweep(env) == {"held": 0, "kept": 3}
    assert sso_refresh["calls"] == []


# ── The sweep: owner mismatch ────────────────────────────────────────────────

def test_sweep_removes_an_owner_mismatch_as_a_transfer(seeded, sso_refresh):
    env = seeded
    _set(env, ALT_ID, owner_hash="owner-A", access_token=_token(ALT_ID, "owner-B", ALT_SCOPES))
    assert _sweep(env) == {"held": 0, "transfer": 1, "kept": 2}
    assert env.char(ALT_ID) is None
    # As the SSO callback's transfer: the history is gone at once.
    now = env.q(lambda db: _counts(db, ALT_ID))
    assert _only(now, LIVE + HISTORY + USER_ROWS) == _zero(LIVE + HISTORY + USER_ROWS)
    job = env.q(lambda db: _job(db, ALT_ID))
    assert (job.reason, job.delete_history) == ("transfer", False)
    [(uid, cid, _detail)] = _events(env, "character_transferred")
    assert (uid, cid) == (USER_ID, ALT_ID)
    assert sso_refresh["calls"] == []
    _assert_untouched(env, MAIN_ID)
    _assert_untouched(env, STRANGER_ID)


@pytest.mark.parametrize("days,removed", [(2, False), (91, True)])
def test_a_refresh_for_another_owner_counts_as_refused(env, sso_refresh, days, removed):
    """Not stored, so the sweep sees a dead authorization: kept until the
    90-day rule, then removed by it (not as a transfer: the row still holds
    the old owner's token)."""
    _set(env, ALT_ID, owner_hash="owner-A", last_authorized_at=_ago(days),
         access_token=_token(ALT_ID, "owner-A", ALT_SCOPES))
    before = _token_state(env, ALT_ID)
    sso_refresh["respond"] = _refreshes(ALT_ID, owner="owner-B")
    out = _sweep(env)
    if removed:
        assert out == {"held": 0, "expiry": 1, "kept": 2}
        assert env.char(ALT_ID) is None
        assert len(_events(env, "character_expired")) == 1
    else:
        assert out == {"held": 0, "checked_revoked": 1, "kept": 2}
        assert _token_state(env, ALT_ID) == before


def test_sweep_skips_characters_on_no_account(env):
    _set(env, ALT_ID, user_id=None, last_sso_at=_ago(400))
    assert _sweep(env) == {"held": 0, "kept": 2}
    assert env.char(ALT_ID) is not None


def test_one_failure_does_not_stop_the_sweep(env, monkeypatch):
    real = expiry._removal_rule

    def boom(char, now, *, refused):
        if char.character_id == ALT_ID:
            raise RuntimeError("boom")
        return real(char, now, refused=refused)
    monkeypatch.setattr(expiry, "_removal_rule", boom)
    _set(env, STRANGER_ID, last_sso_at=_ago(400))
    assert _sweep(env) == {"held": 0, "failed": 1, "kept": 1, "sso_cap": 1}
    assert env.char(STRANGER_ID) is None and env.char(ALT_ID) is not None


# ── The breaker: no mass removal in one run ──────────────────────────────────

EXTRA_ID = 91000000


def _add_pilots(env, n):
    """n more healthy linked characters (fresh stamps) on USER_ID's account."""
    async def go(db):
        for i in range(n):
            db.add(_char(EXTRA_ID + i, USER_ID, f"Extra {i:02d}", ALT_SCOPES))
        await db.commit()
    env.q(go)
    return [EXTRA_ID + i for i in range(n)]


def _held_rows(env):
    async def go(db):
        rows = (await db.execute(select(AdminAuditLog).where(
            AdminAuditLog.event_type == "character_expiry_held"))).scalars().all()
        return [(r.user_id, r.character_id, r.detail) for r in rows]
    return env.q(go)


def test_breaker_rule():
    assert not expiry._breaker_trips(2, 2, 0.25)        # under the floor of 3
    assert not expiry._breaker_trips(3, 12, 0.25)       # exactly the share is allowed
    assert expiry._breaker_trips(3, 11, 0.25)
    assert not expiry._breaker_trips(100, 100, 1.0)     # 1.0 or more: off


def test_the_breaker_holds_a_mass_removal(env, sso_refresh, caplog):
    """4 of 7 characters due at once (2 refused past 90 days, 2 past the cap):
    none goes; a refresh that worked in the same run keeps its stamp."""
    caplog.set_level("ERROR", logger="app.auth.expiry")
    e0, e1, e2, e3 = _add_pilots(env, 4)
    for cid in (ALT_ID, e0):
        _set(env, cid, last_authorized_at=_ago(91))
    for cid in (e1, e2):
        _set(env, cid, last_sso_at=_ago(400))
    _set(env, e3, last_authorized_at=_ago(2), refresh_token="working-refresh")
    sso_refresh["respond"] = lambda data: (_refreshes(e3)(data) if data["refresh_token"] == "working-refresh"
                                           else rejected(data))

    assert _sweep(env) == {"held": 4, "checked_ok": 1, "kept": 2}
    for cid in (ALT_ID, e0, e1, e2):
        assert env.char(cid).user_id == USER_ID, cid
    assert _fresh(env.char(e3).last_authorized_at)
    assert _events(env, "character_expired") == []
    assert _count(env, CharacterPurge) == 0
    [(uid, cid, detail)] = _held_rows(env)
    assert (uid, cid) == (None, None)
    assert "held 4 removals (expiry=2, sso_cap=2) of 7 linked characters" in detail
    assert "CHARACTER_EXPIRY_MAX_SHARE=0.25" in detail
    assert any(r.levelname == "ERROR" and "holding 4 removals" in r.getMessage()
               for r in caplog.records)


def test_under_the_count_floor_the_removals_go(env, sso_refresh):
    """2 of 2 linked characters is every one of them, but under the floor."""
    _set(env, STRANGER_ID, user_id=None)
    for cid in (MAIN_ID, ALT_ID):
        _set(env, cid, last_authorized_at=_ago(91))
    sso_refresh["respond"] = rejected
    assert _sweep(env) == {"held": 0, "expiry": 2}
    assert env.char(MAIN_ID) is None and env.char(ALT_ID) is None
    assert _held_rows(env) == []


@pytest.mark.parametrize("linked,held", [(20, False), (12, False), (11, True)])
def test_the_share_decides_at_three_or_more(env, linked, held):
    extras = _add_pilots(env, linked - 3)
    for cid in extras[:3]:
        _set(env, cid, last_sso_at=_ago(400))
    out = _sweep(env)
    if held:
        assert out == {"held": 3, "kept": linked - 3}
        assert all(env.char(cid) is not None for cid in extras[:3])
        assert len(_held_rows(env)) == 1
    else:
        assert out == {"held": 0, "sso_cap": 3, "kept": linked - 3}
        assert all(env.char(cid) is None for cid in extras[:3])
        assert _held_rows(env) == []


def test_transfers_go_while_expiries_are_held(env):
    extras = _add_pilots(env, 3)
    for cid in extras + [STRANGER_ID]:
        _set(env, cid, last_sso_at=_ago(400))
    _set(env, ALT_ID, owner_hash="owner-A", access_token=_token(ALT_ID, "owner-B", ALT_SCOPES))
    assert _sweep(env) == {"held": 4, "transfer": 1, "kept": 1}
    assert env.char(ALT_ID) is None
    assert [(u, c) for u, c, _d in _events(env, "character_transferred")] == [(USER_ID, ALT_ID)]
    assert all(env.char(cid) is not None for cid in extras + [STRANGER_ID])
    [(_u, _c, detail)] = _held_rows(env)
    assert "held 4 removals (sso_cap=4) of 6 linked characters" in detail


def test_a_share_of_one_turns_the_breaker_off(env, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "character_expiry_max_share", 1.0)
    extras = _add_pilots(env, 3)
    for cid in extras + [STRANGER_ID]:
        _set(env, cid, last_sso_at=_ago(400))
    assert _sweep(env) == {"held": 0, "sso_cap": 4, "kept": 2}
    assert all(env.char(cid) is None for cid in extras + [STRANGER_ID])
    assert _held_rows(env) == []


def test_a_candidate_that_signs_in_between_the_phases_stays(env, monkeypatch):
    """MAIN is due (past the cap) in phase 1; ALT's refresh, later in phase 1,
    is when its owner signs in with MAIN. Phase 2 reads it again and keeps it."""
    from app.esi import client as client_mod
    _set(env, MAIN_ID, last_sso_at=_ago(400))
    _set(env, ALT_ID, last_authorized_at=_ago(2))

    class MainSignsIn:
        async def post(self, url, headers=None, data=None):
            async with async_sessionmaker(env.engine)() as db:
                await db.execute(text("UPDATE characters SET last_authorized_at = :t, "
                                      "last_sso_at = :t WHERE character_id = :c"),
                                 {"t": _naive(_now()).isoformat(" "), "c": MAIN_ID})
                await db.commit()
            return _refreshes(ALT_ID)(data)
    monkeypatch.setattr(client_mod, "get_http_client", lambda: MainSignsIn())

    assert _sweep(env) == {"held": 0, "changed": 1, "checked_ok": 1, "kept": 1}
    assert env.char(MAIN_ID).user_id == USER_ID
    assert _events(env, "character_expired") == []


# ── Held removals: stored, then reviewed in Admin › Overview ─────────────────

def _hold(env):
    """Trip the breaker: 4 of USER_ID's 5 characters (6 linked in all) past
    the sign-in cap. Returns the held ids, ALT first."""
    extras = _add_pilots(env, 3)
    held = [ALT_ID] + extras
    for cid in held:
        _set(env, cid, last_sso_at=_ago(400))
    assert _sweep(env) == {"held": 4, "kept": 2}
    return held


def _held_set(env):
    async def go(db):
        rows = (await db.execute(select(HeldCharacterRemoval)
                                 .order_by(HeldCharacterRemoval.character_id))).scalars().all()
        return [(r.character_id, r.character_name, r.user_id, r.rule, r.last_proof_at, r.held_at)
                for r in rows]
    return env.q(go)


def _make_admin(env, uid=OTHER_ID):
    async def go(db):
        u = await _scalar(db, select(User).where(User.id == uid))
        u.role, u.is_admin = "admin", True
        await db.commit()
    env.q(go)
    return env.client({"user_id": uid})


def _audit_rows(env, event_type):
    async def go(db):
        rows = (await db.execute(select(AdminAuditLog).where(
            AdminAuditLog.event_type == event_type).order_by(AdminAuditLog.id))).scalars().all()
        return [(r.user_id, r.character_id, r.ip_address, r.detail) for r in rows]
    return env.q(go)


def test_a_hold_is_stored_and_a_clean_sweep_clears_it(env):
    held = _hold(env)
    rows = _held_set(env)
    assert [(cid, uid, rule) for cid, _n, uid, rule, _p, _h in rows] == [
        (cid, USER_ID, "sso_cap") for cid in sorted(held)]
    assert dict((cid, name) for cid, name, *_ in rows)[ALT_ID] == "Alt Pilot"
    for cid, _n, _u, _r, proof, held_at in rows:
        assert _same(proof, env.char(cid).last_sso_at) and _fresh(held_at)

    # The next day's sweep holds them again: the set is replaced, and each
    # keeps the time it was first held.
    assert _sweep(env) == {"held": 4, "kept": 2}
    assert [r[5] for r in _held_set(env)] == [r[5] for r in rows]

    # Three sign in; the one left due is no mass removal, so it goes and the
    # set is emptied.
    for cid in held[1:]:
        _set(env, cid, last_sso_at=_ago(0))
    assert _sweep(env) == {"held": 0, "sso_cap": 1, "kept": 5}
    assert env.char(ALT_ID) is None
    assert _held_set(env) == []


def test_admin_overview_lists_the_held_removals(env):
    _hold(env)
    _set(env, EXTRA_ID, character_name="<b>Extra</b>")
    r = _make_admin(env).get("/admin/section/overview")
    assert r.status_code == 200
    page = " ".join(r.text.split())
    assert "Held removals" in page
    assert "Alt Pilot <span class=\"b-muted-sm\">· Main Pilot (account 601)</span>" in page
    assert "&lt;b&gt;Extra&lt;/b&gt;" in page and "<b>Extra</b>" not in page
    assert page.count("no EVE sign-in with it for over 365 days") == 4
    assert f"last sign-in {_naive(_ago(400)):%Y-%m-%d}" in page
    assert 'hx-post="/admin/action/held-removals/remove"' in page
    assert 'hx-post="/admin/action/held-removals/keep"' in page


def test_admin_overview_shows_nothing_when_nothing_is_held(env):
    r = _make_admin(env).get("/admin/section/overview")
    assert r.status_code == 200
    assert "Held removals" not in r.text and "held-removals" not in r.text


def test_non_admins_are_refused(env):
    _hold(env)
    user = env.client({"user_id": OTHER_ID})        # role "user"
    assert user.get("/admin/section/overview").status_code == 403
    assert user.post("/admin/action/held-removals/remove").status_code == 403
    assert user.post("/admin/action/held-removals/keep").status_code == 403
    assert len(_held_set(env)) == 4
    assert all(env.char(cid) is not None for cid in [ALT_ID, EXTRA_ID])


def test_remove_takes_only_those_still_due(env, no_pause):
    held = _hold(env)
    alt, e0, e1, e2 = held
    _set(env, e0, last_sso_at=_ago(0))                       # signed in since
    _set(env, e1, user_id=OTHER_ID)                          # left the account
    admin = _make_admin(env)

    r = admin.post("/admin/action/held-removals/remove")
    assert r.status_code == 200
    assert r.headers["HX-Trigger"] == "held-removals-changed"   # the banner re-fetches
    assert "Removed 2 held characters; kept 2 that signed in" in " ".join(r.text.split())
    assert env.char(alt) is None and env.char(e2) is None
    assert env.char(e0).user_id == USER_ID and env.char(e1).user_id == OTHER_ID
    for cid in (alt, e2):
        job = env.q(lambda db, cid=cid: _job(db, cid))
        assert (job.reason, job.delete_history) == ("expired", True)
    expired = _audit_rows(env, "character_expired")
    assert [(u, c, ip) for u, c, ip, _d in expired] == [(USER_ID, alt, "testclient"),
                                                         (USER_ID, e2, "testclient")]
    assert all(f"approved by an admin (user {OTHER_ID})" in d for *_x, d in expired)
    [(uid, cid, ip, detail)] = _audit_rows(env, "character_expiry_approved")
    assert (uid, cid, ip) == (OTHER_ID, None, "testclient")
    assert "2 removed, 2 kept" in detail
    assert _held_set(env) == []
    assert "Held removals" not in r.text


def test_keep_for_now_clears_the_hold_and_removes_nobody(env):
    held = _hold(env)
    r = _make_admin(env).post("/admin/action/held-removals/keep")
    assert r.status_code == 200
    assert "Kept them for now" in r.text and "Held removals" not in r.text
    assert r.headers["HX-Trigger"] == "held-removals-changed"
    assert _held_set(env) == []
    assert all(env.char(cid).user_id == USER_ID for cid in held)
    assert _events(env, "character_expired") == []
    [(uid, cid, ip, detail)] = _audit_rows(env, "character_expiry_kept")
    assert (uid, cid, ip) == (OTHER_ID, None, "testclient")
    assert "Kept 4 held removals for now" in detail


@pytest.mark.parametrize("action", ["remove", "keep"])
def test_both_actions_need_the_csrf_token(env, action):
    held = _hold(env)
    admin = _make_admin(env)
    del admin.headers["X-CSRF-Token"]
    assert admin.post(f"/admin/action/held-removals/{action}").status_code == 403
    assert len(_held_set(env)) == 4
    assert all(env.char(cid) is not None for cid in held)


# ── Held removals: the site-wide banner and Admin › Users ───────────────────

SLOT = 'hx-get="/status/held-removals"'


def _set_role(env, uid, role):
    async def go(db):
        u = await _scalar(db, select(User).where(User.id == uid))
        u.role, u.is_admin = role, role in ("admin", "manager")
        await db.commit()
    env.q(go)


def _as(env, uid, role):
    """A signed-in session as base.html sees one after sign-in (is_admin is
    what _start_session stores), with the role in the database to match."""
    _set_role(env, uid, role)
    return env.client({"user_id": uid, "is_admin": role in ("admin", "manager"), "role": role})


@pytest.mark.parametrize("role", ["admin", "manager"])
def test_the_banner_reaches_admins_and_managers_on_any_page(env, role):
    _hold(env)
    c = _as(env, OTHER_ID, role)
    page = c.get("/account")                                # not an admin page
    assert page.status_code == 200 and SLOT in page.text
    r = c.get("/status/held-removals")
    assert r.status_code == 200
    text_ = " ".join(r.text.split())
    assert "4 character removals are held for review" in text_
    assert 'href="/admin?tab=overview"' in text_


def test_the_banner_is_not_for_a_plain_user_or_a_visitor(env):
    _hold(env)
    user = _as(env, OTHER_ID, "user")
    assert SLOT not in user.get("/account").text
    assert user.get("/status/held-removals").text == ""
    # The route decides from the database, not from the session's flag.
    forged = env.client({"user_id": OTHER_ID, "is_admin": True})
    assert forged.get("/status/held-removals").text == ""
    visitor = env.client()
    assert SLOT not in visitor.get("/").text
    assert visitor.get("/status/held-removals").text == ""


def test_no_banner_when_nothing_is_held(env):
    c = _as(env, OTHER_ID, "admin")
    r = c.get("/status/held-removals")
    assert r.status_code == 200 and r.text == ""


def test_the_banner_counts_what_overview_lists(env):
    held = _hold(env)
    _set(env, held[1], user_id=OTHER_ID)                    # left the account it was held on
    r = _as(env, OTHER_ID, "admin").get("/status/held-removals")
    assert "3 character removals are held for review" in " ".join(r.text.split())


def _char_chunks(page):
    """Admin › Users split into one chunk per character row (with whatever
    follows it before the next row)."""
    return page.split('<div class="adm-char" ')[1:]


def test_admin_users_marks_held_characters_only(env):
    held = _hold(env)
    _set(env, held[1], character_name="<b>Extra</b>")
    r = _as(env, OTHER_ID, "admin").get("/admin/section/users")
    assert r.status_code == 200
    page = " ".join(r.text.split())
    chunks = _char_chunks(page)
    assert len(chunks) == 6                                  # every linked character
    marked = [c for c in chunks if "Held for removal:" in c]
    assert len(marked) == 4
    alt = next(c for c in chunks if "Alt Pilot</span>" in c)
    assert ("Held for removal: no EVE sign-in with it for over 365 days · held since " in alt
            and 'href="/admin?tab=overview">Review in Overview</a>' in alt)
    main = next(c for c in chunks if "Main Pilot</span>" in c)
    assert "Held for removal" not in main
    assert "&lt;b&gt;Extra&lt;/b&gt;" in page and "<b>Extra</b>" not in page
    # The two clocks, in the name's tooltip.
    assert (f'title="Last sign-in {_naive(_ago(400)):%Y-%m-%d} · last working authorization '
            f'{_naive(_now()):%Y-%m-%d}"') in alt


def test_admin_users_marks_nothing_when_nothing_is_held(env):
    r = _as(env, OTHER_ID, "admin").get("/admin/section/users")
    assert r.status_code == 200 and "Held for removal" not in r.text


# ── The account after a removal ─────────────────────────────────────────────

def _remove_main(env, monkeypatch, path):
    if path == "sweep":
        _set(env, MAIN_ID, last_sso_at=_ago(400))
        _sweep(env)
    else:   # what the SSO callback does on a transfer, the existing path
        _set(env, MAIN_ID, owner_hash="owner-A")
        _sso(monkeypatch, MAIN_ID, "Main Pilot", owner="owner-B")
        env.client(_pending("login")).get(CALLBACK)


@pytest.mark.parametrize("path", ["sweep", "transfer"])
def test_removing_the_main_promotes_nobody_and_keeps_the_account(env, monkeypatch, path):
    epoch = _user(env, USER_ID).session_epoch
    _remove_main(env, monkeypatch, path)
    assert _chars_of(env, USER_ID) == [(ALT_ID, False)]   # no new main
    user = _user(env, USER_ID)
    assert user is not None and user.session_epoch == epoch
    # A session still pointing at the removed main still works.
    r = env.client({"user_id": USER_ID, "session_epoch": epoch,
                    "active_character_id": MAIN_ID}).get("/account")
    assert r.status_code == 200 and "Alt Pilot" in r.text and "Main Pilot" not in r.text


@pytest.mark.parametrize("path", ["sweep", "transfer"])
def test_removing_the_last_character_leaves_an_empty_account(env, monkeypatch, path):
    if path == "sweep":
        _set(env, STRANGER_ID, last_sso_at=_ago(400))
        _sweep(env)
    else:
        _set(env, STRANGER_ID, owner_hash="owner-A")
        _sso(monkeypatch, STRANGER_ID, "Someone Else", owner="owner-B")
        env.client(_pending("login")).get(CALLBACK)
    assert _chars_of(env, OTHER_ID) == []
    assert _user(env, OTHER_ID) is not None


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_the_scheduler_runs_the_sweep():
    from app.routes.dashboard import _background_scheduler
    assert "run_expiry_sweep()" in inspect.getsource(_background_scheduler)


def test_the_audit_log_filter_knows_the_new_event():
    from app.routes.admin import audit_group
    from tests.test_admin_audit_filter import _written_event_types
    constants, _dynamic, unresolved = _written_event_types()
    assert {"character_expired", "character_expiry_held", "character_transferred"} <= constants
    assert audit_group("character_expired") == "users"
    assert audit_group("character_expiry_held") == "users"
    assert not unresolved


def test_expired_is_a_removal_reason():
    assert purge.REASON_EXPIRED in purge.REMOVAL_REASONS
    assert len(purge.REASON_EXPIRED) <= CharacterPurge.__table__.c.reason.type.length


# ── The warning ──────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _stamps(authorized_days=0.0, sso_days=0.0):
    return {"last_authorized_at": NOW - timedelta(days=authorized_days),
            "last_sso_at": NOW - timedelta(days=sso_days)}


def test_warning_none_for_a_healthy_character():
    assert expiry.removal_warning(_stamps(0.5, 100), NOW) is None


def test_warning_90_day_rule_after_a_few_days_without_a_proof():
    assert expiry.removal_warning(_stamps(2.9), NOW) is None
    rule, due = expiry.removal_warning(_stamps(4), NOW)
    assert rule == expiry.RULE_EXPIRY and due == NOW + timedelta(days=86)


def test_warning_90_day_rule_at_once_for_a_dead_token():
    rule, due = expiry.removal_warning(_stamps(0.5), NOW, token_dead=True)
    assert rule == expiry.RULE_EXPIRY and due == NOW + timedelta(days=89.5)


def test_warning_cap_within_30_days():
    assert expiry.removal_warning(_stamps(0, 334), NOW) is None
    rule, due = expiry.removal_warning(_stamps(0, 336), NOW)
    assert rule == expiry.RULE_SSO_CAP and due == NOW + timedelta(days=29)


def test_warning_both_rules_give_the_earlier_date():
    rule, due = expiry.removal_warning(_stamps(80, 340), NOW)    # 10 days vs 25
    assert (rule, due) == (expiry.RULE_EXPIRY, NOW + timedelta(days=10))
    rule, due = expiry.removal_warning(_stamps(10, 350), NOW)    # 80 days vs 15
    assert (rule, due) == (expiry.RULE_SSO_CAP, NOW + timedelta(days=15))


def test_warning_reads_naive_stamps_as_utc_and_ignores_missing_ones():
    naive = {"last_authorized_at": (NOW - timedelta(days=4)).replace(tzinfo=None),
             "last_sso_at": None}
    assert expiry.removal_warning(naive, NOW) == (expiry.RULE_EXPIRY, NOW + timedelta(days=86))
    assert expiry.removal_warning({"last_authorized_at": None, "last_sso_at": None},
                                  NOW, token_dead=True) is None


def test_warning_text():
    assert (expiry.warning_text(datetime(2027, 2, 3, tzinfo=timezone.utc), NOW)
            == "will be removed on 2027-02-03 unless you sign in with it at EVE")
    assert expiry.warning_text(NOW - timedelta(days=1), NOW).startswith("is due to be removed")


def test_attention_item_for_an_affected_pilot_only():
    from app.dashboard.attention import build_attention
    from tests.test_dashboard_attention import _pilot
    healthy = _pilot(cid=1, name="Fine Pilot", **_stamps(0.1, 10))
    affected = _pilot(cid=2, name="Alt Pilot", **_stamps(0.1, 350))
    items = build_attention([healthy, affected], NOW)
    [item] = [i for i in items if i.key.startswith("auth_expiry:")]
    assert (item.character_id, item.severity) == (2, "amber")
    assert item.text == f"will be removed on {NOW + timedelta(days=15):%Y-%m-%d} unless you sign in with it at EVE"
    assert item.action_url == "/account/permissions/2"


def test_a_dead_token_says_it_on_the_red_item_not_a_second_row():
    from app.dashboard.attention import build_attention
    from tests.test_dashboard_attention import _pilot
    p = _pilot(cid=3, name="Dead Pilot", needs_reauth=True, **_stamps(10, 10))
    items = build_attention([p], NOW)
    assert [i.key for i in items if i.character_id == 3] == ["reauth:3"]
    assert items[0].text.endswith(f"it will be removed on {NOW + timedelta(days=80):%Y-%m-%d} "
                                  "unless you sign in with it at EVE")


# ── The warning on the pages ─────────────────────────────────────────────────

def _expected(due: datetime) -> str:
    return f"Alt Pilot will be removed on {due:%Y-%m-%d} unless you sign in with it at EVE."


def test_account_page_warns_for_an_affected_character_only(env):
    signed_in = _ago(350)
    _set(env, ALT_ID, last_sso_at=signed_in)
    r = env.user().get("/account")
    assert r.status_code == 200
    page = " ".join(r.text.split())
    assert _expected(signed_in + timedelta(days=365)) in page
    assert (f"at least once every 365 days; this one last did on "
            f"{_naive(signed_in):%Y-%m-%d}.") in page
    assert f'href="/account/permissions/{ALT_ID}">Renew</a>' in page
    assert "Main Pilot will be removed" not in page


def test_account_page_escapes_the_name(env):
    _set(env, ALT_ID, last_sso_at=_ago(350), character_name="<b>Alt</b>")
    page = env.user().get("/account").text
    assert "&lt;b&gt;Alt&lt;/b&gt; will be removed on" in page
    assert "<b>Alt</b> will be removed" not in page


def test_dashboard_attention_warns_for_an_affected_character_only(env):
    authorized = _ago(10)
    _set(env, ALT_ID, last_authorized_at=authorized)
    r = env.user().get("/dashboard/attention")
    assert r.status_code == 200
    page = " ".join(r.text.split())
    due = authorized + timedelta(days=90)
    assert (f'<span class="da-pilot">Alt Pilot</span> · will be removed on {due:%Y-%m-%d} '
            "unless you sign in with it at EVE") in page
    assert "auth_expiry:%d" % ALT_ID in page
    assert "auth_expiry:%d" % MAIN_ID not in page
    assert "Main Pilot</span> · will be removed" not in page


def test_the_banner_slot_listens_for_the_change_event():
    """Otherwise it announces a held set for up to five minutes after an
    admin has dealt with it."""
    from pathlib import Path
    base = Path("app/templates/base.html").read_text()
    slot = base[base.index('id="held-removals-slot"'):][:400]
    assert "held-removals-changed from:body" in slot
