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
        _sessionmaker = SessionLocal
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
        # Mirrors what the sidecar actually publishes, `remote` included. A
        # default that lags the supervisor's real key set would let these tests
        # keep passing for a heartbeat shape production never produces.
        "checks": checks or {"socket": "ok", "git": "ok", "compose": "ok",
                             "remote": "ok", "deployed": "ok"},
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


def test_panel_disables_the_button_on_a_failed_remote_check(env):
    """The `remote` check is newer than this panel, so it is worth pinning that
    the panel needs no knowledge of it.

    The template rejects any check whose value is not "ok" rather than testing
    four known names, which is what makes a new check disable the button with no
    app-side change. If someone ever "simplifies" that into an explicit list,
    this fails — and the failure mode it guards against is the one that shipped:
    a panel showing an enabled Update button on a host where the update could
    not possibly work.
    """
    _beat(env.control, checks={
        "socket": "ok", "git": "ok", "compose": "ok", "deployed": "ok",
        "remote": ("FAIL: origin resolves to git@example.org:team/repo.git, "
                   "which git can only reach over SSH."),
    })
    body = env.admin().get("/admin/update/status").text
    assert "remote" in body
    assert "can only reach over SSH" in body
    assert "disabled" in body


# ── Sidecar version skew, end to end through the real route ─────────────────
#
# The panel tests render the template standalone with a hand-built context.
# These drive the real _updater_context off a real heartbeat file, so a context
# key that was never wired up fails here rather than passing there.

_MANUAL_RECREATE = "docker compose --profile updater up -d updater"


def _beat_with(control, version, current_tag, self_update="absent"):
    """A heartbeat as the sidecar writes it, optionally without `self_update`.

    "absent" rather than None is the default because those are different
    heartbeats: a sidecar too old to have the field, versus one that has
    nothing to report. Both must render identically, and only one of them can
    be written by omitting the key.
    """
    payload = {
        "version": version,
        "current_tag": current_tag,
        "targets": [],
        "checks": {"socket": "ok", "git": "ok", "compose": "ok",
                   "remote": "ok", "deployed": "ok"},
    }
    if self_update != "absent":
        payload["self_update"] = self_update
    (control / "updater.json").write_text(json.dumps(payload))


def test_panel_warns_when_the_sidecar_trails_the_app(env):
    _beat_with(env.control, version="v1.2.2", current_tag="v1.2.3")
    body = env.admin().get("/admin/update/status").text
    assert "v1.2.2" in body
    assert _MANUAL_RECREATE in body


def test_a_lagging_sidecar_keeps_its_buttons(env):
    """Updating is how a lagging sidecar heals itself — the panel must not
    disable the one path out of the skew."""
    _beat_with(env.control, version="v1.1.0", current_tag="v1.2.0")
    body = env.admin().get("/admin/update/status").text
    assert _MANUAL_RECREATE in body
    assert "Update to v1.3.0" in body
    assert "disabled" not in body


def test_panel_is_quiet_when_the_sidecar_is_in_step(env):
    _beat_with(env.control, version="v1.2.0", current_tag="v1.2.0")
    body = env.admin().get("/admin/update/status").text
    assert _MANUAL_RECREATE not in body


def test_panel_is_quiet_when_the_sidecar_is_ahead_of_the_app(env):
    """Forward-only self-update leaves the sidecar ahead after a rollback. That
    is the intended state, not a problem to report."""
    _beat_with(env.control, version="v1.3.0", current_tag="v1.2.0")
    body = env.admin().get("/admin/update/status").text
    assert _MANUAL_RECREATE not in body
    assert "still running" not in body


def test_panel_reports_a_self_update_in_progress_neutrally(env):
    _beat_with(env.control, version="v1.2.2", current_tag="v1.2.3",
               self_update={"state": "handed_off", "target": "v1.2.3",
                            "error": None, "at": "2026-09-21T10:00:00Z"})
    body = env.admin().get("/admin/update/status").text
    assert "upgrading itself" in body
    # The skew is about to fix itself; telling the operator to run a command by
    # hand at that moment would be wrong.
    assert _MANUAL_RECREATE not in body


def test_panel_reports_why_a_self_update_failed(env):
    _beat_with(env.control, version="v1.2.2", current_tag="v1.2.3",
               self_update={"state": "failed", "target": "v1.2.3",
                            "error": "could not pull: manifest unknown",
                            "at": "2026-09-21T10:00:00Z"})
    body = env.admin().get("/admin/update/status").text
    assert "manifest unknown" in body
    assert _MANUAL_RECREATE in body


def test_a_heartbeat_without_the_field_renders_as_it_always_did(env):
    """The bootstrap case: every sidecar from v1.2.2 and earlier."""
    _beat_with(env.control, version="v1.2.0", current_tag="v1.2.0")
    body = env.admin().get("/admin/update/status").text
    assert "upgrading itself" not in body
    assert _MANUAL_RECREATE not in body
    assert "Update to v1.3.0" in body


def test_a_self_update_state_this_app_has_never_heard_of_is_ignored(env):
    """The sidecar is NEWER than the app by construction, so it can publish a
    state this version does not know. That must render as nothing, not as a
    broken panel."""
    _beat_with(env.control, version="v1.3.0", current_tag="v1.3.0",
               self_update={"state": "reticulating", "target": "v1.4.0"})
    resp = env.admin().get("/admin/update/status")
    assert resp.status_code == 200
    assert "reticulating" not in resp.text


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


# ── Scheduling and the auto-update policy ────────────────────────────────────

def _future(hours=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def _policy_row(env_):
    from app.db.models import UpdatePolicy

    async def q():
        async with env_._sessionmaker() as db:
            return (await db.execute(select(UpdatePolicy))).scalars().first()
    return _run(q())


def _schedules(env_):
    from app.db.models import UpdateSchedule

    async def q():
        async with env_._sessionmaker() as db:
            rows = (await db.execute(select(UpdateSchedule))).scalars().all()
            return [(r.target_tag, r.state) for r in rows]
    return _run(q())


def test_schedule_requires_an_updater(env):
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert r.status_code == 404


def test_schedule_is_created(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert r.status_code == 200
    assert ("v1.3.0", "pending") in _schedules(env)


def test_scheduling_does_not_submit_a_request(env):
    """Scheduling defers; only the background loop submits. Two ways to start a
    deploy would mean two sets of guard rails."""
    _beat(env.control)
    env.admin().post("/admin/update/schedule",
                     data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert _request_file(env.control) is None


def test_schedule_rejects_a_past_time(env):
    _beat(env.control)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": past, "tz": "UTC"})
    assert r.status_code == 400
    assert "in the past" in r.text


def test_schedule_rejects_an_unknown_timezone(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": _future(), "tz": "Mars/Olympus"})
    assert r.status_code == 400
    assert "unknown timezone" in r.text


def test_schedule_rejects_a_malformed_time(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": "next tuesday", "tz": "UTC"})
    assert r.status_code == 400


def test_schedule_rejects_a_bad_tag(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.2.3; rm -rf /", "run_at": _future(), "tz": "UTC"})
    assert r.status_code == 400
    assert _schedules(env) == []


def test_a_second_schedule_supersedes_the_first(env):
    """At most one pending schedule — two updates nobody is tracking is worse
    than replacing the one that was asked for."""
    _beat(env.control)
    c = env.admin()
    c.post("/admin/update/schedule", data={"tag": "v1.3.0", "run_at": _future(24), "tz": "UTC"})
    c.post("/admin/update/schedule", data={"tag": "v1.4.0", "run_at": _future(48), "tz": "UTC"})
    rows = dict(_schedules(env))
    assert rows["v1.3.0"] == "superseded"
    assert rows["v1.4.0"] == "pending"


def test_schedule_can_be_cancelled(env):
    _beat(env.control)
    c = env.admin()
    c.post("/admin/update/schedule", data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    r = c.post("/admin/update/schedule/cancel")
    assert r.status_code == 200
    assert dict(_schedules(env))["v1.3.0"] == "cancelled"


def test_schedule_is_audit_logged(env):
    _beat(env.control)
    env.admin().post("/admin/update/schedule",
                     data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert any(e == "admin_update_scheduled" for e, _ in env.audit_events())


@pytest.mark.parametrize("path", ["/admin/update/schedule", "/admin/update/policy"])
def test_schedule_endpoints_are_admin_gated(env, path):
    _beat(env.control)
    r = env.plain().post(path, data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert r.status_code == 403


# ── Policy ───────────────────────────────────────────────────────────────────

def test_policy_saves_and_enables(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": "6", "local_time": "04:00",
        "tz": "America/New_York", "patch_only": "on"})
    assert r.status_code == 200
    p = _policy_row(env)
    assert p.enabled is True and p.weekday == 6
    assert p.local_time == "04:00" and p.timezone == "America/New_York"
    assert p.patch_only is True


def test_unchecked_boxes_turn_features_off(env):
    """An HTML checkbox sends nothing when unchecked — a handler that only reads
    present fields can never turn anything off."""
    _beat(env.control)
    c = env.admin()
    c.post("/admin/update/policy", data={"enabled": "on", "weekday": "6",
                                         "local_time": "04:00", "tz": "UTC",
                                         "patch_only": "on"})
    c.post("/admin/update/policy", data={"weekday": "6", "local_time": "04:00", "tz": "UTC"})
    p = _policy_row(env)
    assert p.enabled is False and p.patch_only is False


def test_enabling_claims_the_current_window(env):
    """Turning the policy on must not instantly deploy because today's window
    already passed. Enabling a schedule should never be indistinguishable from
    pressing Update."""
    _beat(env.control)
    env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": str(datetime.now(timezone.utc).weekday()),
        "local_time": "00:00", "tz": "UTC", "patch_only": "on"})
    assert _policy_row(env).last_fired_window is not None


def test_saving_the_policy_clears_a_pause(env):
    """The operator is looking at the form; an explicit save IS the
    acknowledgement. Otherwise re-enabling would be silently ineffective."""
    _beat(env.control)
    c = env.admin()
    c.post("/admin/update/policy", data={"enabled": "on", "weekday": "6",
                                         "local_time": "04:00", "tz": "UTC"})

    from app.db.models import UpdatePolicy

    async def pause():
        async with env._sessionmaker() as db:
            p = (await db.execute(select(UpdatePolicy))).scalars().first()
            p.paused_reason = "automatic update to v1.3.0 failed"
            p.enabled = False
            await db.commit()
    _run(pause())

    c.post("/admin/update/policy", data={"enabled": "on", "weekday": "6",
                                         "local_time": "04:00", "tz": "UTC"})
    p = _policy_row(env)
    assert p.paused_reason is None and p.enabled is True


def test_policy_rejects_a_bad_timezone(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": "6", "local_time": "04:00", "tz": "Mars/Olympus"})
    assert r.status_code == 400


def test_policy_rejects_a_bad_time(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": "6", "local_time": "25:99", "tz": "UTC"})
    assert r.status_code == 400


def test_policy_is_audit_logged(env):
    _beat(env.control)
    env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": "6", "local_time": "04:00", "tz": "UTC"})
    assert any(e == "admin_update_policy" for e, _ in env.audit_events())


def test_no_push_channel_gets_a_note_not_a_warning_and_gates_nothing(env):
    """Automatic updates are not gated on any external service. With no push
    channel the panel says where results will appear instead, and the policy
    still enables."""
    _beat(env.control)
    r = env.admin().post("/admin/update/policy", data={
        "enabled": "on", "weekday": "6", "local_time": "04:00", "tz": "UTC"})
    assert r.status_code == 200
    assert _policy_row(env).enabled is True
    body = env.admin().get("/admin/update/status").text
    assert "No push channel is set" in body
    assert "in Vigilant only" in body
    assert "No Discord notification is configured" not in body
    assert "banner" in body and "audit log" in body


def _alert_settings(monkeypatch, alert_types):
    import app.notify.discord as discord_notify

    class Fake:
        discord_webhook_url = "https://discord.example/webhook"
        discord_alert_types = alert_types
    monkeypatch.setattr(discord_notify, "get_settings", lambda: Fake)


def test_opting_in_to_release_notices_does_not_count_as_auto_update_reports(env, monkeypatch):
    """The regression the separate type exists to prevent. A webhook that only
    carries "a release is out" notices would drop every run report, so the
    panel must say Discord will not be told."""
    _alert_settings(monkeypatch, "structure_attack,update_available")
    _beat(env.control)
    body = env.admin().get("/admin/update/status").text
    assert "No push channel is set" in body
    assert "is not in <code>DISCORD_ALERT_TYPES</code>" in body


def test_opting_in_to_auto_update_reports_counts_as_a_push_channel(env, monkeypatch):
    _alert_settings(monkeypatch, "structure_attack,auto_update")
    _beat(env.control)
    body = env.admin().get("/admin/update/status").text
    assert "No push channel is set" not in body
    assert "sends to the webhook in <code>DISCORD_WEBHOOK_URL</code>" in body
    assert "Send test notification to Discord" in body


# ── Run reports: the admin banner ────────────────────────────────────────────

def _add_report(env_, outcome="failed", kind="automatic", days_ago=0, **kw):
    from app.db.models import UpdateRunReport

    async def go():
        async with env_._sessionmaker() as db:
            r = UpdateRunReport(
                created_at=(datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None),
                kind=kind, outcome=outcome, from_tag="v1.2.0", to_tag="v1.3.0",
                detail=kw.pop("detail", f"a {outcome} run"), **kw)
            db.add(r)
            await db.commit()
            return r.id
    return _run(go())


def _report_row(env_, rid):
    from app.db.models import UpdateRunReport

    async def go():
        async with env_._sessionmaker() as db:
            return await db.get(UpdateRunReport, rid)
    return _run(go())


def test_the_report_banner_is_empty_for_anonymous_and_plain_users(env):
    _add_report(env, "failed")
    for client in (env.anon(), env.plain()):
        r = client.get("/status/update-reports")
        assert r.status_code == 200
        assert r.text == ""


def test_a_failure_stays_on_the_banner_until_acknowledged(env):
    rid = _add_report(env, "reverted", detail="rolled back to v1.2.0", days_ago=30)
    body = env.admin().get("/status/update-reports").text
    assert "rolled back to v1.2.0" in body
    assert "Acknowledge" in body
    assert 'href="/admin"' in body
    assert f'hx-post="/admin/update/report/{rid}/ack"' in body
    assert "data-alert-id" not in body and 'id="update-banner"' not in body

    r = env.admin().post(f"/admin/update/report/{rid}/ack")
    assert r.status_code == 200
    assert "rolled back to v1.2.0" not in r.text
    assert env.admin().get("/status/update-reports").text == ""
    assert _report_row(env, rid).acknowledged_by == ADMIN_ID
    assert any(e == "admin_update_report_acknowledged" for e, _ in env.audit_events())


def test_a_success_is_dismissible_and_lapses_after_a_week(env):
    fresh = _add_report(env, "succeeded", kind="scheduled", detail="fresh success")
    _add_report(env, "succeeded", detail="old success", days_ago=8)
    body = env.admin().get("/status/update-reports").text
    assert "fresh success" in body and "old success" not in body
    assert "Acknowledge" not in body and "Dismiss" in body

    env.admin().post(f"/admin/update/report/{fresh}/ack")
    assert env.admin().get("/status/update-reports").text == ""


def test_only_an_admin_can_acknowledge(env):
    rid = _add_report(env, "failed")
    assert env.plain().post(f"/admin/update/report/{rid}/ack").status_code == 403
    assert _report_row(env, rid).acknowledged_at is None


def test_acknowledging_needs_the_csrf_token(env):
    rid = _add_report(env, "failed")
    c = env.admin()
    del c.headers["X-CSRF-Token"]
    assert c.post(f"/admin/update/report/{rid}/ack").status_code == 403
    assert _report_row(env, rid).acknowledged_at is None


def test_acknowledging_works_with_no_updater_running(env):
    """A report saying the updater died must still be acknowledgeable."""
    rid = _add_report(env, "skipped")
    assert env.admin().post(f"/admin/update/report/{rid}/ack").status_code == 200


# ── Run reports: the panel's history ─────────────────────────────────────────

def test_the_panel_lists_recent_runs_with_their_deliveries(env):
    _beat(env.control)
    _add_report(env, "succeeded", kind="scheduled",
                deliveries=json.dumps({"webhook": {"state": "sent", "at": "x", "error": None}}))
    _add_report(env, "failed",
                deliveries=json.dumps({"webhook": {"state": "failed", "at": "x",
                                                   "error": "HTTP 500"}}))
    _add_report(env, "skipped")
    body = env.admin().get("/admin/update/status").text
    assert "Recent scheduled and automatic runs" in body
    for needle in ("succeeded", "failed", "skipped", "webhook: sent",
                   "webhook: failed (HTTP 500)", "in Vigilant only"):
        assert needle in body, needle


# ── Push channel settings ────────────────────────────────────────────────────

HOOK = "https://ntfy.example/vigilant-secret-topic-abc123"


def _notify_row(env_):
    from app.db.models import UpdateNotifySettings

    async def q():
        async with env_._sessionmaker() as db:
            return (await db.execute(select(UpdateNotifySettings))).scalars().first()
    return _run(q())


def _save_notify(client, **data):
    form = {"discord_policy": "all", "webhook_url": "", "webhook_format": "json",
            "webhook_policy": "all"}
    form.update(data)
    return client.post("/admin/update/notify", data=form)


def test_a_webhook_is_saved_but_never_rendered_back(env):
    _beat(env.control)
    r = _save_notify(env.admin(), webhook_url=HOOK, webhook_format="ntfy",
                     webhook_policy="problems")
    assert r.status_code == 200
    row = _notify_row(env)
    assert (row.webhook_url, row.webhook_format, row.webhook_policy) == (HOOK, "ntfy", "problems")
    for text in (r.text, env.admin().get("/admin/update/status").text):
        assert "secret-topic" not in text
        assert "https://ntfy.example/" in text                   # the redacted form
    assert all("secret-topic" not in (d or "") for _, d in env.audit_events())


def test_the_webhook_is_encrypted_at_rest(env):
    from sqlalchemy import text

    _beat(env.control)
    _save_notify(env.admin(), webhook_url=HOOK)

    async def raw():
        async with env._sessionmaker() as db:
            return (await db.execute(text("SELECT webhook_url FROM update_notify_settings"))).scalar()
    assert "secret-topic" not in _run(raw())


def test_a_blank_url_keeps_the_webhook_and_remove_clears_it(env):
    _beat(env.control)
    c = env.admin()
    _save_notify(c, webhook_url=HOOK)
    _save_notify(c, webhook_url="", webhook_policy="problems")
    assert _notify_row(env).webhook_url == HOOK
    _save_notify(c, webhook_remove="on")
    assert _notify_row(env).webhook_url is None


@pytest.mark.parametrize("bad", ["ftp://example.com/x", "not a url", "https:///x"])
def test_a_bad_webhook_is_refused_in_the_open_form(env, bad):
    _beat(env.control)
    r = _save_notify(env.admin(), webhook_url=bad)
    assert r.status_code == 400
    assert "webhook:" in r.text
    assert '<details class="updater-notify" open' in r.text
    assert _notify_row(env) is None or _notify_row(env).webhook_url is None


def test_notification_settings_are_admin_only(env):
    _beat(env.control)
    assert _save_notify(env.plain(), webhook_url=HOOK).status_code == 403
    assert env.plain().post("/admin/update/notify/test",
                            data={"channel": "webhook"}).status_code == 403


def test_the_test_button_needs_an_updater(env):
    assert env.admin().post("/admin/update/notify/test",
                            data={"channel": "webhook"}).status_code == 404


def test_the_test_button_sends_and_shows_the_last_delivery(env, monkeypatch):
    import app.ops.update_reports as ur

    posts = []

    class Resp:
        status_code = 200

    class Client:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def post(self, url, **kwargs):
            posts.append(url)
            return Resp()
    monkeypatch.setattr(ur.httpx, "AsyncClient", Client)

    _beat(env.control)
    _save_notify(env.admin(), webhook_url=HOOK, webhook_format="ntfy")
    r = env.admin().post("/admin/update/notify/test", data={"channel": "webhook"})
    assert r.status_code == 200
    assert posts == [HOOK]
    assert "Test notification sent to the webhook" in r.text
    assert "Last delivery:" in r.text and "ok," in r.text
    assert _notify_row(env).webhook_last_ok is True


# ── A refused policy save keeps the form open, with what was typed ───────────

def test_a_refused_policy_save_reopens_the_form_with_the_input(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/policy", data={
        "weekday": "3", "local_time": "25:99", "tz": "Europe/London"})
    assert r.status_code == 400
    assert '<details class="updater-policy" open' in r.text
    assert 'value="25:99"' in r.text
    assert 'value="Europe/London"' in r.text
    assert '<option value="3" selected>' in r.text


def test_a_refused_schedule_keeps_the_typed_time(env):
    _beat(env.control)
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": "tomorrow-ish", "tz": "UTC"})
    assert r.status_code == 400
    assert '<details class="updater-schedule-form" open' in r.text
    assert 'value="tomorrow-ish"' in r.text


def test_an_older_tag_cannot_be_scheduled(env):
    """Only upgrades can be scheduled; going back is the Roll back button."""
    _beat(env.control, current_tag="v1.4.0")
    r = env.admin().post("/admin/update/schedule",
                         data={"tag": "v1.3.0", "run_at": _future(), "tz": "UTC"})
    assert r.status_code == 400
    assert "not newer" in r.text
    assert _schedules(env) == []
