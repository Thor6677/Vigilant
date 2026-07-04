# Activity History Browser + Daily ISK Backfill (T-040) — Design

**Date:** 2026-07-04
**Status:** Approved
**Tickets:** T-040 (ISK aggregate backfill), plus the scrollable-history feature request

## Problem

1. `/tools/activity`'s 5y/all windows compute ISK by raw-scanning the entire ~60M-row
   killmails table. SQLite's GROUP BY temp b-tree pushed the container past its 2.5GB
   cgroup limit and OOM-killed uvicorn when the startup pre-warmer ran them
   (2026-07-04 01:17 UTC). Hotfix c3315c3 excluded 5y/all from the warm list; they are
   compute-on-click (1–2 min, still OOM-risky).
2. The page only offers fixed preset windows. The user wants an eve-offline.net-style
   history browser: a ~1-year viewport with a scrollbar to pan back through the years.

Both share one data foundation: fast day-level aggregates across the full timeline.
PCU dailies exist 2003+ (`player_count_daily_aggregates`), kill counts 2007+
(`killmail_daily_aggregates`, source `zkb-totals`), but per-day **ISK only exists since
2026-03-21** (source `vigilant`, 106 rows). The killmails table has EVERef-backfilled
ISK back to ~2016 — it just isn't aggregated.

## Decisions made during brainstorm

- **Dedicated view**, not a change to the preset charts: a lazy-loaded **"History"
  section at the bottom of `/tools/activity`**. Presets stay untouched.
- **Approach A — ship the whole timeline once, pan client-side.** ~8,400 daily points
  (~80KB gzipped) in one payload; the viewport is sliced client-side, so panning does
  zero network I/O. No new JS dependency (no chartjs-plugin-zoom): viewport slicing is
  a few lines against the already-vendored Chart.js.

## Component 1 — Chunked ISK backfill (T-040)

**What:** fill `killmail_daily_aggregates` (source=`vigilant`) with per-day
`kill_count` + `total_isk_destroyed` from the killmails table for
**2016-01-01 → 2026-03-20** (the pre-existing-coverage boundary).

**How:**
- New module `app/intel/killmail_isk_backfill.py`.
- One month per query: `SELECT date(killmail_time), count(*), sum(total_value) FROM
  killmails WHERE killmail_time >= :m0 AND killmail_time < :m1 GROUP BY 1` — ~600k rows
  per chunk, bounded memory (this is the same aggregation the OOM query did, just
  windowed). `await asyncio.sleep(2)` between chunks; whole run ~1–2h in the background.
- **Resumable & insert-only:** before each month, query which dates already have a
  `vigilant` row and skip them (the `uq_kda_source_date` constraint backstops races).
  Never updates existing rows — the daily rollup owns current dates.
- **Trigger:** fire-and-forget task in `main.py` on every startup. No global
  "already done" probe — that would break resume (a half-finished run looks
  "present"). Instead the job always iterates the months and the per-month
  skip check makes completed months free: a fully-backfilled DB costs ~120
  cheap date queries once per boot, a partial one resumes exactly at the gap.
- Uses its own `AsyncSessionLocal()` per chunk (async-session safety).

**Then, read-path switch in `app/routes/player_stats.py`:**
- `_build_activity_payload`'s day+ bin branch (1y/5y/all) computes `isk_buckets` from
  `KillmailDailyAggregate` (per-date best-source, vigilant preferred — identical
  pattern to the existing kills read) instead of the raw killmails scan.
- Pre-2016 dates have no ISK row → bin stays 0 (same as today's behavior for
  pre-retention bins; the stale "30-day discovery retention" comments get corrected).
- **5y/all rejoin `warm_activity_cache()`**; the DO-NOT-RE-ADD comment is replaced
  with a pointer to this spec. Gate: the warmer re-adds them unconditionally — if the
  backfill hasn't finished yet, the aggregate read just returns partial ISK (correct,
  cheap, no OOM), so there is no ordering hazard.

## Component 2 — History endpoint

`GET /tools/activity/history.json` — auth-gated like the page (session `user_id`,
401 JSON otherwise).

**Payload** (parallel arrays, one entry per calendar day, 2003-05-28 → today):

```json
{
  "dates":    ["2003-05-28", ...],
  "pcu_avg":  [24500, ...],   // null where no PCU data
  "pcu_peak": [31200, ...],
  "kills":    [null, ..., 8231, ...],  // null before 2007-12-05
  "isk":      [null, ..., 1.2e12, ...] // null before ISK coverage (~2016)
}
```

- PCU from `PlayerCountDailyAggregate` with the existing `_PCU_SOURCE_PRIORITY`
  per-date dedup. Kills+ISK from `KillmailDailyAggregate` with the existing
  vigilant-over-zkb preference. Missing = `null`, never zero-faked (Chart.js renders
  gaps).
- Server-side SWR cache, TTL 1h, plus a slot at the END of `warm_activity_cache()`'s
  list. Reuses the `_payload_cache`/`_refreshing` machinery with key `"history"`
  (TTL map entry added; the window-validation paths ignore it since `"history"` is not
  in `_WINDOWS`).
- Error path: 500s return `{"error": "..."}` JSON; never a half-built payload.

## Component 3 — History UI section

- New `<section>` at the bottom of `tools_activity.html`: heading `HISTORY`, htmx
  lazy-load trigger (`hx-trigger="revealed"`) fetching a small partial
  (`partials/activity_history.html`) that carries the chart canvas + controls +
  nonce'd script; the script then fetches `history.json` once.
- One Chart.js line chart, ~365-day viewport: PCU avg (line), kills (line, second
  axis), ISK (line, second axis, formatted); series legend toggles. Styling matches
  the page's existing charts (design-system tokens; hardcoded-hex parity with the
  other charts is acceptable — consistent with the batch D cosmetic note).
- Controls under the chart: a full-width `<input type="range">` spanning
  day-0 → (today − 365), styled with the b-* input look; `‹ year` / `year ›` step
  buttons; a `b-muted-sm` label showing the visible span (e.g. "2014-06-01 →
  2015-06-01"). Slider/button events slice the cached arrays and `chart.update()` —
  no network on pan.
- Script placement: inside the partial (htmx-partial script rules), nonce'd,
  functions guarded with `window.fn = window.fn || ...` where redefinition is possible.
- Failure state: fetch error → standard `b-empty` block in the section.

## Error handling

- Backfill: per-chunk try/except + `log.exception`; a failed chunk is retried on next
  boot (resume check finds the gap). Insert-only + unique constraint = no rollup
  interference.
- Endpoint: exceptions → logged + `{"error"}` JSON; SWR never caches an error payload.
- UI: missing/null series render as gaps; endpoint failure shows the empty state, the
  rest of the page is unaffected (lazy section).

## Testing & verification

- Unit: backfill chunk function against a fixture month (correct per-day rows,
  skip-existing honored); history endpoint shape (nulls where coverage lacks, arrays
  equal length); existing 16 tests keep passing.
- Prod (post-deploy): EXPLAIN the new 1y/5y/all ISK read (must hit the aggregate
  table); `warm_activity_cache` completes with 9 windows + history; container memory
  flat through a full warm (the OOM regression test); backfill progress visible in
  logs, ~monthly cadence.

## Out of scope

- Free zoom (chartjs-plugin-zoom), pinch gestures.
- Panning the preset charts (breakdown/zone/daily-kills stay preset-driven).
- Pre-2016 ISK (no source exists).
- Backfilling zone/breakdown aggregates (only main-chart series ship in history.json).
