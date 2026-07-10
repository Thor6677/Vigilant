# Industry P&L Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution model:** Task 1–2 → **Opus** subagents (pure engines, correctness-critical). Task 3–4 → **Sonnet** subagents (plumbing/UI). **Fable reviews each task's diff + test output before the next task starts.**

**Goal:** Manufactured items flow into the Trading P&L FIFO as "build lots" priced at completion-date build cost, yielding Trading / Industry / Total realized-profit splits on `/market/pnl`.

**Architecture:** One pure matcher (`app/market/pnl.py`) gains lot source tags; a new pure valuation module computes job build cost at ingest; a new append-only `industry_job_history` table is filled by the existing hourly "industry" sync fetcher; the route synthesizes build lots from stored rows. Spec: `docs/superpowers/specs/2026-07-10-industry-pnl-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy async (SQLite) + Jinja2/htmx. Tests: pytest (`.venv/bin/python -m pytest`).

**Project rules that bite here:** each concurrent coroutine needs its own `AsyncSessionLocal()`; new tables auto-create via `Base.metadata.create_all` (no ALTER needed); Jinja dict access uses `['key']` for keys like `items`; commit after each task.

---

### Task 1: Matcher source tags + per-source aggregation (Opus)

**Goal:** `match_fifo` distinguishes trade vs build lots (fee asymmetry included); aggregations report per-source subtotals; existing behavior is unchanged when no build lots exist.

**Files:**
- Modify: `app/market/pnl.py`
- Test: `tests/test_pnl.py` (append; do not alter existing tests — they must pass untouched)

**Acceptance Criteria:**
- [ ] Transactions accept optional `source` key: `"trade"` (default) | `"build"`.
- [ ] Build lots enter the queue at raw `unit_price` (no `1+broker_fee` markup); trade lots keep the existing markup. Sell-side fees apply identically to both.
- [ ] Every match row gains `lot_source`.
- [ ] `aggregate_by_type` rows gain `trade_profit` / `build_profit`; `totals()` gains the same keys; `aggregate_monthly` rows gain them too.
- [ ] All pre-existing tests in `tests/test_pnl.py` pass unmodified.

**Verify:** `.venv/bin/python -m pytest tests/test_pnl.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pnl.py`)

```python
# ── Industry P&L: build lots (T-041 item 2) ─────────────────────────────────

def test_build_lot_no_buy_broker_fee():
    """A build lot's cost basis is raw build cost — no acquisition broker fee."""
    txs = [
        {"type_id": 1, "quantity": 10, "unit_price": 100.0, "is_buy": True,
         "date": "2026-01-01", "source": "build"},
        {"type_id": 1, "quantity": 10, "unit_price": 200.0, "is_buy": False,
         "date": "2026-01-02"},
    ]
    r = match_fifo(txs, broker_fee=0.01, sales_tax=0.02)
    m = r[1]["realized"][0]
    assert m["lot_source"] == "build"
    assert m["cost_basis"] == pytest.approx(100.0 * 10)          # raw, no 1.01x
    assert m["proceeds"] == pytest.approx(200.0 * (1 - 0.02 - 0.01) * 10)


def test_trade_and_build_lots_interleave_fifo_order():
    """Sells consume oldest lots first regardless of source; rows are tagged."""
    txs = [
        {"type_id": 1, "quantity": 5, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},                                   # trade lot
        {"type_id": 1, "quantity": 5, "unit_price": 7.0, "is_buy": True,
         "date": "2026-01-02", "source": "build"},                # build lot
        {"type_id": 1, "quantity": 8, "unit_price": 20.0, "is_buy": False,
         "date": "2026-01-03"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    rows = r[1]["realized"]
    assert [(m["qty"], m["lot_source"]) for m in rows] == [(5, "trade"), (3, "build")]


def test_per_source_aggregation_splits():
    txs = [
        {"type_id": 1, "quantity": 1, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},
        {"type_id": 1, "quantity": 1, "unit_price": 5.0, "is_buy": True,
         "date": "2026-01-02", "source": "build"},
        {"type_id": 1, "quantity": 2, "unit_price": 20.0, "is_buy": False,
         "date": "2026-02-01"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    by_type = aggregate_by_type(r)[0]
    assert by_type["trade_profit"] == pytest.approx(10.0)   # 20-10
    assert by_type["build_profit"] == pytest.approx(15.0)   # 20-5
    t = totals(r)
    assert t["trade_profit"] == pytest.approx(10.0)
    assert t["build_profit"] == pytest.approx(15.0)
    monthly = aggregate_monthly(r)
    assert monthly[0]["trade_profit"] == pytest.approx(10.0)
    assert monthly[0]["build_profit"] == pytest.approx(15.0)


def test_default_source_is_trade_and_legacy_shape_unchanged():
    txs = [
        {"type_id": 1, "quantity": 1, "unit_price": 10.0, "is_buy": True,
         "date": "2026-01-01"},
        {"type_id": 1, "quantity": 1, "unit_price": 20.0, "is_buy": False,
         "date": "2026-01-02"},
    ]
    r = match_fifo(txs, broker_fee=0.0, sales_tax=0.0)
    assert r[1]["realized"][0]["lot_source"] == "trade"
```

Check the imports at the top of `tests/test_pnl.py` — ensure `aggregate_monthly`, `totals`, `pytest` are imported; add if missing.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_pnl.py -q`
Expected: new tests FAIL (`KeyError: 'lot_source'` / `'trade_profit'`).

- [ ] **Step 3: Implement in `app/market/pnl.py`**

`_Lot` gains `source: str = "trade"`. In `match_fifo`:

```python
        if t["is_buy"]:
            queues[type_id].append(_Lot(
                qty=qty, unit_price=price, date=t["date"],
                source=t.get("source", "trade")))
            continue
```

In the sell loop, fee asymmetry + tagging:

```python
            # Build lots carry raw build cost — no acquisition broker fee
            # (you didn't place a buy order to make the item). Trade lots
            # keep the buy-side markup. Sell-side fees are identical.
            if lot.source == "build":
                buy_cost_eff = lot.unit_price
            else:
                buy_cost_eff = lot.unit_price * (1 + broker_fee)
            sell_rev_eff = price * (1 - sales_tax - broker_fee)
```

and add `"lot_source": lot.source` to the match-row dict. Echo `"source": lot.source` in the leftover-lots output dicts.

Aggregations — in `aggregate_by_type` per-row loop add:

```python
        trade_profit = sum(m["profit"] for m in matches if m["lot_source"] == "trade")
        build_profit = sum(m["profit"] for m in matches if m["lot_source"] == "build")
```

and include both keys in the row dict. Mirror the same two sums in `totals()` accumulation and in `aggregate_monthly` bucket dicts (initialize `"trade_profit": 0.0, "build_profit": 0.0` in `setdefault`, add `m["profit"]` to the matching key). Update the module docstring to describe build lots.

- [ ] **Step 4: Verify** — `.venv/bin/python -m pytest tests/test_pnl.py -q` → all pass; then full suite `.venv/bin/python -m pytest tests/ -q`.

- [ ] **Step 5: Commit**

```bash
git add app/market/pnl.py tests/test_pnl.py
git commit -m "feat(pnl): FIFO lot source tags + per-source aggregation for industry P&L"
```

---

### Task 2: Job build-cost valuation — `app/industry/job_cost.py` (Opus)

**Goal:** Pure per-job build-cost math + a completion-date price lookup helper over the market-history cache.

**Files:**
- Create: `app/industry/job_cost.py`
- Test: `tests/test_job_cost.py` (new)

**Acceptance Criteria:**
- [ ] `job_build_cost(materials, runs, me, install_cost, price_fn) -> tuple[float, str] | tuple[None, None]` — ME-adjusted material sum (reusing `calc_material` with neutral structure/rig/security: `struct_mat=1.0, rig_mat_base=0.0, sec_mult=1.0`) + install fee. Any unpriced material → `(None, None)`.
- [ ] `ME_MANUFACTURING = 10`, `ME_REACTION = 0` module constants (the documented assumption).
- [ ] `price_at(db, type_id, on_date) -> tuple[float, str] | tuple[None, None]` — Jita (region 10000002) `MarketHistory.average` for the nearest day ≤ `on_date` within 7 days → basis `"history"`; else current reference price via `app.market.lp.get_price_map` fallback handled by the CALLER (bulk) — see `prices_at_bulk`.
- [ ] `prices_at_bulk(db, type_ids, on_date, reference_map) -> dict[int, tuple[float, str]]` — one query for all types (`MarketHistory.type_id.in_(...)`, `date <= on_date`, `date >= on_date - 7d`, region 10000002, newest-first per type), falling back per-type to `reference_map` with basis `"reference"`; types in neither are absent from the result.

**Verify:** `.venv/bin/python -m pytest tests/test_job_cost.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Failing tests** (`tests/test_job_cost.py`)

```python
"""Job build-cost valuation (T-041 item 2). Pure math + history lookup.

DB tests follow tests/test_networth.py's pattern: temp SQLite file +
create_all + AsyncSessionLocal-style sessionmaker, run via a private loop.
"""
import asyncio
import tempfile
from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, MarketHistory
from app.industry.job_cost import (
    ME_MANUFACTURING, job_build_cost, prices_at_bulk,
)


def test_job_build_cost_me_adjusted():
    # 100 base qty, 3 runs, ME 10, neutral facility → calc_material applies
    # ceil/floor rules from app.industry.manufacturing — mirror them here:
    from app.industry.manufacturing import calc_material
    mats = [{"type_id": 34, "quantity": 100}]
    expected_qty = calc_material(100, 3, 10, 1.0, 0.0, 1.0)
    cost, basis = job_build_cost(
        mats, runs=3, me=10, install_cost=500.0,
        price_fn=lambda tid: (5.0, "history"))
    assert cost == pytest.approx(expected_qty * 5.0 + 500.0)
    assert basis == "history"


def test_job_build_cost_unpriced_material_is_none():
    cost, basis = job_build_cost(
        [{"type_id": 34, "quantity": 10}], runs=1, me=0, install_cost=0.0,
        price_fn=lambda tid: (None, None))
    assert cost is None and basis is None


def test_job_build_cost_mixed_basis_reports_reference():
    # If ANY material fell back to reference pricing, the whole job's basis
    # is "reference" (the weaker claim wins).
    prices = {34: (5.0, "history"), 35: (2.0, "reference")}
    cost, basis = job_build_cost(
        [{"type_id": 34, "quantity": 1}, {"type_id": 35, "quantity": 1}],
        runs=1, me=0, install_cost=0.0,
        price_fn=lambda tid: prices[tid])
    assert cost is not None and basis == "reference"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_prices_at_bulk_nearest_prior_within_7d_and_fallback():
    async def _t():
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(MarketHistory(region_id=10000002, type_id=34,
                                 date=date(2026, 7, 1), average=5.5,
                                 highest=6.0, lowest=5.0, volume=1000,
                                 order_count=10))
            db.add(MarketHistory(region_id=10000002, type_id=34,
                                 date=date(2026, 6, 20), average=4.0,
                                 highest=5.0, lowest=3.0, volume=1000,
                                 order_count=10))
            await db.commit()
            out = await prices_at_bulk(
                db, [34, 35], date(2026, 7, 3), reference_map={35: 9.0})
        assert out[34] == (5.5, "history")      # nearest ≤ 2026-07-03 within 7d
        assert out[35] == (9.0, "reference")    # no history → reference
    _run(_t())
```

(Adjust `MarketHistory` constructor kwargs to the real column names — read the model first; the test above assumes `region_id/type_id/date/average/...`.)

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: app.industry.job_cost`.

- [ ] **Step 3: Implement `app/industry/job_cost.py`**

```python
"""Completed-job build-cost valuation (Industry P&L, T-041 item 2).

Cost basis is the job's COMPLETION DATE (user decision in the spec): each
material is valued at the Jita daily average for the nearest day at or
before completion (within 7 days), falling back to the current global
reference price. Basis is tracked per job — "history" only when every
material priced from history; any reference fallback downgrades the whole
job to "reference".

ME is assumed (ESI jobs don't expose blueprint ME): ME 10 manufacturing,
ME 0 reactions — surfaced in the /market/pnl footnote. Facility bonuses
are assumed neutral (jobs don't record their structure/rigs).
"""
from __future__ import annotations

from datetime import date as date_cls, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketHistory
from app.industry.manufacturing import calc_material

ME_MANUFACTURING = 10
ME_REACTION = 0
JITA_REGION_ID = 10000002
_HISTORY_WINDOW_DAYS = 7


def job_build_cost(materials, runs, me, install_cost, price_fn):
    """Total build cost for one job, or (None, None) if any material is
    unpriceable. `price_fn(type_id) -> (price, basis) | (None, None)`.
    Returns (cost, basis) where basis is "history" iff every material was
    history-priced, else "reference"."""
    total = float(install_cost or 0.0)
    basis = "history"
    for m in materials:
        price, b = price_fn(m["type_id"])
        if price is None:
            return None, None
        qty = calc_material(m["quantity"], runs, me, 1.0, 0.0, 1.0)
        total += price * qty
        if b != "history":
            basis = "reference"
    return total, basis


async def prices_at_bulk(
    db: AsyncSession,
    type_ids: list[int],
    on_date: date_cls,
    reference_map: dict[int, float],
) -> dict[int, tuple[float, str]]:
    """{type_id: (price, basis)} for every resolvable type. History rows
    (Jita, nearest day <= on_date within the window) win; reference map is
    the fallback; unresolvable types are omitted."""
    out: dict[int, tuple[float, str]] = {}
    if type_ids:
        cutoff = on_date - timedelta(days=_HISTORY_WINDOW_DAYS)
        rows = (await db.execute(
            select(MarketHistory.type_id, MarketHistory.date, MarketHistory.average)
            .where(MarketHistory.region_id == JITA_REGION_ID)
            .where(MarketHistory.type_id.in_(type_ids))
            .where(MarketHistory.date <= on_date)
            .where(MarketHistory.date >= cutoff)
            .order_by(MarketHistory.type_id, MarketHistory.date.desc())
        )).all()
        for tid, _d, avg in rows:
            if tid not in out and avg is not None:
                out[tid] = (float(avg), "history")
    for tid in type_ids:
        if tid not in out and tid in reference_map:
            out[tid] = (float(reference_map[tid]), "reference")
    return out
```

- [ ] **Step 4: Verify** — `.venv/bin/python -m pytest tests/test_job_cost.py tests/test_pnl.py -q` → pass.

- [ ] **Step 5: Commit**

```bash
git add app/industry/job_cost.py tests/test_job_cost.py
git commit -m "feat(industry): completion-date job build-cost valuation"
```

---

### Task 3: `IndustryJobHistory` table + ingest in the industry sync fetcher (Sonnet)

**Goal:** Completed manufacturing/reaction jobs persist append-only with build_cost computed at ingest; NULL costs retry on later syncs.

**Files:**
- Modify: `app/db/models.py` (new model, place near `WalletTransaction`)
- Modify: `app/routes/dashboard.py` (`fetch_industry_jobs_data`)
- Test: `tests/test_industry_job_ingest.py` (new)

**Acceptance Criteria:**
- [ ] New model `IndustryJobHistory` (`industry_job_history`): `job_id` Integer PK, `character_id` Integer indexed, `activity_id` Integer, `blueprint_type_id` Integer, `product_type_id` Integer indexed, `runs` Integer, `output_qty` Integer, `install_cost` Float default 0, `build_cost` Float nullable, `cost_basis` String nullable, `start_date` DateTime nullable, `completed_date` DateTime indexed. (New table → auto-creates, no migration line.)
- [ ] `fetch_industry_jobs_data` also fetches `include_completed=True`, filters `status == "delivered"` and `activity_id in (1, 11)` (verify reactions id from a real payload; ESI uses 11 for reactions — assert in code comment), and inserts-or-ignores new rows.
- [ ] `output_qty = runs × SDEBlueprintInfo.product_quantity` (default 1 when blueprint unknown).
- [ ] Ingest computes `build_cost` via `prices_at_bulk` + `job_build_cost` (ME by activity: 10 manufacturing / 0 reactions); reference map from `app.market.lp.get_price_map(db)` fetched once per fetcher run.
- [ ] Rows already stored with `build_cost IS NULL` are re-valued each run (UPDATE when a cost resolves).
- [ ] The trimmed active-jobs JSON return (net-worth WIP) is byte-identical to before.

**Verify:** `.venv/bin/python -m pytest tests/test_industry_job_ingest.py tests/test_networth.py -q` → pass.

**Steps:**

- [ ] **Step 1: Failing test** — structure the ingest as a testable pure-ish helper. In `app/routes/dashboard.py` add `async def _persist_completed_jobs(db, character_id, jobs: list[dict]) -> int` that takes the RAW ESI job dicts, does the filtering/valuation/upsert, returns inserted count; `fetch_industry_jobs_data` calls it. Test the helper directly with a temp DB (pattern from `tests/test_networth.py`): seed `SDEBlueprintInfo(blueprint_type_id=999, product_type_id=44, product_quantity=10)` + `SDEBlueprintMaterial(blueprint_type_id=999, activity_id=1, material_type_id=34, quantity=100)` + a `MarketHistory` row for 34, call with a fixture delivered job (`job_id=1, activity_id=1, blueprint_type_id=999, product_type_id=44, runs=3, cost=500.0, status="delivered", completed_date="2026-07-01T12:00:00Z", start_date="2026-06-30T12:00:00Z"`), assert: one row, `output_qty == 30`, `build_cost` matches hand-computed `calc_material(100,3,10,1.0,0.0,1.0)*price + 500`, `cost_basis == "history"`; second call inserts nothing (idempotent); a job with an unpriceable material stores `build_cost None` and is re-valued on a later call after history is seeded.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Model in `models.py`; `_persist_completed_jobs` in `dashboard.py`:

```python
async def _persist_completed_jobs(db: AsyncSession, character_id: int,
                                  jobs: list[dict]) -> int:
    """Persist delivered manufacturing/reaction jobs with build cost valued
    at completion date (Industry P&L, T-041 item 2). Idempotent via
    INSERT OR IGNORE on job_id; NULL build_costs retry valuation here on
    every sync until prices resolve."""
    from app.db.models import IndustryJobHistory
    from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial
    from app.industry.job_cost import (
        ME_MANUFACTURING, ME_REACTION, job_build_cost, prices_at_bulk,
    )
    from app.market import lp as market_lp

    # ESI activity ids: 1 = manufacturing, 11 = reactions.
    delivered = [j for j in jobs
                 if j.get("status") == "delivered"
                 and j.get("activity_id") in (1, 11)
                 and j.get("job_id") and j.get("completed_date")]
    if not delivered:
        # Still retry NULL-cost rows even when nothing new arrived.
        delivered = []

    existing = {r[0] for r in (await db.execute(
        select(IndustryJobHistory.job_id).where(
            IndustryJobHistory.character_id == character_id)
    )).all()}
    new_jobs = [j for j in delivered if j["job_id"] not in existing]

    # Bulk-load blueprint data for everything we might value this run.
    null_rows = (await db.execute(
        select(IndustryJobHistory).where(
            IndustryJobHistory.character_id == character_id,
            IndustryJobHistory.build_cost.is_(None))
    )).scalars().all()
    bp_ids = ({j["blueprint_type_id"] for j in new_jobs}
              | {r.blueprint_type_id for r in null_rows})
    if not bp_ids and not new_jobs:
        return 0

    info_rows = (await db.execute(
        select(SDEBlueprintInfo).where(
            SDEBlueprintInfo.blueprint_type_id.in_(bp_ids))
    )).scalars().all()
    qty_by_bp = {r.blueprint_type_id: (r.product_quantity or 1) for r in info_rows}
    mat_rows = (await db.execute(
        select(SDEBlueprintMaterial).where(
            SDEBlueprintMaterial.blueprint_type_id.in_(bp_ids))
    )).scalars().all()
    mats_by_bp: dict[int, list[dict]] = {}
    for m in mat_rows:
        mats_by_bp.setdefault(m.blueprint_type_id, []).append(
            {"type_id": m.material_type_id, "quantity": m.quantity})

    reference_map = await market_lp.get_price_map(db)

    async def _value(bp_id, activity_id, runs, install_cost, completed):
        mats = mats_by_bp.get(bp_id)
        if not mats:
            return None, None
        me = ME_MANUFACTURING if activity_id == 1 else ME_REACTION
        prices = await prices_at_bulk(
            db, [m["type_id"] for m in mats], completed.date(), reference_map)
        return job_build_cost(mats, runs, me, install_cost,
                              lambda tid: prices.get(tid, (None, None)))

    inserted = 0
    for j in new_jobs:
        completed = _parse_esi_dt(j["completed_date"])
        runs = int(j.get("runs") or 0)
        cost, basis = await _value(j["blueprint_type_id"], j["activity_id"],
                                   runs, float(j.get("cost") or 0.0), completed)
        db.add(IndustryJobHistory(
            job_id=j["job_id"], character_id=character_id,
            activity_id=j["activity_id"],
            blueprint_type_id=j["blueprint_type_id"],
            product_type_id=j.get("product_type_id"),
            runs=runs,
            output_qty=runs * qty_by_bp.get(j["blueprint_type_id"], 1),
            install_cost=float(j.get("cost") or 0.0),
            build_cost=cost, cost_basis=basis,
            start_date=_parse_esi_dt(j["start_date"]) if j.get("start_date") else None,
            completed_date=completed,
        ))
        inserted += 1

    for row in null_rows:
        cost, basis = await _value(row.blueprint_type_id, row.activity_id,
                                   row.runs, row.install_cost or 0.0,
                                   row.completed_date)
        if cost is not None:
            row.build_cost, row.cost_basis = cost, basis

    await db.commit()
    return inserted
```

In `fetch_industry_jobs_data._get`, after the existing trimmed-list build, call ESI once with `include_completed=True` and pass the full payload to `_persist_completed_jobs(db, char.character_id, all_jobs)` — reuse ONE ESI call: change the existing `get_character_jobs(client, char.character_id)` to `get_character_jobs(client, char.character_id, include_completed=True)` and derive the trimmed active list from the same payload (filter statuses as today). Wrap persist in try/except logging a warning (persist failure must not break the net-worth field).

- [ ] **Step 4: Verify** — new test + `tests/test_networth.py` + full suite.

- [ ] **Step 5: Commit**

```bash
git add app/db/models.py app/routes/dashboard.py tests/test_industry_job_ingest.py
git commit -m "feat(industry): persist completed jobs with completion-date build cost"
```

---

### Task 4: Route composition + `/market/pnl` UI splits (Sonnet)

**Goal:** The page shows Trading / Industry / Total splits from one matcher run; build lots synthesized from stored jobs.

**Files:**
- Modify: `app/routes/pnl.py`
- Modify: `app/templates/pnl.html`
- Test: `tests/test_pnl_route.py` (new; TestClient smoke like `tests/test_networth.py`'s page test)

**Acceptance Criteria:**
- [ ] Route loads `IndustryJobHistory` rows for the target characters (`build_cost IS NOT NULL`, `product_type_id IS NOT NULL`, `output_qty > 0`) and appends synthetic transactions: `{"type_id": product_type_id, "quantity": output_qty, "unit_price": build_cost / output_qty, "is_buy": True, "date": completed_date, "source": "build", "transaction_id": -job_id}` (negative id keeps the sort tiebreaker stable and collision-free).
- [ ] NULL-cost jobs counted and surfaced ("N jobs awaiting pricing").
- [ ] Stat tiles: Total / Trading / Industry realized ISK (from `totals()` keys). Per-type table gains a small trade/build split annotation on rows where `build_profit != 0`. Monthly chart becomes two stacked datasets (trade + build) — keep the existing Chart.js structure, add the second dataset.
- [ ] Footnote gains: ME 10/0 assumption, completion-date basis, neutral-facility assumption, unpriced-job exclusion count. Page title stays; subtitle mentions industry.
- [ ] Smoke test: authenticated page renders 200 with and without job rows.

**Verify:** `.venv/bin/python -m pytest tests/test_pnl_route.py tests/ -q` → all pass.

**Steps:**

- [ ] **Step 1:** Smoke test first (fixture user + character + one WalletTransaction pair + one IndustryJobHistory row in temp DB via the `tests/test_networth.py` TestClient pattern; assert 200, "Industry" present in body, and the awaiting-pricing note when a NULL-cost row exists).
- [ ] **Step 2:** Run → fails (no Industry markup yet).
- [ ] **Step 3:** Implement route composition (query + synthetic transactions + `_assumptions()` gains `me_manufacturing`, `me_reaction`, `unpriced_jobs` count) and template updates (tiles row: three `.b-stat` tiles; monthly chart second dataset with the existing palette's muted accent; footnote lines).
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5: Commit**

```bash
git add app/routes/pnl.py app/templates/pnl.html tests/test_pnl_route.py
git commit -m "feat(pnl): industry/trading P&L splits on /market/pnl"
```

---

## Deploy & verification (coordinator/Fable, after all tasks)

1. Pre-deploy checklist (syntax, tests, no auth-touching files) → commit → push → `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`.
2. New table auto-creates on boot. First hourly sync ingests up to 90d of completed jobs per character — watch `docker logs` for the fetcher warnings.
3. Manual verify: `/market/pnl` shows the three tiles; a character with recent builds shows non-zero Industry P&L; footnote lists the new assumptions.
