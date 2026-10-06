"""The alliance page's Recent Changes list: when each change happened.

ISS-129a: `changed_at` is stored as naive UTC, and the API sent it with
`isoformat()`, which has no zone. The page reads it with `new Date(...)`,
which takes a zoneless date-time as the viewer's local time, then prints it
with `toISOString()` (UTC): a change at 00:15 UTC showed as 04:15 to a
viewer in New York. The API now sends an explicit UTC offset, so the page
shows the UTC time it means to whatever the viewer's zone.

The page script runs under node against a stub DOM and stub fetch() (the
same approach as tests/test_mobile_r5_maps_alliance.py), with TZ set so the
result does not depend on the machine's zone. Names and ids are invented."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models as models
from app.db.models import Base, SovereigntyChangeEvent
from app.routes import starmap as starmap_mod
from tests._mobile import cells_rows, render_page, row_labelled

_NS = types.SimpleNamespace

ALLIANCE = 99000001
OTHER = 99000002


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── the API ───────────────────────────────────────────────────────────


class _NoESI:
    """Stands in for httpx.AsyncClient: the alliance header lookup gets a 404."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path):
        return _NS(status_code=404, json=lambda: {})


# Yesterday 00:15 UTC (naive, as stored): early enough in the UTC day that a
# zone west of UTC would move it to the day before.
_WHEN = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    hour=0, minute=15, second=7, microsecond=123456, tzinfo=None)


@pytest.fixture
def sov_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(SovereigntyChangeEvent(system_id=30000001, old_alliance_id=OTHER,
                                          new_alliance_id=ALLIANCE, changed_at=_WHEN))
            db.add(SovereigntyChangeEvent(system_id=30000002, old_alliance_id=ALLIANCE,
                                          new_alliance_id=None,
                                          changed_at=_WHEN - timedelta(hours=3)))
            await db.commit()
    _run(seed())
    # The route imports AsyncSessionLocal at call time, from app.db.models.
    monkeypatch.setattr(models, "AsyncSessionLocal", SessionLocal)
    monkeypatch.setattr(starmap_mod, "httpx", _NS(AsyncClient=_NoESI))
    yield
    _run(engine.dispose())


def _payload():
    resp = _run(starmap_mod.alliance_detail(ALLIANCE, _NS(session={"user_id": 1})))
    assert resp.status_code == 200
    return json.loads(resp.body)


def test_changed_at_is_sent_as_utc(sov_db):
    changes = _payload()["recent_changes"]
    assert [c["system_id"] for c in changes] == [30000001, 30000002]
    for c, stored in zip(changes, (_WHEN, _WHEN - timedelta(hours=3))):
        assert set(c) == {"system_id", "changed_at", "old_alliance_id", "new_alliance_id", "direction"}
        sent = datetime.fromisoformat(c["changed_at"])
        assert sent.utcoffset() == timedelta(0), c["changed_at"]
        assert sent == stored.replace(tzinfo=timezone.utc)
    assert changes[0]["changed_at"] == _WHEN.isoformat() + "+00:00"
    assert [c["direction"] for c in changes] == ["gain", "loss"]


# ── the page script under node ────────────────────────────────────────

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [srcPath, fxPath] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));

const els = {};
const el = id => (els[id] = els[id] || { id, textContent: '', innerHTML: '', src: '' });
const window = { mRowInit() {}, initToggleExpandedAria() {} };
const ok = body => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
function fetch(url) {
  if (url.startsWith('/api/map/alliance/')) return ok(fx.detail);
  if (url === '/map/data/systems.json') return ok(fx.systems);
  if (url.startsWith('/api/map/alliances?ids=')) return ok(fx.names);
  return Promise.resolve({ ok: false, json: () => Promise.resolve(null) });
}
const sandbox = { window, document: { getElementById: el }, fetch, console };
vm.createContext(sandbox);
Promise.resolve(vm.runInContext(src, sandbox)).then(() => {
  process.stdout.write(JSON.stringify({ html: el('changes-list').innerHTML }));
}, err => { console.error(err); process.exit(1); });
"""


def _page_script():
    html = render_page(starmap_mod, "alliance_detail.html", f"/alliance/{ALLIANCE}",
                       alliance_id=ALLIANCE)
    scripts = [s for s in re.findall(r"<script nonce=\"test-nonce\">(.*?)</script>", html, re.S)
               if "changes-list" in s]
    assert len(scripts) == 1
    return scripts[0]


@pytest.fixture(scope="module")
def run_page(tmp_path_factory):
    """run_page(detail, systems, names, tz) -> the list's innerHTML."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("alliance-changes")
    (d / "harness.js").write_text(_HARNESS)
    (d / "page.js").write_text(_page_script())
    count = [0]

    def run(detail, systems, names, tz="UTC"):
        count[0] += 1
        fx = d / f"fx{count[0]}.json"
        fx.write_text(json.dumps({"detail": detail, "systems": systems, "names": names}))
        res = subprocess.run(["node", str(d / "harness.js"), str(d / "page.js"), str(fx)],
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", ""), "TZ": tz})
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)["html"]
    return run


_SYSTEMS = [{"id": 30000001, "name": "SMP-01", "regName": "Sample Region"},
            {"id": 30000002, "name": "SMP-02", "regName": "Sample Region"}]


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_when_shows_the_utc_time_in_any_viewer_zone(sov_db, run_page, tz):
    detail = _payload()
    html = run_page(detail, _SYSTEMS, {str(OTHER): "Sample Counterparty Alliance"}, tz)
    rows = cells_rows(html)
    assert len(rows) == 2
    for row, stored in zip(rows, (_WHEN, _WHEN - timedelta(hours=3))):
        assert row_labelled(row)["When"]["text"] == stored.strftime("%Y-%m-%d %H:%M")
