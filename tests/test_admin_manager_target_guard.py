"""ISS-164: a manager may not act on an admin's account, in any admin route.

Set-role and remove-user refused a manager who targeted an admin; remove-
character did not, so a manager could strip an admin's characters one by one
(their history queued for deletion and their tokens revoked). All three now
ask one helper, and the users panel uses it to decide which controls to show.

The routes are driven for real through the `env` fixture; SSO and token
revocation are stubbed there. Ids and names are invented.
"""
import inspect
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import AdminAuditLog, CharacterPurge, User
from app.routes import admin as admin_mod
from tests._mobile import render_page
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    OTHER_ID, STRANGER_ID, USER_ID, env,
)


def _roles(env, actor: str, target: str):
    """USER_ID becomes `actor` and OTHER_ID (owner of STRANGER_ID) `target`.
    Set directly, as an admin would, without rotating the session epoch."""
    async def go(db):
        for uid, role in ((USER_ID, actor), (OTHER_ID, target)):
            u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            u.role, u.is_admin = role, role in ("admin", "manager")
        await db.commit()
    env.q(go)


def _purges(env, cid):
    async def go(db):
        return (await db.execute(
            select(CharacterPurge).where(CharacterPurge.character_id == cid))).scalars().all()
    return env.q(go)


def _audit(env, event):
    async def go(db):
        return (await db.execute(
            select(AdminAuditLog).where(AdminAuditLog.event_type == event))).scalars().all()
    return env.q(go)


def _role_of(env, uid):
    async def go(db):
        return (await db.execute(select(User.role).where(User.id == uid))).scalar_one_or_none()
    return env.q(go)


@pytest.fixture
def revoked(monkeypatch):
    calls = []

    async def fake_revoke(token):
        calls.append(token)
        return True
    monkeypatch.setattr("app.auth.tokens.revoke_refresh_token", fake_revoke)
    return calls


# ── Remove character ─────────────────────────────────────────────────────────

def test_a_manager_cannot_remove_an_admins_character(env, revoked):
    _roles(env, actor="manager", target="admin")
    r = env.user().post(f"/admin/action/remove-character/{STRANGER_ID}")
    assert r.status_code == 200
    assert "Managers cannot" in r.text
    assert env.char(STRANGER_ID) is not None
    assert _purges(env, STRANGER_ID) == []
    assert revoked == []
    assert _audit(env, "admin_remove_character") == []


@pytest.mark.parametrize("actor,target", [
    ("manager", "user"), ("manager", "manager"), ("admin", "admin"), ("admin", "user"),
])
def test_other_pairs_still_remove_the_character(env, revoked, actor, target):
    """The control: only manager-on-admin is refused."""
    _roles(env, actor=actor, target=target)
    r = env.user().post(f"/admin/action/remove-character/{STRANGER_ID}")
    assert r.status_code == 200
    assert "Managers cannot" not in r.text
    assert env.char(STRANGER_ID) is None
    assert len(_purges(env, STRANGER_ID)) == 1
    assert revoked == ["old-refresh"]


# ── The two routes that already had the rule keep it ─────────────────────────

def test_a_manager_cannot_change_an_admins_role(env):
    _roles(env, actor="manager", target="admin")
    r = env.user().post(f"/admin/action/set-role/{OTHER_ID}/user")
    assert "Managers cannot modify admin users." in r.text
    assert _role_of(env, OTHER_ID) == "admin"


def test_a_manager_still_cannot_grant_admin(env):
    _roles(env, actor="manager", target="user")
    r = env.user().post(f"/admin/action/set-role/{OTHER_ID}/admin")
    assert "Managers cannot grant admin role." in r.text
    assert _role_of(env, OTHER_ID) == "user"


def test_a_manager_cannot_delete_an_admin(env):
    _roles(env, actor="manager", target="admin")
    r = env.user().post(f"/admin/action/remove-user/{OTHER_ID}")
    assert "Managers cannot delete admin users." in r.text
    assert _role_of(env, OTHER_ID) == "admin"
    assert env.char(STRANGER_ID) is not None


def test_every_user_action_route_uses_the_one_rule():
    """So the next route that acts on another user's account can't drift."""
    for fn in (admin_mod.admin_set_role, admin_mod.admin_remove_user,
               admin_mod.admin_remove_character):
        src = inspect.getsource(fn)
        assert "manager_blocked(" in src, fn.__name__
        # No inline copy of the target check left beside it.
        assert '.role == "admin"' not in src, fn.__name__


def test_the_rule():
    blocked = admin_mod.manager_blocked
    assert blocked("manager", "admin")
    assert not blocked("manager", "manager")
    assert not blocked("manager", "user")
    assert not blocked("manager", None)       # a character with no owner row
    assert not blocked("admin", "admin")


# ── The users panel ──────────────────────────────────────────────────────────

def _panel(admin_role):
    now = datetime.now(timezone.utc)
    ns = types.SimpleNamespace
    char = lambda cid, name: dict(  # noqa: E731
        character_id=cid, character_name=name, corporation_name="Sample Corp",
        token_status="ok", sync_status="idle", last_synced=now - timedelta(minutes=7),
        scope_count=10)
    users = [
        dict(user=ns(id=1, role=admin_role, last_login=now), char_count=1,
             characters=[char(90000101, "Sample Pilot Self")],
             display_name="Sample Pilot Self", main_character_id=90000101),
        dict(user=ns(id=2, role="manager", last_login=now), char_count=1,
             characters=[char(90000102, "Sample Pilot Manager")],
             display_name="Sample Pilot Manager", main_character_id=90000102),
        dict(user=ns(id=3, role="admin", last_login=now), char_count=1,
             characters=[char(90000103, "Sample Pilot Admin")],
             display_name="Sample Pilot Admin", main_character_id=90000103),
    ]
    return render_page(admin_mod, "partials/admin_users.html", "/admin/section/users",
                       users=users, allowlist=[], allowlist_enabled=False,
                       format_age=lambda dt: "now", admin_id=1, admin_role=admin_role)


def test_a_manager_is_not_offered_removal_of_an_admins_character():
    html = _panel("manager")
    assert "/admin/action/remove-character/90000101" in html   # their own
    assert "/admin/action/remove-character/90000102" in html   # a manager's
    assert "/admin/action/remove-character/90000103" not in html
    assert "/admin/action/remove-user/3" not in html
    assert "/admin/action/remove-user/2" in html


def test_an_admin_is_offered_every_removal():
    html = _panel("admin")
    for cid in (90000101, 90000102, 90000103):
        assert f"/admin/action/remove-character/{cid}" in html
    assert "/admin/action/remove-user/2" in html and "/admin/action/remove-user/3" in html
