"""Intel pastes are capped and the public scan page renders a bounded page (ISS-168).

POST /intel/parse took a paste of any length: every d-scan line became a
stored item, every local line an ESI lookup, and merges grew a stored scan
without limit. GET /intel/<id> needs no session and rendered every stored
item and the whole raw paste, so one large scan cost its full size on every
view, on the single worker's event loop.

Scans stored before these caps can still be any size; the page renders a
bounded number of their rows and says so. ESI and the SDE are not reached:
the local resolver is stubbed and the temp database has no SDE rows.
"""
import asyncio
import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, DScanResult, User, get_db
from app.routes import dscan as dscan_mod

USER_ID = 9431
_CSRF = "test-csrf-token-intel-caps-0123456789"


def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _cookie(**data):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


class _Env:
    def __init__(self, app, session_local):
        self.app = app
        self.session_local = session_local

    def client(self, signed_in=True):
        c = TestClient(self.app, base_url="https://testserver", follow_redirects=False)
        session = {"csrf_token": _CSRF, **({"user_id": USER_ID} if signed_in else {})}
        c.cookies.set("vigilant_session", _cookie(**session))
        return c

    def post(self, path, **form):
        return self.client().post(path, data=form, headers={"X-CSRF-Token": _CSRF})

    def rows(self):
        async def _rows():
            async with self.session_local() as db:
                return (await db.execute(select(DScanResult))).scalars().all()
        return _run_async(_rows)

    def store(self, scan_id, items, summary, paste="x"):
        async def _store():
            now = datetime.now(timezone.utc)
            async with self.session_local() as db:
                db.add(DScanResult(id=scan_id, paste_data=paste, parsed_json=json.dumps(items),
                                   summary_json=json.dumps(summary), user_id=USER_ID,
                                   created_at=now, expires_at=now + timedelta(hours=24)))
                await db.commit()
        _run_async(_store)


@pytest.fixture
def env(monkeypatch):
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield _Env(main.app, SessionLocal)
    finally:
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)


@pytest.fixture
def resolver(monkeypatch):
    """Stands in for the ESI-backed local resolver and counts its names."""
    asked = []

    async def _resolve(names):
        asked.extend(names)
        return {"type": "local", "total": len(names), "resolved": 0, "unresolved_count": 0,
                "unresolved": [], "characters": [], "by_corp": {}, "by_alliance": {},
                "corp_to_alliance": {}}

    monkeypatch.setattr(dscan_mod, "resolve_local_scan", _resolve)
    return asked


def _dscan_lines(n, start=0):
    return [f"{587 + (i % 3)}\tItem-{start + i:05d}\tRifter\t{i + 1} km" for i in range(n)]


# ── Parse-time caps ─────────────────────────────────────────────────────────

def test_parse_refuses_too_many_lines(env):
    r = env.post("/intel/parse", paste_text="\n".join(_dscan_lines(dscan_mod.INTEL_MAX_LINES + 1)))
    assert r.status_code == 200
    assert f"the limit is {dscan_mod.INTEL_MAX_LINES:,}" in r.text
    assert env.rows() == []


def test_parse_refuses_an_overlong_line(env):
    paste = "\n".join(_dscan_lines(3)) + "\n587\t" + "a" * dscan_mod.INTEL_MAX_LINE_CHARS + "\tRifter\t1 km"
    r = env.post("/intel/parse", paste_text=paste)
    assert r.status_code == 200
    assert "Line 4 is" in r.text
    assert env.rows() == []


def test_parse_refuses_a_paste_over_the_size_limit(env):
    line = "587\t" + "a" * (dscan_mod.INTEL_MAX_LINE_CHARS - 20) + "\tRifter\t1 km"
    lines = dscan_mod.INTEL_MAX_PASTE_CHARS // len(line) + 1
    assert lines <= dscan_mod.INTEL_MAX_LINES   # only the size limit applies
    r = env.post("/intel/parse", paste_text="\n".join([line] * lines))
    assert r.status_code == 200
    assert f"the limit is {dscan_mod.INTEL_MAX_PASTE_CHARS:,}" in r.text
    assert env.rows() == []


def test_local_scan_refuses_too_many_names(env, resolver):
    names = "\n".join(f"Pilot {i:05d}" for i in range(dscan_mod.LOCAL_MAX_NAMES + 1))
    r = env.post("/intel/parse", paste_text=names)
    assert r.status_code == 200
    assert f"{dscan_mod.LOCAL_MAX_NAMES:,}" in r.text
    assert resolver == [] and env.rows() == []


def test_a_normal_paste_is_still_stored(env, resolver):
    r = env.post("/intel/parse", paste_text="\n".join(_dscan_lines(20)))
    assert r.status_code == 303 and r.headers["location"].startswith("/intel/")
    (row,) = env.rows()
    assert len(json.loads(row.parsed_json)) == 20

    r = env.post("/intel/parse", paste_text="Pilot One\nPilot Two")
    assert r.status_code == 303 and resolver == ["Pilot One", "Pilot Two"]


# ── Merges ──────────────────────────────────────────────────────────────────

def _items(n):
    return [{"type_id": 30000 + i, "type_name": f"Item-{i:05d}", "distance_m": None,
             "distance_str": "-", "group_id": None, "hull_name": f"Item-{i:05d}",
             "category": "Other"} for i in range(n)]


def test_merge_refuses_a_paste_over_the_limits(env):
    env.store("mergecap", _items(3), dscan_mod.build_dscan_summary(_items(3)))
    r = env.post("/intel/mergecap/merge", paste_text="\n".join(_dscan_lines(dscan_mod.INTEL_MAX_LINES + 1)),
                 dedup="off")
    assert r.status_code == 303 and r.headers["location"] == "/intel/mergecap?merge_error=paste"
    (row,) = env.rows()
    assert len(json.loads(row.parsed_json)) == 3


def test_merge_refuses_to_grow_a_scan_past_the_item_cap(env):
    full = _items(dscan_mod.INTEL_MAX_ITEMS - 2)
    env.store("mergefull", full, dscan_mod.build_dscan_summary(full))
    r = env.post("/intel/mergefull/merge", paste_text="\n".join(_dscan_lines(5)), dedup="off")
    assert r.status_code == 303 and r.headers["location"] == "/intel/mergefull?merge_error=full"
    (row,) = env.rows()
    assert len(json.loads(row.parsed_json)) == len(full)

    page = env.client().get(r.headers["location"])
    assert f"at most {dscan_mod.INTEL_MAX_ITEMS:,} items" in page.text


def test_merge_still_adds_a_small_paste(env):
    env.store("mergeok", _items(3), dscan_mod.build_dscan_summary(_items(3)))
    r = env.post("/intel/mergeok/merge", paste_text="\n".join(_dscan_lines(5)), dedup="off")
    assert r.status_code == 303 and r.headers["location"] == "/intel/mergeok"
    (row,) = env.rows()
    assert len(json.loads(row.parsed_json)) == 8


# ── The public page ─────────────────────────────────────────────────────────

def test_public_dscan_page_renders_a_bounded_number_of_rows(env):
    stored = dscan_mod.INTEL_RENDER_ROWS + 500     # a scan stored before the caps
    items = _items(stored)
    paste = "z" * (dscan_mod.INTEL_MAX_STORED_PASTE_CHARS + 1000)
    env.store("bigscan1", items, dscan_mod.build_dscan_summary(items), paste=paste)

    r = env.client(signed_in=False).get("/intel/bigscan1")
    assert r.status_code == 200
    assert r.text.count(">Item-") == dscan_mod.INTEL_RENDER_ROWS
    assert f"Showing the first {dscan_mod.INTEL_RENDER_ROWS:,} of {stored:,}" in r.text
    assert f"Other on Scan ({stored})" in r.text      # the counts still cover every item
    assert r.text.count("z" * 1000) <= dscan_mod.INTEL_MAX_STORED_PASTE_CHARS // 1000
    assert "Raw paste cut off" in r.text


def test_public_local_page_renders_a_bounded_number_of_rows(env):
    stored = dscan_mod.INTEL_RENDER_ROWS + 500
    chars = [{"name": f"Pilot {i:05d}", "character_id": 90000000 + i, "corporation_id": 98000001,
              "corporation_name": "Corp", "alliance_id": None, "alliance_name": None}
             for i in range(stored)]
    summary = {"type": "local", "total": stored, "resolved": stored, "unresolved_count": 0,
               "unresolved": [], "characters": [], "by_corp": {"Corp": stored},
               "by_alliance": {}, "corp_to_alliance": {"Corp": None}}
    env.store("bigscan2", chars, summary)

    r = env.client(signed_in=False).get("/intel/bigscan2")
    assert r.status_code == 200
    assert r.text.count("b-table-row intel-pilot-row") == dscan_mod.INTEL_RENDER_ROWS
    assert f"Showing the first {dscan_mod.INTEL_RENDER_ROWS:,} of {stored:,}" in r.text
