"""T-090 PR 1: EVE Ref first-seen ingest, the tail bound it feeds, and the
admin panel. Every id and date is invented. HTTP is stubbed with
httpx.MockTransport; nothing leaves the process."""
import ast
import bz2
import json
import pathlib
from datetime import date, datetime, time, timedelta

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
                                  point_date, point_id, run, today)

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


def _midnight(d):
    return datetime.combine(d, time())


def _seed_user_point(SL, sid, mid, half_days=1):
    async def go():
        async with SL() as s:
            s.add(StructureAgeCalibration(
                structure_id=sid, anchor_mid=mid, anchor_low=mid - timedelta(days=half_days),
                anchor_high=mid + timedelta(days=half_days), days_wide=2.0 * half_days,
                fetched_at=datetime(2026, 6, 6), source="user"))
            await s.commit()
    run(go())


def test_ids_with_a_calibration_row_are_unchanged_by_first_seen_data(db):
    user_id = point_id(50) + 1
    _seed_user_point(db, user_id, _midnight(point_date(50)))
    exact = [point_id(i) for i in (0, 1, 50, 120, POINTS - 2, POINTS - 1)] + [user_id]
    before = {sid: _estimate(db, sid) for sid in exact}
    assert all(e["method"] == "exact" for e in before.values())
    # An early first sighting of the id itself, one far outside its window, one
    # inside it, one in 2030, and three early votes in the gap above it.
    rows = []
    for n, sid in enumerate(exact):
        own = (D(2016, 5, 1), _midnight(point_date(50)), D(2030, 1, 1))[n % 3]
        rows.append((sid, own, False))
        rows += [(sid + k, D(2016, 5, 1), False) for k in (100, 200, 300)]
    _seed_first_seen(db, rows)
    after = {sid: _estimate(db, sid) for sid in exact}
    assert after == before
    assert all(e["method"] == "exact" for e in after.values())


def test_first_seen_votes_only_count_inside_their_own_gap(db):
    lo, hi = point_id(100), point_id(101)
    x = lo + RATE // 2
    ids = [lo + RATE // 4, x, lo + 3 * RATE // 4]
    before = {sid: _estimate(db, sid) for sid in ids}
    assert all(e["method"] == "interpolate" and "source" not in e for e in before.values())
    # Early first sightings everywhere EXCEPT inside this gap: above its
    # upper neighbour, in other gaps, and absurd 2016 rows.
    rows = [(hi + k, D(2016, 5, 1), False) for k in (1, 2, 3, 4)]
    rows += [(point_id(i) + RATE // 2 + k, _midnight(point_date(i)), False)
             for i in (3, 60, 99, 102, 150) for k in (0, 1, 2, 3)]
    rows += [(point_id(i) + 5, D(2016, 5, 1), False) for i in (10, 98, 103, 190)]
    _seed_first_seen(db, rows)
    assert {sid: _estimate(db, sid) for sid in ids} == before
    # Control: three votes inside the gap above x do move it, toward the bound.
    vote = _midnight(point_date(100))
    _seed_first_seen(db, [(x + k, vote, False) for k in (1, 2, 3)])
    est = _estimate(db, x)
    assert est != before[x]
    assert est["method"] == "interpolate" and est["source"] == "everef"
    assert est["high"] == vote < before[x]["high"]


T0 = D(2026, 1, 7)       # the neighbouring anchors' lowest date in the grid tests


def _grid_bases():
    straight = {"method": "interpolate", "mid": D(2026, 1, 13), "low": D(2026, 1, 10),
                "high": D(2026, 1, 16), "days_wide": 6.0}
    wide = {"method": "interpolate", "mid": D(2026, 1, 12), "low": D(2026, 1, 1),
            "high": D(2026, 1, 21), "days_wide": 20.0, "note": "base note"}
    return [(straight, T0), (wide, D(2025, 12, 25))]


def test_refine_between_invariants_over_a_grid_of_bounds():
    for est, floor in _grid_bases():
        w = est["high"] - est["low"]
        m = timedelta(days=sfs.MIN_REFINED_DAYS)
        bounds = [floor - timedelta(days=5), floor - timedelta(hours=1), floor,
                  floor + timedelta(days=1), floor + m, est["low"],
                  est["low"] + timedelta(days=1), est["mid"],
                  est["mid"] + timedelta(hours=6), est["high"] - timedelta(hours=1),
                  est["high"], est["high"] + timedelta(days=5)]
        for b in bounds:
            for own, soft in ((b, None), (None, b), (b, b + timedelta(days=9)),
                              (b + timedelta(days=9), b)):
                out = sfs.refine_between(est, own, soft, floor)
                if b >= est["high"]:
                    assert out is est
                    continue
                assert out["method"] == est["method"] != "exact"
                assert out["source"] == "everef"
                assert out["high"] <= est["high"] and out["low"] <= est["low"]
                assert out["mid"] <= est["mid"]
                assert out["high"] - out["low"] <= w
                assert out["high"] - out["low"] >= min(w, m)
                assert out["low"] >= floor
                assert out["low"] <= out["mid"] <= out["high"]
                if b >= floor + m:
                    assert out["high"] == b
                else:
                    assert out["high"] == floor + m
                assert out["days_wide"] == round((out["high"] - out["low"]).total_seconds() / 86400, 1)


def test_refine_between_without_a_bound_returns_the_estimate_itself():
    est, floor = _grid_bases()[0]
    assert sfs.refine_between(est, None, None, floor) is est


def test_refine_between_worked_example():
    est, floor = _grid_bases()[0]
    for b in (D(2026, 1, 16), D(2026, 1, 20)):
        assert sfs.refine_between(est, b, None, floor) is est
    out = sfs.refine_between(est, D(2026, 1, 14), None, floor)
    assert (out["low"], out["mid"], out["high"], out["days_wide"]) == (
        D(2026, 1, 8), D(2026, 1, 11), D(2026, 1, 14), 6.0)
    out = sfs.refine_between(est, None, D(2026, 1, 10), floor)
    assert (out["low"], out["mid"], out["high"], out["days_wide"]) == (
        D(2026, 1, 7), D(2026, 1, 8, 12), D(2026, 1, 10), 3.0)
    for b in (D(2026, 1, 8), D(2026, 1, 1)):
        out = sfs.refine_between(est, b, None, floor)
        assert (out["low"], out["mid"], out["high"], out["days_wide"]) == (
            D(2026, 1, 7), D(2026, 1, 8), D(2026, 1, 9), 2.0)
        assert out["method"] == "interpolate" and out["source"] == "everef"


def test_own_first_seen_tightens_a_straight_line_estimate_in_range(db):
    x = point_id(100) + RATE // 2
    base = _estimate(db, x)
    seen = _midnight(point_date(100))
    assert seen < base["high"]
    _seed_first_seen(db, [(x, seen, False)])
    est = _estimate(db, x)
    assert est["method"] == "interpolate" and est["source"] == "everef"
    assert est["high"] == seen and est["mid"] <= base["mid"] and est["low"] <= base["low"]
    assert est["low"] >= _midnight(point_date(100)) - timedelta(days=3)     # lower neighbour's anchor_low
    assert est["high"] - est["low"] <= base["high"] - base["low"]
    assert f"EVE Ref first saw it on {seen.strftime('%d %b %Y')}" in est["note"]
    assert "neighbouring calibration points" in est["note"]


def test_local_soft_bound_tightens_an_estimate_next_to_a_user_point(db):
    user_id = NEWEST_ID + 40 * RATE
    _seed_user_point(db, user_id, _midnight(today() - timedelta(days=60)))
    x = NEWEST_ID + 20 * RATE
    base = _estimate(db, x)
    assert base["method"] == "interpolate" and "source" not in base
    assert base["note"].startswith("Estimated from the nearest points")
    vote = base["mid"] - timedelta(days=2)
    assert vote < base["high"]
    # Votes above the user point do not count; three inside the gap do.
    _seed_first_seen(db, [(user_id + k, D(2016, 5, 1), False) for k in (1, 2, 3)])
    assert _estimate(db, x) == base
    _seed_first_seen(db, [(x + k, vote, False) for k in (1, 2, 3)])
    est = _estimate(db, x)
    assert est["method"] == "interpolate" and est["source"] == "everef"
    assert est["high"] == vote and est["mid"] <= base["mid"] and est["low"] <= base["low"]
    assert est["high"] - est["low"] <= base["high"] - base["low"]
    assert est["note"].startswith("Estimated from the nearest points")
    assert "EVE Ref's first sightings of nearby structures" in est["note"]


def test_an_early_bound_holds_the_window_at_the_lower_neighbour(db):
    x = point_id(100) + RATE // 2
    _seed_first_seen(db, [(x, D(2016, 5, 1), False)])
    est = _estimate(db, x)
    floor = _midnight(point_date(100)) - timedelta(days=3)
    assert est["low"] == floor and est["high"] == floor + timedelta(days=sfs.MIN_REFINED_DAYS)
    assert est["method"] == "interpolate"
    assert "held at the lower neighbour" in est["note"]


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


def test_old_triff_scheduler_blocks_and_the_admin_trigger_are_gone():
    dash = (ROOT / "app/routes/dashboard.py").read_text()
    for gone in ("structure_age_scraper", "refresh_from_everef", "_structure_age_done",
                 "_last_sa_refresh"):
        assert gone not in dash
    admin = (ROOT / "app/routes/admin.py").read_text()
    assert "run_full_scrape" not in admin and "structure_age_scraper" not in admin
    assert not (ROOT / "app/intel/structure_age_scraper.py").exists()


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
