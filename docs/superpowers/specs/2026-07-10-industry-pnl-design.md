# Industry P&L — Design Spec

**Date:** 2026-07-10 · **Ticket:** T-041 item 2 · **Status:** approved

Link manufacture build costs to sale proceeds. Today `/market/pnl` FIFO-matches
market buys against sells only; items you *built* and sold show as "unmatched
sells" and contribute nothing to P&L.

## Decisions (made with user)

1. **Unified FIFO.** Completed manufacturing/reaction jobs inject "build lots"
   into the same per-type FIFO queue the trading matcher uses. Sells consume
   oldest lots regardless of origin. Every match row is tagged
   `source="trade"|"build"` so one engine yields Trading / Industry / Total
   splits. No second matcher, no cross-engine sell dedup problem.
2. **Cost basis = price at completion date.** Materials valued at Jita
   daily-average on the job's completion date (existing market-history cache),
   falling back to the current global reference price (fallback flagged).
   Cost is computed **once at ingest** and stored, so P&L rows never drift
   with today's prices.
3. **ME assumption** (ESI jobs don't expose blueprint ME): ME 10 for
   manufacturing, ME 0 for reactions. Module constants, surfaced in the page
   footnote alongside the existing flat-fee assumptions.

## Architecture

### 1. Persistence — `IndustryJobHistory` (new table)

Append-only, mirrors `wallet_transactions`:

| column | notes |
|---|---|
| job_id | PK (ESI job_id) |
| character_id | indexed |
| activity_id | 1 = manufacturing, 11 = reactions (verify reaction id vs ESI during impl) |
| blueprint_type_id, product_type_id | |
| runs, output_qty | output_qty = runs × SDEBlueprintInfo.product_quantity |
| install_cost | ESI `cost` field |
| build_cost | total ISK, NULL when any material unpriceable at ingest |
| cost_basis | 'history' \| 'reference' \| NULL (which price source valued it) |
| start_date, completed_date | |

Auto-creates via `Base.metadata.create_all` (new table — no ALTER needed).

### 2. Sync — extend the existing hourly `industry` field fetcher

`fetch_industry_jobs_data` (app/routes/dashboard.py) additionally calls ESI
with `include_completed=true` (90-day retention), filters to
`status="delivered"` + activity ∈ {manufacturing, reactions}, computes
build_cost for NEW job_ids (insert-or-ignore), and persists rows on its own
session — exactly the `fetch_wallet_transactions_data` persist-in-fetcher
pattern. The trimmed active-jobs JSON for net-worth WIP is unchanged.

Backfill of missing build_cost: rows with `build_cost IS NULL` retry valuation
on each sync tick (price history may arrive later).

### 3. Valuation — `app/industry/job_cost.py` (new, pure + one I/O helper)

- Pure: `job_build_cost(materials_per_run, runs, me, install_cost, price_fn)
  -> (cost, basis) | (None, None)` reusing `calc_material` for ME math
  (structure/rig/security multipliers assumed neutral — documented; jobs
  don't record their facility bonuses).
- I/O helper: `price_at(db, type_id, date)` — Jita daily average from the
  market-history cache for that date (nearest prior day within 7d), else
  current reference price, else None. Bulk variant for ingest batches.

### 4. Matcher — `app/market/pnl.py` (pure, extended)

- Input transactions gain optional `source` ("trade" default) and per-lot fee
  treatment: trade buy lots cost `price × (1 + broker_fee)`; build lots enter
  at raw unit build cost (no acquisition broker fee).
- `_Lot` gains `source`; match rows gain `lot_source`.
- `aggregate_by_type`, `aggregate_monthly`, `totals` gain per-source subtotals
  (`trade_profit`, `build_profit`).
- Existing behavior with no build lots must be byte-identical (regression
  tests stay green untouched).

### 5. Route/UI — `/market/pnl`

- Route composes the transaction list: wallet_transactions (as today) + build
  lots synthesized from IndustryJobHistory rows (`is_buy=True`,
  `unit_price=build_cost/output_qty`, `date=completed_date`,
  `source="build"`; rows with NULL build_cost excluded and counted).
- Stat tiles: Total / Trading / Industry realized profit. Per-type table:
  source split column. Monthly chart: stacked trade vs build. Footnote: ME
  assumption, completion-date basis, neutral-facility assumption, NULL-cost
  exclusion count.

## Error handling

- ESI failures: warning + retry next tick (existing fetcher convention).
- Unpriceable jobs: NULL cost, excluded from P&L, counted on the page.
- Characters lacking `esi-industry.read_character_jobs.v1`: missing_scope
  (existing convention).

## Testing

- Pure matcher: build lots + trade lots interleave, fee asymmetry, source
  tagging, per-source aggregation — hand-computed fixtures.
- `job_build_cost`: ME math vs hand-computed; NULL propagation.
- Ingest: fixture ESI payload → rows written idempotently; NULL-cost retry.
- Route smoke: page renders with and without job history.

## Execution notes

Opus: matcher extension + job_cost valuation (correctness-critical, pure).
Sonnet: table/model/migration, fetcher extension, route composition, template.
Deploy: standard (new table auto-creates). First sync after deploy ingests up
to 90d of completed jobs per character.
