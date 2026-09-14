"""The three updater endpoints: gating, feature detection, and the audit trail.

Signed-session-cookie TestClient idiom from tests/test_pnl_route.py, plus a
tmpdir standing in for the /control volume. No sidecar is involved anywhere —
these tests assert what the *app* does with the files it finds.
"""
import asyncio
import base64
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AdminAuditLog, Base, UpdateStatus, User, get_db

ADMIN_ID = 501
PLAIN_ID = 502
CSRF = "test-csrf-token"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp DB wired into the app, a control dir, and clients for both roles.

    Yields a small namespace rather than several fixtures because every test
    needs the control dir and at least one client, and splitting them would mean
    repeating the dependency-override teardown in each.
    """
    import app.main as main

    monkeypatch.setenv("VIGILANT_CONTROL_DIR", str(tmp_path))

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=ADMIN_ID, role="admin", is_admin=True))
            db.add(User(id=PLAIN_ID, role="user", is_admin=False))
            db.add(UpdateStatus(id=1, latest_tag="v1.3.0"))
            await db.commit()

    _run(seed())

    async def _override():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = _override

    def client(user_id):
        signer = itsdangerous.TimestampSigner(main.settings.secret_key)
        data = base64.b64encode(
            json.dumps({"user_id": user_id, "csrf_token": CSRF}).encode()
        )
        c = TestClient(main.app, base_url="https://testserver",
                       raise_server_exceptions=False)
        c.cookies.set("vigilant_session", signer.sign(data).decode())
        c.headers.update({"X-CSRF-Token": CSRF})
        return c

    class Env:
        control = tmp_path
        admin = staticmethod(lambda: client(ADMIN_ID))
        plain = staticmethod(lambda: client(PLAIN_ID))
        anon = staticmethod(
            lambda: TestClient(main.app, base_url="https://testserver",
                               raise_server_exceptions=False)
        )

        @staticmethod
        def audit_events():
            async def q():
                async with SessionLocal() as db:
                    rows = (await db.execute(select(AdminAuditLog))).scalars().all()
                    return [(r.event_type, r.detail) for r in rows]
            return _run(q())

    yield Env
    main.app.dependency_overrides.pop(get_db, None)


def _beat(control, current_tag="v1.2.0", targets=("v1.1.0",), checks=None):
    (control / "updater.json").write_text(json.dumps({
        "version": "v1.2.0",
        "current_tag": current_tag,
        "targets": list(targets),
        "checks": checks or {"socket": "ok", "git": "ok",
                             "compose": "ok", "deployed": "ok"},
    }))


def _stale_beat(control):
    _beat(control)
    past = time.time() - 3600
    import os
    os.utime(control / "updater.json", (past, past))


def _request_file(control):
    p = control / "request.json"
    return json.loads(p.read_text()) if p.exists() else None


# ── Feature detection ────────────────────────────────────────────────────────

def test_post_404s_without_a_heartbeat(env):
    """Profile off means the endpoint genuinely does not exist."""
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 404
    assert _request_file(env.control) is None


def test_post_404s_when_the_heartbeat_is_stale(env):
    """A sidecar that died must not leave a button that hangs."""
    _stale_beat(env.control)
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 404


def test_status_does_not_404_without_a_heartbeat(env):
    """The panel has to survive the app's own restart, during which the sidecar
    may miss a beat. 404 here would replace a live progress view with an error
    at exactly the moment the operator is watching it."""
    r = env.admin().get("/admin/update/status")
    assert r.status_code == 200
    assert "No updater is running" in r.text


# ── Gating ───────────────────────────────────────────────────────────────────

def test_status_is_not_readable_anonymously(env):
    """follow_redirects=False matters: require_admin answers a session-less
    caller with a 303 to "/", and a client that follows it reports the
    homepage's 200 — which would make this test pass while the gate was
    removed."""
    _beat(env.control)
    r = env.anon().get("/admin/update/status", follow_redirects=False)
    assert r.status_code in (303, 403)
    assert "updater-panel" not in r.text


def test_status_is_not_readable_by_a_plain_user(env):
    _beat(env.control)
    assert env.plain().get("/admin/update/status").status_code == 403


@pytest.mark.parametrize("path", ["/admin/update", "/admin/rollback"])
def test_posts_are_admin_gated(env, path):
    _beat(env.control)
    r = env.plain().post(path, data={"tag": "v1.1.0"})
    assert r.status_code == 403
    assert _request_file(env.control) is None


@pytest.mark.parametrize("path", ["/admin/update", "/admin/rollback"])
def test_posts_require_csrf(env, path):
    """The header is the whole gate for htmx-driven posts."""
    _beat(env.control)
    c = env.admin()
    c.headers.pop("X-CSRF-Token")
    r = c.post(path, data={"tag": "v1.1.0"})
    assert r.status_code == 403
    assert _request_file(env.control) is None


# ── Update ───────────────────────────────────────────────────────────────────

def test_update_queues_a_request(env):
    _beat(env.control)
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 200
    written = _request_file(env.control)
    assert written["action"] == "update"
    assert written["tag"] == "v1.3.0"
    assert written["requested_by"] == ADMIN_ID


def test_update_is_audit_logged_with_the_requester(env):
    _beat(env.control)
    env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    events = env.audit_events()
    assert any(e == "admin_update_requested" and "v1.3.0" in d for e, d in events)


def test_update_refuses_a_no_op(env):
    """Re-deploying the RUNNING release is refused as a no-op…"""
    _beat(env.control, current_tag="v1.2.0")
    r = env.admin().post("/admin/update", data={"tag": "v1.2.0"})
    assert r.status_code == 400
    assert _request_file(env.control) is None


def test_update_does_not_require_the_target_to_be_newer(env):
    """…but an older tag is allowed: re-deploying is a legitimate way to
    recover a container that came up wrong, so this is not a one-way ratchet."""
    _beat(env.control, current_tag="v1.2.0")
    r = env.admin().post("/admin/update", data={"tag": "v1.1.0"})
    assert r.status_code == 200
    assert _request_file(env.control)["tag"] == "v1.1.0"


def test_update_rejects_an_injection_tag(env):
    _beat(env.control)
    r = env.admin().post("/admin/update", data={"tag": "v1.2.3; rm -rf /"})
    assert r.status_code == 400
    assert _request_file(env.control) is None


# ── Rollback ─────────────────────────────────────────────────────────────────

def test_rollback_to_a_published_target_is_queued(env):
    _beat(env.control, targets=("v1.1.0", "v1.0.0"))
    r = env.admin().post("/admin/rollback", data={"tag": "v1.0.0"})
    assert r.status_code == 200
    written = _request_file(env.control)
    assert written["action"] == "rollback"
    assert written["tag"] == "v1.0.0"


def test_rollback_target_must_be_published(env):
    """The list comes from .deployed via the heartbeat — a release this host has
    actually run. Anything else is refused before it reaches the sidecar."""
    _beat(env.control, targets=("v1.1.0",))
    r = env.admin().post("/admin/rollback", data={"tag": "v0.9.0"})
    assert r.status_code == 400
    assert _request_file(env.control) is None


def test_rollback_is_audit_logged(env):
    _beat(env.control, targets=("v1.1.0",))
    env.admin().post("/admin/rollback", data={"tag": "v1.1.0"})
    assert any(e == "admin_rollback_requested" for e, _ in env.audit_events())


# ── Busy and interrupted ─────────────────────────────────────────────────────

def _status(control, state, started_minutes_ago=1):
    started = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago))
    (control / "status.json").write_text(json.dumps({
        "state": state,
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "action": "update", "to_tag": "v1.3.0", "step": "deploying",
    }))


def test_update_refuses_while_a_run_is_in_flight(env):
    _beat(env.control)
    _status(env.control, "running", started_minutes_ago=1)
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 409
    assert _request_file(env.control) is None


def test_interrupted_run_is_reported_distinctly(env):
    """Not 'busy': a busy updater finishes on its own, an interrupted one never
    will, and the operator needs to be told to look at the host."""
    _beat(env.control)
    _status(env.control, "running", started_minutes_ago=120)
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 409
    assert "interrupted" in r.text.lower()


def test_update_allowed_after_a_finished_run(env):
    _beat(env.control)
    (env.control / "status.json").write_text(json.dumps({
        "state": "success", "action": "update", "to_tag": "v1.2.0",
    }))
    assert env.admin().post("/admin/update", data={"tag": "v1.3.0"}).status_code == 200


# ── Panel rendering ──────────────────────────────────────────────────────────

def test_panel_offers_the_latest_known_release(env):
    """The tag on the button comes from update_status — what the hourly checker
    saw on GitHub — not from the heartbeat, which only knows what is deployed."""
    _beat(env.control, current_tag="v1.2.0")
    body = env.admin().get("/admin/update/status").text
    assert "v1.3.0" in body


def test_panel_reports_up_to_date_when_current_matches_latest(env):
    _beat(env.control, current_tag="v1.3.0")
    body = env.admin().get("/admin/update/status").text
    assert "Up to date" in body


def test_panel_surfaces_failed_self_checks(env):
    """Environment problems show as a disabled button with a readable reason
    rather than a mid-deploy explosion."""
    _beat(env.control, checks={"socket": "FAIL: permission denied", "git": "ok",
                               "compose": "ok", "deployed": "ok"})
    body = env.admin().get("/admin/update/status").text
    assert "permission denied" in body
    assert "disabled" in body


def test_panel_has_no_inline_event_handlers(env):
    """v1.1.0 enforces CSP with no 'unsafe-inline' in script-src — an onclick
    here would be silently dead in production."""
    import re
    _beat(env.control)
    body = env.admin().get("/admin/update/status").text
    assert not re.search(r"\son(click|change|submit|input|load)=", body)


# ── The gap between submit and pickup ────────────────────────────────────────

def test_panel_shows_queued_while_a_request_awaits_pickup(env):
    """status.json still describes the PREVIOUS run until the sidecar claims.

    That window is exactly when the operator has just clicked, so re-displaying
    the last run there answers a click with a stale outcome — which may well
    read "succeeded". Observed on the throwaway stack, 2026-09-14.
    """
    _beat(env.control)
    (env.control / "status.json").write_text(json.dumps({
        "state": "success", "action": "update", "to_tag": "v1.2.0",
        "log_tail": ["PREVIOUS RUN OUTPUT"],
    }))
    r = env.admin().post("/admin/update", data={"tag": "v1.3.0"})
    assert r.status_code == 200
    assert "queued" in r.text
    assert "PREVIOUS RUN OUTPUT" not in r.text
    assert "succeeded" not in r.text


def test_queued_state_is_derived_from_the_request_file(env):
    """Stateless: claim_request() renames request.json away as its first act, so
    the file's existence IS the signal — no cross-restart bookkeeping needed."""
    _beat(env.control)
    (env.control / "request.json").write_text(json.dumps({"id": "x", "action": "update"}))
    body = env.admin().get("/admin/update/status").text
    assert "queued" in body


def test_no_queued_state_once_the_request_is_claimed(env):
    _beat(env.control)
    (env.control / "status.json").write_text(json.dumps({
        "state": "success", "action": "update", "to_tag": "v1.2.0",
    }))
    body = env.admin().get("/admin/update/status").text
    assert "succeeded" in body


# ── The overview section renders the panel itself ────────────────────────────

def test_overview_section_contains_the_rendered_panel(env):
    """End-to-end version of the inline-rendering rule: the section's own HTML
    must already contain the panel, not a placeholder that fetches it."""
    _beat(env.control, current_tag="v1.2.0")
    r = env.admin().get("/admin/section/overview")
    assert r.status_code == 200
    assert 'id="updater-panel"' in r.text
    assert 'hx-get="/admin/update/status"' not in r.text


def test_overview_section_survives_a_missing_updater(env):
    """With no sidecar the section must still render — the panel degrades to the
    'no updater' note rather than 500ing the whole Overview tab."""
    r = env.admin().get("/admin/section/overview")
    assert r.status_code == 200
    assert "No updater is running" in r.text
