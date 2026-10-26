"""The alliance page's Recent Changes list: when each change happened.

ISS-129a: `changed_at` is stored as naive UTC, and the API sent it with
`isoformat()`, which has no zone. The page reads it with `new Date(...)`,
which takes a zoneless date-time as the viewer's local time, then prints it
with `toISOString()` (UTC): a change at 00:15 UTC showed as 04:15 to a
viewer in New York. The API now sends an explicit UTC offset, so the page
shows the UTC time it means to whatever the viewer's zone.

ISS-129b: the rows are a template string written with innerHTML, and the
system and region names (SDE, sent by the API), the counterparty's name (ESI),
the direction and the counterparty id went in unescaped. Each is now
escaped (the id is URL-encoded in the link), so a name renders as text and
adds no elements. Ordinary names give the same markup as before.

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
from html.parser import HTMLParser
from urllib.parse import quote

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models as models
import app.db.sde_models  # noqa: F401  (registers the SDE tables)
from app.db.models import Base, SovereigntyChangeEvent
from app.db.sde_models import SDERegion, SDESystem
from app.routes import starmap as starmap_mod
from tests._mobile import cells_rows, render_page, row_keys, row_labelled, row_lead

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
            db.add(SDERegion(region_id=10000001, region_name="Sample Region"))
            # 30000002 is left out of the SDE: its names come back null.
            db.add(SDESystem(system_id=30000001, system_name="SMP-01",
                             region_id=10000001))
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
        assert set(c) == {"system_id", "system_name", "region_name", "changed_at",
                          "old_alliance_id", "new_alliance_id", "direction"}
        sent = datetime.fromisoformat(c["changed_at"])
        assert sent.utcoffset() == timedelta(0), c["changed_at"]
        assert sent == stored.replace(tzinfo=timezone.utc)
    assert changes[0]["changed_at"] == _WHEN.isoformat() + "+00:00"
    assert [c["direction"] for c in changes] == ["gain", "loss"]


def test_names_come_from_the_sde(sov_db):
    known, unknown = _payload()["recent_changes"]
    assert (known["system_name"], known["region_name"]) == ("SMP-01", "Sample Region")
    assert (unknown["system_name"], unknown["region_name"]) == (None, None)


def test_alliance_label_slice_covers_later_rows(sov_db, monkeypatch):
    """Names are looked up for the same rows the payload sends, not just the first."""
    async def add():
        async with models.AsyncSessionLocal() as db:
            db.add(SDESystem(system_id=30000003, system_name="SMP-03", region_id=10000001))
            db.add(SovereigntyChangeEvent(system_id=30000003, old_alliance_id=OTHER,
                                          new_alliance_id=ALLIANCE,
                                          changed_at=_WHEN - timedelta(hours=6)))
            await db.commit()
    _run(add())
    last = _payload()["recent_changes"][2]
    assert (last["system_id"], last["system_name"]) == (30000003, "SMP-03")


# ── the page script under node ────────────────────────────────────────

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [srcPath, fxPath] = process.argv.slice(-2);
const src = fs.readFileSync(srcPath, 'utf8');
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));

const els = {};
const el = id => (els[id] = els[id] || { id, textContent: '', innerHTML: '', src: '' });
const urls = [];
const window = { mRowInit() {}, initToggleExpandedAria() {} };
const ok = body => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
function fetch(url) {
  urls.push(url);
  if (url.startsWith('/api/map/alliance/')) return ok(fx.detail);
  if (url.startsWith('/api/map/alliances?ids=')) return ok(fx.names);
  return Promise.resolve({ ok: false, json: () => Promise.resolve(null) });
}
const sandbox = { window, document: { getElementById: el }, fetch, console };
vm.createContext(sandbox);
Promise.resolve(vm.runInContext(src, sandbox)).then(() => {
  process.stdout.write(JSON.stringify({ html: el('changes-list').innerHTML, urls }));
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
    """run_page(detail, names, tz) -> the list's innerHTML."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("alliance-changes")
    (d / "harness.js").write_text(_HARNESS)
    (d / "page.js").write_text(_page_script())
    count = [0]

    def run(detail, names, tz="UTC"):
        count[0] += 1
        fx = d / f"fx{count[0]}.json"
        fx.write_text(json.dumps({"detail": detail, "names": names}))
        res = subprocess.run(["node", str(d / "harness.js"), str(d / "page.js"), str(fx)],
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", ""), "TZ": tz})
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        # Names come from the payload: no map data file, however it is spelled.
        assert not [u for u in out["urls"] if "systems" in u or "/map/data/" in u], out["urls"]
        return out["html"]
    return run


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_when_shows_the_utc_time_in_any_viewer_zone(sov_db, run_page, tz):
    detail = _payload()
    html = run_page(detail, {str(OTHER): "Sample Counterparty Alliance"}, tz)
    rows = cells_rows(html)
    assert len(rows) == 2
    for row, stored in zip(rows, (_WHEN, _WHEN - timedelta(hours=3))):
        assert row_labelled(row)["When"]["text"] == stored.strftime("%Y-%m-%d %H:%M")


# ── hostile names (ISS-129b) ──────────────────────────────────────────

HOSTILE_SYS = '<img src=x id="pwn-sys">Sample & Co'
HOSTILE_REG = 'Region "Q" \'A\' <b>bold</b>'
HOSTILE_ALLIANCE = 'A&amp;B <script>alert(1)</script> "q"'
HOSTILE_DIR = 'loss" onmouseover="alert(1)'
HOSTILE_ID = '1"><img src=x id="pwn-id">'


def _detail(second_id, second_direction, first_names=("SMP-01", "Sample Region")):
    """A gain from OTHER in system 30000001 (named by first_names), then a
    change in 30000002 whose counterparty id and direction are the caller's."""
    return {"name": "Sample Alliance", "ticker": "SMPL", "sov_system_count": 2,
            "sov_gained_7d": 1, "sov_lost_7d": 1, "date_founded": None,
            "recent_changes": [
                {"system_id": 30000001, "system_name": first_names[0],
                 "region_name": first_names[1], "changed_at": "2026-10-05T00:15:00+00:00",
                 "old_alliance_id": OTHER, "new_alliance_id": ALLIANCE, "direction": "gain"},
                {"system_id": 30000002, "system_name": "SMP-02",
                 "region_name": "Sample Region", "changed_at": "2026-10-04T21:15:00+00:00",
                 "old_alliance_id": ALLIANCE, "new_alliance_id": second_id,
                 "direction": second_direction},
            ]}


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, sorted(k for k, _ in attrs)))


def _tags(html):
    p = _Tags()
    p.feed(html)
    return p.tags


def _normal(run_page):
    return run_page(_detail(99000003, "loss"),
                    {str(OTHER): "Sample Counterparty Alliance"})


def _hostile(run_page):
    return run_page(_detail(HOSTILE_ID, HOSTILE_DIR, (HOSTILE_SYS, HOSTILE_REG)),
                    {str(OTHER): HOSTILE_ALLIANCE})


def test_hostile_names_add_no_elements_or_attributes(run_page):
    hostile = _tags(_hostile(run_page))
    assert hostile == _tags(_normal(run_page))
    assert {t for t, _ in hostile} <= {"div", "span", "a"}
    assert not [a for _, attrs in hostile for a in attrs if a.startswith("on") or a == "id"]


def test_hostile_names_render_as_their_own_text(run_page):
    gain, change = cells_rows(_hostile(run_page))
    cells = row_labelled(gain)
    assert cells["Name"]["text"] == HOSTILE_SYS
    assert cells["Region"]["text"] == HOSTILE_REG
    k1, k2 = row_keys(gain)
    assert k1["text"] == HOSTILE_SYS
    assert k2["text"] == f"from {HOSTILE_ALLIANCE}"
    assert k2["kids"] == [{"class": "b-text", "href": f"/alliance/{OTHER}"}]
    # The direction stays inside the class attribute; the id is URL-encoded.
    (lead,) = row_lead(change)
    assert lead["attrs"] == {"class": f"b-change-dir {HOSTILE_DIR}", "data-m": "lead"}
    k1, k2 = row_keys(change)
    assert k2["text"] == f"to Alliance {HOSTILE_ID}"
    assert k2["kids"] == [{"class": "b-text", "href": "/alliance/" + quote(HOSTILE_ID, safe="")}]
