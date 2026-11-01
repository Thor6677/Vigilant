"""T-090 PR 1: EVE Ref first-seen ingest, the tail bound it feeds, and the
admin panel. Every id and date is invented. HTTP is stubbed with
httpx.MockTransport; nothing leaves the process."""
import ast
import bz2
import json
import pathlib
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

import app.db.models as models
from app.db.models import (StructureAgeCalibration, StructureFirstSeen,
                           StructureFirstSeenImport, StructureAgeSubmission)
from app.intel import everef_structures as ev
from app.intel import structure_age_check as sac
from app.intel import structure_first_seen as sfs
from app.routes import structure_age as sa_mod
from tests._structure_age import (NEWEST_ID, POINTS, RATE, client, make_db, override_db,
                                  point_id, run, today)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TYPE = 35832
D = datetime
NAME1 = "structures-2026-02-01_00-00-00.v2.json.bz2"


def _doc(entries):
    """entries: {id: first_seen string, None, or 'MISSING' to omit the key}"""
    out = {}
    for sid, fs in entries.items():
        v = {"structure_id": sid, "type_id": TYPE,
             "last_seen_public_structure": "2026-10-01T00:00:00Z"}
        if fs != "MISSING":
            v["first_seen"] = fs
        out[str(sid)] = v
    return out


def _bz(doc):
    return bz2.compress(json.dumps(doc).encode())


@pytest.fixture
def db(monkeypatch):
    engine, SL = run(make_db(users=[(4501, "admin")]))
    monkeypatch.setattr(models, "AsyncSessionLocal", SL)
    yield SL
    run(engine.dispose())


def _first_seen(SL):
    async def go():
        async with SL() as s:
            return {r.structure_id: r for r in (await s.execute(select(StructureFirstSeen))).scalars()}
    return run(go())


def _count(SL, model):
    async def go():
        async with SL() as s:
            return await s.scalar(select(func.count()).select_from(model))
    return run(go())


def _ingest(name, doc, etag=None):
    return run(ev.ingest_bytes(name, _bz(doc), etag=etag))


A, B, C = NEWEST_ID + 10 * RATE, NEWEST_ID + 11 * RATE, NEWEST_ID + 12 * RATE


# ── parsing ───────────────────────────────────────────────────────────────

def test_parse_keeps_good_rows_and_drops_missing_1970_and_garbage():
    doc = _doc({A: "2026-09-01T10:00:00Z", B: "MISSING", C: "1970-01-01T00:00:00Z",
                A + 1: "not a date", A + 2: None, A + 3: "2999-01-01T00:00:00Z",
                A + 4: "2026-09-02T00:00:00+00:00"})
    doc[str(A + 5)] = "not an object"
    rows = {r.structure_id: r for r in ev.parse_snapshot(_bz(doc))}
    assert set(rows) == {A, A + 4}
    assert rows[A].first_seen == D(2026, 9, 1, 10) and rows[A].type_id == TYPE
    assert rows[A].last_listed == D(2026, 10, 1)


def test_plain_json_parses_and_a_non_snapshot_raises():
    assert len(ev.parse_snapshot(json.dumps(_doc({A: "2026-09-01T00:00:00Z"})).encode())) == 1
    for bad in (b"[1,2,3]", b"not json", b"BZh9garbage"):
        with pytest.raises(ev.SnapshotError):
            ev.parse_snapshot(bad)


# ── storage rules ─────────────────────────────────────────────────────────

def test_minimum_first_seen_survives_a_reset_and_a_removal(db):
    _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z", B: "2026-01-21T00:00:00Z"}))
    # later file: A reset to a later date, B gone entirely
    _ingest("structures-2026-03-01_00-00-00.v2.json.bz2", _doc({A: "2026-03-01T00:00:00Z"}))
    rows = _first_seen(db)
    assert rows[A].first_seen == D(2026, 1, 20) and rows[A].source == NAME1
    assert rows[B].first_seen == D(2026, 1, 21)
    # an earlier first_seen in a still later file does lower it, and says where from
    _ingest("structures-2026-04-01_00-00-00.v2.json.bz2", _doc({A: "2026-01-10T00:00:00Z"}))
    assert _first_seen(db)[A].first_seen == D(2026, 1, 10)
    assert _first_seen(db)[A].source.startswith("structures-2026-04")


async def _sources(SL):
    async with SL() as s:
        return set((await s.execute(select(StructureAgeCalibration.source))).scalars())


def test_every_row_has_an_explicit_source_and_nothing_hits_calibration(db):
    before = _count(db, StructureAgeCalibration)
    _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z"}))
    assert all(r.source for r in _first_seen(db).values())
    assert _count(db, StructureAgeCalibration) == before
    assert run(_sources(db)) == {"triff"}


def test_new_modules_never_write_calibration_and_the_writer_sets_source():
    ingest = (ROOT / "app/intel/everef_structures.py").read_text()
    reader = (ROOT / "app/intel/structure_first_seen.py").read_text()
    names = {n.id for n in ast.walk(ast.parse(ingest)) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(ast.parse(ingest)) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    assert "StructureAgeCalibration" not in names
    assert not any(w in reader for w in ("insert(", "update(", "delete(", ".add(", "commit"))
    assert '"source": name' in ingest      # the only structure_first_seen writer sets it


async def _stamp(SL, name):
    async with SL() as s:
        r = await s.get(StructureFirstSeenImport, name)
        return (r.imported_at, r.rows, r.etag)


def test_reimporting_a_recorded_file_changes_nothing(db):
    assert _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z"}), etag="e1") == "imported"
    stamp = run(_stamp(db, NAME1))
    assert _ingest(NAME1, _doc({A: "2025-12-01T00:00:00Z", B: "2025-12-01T00:00:00Z"}),
                   etag="e1") == "skipped"
    assert set(_first_seen(db)) == {A} and run(_stamp(db, NAME1)) == stamp
    assert _count(db, StructureFirstSeenImport) == 1


def test_a_bad_file_writes_nothing_and_is_retried(db):
    assert run(ev.ingest_bytes(NAME1, b"BZh9 truncated", etag="e")) == "failed"
    assert run(ev.ingest_bytes(NAME1, b"[]", etag="e")) == "failed"
    assert _count(db, StructureFirstSeen) == 0 and _count(db, StructureFirstSeenImport) == 0
    assert _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z"}), etag="e") == "imported"


def test_a_failure_inside_the_transaction_rolls_back_rows_and_record(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(ev, "refresh_censored", boom)
    assert _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z"})) == "failed"
    assert _count(db, StructureFirstSeen) == 0 and _count(db, StructureFirstSeenImport) == 0


# ── censored dates and the soft bound ─────────────────────────────────────

def test_censored_dates_are_detected_automatically():
    counts = {date(2026, 1, d): 3 for d in range(1, 29)}
    counts[date(2026, 1, 29)] = 60
    counts[date(2026, 1, 30)] = 40          # busier than normal, but under the floor of 50
    assert ev.censored_dates(counts) == {date(2026, 1, 29)}
    big = {date(2026, 1, d): 40 for d in range(1, 29)}
    big[date(2026, 1, 29)] = 250            # > 5x the median
    assert ev.censored_dates(big) == {date(2026, 1, 29)}
    assert ev.censored_dates({}) == set()


def test_import_flags_ids_on_a_mass_date_and_unflags_when_it_stops_being_one(db):
    ids = {A + i: "2026-05-20T00:00:00Z" for i in range(60)}
    ids[B] = "2026-05-21T00:00:00Z"
    ids.update({C + i: f"2026-04-{1 + i % 28:02d}T00:00:00Z" for i in range(56)})   # ordinary days
    _ingest("structures-2026-06-01_00-00-00.v2.json.bz2", _doc(ids))
    rows = _first_seen(db)
    assert rows[A].censored and rows[A + 59].censored and not rows[B].censored
    # the same ids turn out to have been first seen on many different earlier days
    _ingest("structures-2026-07-01_00-00-00.v2.json.bz2",
            _doc({A + i: f"2026-04-{1 + i % 28:02d}T00:00:00Z" for i in range(60)}))
    assert not any(r.censored for r in _first_seen(db).values())


def _seed_bound(SL):
    async def seed():
        async with SL() as s:
            for off, day, cens in [(0, 20, False), (1, 18, False), (2, 25, False),
                                   (3, 10, True), (4, 22, False), (-1, 1, False)]:
                s.add(StructureFirstSeen(structure_id=A + off, first_seen=D(2026, 8, day),
                                         censored=cens, source="t"))
            await s.commit()
    run(seed())


def _bound(SL, x):
    async def go():
        async with SL() as s:
            return await sfs.soft_upper_bound(s, x)
    return run(go())


def test_soft_bound_is_the_kth_smallest_uncensored_first_seen_at_or_above_the_id(db):
    _seed_bound(db)
    assert _bound(db, A) == D(2026, 8, 22)        # 18, 20, 22, 25 uncensored -> 3rd smallest
    assert _bound(db, A + 1) == D(2026, 8, 25)    # 18, 22, 25
    assert _bound(db, A + 2) is None              # only two uncensored left
    assert _bound(db, A - 1) == D(2026, 8, 20)    # id A-1 (day 1) joins: 1, 18, 20


# ── estimates ─────────────────────────────────────────────────────────────

def _estimate(SL, sid):
    async def go():
        async with SL() as s:
            return await sa_mod._estimate(s, sid)
    return run(go())


def _seed_first_seen(SL, rows):
    async def go():
        async with SL() as s:
            for sid, when, cens in rows:
                s.add(StructureFirstSeen(structure_id=sid, first_seen=when, censored=cens,
                                         source="t"))
            await s.commit()
    run(go())


def test_in_range_estimates_are_unchanged_by_first_seen_data(db):
    ids = [point_id(i) + off for i in (0, 1, 50, 120, POINTS - 2, POINTS - 1)
           for off in (0, 1, RATE // 3, RATE - 1)
           if point_id(i) + off <= NEWEST_ID]
    before = {sid: _estimate(db, sid) for sid in ids}
    assert all(e["method"] in ("exact", "interpolate") for e in before.values())
    # lots of EVE Ref data over the same range, some absurdly early, one per id
    _seed_first_seen(db, [(sid, D(2016, 5, 1), False) for sid in ids]
                     + [(point_id(i) + 5, D(2026, 1, 1), False) for i in range(POINTS)])
    assert {sid: _estimate(db, sid) for sid in ids} == before


def test_a_tail_estimate_is_tightened_never_exact_and_labelled(db):
    x = NEWEST_ID + 30 * RATE
    base = _estimate(db, x)
    assert base["method"] == "extrapolate" and "source" not in base
    seen = (base["mid"] - timedelta(days=20)).replace(hour=0, minute=0, second=0)
    _seed_first_seen(db, [(x, seen, False)])
    est = _estimate(db, x)
    assert est["method"] == "extrapolate" and est["method"] != "exact"
    assert est["source"] == "everef" and est["high"] <= seen < base["high"]
    assert est["mid"] <= base["mid"] and est["low"] <= base["low"]
    assert "EVE Ref first saw it on" in est["note"] and seen.strftime("%d %b %Y") in est["note"]


def test_soft_bound_alone_tightens_the_tail_and_censored_ids_do_not_vote(db):
    x = NEWEST_ID + 30 * RATE
    base = _estimate(db, x)
    early = base["mid"] - timedelta(days=40)
    _seed_first_seen(db, [(x + RATE * i, early, True) for i in range(1, 6)])
    assert _estimate(db, x) == base               # only censored votes: nothing changes
    _seed_first_seen(db, [(x + RATE * i, early + timedelta(days=i), False) for i in range(6, 9)])
    est = _estimate(db, x)
    assert est["high"] == early + timedelta(days=8) and est["source"] == "everef"


def test_an_id_eve_ref_has_seen_still_takes_a_user_anchor_and_is_not_confirmed(db):
    x = NEWEST_ID + 30 * RATE
    _seed_first_seen(db, [(x, D.combine(today() - timedelta(days=30), datetime.min.time()), False)])

    async def check():
        async with db() as s:
            return await sac.check_submission(s, x, today() - timedelta(days=60), 4501)
    verdict = run(check())
    assert verdict.ok, verdict
    undo = override_db(db)
    try:
        body = client(4501).get("/tools/structure-age/partial",
                                params={"paste": f"<url=showinfo:35833//{x}>Sample</url>"}).text
    finally:
        undo()
    assert "confirmed" not in body.lower() and ">exact<" not in body
    assert "already has a confirmed date" not in body


# ── HTTP ──────────────────────────────────────────────────────────────────

TODAY = date(2026, 10, 30)
DAY_DIR = "https://data.everef.net/structures/history/2026/2026-10-30"


def _transport(routes, seen=None):
    def handler(request):
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        r = routes.get(url)
        if r is None:
            return httpx.Response(404)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, stream=httpx.ByteStream(r))
    return httpx.MockTransport(handler)


def _index(*names):
    return json.dumps({"files": [{"name": n, "size": 10} for n in names]}).encode()


def test_daily_ingest_picks_the_newest_v2_file_and_ignores_v1(db):
    new, old = "structures-2026-10-30_00-41-08.v2.json.bz2", "structures-2026-10-30_00-01-08.v2.json.bz2"
    seen = []
    t = _transport({f"{DAY_DIR}/index.json": _index("structures-2026-10-30_00-41-08.json.bz2", old, new),
                    f"{DAY_DIR}/{new}": _bz(_doc({A: "2026-10-01T00:00:00Z"}))}, seen)
    assert run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0)) == {"status": "imported", "file": new}
    assert set(_first_seen(db)) == {A} and len(seen) == 2
    # same file again: index only, no download
    seen.clear()
    assert run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0))["status"] == "up_to_date"
    assert seen == [f"{DAY_DIR}/index.json"]


def test_daily_ingest_walks_back_then_falls_back_to_latest(db):
    seen = []
    t = _transport({ev.LATEST_URL: json.dumps(_doc({A: "2026-10-01T00:00:00Z"})).encode()}, seen)
    res = run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0))
    assert res == {"status": "imported", "file": "structures-latest.v2.json"}
    assert len([u for u in seen if u.endswith("index.json")]) == ev.LOOKBACK_DAYS
    assert run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0))["status"] == "skipped"


def test_http_errors_and_bad_downloads_write_nothing_and_retry_next_run(db):
    name = "structures-2026-10-30_00-41-08.v2.json.bz2"
    idx = {f"{DAY_DIR}/index.json": _index(name)}
    for bad in (httpx.Response(500), b"BZh9 not really bz2"):
        t = _transport({**idx, f"{DAY_DIR}/{name}": bad})
        assert run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0))["status"] == "failed"
        assert _count(db, StructureFirstSeen) == 0 and _count(db, StructureFirstSeenImport) == 0
    t = _transport({**idx, f"{DAY_DIR}/{name}": _bz(_doc({A: "2026-10-01T00:00:00Z"}))})
    assert run(ev.run_daily_ingest(transport=t, today=TODAY, gap=0))["status"] == "imported"
    assert not ev.is_running()


def test_requests_are_paused_between_each_other(db, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(ev.asyncio, "sleep", fake_sleep)
    name = "structures-2026-10-30_00-41-08.v2.json.bz2"
    t = _transport({f"{DAY_DIR}/index.json": _index(name),
                    f"{DAY_DIR}/{name}": _bz(_doc({A: "2026-10-01T00:00:00Z"}))})
    run(ev.run_daily_ingest(transport=t, today=TODAY))
    assert sleeps == [ev.REQUEST_GAP_SECONDS]


def test_backfill_imports_in_order_skips_recorded_and_refuses_foreign_or_v1(db):
    base = "https://data.everef.net/structures/history/2026"
    n1, n2 = "structures-2026-01-01_00-41-09.v2.json.bz2", "structures-2026-02-01_00-41-09.v2.json.bz2"
    seen = []
    t = _transport({f"{base}/2026-01-01/{n1}": _bz(_doc({A: "2026-01-01T00:00:00Z"})),
                    f"{base}/2026-02-01/{n2}": _bz(_doc({B: "2026-02-01T00:00:00Z"}))}, seen)
    urls = [f"{base}/2026-02-01/{n2}", f"{base}/2026-01-01/{n1}",
            "https://example.invalid/structures/x.v2.json.bz2",
            f"{base}/2026-01-01/structures-2026-01-01_00-41-09.json.bz2"]
    res = run(ev.backfill(urls, transport=t, gap=0))
    assert sorted((r["file"], r["status"]) for r in res) == sorted([
        ("structures-2026-01-01_00-41-09.json.bz2", "refused"), (n1, "imported"),
        (n2, "imported"), ("x.v2.json.bz2", "refused")])
    assert seen == [f"{base}/2026-01-01/{n1}", f"{base}/2026-02-01/{n2}"]
    seen.clear()
    assert [r["status"] for r in run(ev.backfill(urls[:2], transport=t, gap=0))] == ["skipped", "skipped"]
    assert seen == []


# ── scheduler and wiring ──────────────────────────────────────────────────

def _scheduler_fn():
    tree = ast.parse((ROOT / "app/routes/dashboard.py").read_text())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_background_scheduler")


def test_scheduler_runs_the_ingest_outside_the_killmail_gate():
    fn = _scheduler_fn()
    gates = [n for n in ast.walk(fn) if isinstance(n, ast.If)
             and "killmails_enabled" in ast.unparse(n.test)]
    assert gates, "killmail gate not found"
    for g in gates:
        assert "everef_structures" not in ast.unparse(g)
    assert "run_daily_ingest" in ast.unparse(fn)


def test_old_triff_scheduler_blocks_are_gone_but_the_admin_trigger_stays():
    dash = (ROOT / "app/routes/dashboard.py").read_text()
    for gone in ("structure_age_scraper", "refresh_from_everef", "_structure_age_done",
                 "_last_sa_refresh"):
        assert gone not in dash
    assert "run_full_scrape" in (ROOT / "app/routes/admin.py").read_text()
    assert (ROOT / "app/intel/structure_age_scraper.py").exists()


# ── admin panel ───────────────────────────────────────────────────────────

def test_admin_panel_shows_ingest_age_staleness_and_shadow_stats(db):
    ids = [point_id(i) for i in (10, 20, 30, 40)]
    _seed_first_seen(db, [(ids[0], D(2030, 1, 1), False), (ids[1], D(2030, 1, 1), False),
                          (ids[2], D(2016, 6, 1), False), (ids[3], D(2030, 1, 1), True)])
    undo = override_db(db)
    try:
        page = client(4501).get("/admin/section/structure-age").text
        assert "EVE Ref first sightings" in page and "Never" in page and "stale" in page
        assert "(3 shared)" in page and "66.7%" in page
        _ingest(NAME1, _doc({A: "2026-01-20T00:00:00Z"}))
        page = client(4501).get("/admin/section/structure-age").text
        assert "Never" not in page and "0.0 d ago" in page
        assert "stale" not in page
    finally:
        undo()
