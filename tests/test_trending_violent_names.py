"""Trending's violent list and the alliance page get system and region names
from the API (ISS-139), not from the shipped map data file.

Before, both pages downloaded the 1.3 MB /map/data/systems.json to label at
most 50 or 100 rows, and could go stale against the SDE. The API now resolves
names from sde_systems / sde_regions and sends them as system_name and
region_name (null when unknown). This covers the lookup helper, the trending
endpoint, the trending script under node (a stub DOM and stub fetch(), as in
tests/test_alliance_recent_changes.py) and the escaping of those names.
Names and ids are invented."""
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

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models as models
import app.db.sde_models  # noqa: F401  (registers the SDE tables)
from app.db.models import Base, SystemActivitySnapshot
from app.db.sde_models import SDERegion, SDESystem
from app.routes import starmap as starmap_mod
from app.sde.lookup import system_labels
from tests._mobile import cells_rows, render_page, row_keys, row_labelled, source

_NS = types.SimpleNamespace

KNOWN = 30000001
NO_REGION = 30000002
UNKNOWN = 30000003


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def sessionmaker_(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(create())
    # The route imports AsyncSessionLocal at call time, from app.db.models.
    monkeypatch.setattr(models, "AsyncSessionLocal", SessionLocal)
    yield SessionLocal
    _run(engine.dispose())


async def _seed_sde(SessionLocal):
    async with SessionLocal() as db:
        db.add(SDERegion(region_id=10000001, region_name="Sample Region"))
        db.add(SDESystem(system_id=KNOWN, system_name="SMP-01", region_id=10000001))
        # A region id with no matching region row.
        db.add(SDESystem(system_id=NO_REGION, system_name="SMP-02", region_id=10999999))
        await db.commit()


# ── the helper ────────────────────────────────────────────────────────


def test_system_labels_known_unknown_and_missing_region(sessionmaker_):
    async def go():
        await _seed_sde(sessionmaker_)
        async with sessionmaker_() as db:
            return await system_labels(db, [KNOWN, NO_REGION, UNKNOWN])
    assert _run(go()) == {KNOWN: ("SMP-01", "Sample Region"), NO_REGION: ("SMP-02", None)}


def test_system_labels_empty_input_and_empty_tables(sessionmaker_):
    async def go():
        async with sessionmaker_() as db:
            return (await system_labels(db, []), await system_labels(db, [KNOWN]))
    assert _run(go()) == ({}, {})


# ── the API ───────────────────────────────────────────────────────────


def _violent(seed_sde=True):
    async def seed():
        if seed_sde:
            await _seed_sde(models.AsyncSessionLocal)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with models.AsyncSessionLocal() as db:
            for sid, ships, pods in ((KNOWN, 5, 1), (UNKNOWN, 9, 0), (NO_REGION, 2, 0)):
                db.add(SystemActivitySnapshot(system_id=sid, captured_at=now - timedelta(hours=1),
                                              ship_kills=ships, pod_kills=pods))
            await db.commit()
    return seed


def _payload():
    resp = _run(starmap_mod.trending_violent(_NS(session={"user_id": 1})))
    assert resp.status_code == 200
    return json.loads(resp.body)["systems"]


def test_trending_violent_carries_names(sessionmaker_):
    _run(_violent()())
    systems = _payload()
    # Ordering (kills, descending) is unchanged by the lookup.
    assert [s["system_id"] for s in systems] == [UNKNOWN, KNOWN, NO_REGION]
    by_id = {s["system_id"]: s for s in systems}
    assert (by_id[KNOWN]["system_name"], by_id[KNOWN]["region_name"]) == ("SMP-01", "Sample Region")
    assert (by_id[NO_REGION]["system_name"], by_id[NO_REGION]["region_name"]) == ("SMP-02", None)
    assert (by_id[UNKNOWN]["system_name"], by_id[UNKNOWN]["region_name"]) == (None, None)
    assert by_id[KNOWN]["ship_kills"] == 5 and by_id[KNOWN]["pod_kills"] == 1


def test_trending_violent_with_empty_sde_gives_null_names(sessionmaker_):
    _run(_violent(seed_sde=False)())
    systems = _payload()
    assert len(systems) == 3
    assert all(s["system_name"] is None and s["region_name"] is None for s in systems)


def test_a_lookup_failing_after_using_the_session_is_logged(sessionmaker_, monkeypatch, caplog):
    import app.sde.lookup as lookup
    from sqlalchemy import text

    async def half_done(db, ids):
        await db.execute(text("select * from no_such_table"))
    monkeypatch.setattr(lookup, "system_labels", half_done)
    _run(_violent()())
    with caplog.at_level("INFO", logger=starmap_mod.log.name):
        systems = _payload()
    assert len(systems) == 3
    assert all(s["system_name"] is None and s["region_name"] is None for s in systems)
    assert [r for r in caplog.records
            if r.levelname == "WARNING" and "system label lookup failed" in r.getMessage()]


def test_a_failing_lookup_does_not_break_the_endpoint(sessionmaker_, monkeypatch):
    import app.sde.lookup as lookup

    async def boom(db, ids):
        raise RuntimeError("sde unavailable")
    monkeypatch.setattr(lookup, "system_labels", boom)
    _run(_violent()())
    systems = _payload()
    assert len(systems) == 3
    assert all(s["system_name"] is None for s in systems)


# ── no map data fetch ─────────────────────────────────────────────────


@pytest.mark.parametrize("template", ["trending.html", "alliance_detail.html"])
def test_templates_do_not_fetch_the_map_data_file(template):
    src = source(template)
    assert "/map/data/" not in src
    assert "systems.json" not in src


# ── the trending script under node ────────────────────────────────────

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
  if (url === '/api/map/trending/sov') return ok(fx.sov);
  if (url === '/api/map/trending/violent') return ok(fx.violent);
  if (url.startsWith('/api/map/alliances?ids=')) return ok(fx.names);
  return Promise.resolve({ ok: false, json: () => Promise.resolve(null) });
}
const sandbox = { window, document: { getElementById: el }, fetch, console };
vm.createContext(sandbox);
Promise.resolve(vm.runInContext(src, sandbox)).then(() => {
  process.stdout.write(JSON.stringify({ html: el('violent-list').innerHTML,
    sov: el('sov-gained').innerHTML, urls }));
}, err => { console.error(err); process.exit(1); });
"""


def _page_script():
    html = render_page(starmap_mod, "trending.html", "/trending")
    scripts = [s for s in re.findall(r"<script nonce=\"test-nonce\">(.*?)</script>", html, re.S)
               if "violent-list" in s]
    assert len(scripts) == 1
    return scripts[0]


@pytest.fixture(scope="module")
def run_page(tmp_path_factory):
    """run_page(systems, names=None) -> {"html": the violent list, "sov": the
    sov-gained list, "urls": every fetch}."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("trending")
    (d / "harness.js").write_text(_HARNESS)
    (d / "page.js").write_text(_page_script())
    count = [0]

    def run(systems, names=None, sov=None):
        count[0] += 1
        fx = d / f"fx{count[0]}.json"
        fx.write_text(json.dumps({
            "sov": sov or {"total_changes": 0, "top_gained": [], "top_lost": []},
            "violent": {"since": "2026-10-05T00:00:00+00:00", "systems": systems},
            "names": names or {}}))
        res = subprocess.run(["node", str(d / "harness.js"), str(d / "page.js"), str(fx)],
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": os.environ.get("PATH", "")})
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)
    return run


def _sys(sid, name, region, ships=7, pods=2):
    return {"system_id": sid, "system_name": name, "region_name": region,
            "ship_kills": ships, "pod_kills": pods, "npc_kills": 0, "jumps": 0}


def test_names_render_from_the_payload_and_nothing_else_is_fetched(run_page):
    out = run_page([_sys(KNOWN, "SMP-01", "Sample Region"), _sys(UNKNOWN, None, None)])
    assert not [u for u in out["urls"] if "/map/data/" in u or "systems.json" in u]
    known, unknown = cells_rows(out["html"])
    cells = row_labelled(known)
    assert cells["Name"]["text"] == "SMP-01"
    assert cells["Region"]["text"] == "Sample Region"
    assert row_keys(known)[0]["text"] == "SMP-01"
    # Missing names: the "System N" fallback, and a dash for the region.
    cells = row_labelled(unknown)
    assert cells["Name"]["text"] == f"System {UNKNOWN}"
    assert cells["Region"]["text"] == "—"


# ── hostile names (ISS-129 parity) ────────────────────────────────────

HOSTILE_SYS = '<img src=x id="pwn-sys">Sample & Co'
HOSTILE_REG = 'Region "Q" \'A\' <b>bold</b>'


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


def test_hostile_names_add_no_elements_and_render_as_text(run_page):
    normal = run_page([_sys(KNOWN, "SMP-01", "Sample Region")])["html"]
    hostile = run_page([_sys(KNOWN, HOSTILE_SYS, HOSTILE_REG)])["html"]
    assert _tags(hostile) == _tags(normal)
    (row,) = cells_rows(hostile)
    cells = row_labelled(row)
    assert cells["Name"]["text"] == HOSTILE_SYS
    assert cells["Region"]["text"] == HOSTILE_REG
    assert row_keys(row)[0]["text"] == HOSTILE_SYS


def test_hostile_alliance_names_add_no_elements(run_page):
    hostile = '<img src=x id="pwn">Sample'
    sov = {"total_changes": 1, "top_gained": [[99000002, 3]], "top_lost": []}
    out = run_page([_sys(KNOWN, "SMP-01", "Sample Region")],
                   names={"99000002": hostile}, sov=sov)
    tags = _tags(out["sov"])
    assert "img" not in [t for t, _ in tags]
    assert not [a for _, attrs in tags for a in attrs if a == "id"]
    assert "&lt;img" in out["sov"]
    # The violent list still renders after the alliance lists (esc is in scope).
    assert len(cells_rows(out["html"])) == 1
