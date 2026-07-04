# Activity History Browser + Daily ISK Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfill per-day ISK aggregates from the 60M-row killmails table so the 5y/all activity windows stop OOM-scanning and can pre-warm, then add an eve-offline.net-style scrollable history section to /tools/activity.

**Architecture:** Three layers per spec `docs/superpowers/specs/2026-07-04-activity-history-browser-design.md`: (1) a resumable month-chunked backfill filling `killmail_daily_aggregates` (source `vigilant`) for 2016→2026-03-20; (2) the 1y/5y/all ISK read in `_build_activity_payload` switches from raw killmail scans to that aggregate, 5y/all rejoin the pre-warmer, and a new `/tools/activity/history.json` ships the full 2003→today daily timeline through the existing SWR cache; (3) a lazy-loaded History section pans a ~365-day Chart.js viewport over the client-cached arrays — zero network per pan.

**Tech Stack:** FastAPI + SQLAlchemy async + aiosqlite (192GB prod DB, 2.5GB cgroup — the OOM constraint), Jinja2 + htmx + Chart.js (already vendored), pytest sync-style with explicit event loops (see tests/test_ambient_kills.py — NO pytest-asyncio marks).

**Hard constraints (from the 2026-07-04 OOM incident):**
- NEVER run an unwindowed GROUP BY over the whole killmails table — month chunks only.
- Background DB work uses its own `AsyncSessionLocal()` per unit of work, never a request session.
- Cache dicts, never rendered HTML (CSP nonce rotation).
- Commit → push → deploy.sh in that order (deploy pulls from GitHub).

---

### Task 1: Resumable ISK backfill module

**Goal:** `app/intel/killmail_isk_backfill.py` — month-chunked, insert-only, resumable backfill of vigilant daily aggregates, with tests.

**Files:**
- Create: `app/intel/killmail_isk_backfill.py`
- Test: `tests/test_isk_backfill.py`

**Acceptance Criteria:**
- [ ] `backfill_month` aggregates one month of killmails into per-day `KillmailDailyAggregate(source='vigilant')` rows
- [ ] Dates that already have a vigilant row are never touched (insert-only; rollup owns current dates)
- [ ] A fully-covered month is skipped WITHOUT running the killmail aggregate query (resume is cheap)
- [ ] `run_backfill` iterates 2016-01 → 2026-03 (exclusive end 2026-03-21), survives a failing month, sleeps only after months that did real work
- [ ] 4 tests pass, existing 16 still pass

**Verify:** `.venv/bin/python -m pytest tests/ -v` → 20 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_isk_backfill.py
"""Tests the month-chunked ISK backfill in isolation (in-memory SQLite).

Same pattern as test_ambient_kills.py: sync-style tests, explicit event
loop, extracted functions exercised directly with an injected session
factory (the app module has import-time side effects).
"""
import asyncio
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import Killmail, KillmailDailyAggregate
from app.intel.killmail_isk_backfill import _month_range, backfill_month


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Killmail.__table__.create(c))
            await conn.run_sync(lambda c: KillmailDailyAggregate.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def _km(kid: int, when: datetime, isk: float) -> Killmail:
    return Killmail(
        killmail_id=kid, killmail_hash="deadbeef", killmail_time=when,
        solar_system_id=30000142, victim_ship_type_id=670, total_value=isk,
    )


def test_month_range_boundaries():
    months = list(_month_range(date(2016, 1, 1), date(2016, 3, 15)))
    assert months == [
        (date(2016, 1, 1), date(2016, 2, 1)),
        (date(2016, 2, 1), date(2016, 3, 1)),
        (date(2016, 3, 1), date(2016, 3, 15)),  # partial final month clamps
    ]


def test_backfill_month_aggregates_per_day(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(1, datetime(2020, 5, 1, 10, 0), 100.0))
            s.add(_km(2, datetime(2020, 5, 1, 12, 0), 50.0))
            s.add(_km(3, datetime(2020, 5, 2, 3, 0), 7.0))
            await s.commit()
        n = await backfill_month(session_factory, date(2020, 5, 1), date(2020, 6, 1))
        async with session_factory() as s:
            rows = (await s.execute(
                select(KillmailDailyAggregate).order_by(KillmailDailyAggregate.date)
            )).scalars().all()
        return n, rows
    n, rows = asyncio.get_event_loop().run_until_complete(run())
    assert n == 2
    assert (rows[0].date, rows[0].kill_count, rows[0].total_isk_destroyed) == (date(2020, 5, 1), 2, 150.0)
    assert (rows[1].date, rows[1].kill_count, rows[1].total_isk_destroyed) == (date(2020, 5, 2), 1, 7.0)
    assert all(r.source == "vigilant" for r in rows)


def test_backfill_month_skips_existing_dates(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(1, datetime(2020, 5, 1, 10, 0), 100.0))
            s.add(_km(2, datetime(2020, 5, 2, 10, 0), 30.0))
            # 2020-05-01 already rolled up (rollup owns it — must not change)
            s.add(KillmailDailyAggregate(
                date=date(2020, 5, 1), source="vigilant",
                kill_count=999, total_isk_destroyed=999.0))
            await s.commit()
        n = await backfill_month(session_factory, date(2020, 5, 1), date(2020, 6, 1))
        async with session_factory() as s:
            existing = (await s.execute(
                select(KillmailDailyAggregate).where(KillmailDailyAggregate.date == date(2020, 5, 1))
            )).scalars().one()
        return n, existing
    n, existing = asyncio.get_event_loop().run_until_complete(run())
    assert n == 1                      # only 05-02 inserted
    assert existing.kill_count == 999  # pre-existing row untouched


def test_backfill_month_fast_skip_when_fully_covered(session_factory):
    async def run():
        async with session_factory() as s:
            # every day of Feb 2020 already covered
            for day in range(1, 30):
                s.add(KillmailDailyAggregate(
                    date=date(2020, 2, day), source="vigilant",
                    kill_count=1, total_isk_destroyed=1.0))
            await s.commit()
        return await backfill_month(session_factory, date(2020, 2, 1), date(2020, 3, 1))
    n = asyncio.get_event_loop().run_until_complete(run())
    assert n == -1  # sentinel: fast-skipped, no aggregate query ran
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_isk_backfill.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.intel.killmail_isk_backfill'`

- [ ] **Step 3: Write the module**

```python
# app/intel/killmail_isk_backfill.py
"""One-time, resumable backfill of daily kill_count + total_isk_destroyed
(T-040, spec docs/superpowers/specs/2026-07-04-activity-history-browser-design.md).

The EVERef import filled killmails back to ~2016 with total_value, but
killmail_daily_aggregates only has vigilant (ISK-bearing) rows since
2026-03-21 — so /tools/activity's 5y/all ISK reads raw-scanned all 60M
rows and OOM-killed the container when pre-warmed (2026-07-04 incident).

Design constraints:
- MONTH CHUNKS ONLY. One month ≈ 600k killmail rows per GROUP BY —
  bounded memory. Never widen the window.
- Insert-only, per-date skip: the daily rollup (killmail_daily_rollup)
  owns recent dates; we never update an existing vigilant row. The
  uq_kda_source_date unique constraint backstops races.
- Resumable: runs on EVERY startup. Fully-covered months fast-skip on a
  cheap date-count probe without touching the killmails table, so a
  finished backfill costs ~120 tiny queries per boot and a half-finished
  one resumes exactly at its gap.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import func, select

from app.db.models import AsyncSessionLocal, Killmail, KillmailDailyAggregate

log = logging.getLogger(__name__)

# EVERef ISK coverage starts ~2016; vigilant rollup coverage starts
# 2026-03-21 (exclusive end — that date already has a rollup row).
BACKFILL_START = date(2016, 1, 1)
BACKFILL_END = date(2026, 3, 21)

_running = False  # single-flight across duplicate startup calls


def _month_range(start: date, end: date):
    """Yield (m0, m1) month windows covering [start, end), m1 exclusive.
    The final window clamps to `end`."""
    cur = date(start.year, start.month, 1)
    while cur < end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        yield cur, min(nxt, end)
        cur = nxt


async def backfill_month(session_factory, m0: date, m1: date) -> int:
    """Aggregate killmails in [m0, m1) into per-day vigilant rows.

    Returns rows inserted, or -1 if the month was already fully covered
    (fast-skip — the killmail aggregate query never ran).
    """
    async with session_factory() as db:
        existing = set((await db.execute(
            select(KillmailDailyAggregate.date).where(
                KillmailDailyAggregate.source == "vigilant",
                KillmailDailyAggregate.date >= m0,
                KillmailDailyAggregate.date < m1,
            )
        )).scalars().all())
        if len(existing) >= (m1 - m0).days:
            return -1

        day_expr = func.date(Killmail.killmail_time)
        rows = (await db.execute(
            select(day_expr, func.count(), func.sum(Killmail.total_value))
            .where(
                Killmail.killmail_time >= datetime(m0.year, m0.month, m0.day),
                Killmail.killmail_time < datetime(m1.year, m1.month, m1.day),
            )
            .group_by(day_expr)
        )).all()

        inserted = 0
        for d_str, kc, isk in rows:
            d = date.fromisoformat(d_str)
            if d in existing:
                continue
            db.add(KillmailDailyAggregate(
                date=d, source="vigilant",
                kill_count=int(kc or 0),
                total_isk_destroyed=float(isk or 0.0),
            ))
            inserted += 1
        if inserted:
            await db.commit()
        return inserted


async def run_backfill() -> None:
    """Startup entry point (fire-and-forget from main.py).

    Waits out the boot rush (pre-warm, consumers), then walks the months.
    A failing month logs and moves on — the next boot's resume check
    finds the gap and retries it.
    """
    global _running
    if _running:
        return
    _running = True
    try:
        await asyncio.sleep(120)
        total = 0
        for m0, m1 in _month_range(BACKFILL_START, BACKFILL_END):
            try:
                n = await backfill_month(AsyncSessionLocal, m0, m1)
            except Exception:
                log.exception("isk-backfill: month %s failed; retrying next boot", m0)
                n = 0
            if n == -1:
                continue  # fast-skip: no sleep needed, nothing was scanned
            total += n
            log.info("isk-backfill: %s +%d day rows", m0.strftime("%Y-%m"), n)
            await asyncio.sleep(2)
        log.info("isk-backfill: pass complete, %d rows inserted", total)
    finally:
        _running = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 20 passed (4 new + 16 existing)

- [ ] **Step 5: Commit**

```bash
git add app/intel/killmail_isk_backfill.py tests/test_isk_backfill.py
git commit -m "feat(activity): resumable month-chunked daily ISK backfill (T-040)"
```

---

### Task 2: Wire backfill + switch 1y/5y/all ISK to the aggregate + re-warm 5y/all

**Goal:** Startup runs the backfill; the day+ bin ISK read stops scanning killmails; 5y/all rejoin the pre-warm list.

**Files:**
- Modify: `app/main.py` (startup block, after the `warm_activity_cache` create_task added at ~line 434)
- Modify: `app/routes/player_stats.py` (the `else:` day+ branch inside `_build_activity_payload`, and `warm_activity_cache`'s window tuple + comment)

**Acceptance Criteria:**
- [ ] `main.py` startup fire-and-forgets `run_backfill()`
- [ ] The day+ bin branch (1y/5y/all) contains NO query against `Killmail` — ISK comes from `KillmailDailyAggregate`
- [ ] `warm_activity_cache` iterates all 9 windows; the OOM DO-NOT-RE-ADD comment is replaced with a pointer to the spec
- [ ] 20 tests pass; `python3 -c "import ast; ast.parse(...)"` clean on both files

**Verify:** `.venv/bin/python -m pytest tests/ -q` → 20 passed

**Steps:**

- [ ] **Step 1: main.py wiring** — directly below the existing `asyncio.create_task(warm_activity_cache())` line:

```python
    # T-040: one-time resumable ISK backfill (month-chunked, self-skipping
    # once complete). Enables the aggregate-based 5y/all reads below.
    from app.intel.killmail_isk_backfill import run_backfill
    asyncio.create_task(run_backfill())
```

- [ ] **Step 2: Replace the day+ ISK scan in `_build_activity_payload`.** The current `else:` branch (day+ bins) starts with a comment "Day+ bins (1y/5y/all): ISK from a raw scan..." followed by `isk_q = select(_bin_expr(Killmail.killmail_time)...)` and its consumption loop. Replace ONLY that comment + isk_q block + loop (keep the `zrows` zone-aggregate read that follows) with:

```python
        # Day+ bins (1y/5y/all): ISK from the daily aggregate — NEVER a raw
        # killmails scan (the unwindowed GROUP BY temp b-tree OOM-killed the
        # container on 2026-07-04; see docs/superpowers/specs/
        # 2026-07-04-activity-history-browser-design.md). Only vigilant rows
        # carry ISK; dates the T-040 backfill hasn't reached yet simply
        # contribute 0 to their bin.
        isk_rows = (await db.execute(
            select(KillmailDailyAggregate.date,
                   KillmailDailyAggregate.total_isk_destroyed)
            .where(
                KillmailDailyAggregate.date >= cutoff.date(),
                KillmailDailyAggregate.total_isk_destroyed.isnot(None),
            )
        )).all()
        for d, isk in isk_rows:
            d_dt = datetime(d.year, d.month, d.day)
            idx = int((d_dt - cutoff).total_seconds() // bin_seconds)
            if 0 <= idx < num_bins:
                isk_buckets[idx] += float(isk or 0.0)
```

(No per-date source dedup needed: `total_isk_destroyed IS NOT NULL` filters to vigilant rows only — zkb-totals rows never carry ISK, and `uq_kda_source_date` guarantees one vigilant row per date. Note `+=` accumulation replaces the old `=` since multiple dates share a bin.)

- [ ] **Step 3: Re-add 5y/all to the warm list.** In `warm_activity_cache`, replace the exclusion comment + 7-window tuple with:

```python
    # All 9 windows warm safely now: the 5y/all ISK read hits the daily
    # aggregate (bounded rows), never the raw killmails table. The
    # 2026-07-04 OOM that forced their exclusion is documented in
    # docs/superpowers/specs/2026-07-04-activity-history-browser-design.md.
    for window in ("30d", "7d", "1d", "90d", "36h", "1h", "1y", "5y", "all"):
```

- [ ] **Step 4: Verify + commit**

Run: `python3 -c "import ast; ast.parse(open('app/main.py').read()); ast.parse(open('app/routes/player_stats.py').read())"` then `.venv/bin/python -m pytest tests/ -q` → 20 passed

```bash
git add app/main.py app/routes/player_stats.py
git commit -m "feat(activity): aggregate-based 5y/all ISK, backfill wiring, full warm list (T-040)"
```

---

### Task 3: history.json endpoint

**Goal:** `/tools/activity/history.json` returns the full 2003→today daily timeline as parallel arrays, SWR-cached and pre-warmed.

**Files:**
- Modify: `app/routes/player_stats.py`
- Test: `tests/test_activity_history.py`

**Acceptance Criteria:**
- [ ] `_build_history_payload(db)` returns `{dates, pcu_avg, pcu_peak, kills, isk}` parallel arrays, one entry per calendar day 2003-05-28→today, `None` (JSON null) where a series lacks coverage
- [ ] PCU dedup uses `_PCU_SOURCE_PRIORITY`; kills prefer vigilant over zkb-totals; ISK from vigilant rows only
- [ ] Endpoint auth-gates (401 JSON), serves through the `_payload_cache`/`_refreshing` SWR machinery under key `"history"`, and `"history"` is warmed at the END of `warm_activity_cache`'s loop
- [ ] 3 new tests pass (arrays equal length; nulls before coverage; values land on the right dates)

**Verify:** `.venv/bin/python -m pytest tests/ -v` → 23 passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_activity_history.py
"""Tests _build_history_payload in isolation (in-memory SQLite),
sync-style per tests/test_ambient_kills.py."""
import asyncio
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import KillmailDailyAggregate, PlayerCountDailyAggregate
from app.routes.player_stats import _build_history_payload, _FIRST_PCU


@pytest.fixture()
def session_factory():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: PlayerCountDailyAggregate.__table__.create(c))
            await conn.run_sync(lambda c: KillmailDailyAggregate.__table__.create(c))

    loop.run_until_complete(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    loop.close()


def _run(session_factory):
    async def run():
        async with session_factory() as s:
            return await _build_history_payload(s)
    return asyncio.get_event_loop().run_until_complete(run())


def test_arrays_parallel_and_span_full_timeline(session_factory):
    p = _run(session_factory)
    n = len(p["dates"])
    # UTC "today", NOT date.today() — local-tz today diverges from the
    # builder's UTC date every US evening and would flake this test.
    from datetime import datetime, timezone
    utc_today = datetime.now(timezone.utc).date()
    assert n == (utc_today - _FIRST_PCU.date()).days + 1
    assert all(len(p[k]) == n for k in ("pcu_avg", "pcu_peak", "kills", "isk"))
    assert p["dates"][0] == "2003-05-28"


def test_values_land_on_their_dates(session_factory):
    async def seed():
        async with session_factory() as s:
            s.add(PlayerCountDailyAggregate(
                date=date(2010, 6, 15), source="eve-offline-net",
                avg_pc=41000, peak_pc=52000, sample_count=24))
            s.add(KillmailDailyAggregate(
                date=date(2010, 6, 15), source="zkb-totals",
                kill_count=18500, total_isk_destroyed=None))
            s.add(KillmailDailyAggregate(
                date=date(2020, 3, 3), source="vigilant",
                kill_count=22000, total_isk_destroyed=5.5e12))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(seed())
    p = _run(session_factory)
    i2010 = p["dates"].index("2010-06-15")
    i2020 = p["dates"].index("2020-03-03")
    assert p["pcu_avg"][i2010] == 41000 and p["pcu_peak"][i2010] == 52000
    assert p["kills"][i2010] == 18500 and p["isk"][i2010] is None
    assert p["kills"][i2020] == 22000 and p["isk"][i2020] == 5.5e12


def test_vigilant_beats_zkb_on_same_date(session_factory):
    async def seed():
        async with session_factory() as s:
            s.add(KillmailDailyAggregate(
                date=date(2026, 4, 1), source="zkb-totals",
                kill_count=100, total_isk_destroyed=None))
            s.add(KillmailDailyAggregate(
                date=date(2026, 4, 1), source="vigilant",
                kill_count=105, total_isk_destroyed=1.0e12))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(seed())
    p = _run(session_factory)
    i = p["dates"].index("2026-04-01")
    assert p["kills"][i] == 105 and p["isk"][i] == 1.0e12
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_activity_history.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_history_payload'`

- [ ] **Step 3: Implement in player_stats.py**

Add `"history": 3600` to `_WINDOW_TTL_SECONDS`. Extend `_refresh_payload` to route builders:

```python
async def _refresh_payload(window: str) -> None:
    """Background SWR refresh. Own DB session (async-session safety)."""
    try:
        async with AsyncSessionLocal() as db:
            if window == "history":
                payload = await _build_history_payload(db)
            else:
                payload = await _build_activity_payload(db, window)
        ...  # (cache-store, except, finally blocks unchanged)
```

In `warm_activity_cache`, after the window loop and before the completion log line, add:

```python
    if "history" not in _payload_cache:
        _refreshing.add("history")
        await _refresh_payload("history")
```

(and change the completion log to `len(_payload_cache)` — it already is — so it reports 10.)

New builder + endpoint (place after `_build_activity_payload`):

```python
async def _build_history_payload(db: AsyncSession) -> dict:
    """Full daily timeline for the History browser: 2003-05-28 → today,
    parallel arrays, None where a series has no coverage. ~8,400 rows of
    pre-aggregated dailies — bounded regardless of killmail volume."""
    start = _FIRST_PCU.date()
    today = datetime.now(timezone.utc).date()
    n = (today - start).days + 1
    dates = [start + timedelta(days=i) for i in range(n)]

    pcu_avg: list[int | None] = [None] * n
    pcu_peak: list[int | None] = [None] * n
    kills: list[int | None] = [None] * n
    isk: list[float | None] = [None] * n

    best_pcu: dict = {}
    for d, src, avg_pc, peak_pc in (await db.execute(
        select(PlayerCountDailyAggregate.date, PlayerCountDailyAggregate.source,
               PlayerCountDailyAggregate.avg_pc, PlayerCountDailyAggregate.peak_pc)
    )).all():
        pri = _PCU_SOURCE_PRIORITY.get(src, 99)
        cur = best_pcu.get(d)
        if cur is None or pri < cur[0]:
            best_pcu[d] = (pri, avg_pc, peak_pc)
    for d, (_pri, avg_pc, peak_pc) in best_pcu.items():
        i = (d - start).days
        if 0 <= i < n:
            pcu_avg[i] = round(float(avg_pc)) if avg_pc is not None else None
            pcu_peak[i] = int(peak_pc) if peak_pc is not None else None

    best_kda: dict = {}
    for d, src, kc, isk_v in (await db.execute(
        select(KillmailDailyAggregate.date, KillmailDailyAggregate.source,
               KillmailDailyAggregate.kill_count,
               KillmailDailyAggregate.total_isk_destroyed)
    )).all():
        cur = best_kda.get(d)
        if cur is None or (cur[0] == "zkb-totals" and src == "vigilant"):
            best_kda[d] = (src, kc, isk_v)
    for d, (_src, kc, isk_v) in best_kda.items():
        i = (d - start).days
        if 0 <= i < n:
            kills[i] = int(kc) if kc is not None else None
            isk[i] = float(isk_v) if isk_v is not None else None

    return {
        "dates": [d.isoformat() for d in dates],
        "pcu_avg": pcu_avg, "pcu_peak": pcu_peak, "kills": kills, "isk": isk,
    }


@router.get("/tools/activity/history.json")
async def tools_activity_history(request: Request, db: AsyncSession = Depends(get_db)):
    if not request.session.get("user_id"):
        return JSONResponse({"error": "auth"}, status_code=401)
    cached = _payload_cache.get("history")
    if cached is not None:
        fresh_until, payload = cached
        if datetime.now(timezone.utc).replace(tzinfo=None) >= fresh_until:
            if "history" not in _refreshing:
                _refreshing.add("history")
                asyncio.create_task(_refresh_payload("history"))
        return JSONResponse(payload)
    try:
        payload = await _build_history_payload(db)
    except Exception:
        log.exception("tools/activity: history build failed")
        return JSONResponse({"error": "history unavailable"}, status_code=500)
    _payload_cache["history"] = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=_WINDOW_TTL_SECONDS["history"]),
        payload,
    )
    return JSONResponse(payload)
```

- [ ] **Step 4: Run tests, then commit**

Run: `.venv/bin/python -m pytest tests/ -v` → 23 passed

```bash
git add app/routes/player_stats.py tests/test_activity_history.py
git commit -m "feat(activity): history.json full daily timeline endpoint (SWR + pre-warm)"
```

---

### Task 4: History UI section

**Goal:** Lazy-loaded History section on /tools/activity: year viewport chart, range slider, year-step buttons, client-side panning.

**Files:**
- Modify: `app/templates/tools_activity.html` (append section inside `{% block content %}` before `{% endblock %}`)
- Create: `app/templates/partials/activity_history.html`
- Modify: `app/routes/player_stats.py` (partial route)

**Acceptance Criteria:**
- [ ] Section lazy-loads via `hx-trigger="revealed"`; nothing fetched until scrolled into view
- [ ] Chart shows a 365-day viewport with PCU (left axis) + kills/ISK (right axis); legend toggles work
- [ ] Slider + `‹ year`/`year ›` buttons pan with NO network requests; span label updates
- [ ] Script lives inside the partial, nonce'd; failure shows `b-empty` state
- [ ] Both templates Jinja-parse

**Verify:** `python3 -c "from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader('app/templates')); e.parse(open('app/templates/tools_activity.html').read()); e.parse(open('app/templates/partials/activity_history.html').read())"` → no output

**Steps:**

- [ ] **Step 1: Partial route in player_stats.py** (after the history.json endpoint):

```python
@router.get("/tools/activity/history-panel", response_class=HTMLResponse)
async def tools_activity_history_panel(request: Request):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=401)
    return templates.TemplateResponse(request, "partials/activity_history.html", {})
```

- [ ] **Step 2: Section in tools_activity.html** — append before `{% endblock %}` (after the last existing chart section):

```html
<!-- ── History browser: full 2003→today timeline, pannable year viewport ── -->
<div class="b-section" style="margin-top:24px;">
    <div class="b-section-header"><h2 class="b-section-title">HISTORY</h2>
        <span class="b-muted-sm">Daily averages since 2003 · drag the slider to travel</span></div>
    <div id="history-panel" hx-get="/tools/activity/history-panel"
         hx-trigger="revealed" hx-swap="innerHTML">
        <div class="b-empty">Loading history…</div>
    </div>
</div>
```

- [ ] **Step 3: The partial** — `app/templates/partials/activity_history.html`:

```html
<div class="b-panel b-pad-md">
    <div style="position:relative;height:320px;"><canvas id="hist-chart"></canvas></div>
    <div style="display:flex;align-items:center;gap:12px;margin-top:12px;">
        <button type="button" class="b-btn b-btn-sm" id="hist-prev">&lsaquo; year</button>
        <input type="range" id="hist-slider" min="0" max="0" value="0" step="1"
               style="flex:1;accent-color:var(--accent);">
        <button type="button" class="b-btn b-btn-sm" id="hist-next">year &rsaquo;</button>
        <span class="b-muted-sm" id="hist-span" style="min-width:170px;text-align:right;"></span>
    </div>
    <div class="b-empty" id="hist-error" style="display:none;">History unavailable — refresh to retry.</div>
</div>

<script nonce="{{ request.state.csp_nonce }}">
(function () {
    var VIEW = 365, H = null, chart = null;
    var slider = document.getElementById('hist-slider');
    var spanEl = document.getElementById('hist-span');

    function fmtIsk(v) {
        if (v == null) return '';
        if (v >= 1e12) return (v / 1e12).toFixed(1) + 'T';
        if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
        return Math.round(v / 1e6) + 'M';
    }

    function render(start) {
        var end = Math.min(start + VIEW, H.dates.length);
        chart.data.labels = H.dates.slice(start, end);
        chart.data.datasets[0].data = H.pcu_avg.slice(start, end);
        chart.data.datasets[1].data = H.kills.slice(start, end);
        chart.data.datasets[2].data = H.isk.slice(start, end);
        chart.update('none');
        spanEl.textContent = H.dates[start] + ' → ' + H.dates[end - 1];
    }

    fetch('/tools/activity/history.json').then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
    }).then(function (data) {
        H = data;
        var maxStart = Math.max(0, H.dates.length - VIEW);
        slider.max = maxStart;
        slider.value = maxStart;  // open on the most recent year
        chart = new Chart(document.getElementById('hist-chart'), {
            type: 'line',
            data: { labels: [], datasets: [
                { label: 'Avg players', data: [], borderColor: '#c8a951',
                  backgroundColor: 'transparent', pointRadius: 0, borderWidth: 1.5, yAxisID: 'y' },
                { label: 'Kills/day', data: [], borderColor: '#8899aa',
                  backgroundColor: 'transparent', pointRadius: 0, borderWidth: 1, yAxisID: 'y1' },
                { label: 'ISK/day', data: [], borderColor: '#aa5555',
                  backgroundColor: 'transparent', pointRadius: 0, borderWidth: 1, yAxisID: 'y1', hidden: true },
            ]},
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { ticks: { maxTicksLimit: 12, color: '#888' }, grid: { color: '#1a1a1a' } },
                    y: { position: 'left', ticks: { color: '#c8a951' }, grid: { color: '#1a1a1a' } },
                    y1: { position: 'right', ticks: { color: '#8899aa', callback: function (v) { return v >= 1e9 ? fmtIsk(v) : v; } }, grid: { drawOnChartArea: false } }
                },
                plugins: { legend: { labels: { color: '#ccc', boxWidth: 12 } },
                    tooltip: { callbacks: { label: function (ctx) {
                        if (ctx.dataset.label === 'ISK/day') return 'ISK: ' + fmtIsk(ctx.parsed.y);
                        return ctx.dataset.label + ': ' + (ctx.parsed.y == null ? '—' : ctx.parsed.y.toLocaleString());
                    } } } }
            }
        });
        render(maxStart);
        slider.addEventListener('input', function () { render(parseInt(slider.value, 10)); });
        document.getElementById('hist-prev').addEventListener('click', function () {
            slider.value = Math.max(0, parseInt(slider.value, 10) - VIEW); render(parseInt(slider.value, 10));
        });
        document.getElementById('hist-next').addEventListener('click', function () {
            slider.value = Math.min(parseInt(slider.max, 10), parseInt(slider.value, 10) + VIEW); render(parseInt(slider.value, 10));
        });
    }).catch(function () {
        document.getElementById('hist-error').style.display = '';
    });
})();
</script>
```

(Chart.js is already loaded globally by tools_activity.html's existing charts. The IIFE keeps names out of global scope, so htmx re-swap can't collide. Colors are hardcoded hex matching the page's existing Chart.js configs — consistent with the batch-D cosmetic convention.)

- [ ] **Step 4: Parse-check + commit**

Run the Verify command above, plus `.venv/bin/python -m pytest tests/ -q` → 23 passed

```bash
git add app/templates/tools_activity.html app/templates/partials/activity_history.html app/routes/player_stats.py
git commit -m "feat(activity): lazy-loaded History browser section with year-viewport panning"
```

---

### Task 5: Deploy + production verification (HUMAN GATE at the end)

**Goal:** Ship it, prove the OOM regression is dead, watch the backfill run, user eyeballs the History section.

**Files:** none (operations)

**Acceptance Criteria:**
- [ ] Deployed via push → deploy.sh; clean startup
- [ ] `warm_activity_cache` logs "pre-warm complete (10 windows)" and the container survives with flat memory (THE OOM regression test)
- [ ] Backfill visible in logs at ~monthly cadence after its 120s delay
- [ ] history.json returns 200 with full-length arrays (spot-check via authenticated browser or curl with session)
- [ ] User approves the History section visually

**Steps:**

- [ ] **Step 1:** `git push origin main` then `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`
- [ ] **Step 2:** Watch logs: `docker logs vigilant-app-1 --since 5m | grep -E 'pre-warm|isk-backfill|ERROR'` — expect "pre-warm complete (10 windows)" within ~3 min and the first "isk-backfill" lines after ~2 more.
- [ ] **Step 3:** `docker stats vigilant-app-1 --no-stream` during the warm — memory must stay well under 2.5GB. `docker inspect vigilant-app-1 --format '{{.State.OOMKilled}} {{.RestartCount}}'` → `false 0`.
- [ ] **Step 4:** Ask the user to open /tools/activity, scroll to HISTORY, and pan around. On approval, mark the plan complete. Backfill takes ~1–2h; verify later with: `SELECT count(*), min(date) FROM killmail_daily_aggregates WHERE source='vigilant'` trending toward ~3,700 rows / 2016-01-01, and 5y/all ISK filling in as it progresses.
