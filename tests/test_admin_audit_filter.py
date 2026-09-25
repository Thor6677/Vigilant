"""T-068: the admin audit log's filter options match what is actually recorded.

The options used to be typed into the template by hand. Three of the six
(login, sync_error, token_refresh_failed) named event types nothing writes, so
they always showed an empty list, while most types that ARE written had no
option. The options now come from AUDIT_FILTERS in app/routes/admin.py, and
this file scans every audit writer in app/ so the two cannot drift apart again.
"""
import ast
import asyncio
import base64
import json
import pathlib
import tempfile

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import AdminAuditLog, Base, User, get_db
from app.routes.admin import AUDIT_FILTERS, audit_group

APP = pathlib.Path("app")


def _written_event_types():
    """Every event type an audit writer in app/ can produce.

    A writer is `AdminAuditLog(event_type=...)`, or any function that passes one
    of its own parameters on as the event type (`_log_audit`, `_submit`, and any
    wrapper added later), found by repeating the scan until nothing new turns
    up. Returns (constants, dynamic, unresolved): literal types (including both
    arms of a conditional), f-string writers as (file, line), and call sites
    whose type this scan cannot read.
    """
    trees = {str(path): ast.parse(path.read_text()) for path in sorted(APP.rglob("*.py"))}
    # writer name -> (positional index or None, keyword name)
    writers = {"AdminAuditLog": (None, "event_type")}

    def call_name(node):
        return node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")

    def event_arg(node, spec):
        index, kw = spec
        if index is not None and len(node.args) > index:
            return node.args[index]
        return next((k.value for k in node.keywords if k.arg == kw), None)

    changed = True
    while changed:
        changed = False
        for tree in trees.values():
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in writers:
                    continue
                params = [a.arg for a in fn.args.args]
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and call_name(node) in writers:
                        value = event_arg(node, writers[call_name(node)])
                        if isinstance(value, ast.Name) and value.id in params:
                            writers[fn.name] = (params.index(value.id), value.id)
                            changed = True
                            break

    constants, dynamic, unresolved = set(), [], []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and call_name(node) in writers):
                continue
            value = event_arg(node, writers[call_name(node)])
            if value is None:
                continue
            where = (path, node.lineno)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants.add(value.value)
            elif isinstance(value, ast.IfExp) and all(
                    isinstance(v, ast.Constant) for v in (value.body, value.orelse)):
                constants.update({value.body.value, value.orelse.value})
            elif isinstance(value, ast.JoinedStr):
                dynamic.append(where)
            elif isinstance(value, ast.Name) and any(
                    spec[1] == value.id for spec in writers.values()):
                continue    # a wrapper passing its own parameter on; its callers are scanned
            else:
                unresolved.append(where)
    return constants, dynamic, unresolved


def test_every_writer_is_readable_by_this_scan():
    _constants, dynamic, unresolved = _written_event_types()
    assert not unresolved, f"audit writer(s) whose event type this test cannot read: {unresolved}"
    # The one f-string writer: update reports, "<auto|scheduled>_update_<outcome>".
    assert [f for f, _line in dynamic] == ["app/ops/update_reports.py"], dynamic
    for prefix in ("auto", "scheduled"):
        for outcome in ("succeeded", "failed", "rolled_back", "skipped"):
            assert audit_group(f"{prefix}_update_{outcome}") == "updates"


def test_every_written_event_type_has_exactly_one_filter_group():
    constants, _dynamic, _unresolved = _written_event_types()
    assert len(constants) >= 15, constants        # the scan is actually finding writers
    for t in sorted(constants):
        groups = [k for k, _label, prefixes in AUDIT_FILTERS
                  if any(t == p or t.startswith(p + "_") for p in prefixes)]
        assert len(groups) == 1, f"event type {t!r} is in groups {groups} — expected exactly one"


def test_every_filter_group_matches_something_written():
    constants, _dynamic, _unresolved = _written_event_types()
    written_groups = {audit_group(t) for t in constants} | {"updates"}  # + the f-string writer
    for key, label, _prefixes in AUDIT_FILTERS:
        assert key in written_groups, f"filter {label!r} matches no event type anything writes"


def test_the_retired_options_are_gone():
    keys = {k for k, _l, _p in AUDIT_FILTERS}
    assert not keys & {"login", "sync_error", "token_refresh_failed"}
    tmpl = pathlib.Path("app/templates/partials/admin_audit.html").read_text()
    for dead in ("login", "sync_error", "token_refresh_failed"):
        assert f'value="{dead}"' not in tmpl


# ── The route ───────────────────────────────────────────────────────────────

ADMIN_ID = 711
_SEEDED = (
    "permissions_changed", "admin_allowlist_add", "admin_set_role",
    "admin_update_requested", "admin_rollback_requested", "auto_update_succeeded",
    "scheduled_update_requested",
    "admin_sde_update", "admin_cache_purge",
    # Decoy: matches "admin_update_%" only if "_" is left as a LIKE wildcard.
    "adminXupdateXrequested",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def admin_client():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=ADMIN_ID, role="admin", is_admin=True))
            for t in _SEEDED:
                db.add(AdminAuditLog(user_id=ADMIN_ID, event_type=t, detail=f"detail-{t}"))
            await db.commit()
    _run(seed())

    async def _override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = _override
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_id": ADMIN_ID}).encode())).decode()
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    yield c
    main.app.dependency_overrides.pop(get_db, None)


def _shown(r):
    return {t for t in _SEEDED if f"detail-{t}" in r.text}


def test_updates_filter_shows_every_update_event_and_nothing_else(admin_client):
    r = admin_client.get("/admin/section/audit?filter=updates")
    assert r.status_code == 200
    assert _shown(r) == {"admin_update_requested", "admin_rollback_requested",
                         "auto_update_succeeded", "scheduled_update_requested"}


def test_each_group_filters_to_its_own_events(admin_client):
    expected = {
        "permissions": {"permissions_changed"},
        "allowlist": {"admin_allowlist_add"},
        "users": {"admin_set_role"},
        "sde": {"admin_sde_update"},
        "cache": {"admin_cache_purge"},
        "syncs": set(),
    }
    for key, want in expected.items():
        r = admin_client.get(f"/admin/section/audit?filter={key}")
        assert _shown(r) == want, key


def test_empty_group_names_its_label(admin_client):
    r = admin_client.get("/admin/section/audit?filter=syncs")
    assert "No audit events in Syncs." in r.text


def test_a_retired_or_unknown_filter_shows_everything(admin_client):
    for stale in ("login", "token_refresh_failed", "nonsense"):
        r = admin_client.get(f"/admin/section/audit?filter={stale}")
        assert _shown(r) == set(_SEEDED), stale
        assert '<option value="" selected>' in " ".join(r.text.split())


def test_the_select_offers_exactly_the_filter_groups(admin_client):
    r = admin_client.get("/admin/section/audit")
    for key, label, _p in AUDIT_FILTERS:
        assert f'value="{key}"' in r.text and label.replace("&", "&amp;") in r.text
    for dead in ("login", "sync_error", "token_refresh_failed"):
        assert f'value="{dead}"' not in r.text
