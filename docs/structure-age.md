# Structure age: EVE Ref first sightings (T-090)

The estimator's calibration table (`structure_age_calibration`) holds triff.tools
points (frozen, read-only, `source='triff'`) and admin-approved user anchors
(`source='user'`). Any row there shows as "exact", so EVE Ref data is never
written to it.

## PR 1 (this change): shadow ingest and a tail bound

- `structure_first_seen`: one row per structure id with the MINIMUM `first_seen`
  over every EVE Ref v2 snapshot imported (upstream drops structures unseen for
  30 days, so later files can only lower it). `censored` marks ids whose date is
  shared by far more ids than a normal day (an upstream sweep or reset); those
  never count as votes. `source` is the file that supplied the kept date.
- `structure_first_seen_imports`: one row per file already ingested, written in
  the same transaction as that file's rows. A failed fetch, bz2, JSON or schema
  problem writes nothing and is retried on the next run.
- `app/intel/everef_structures.py`: daily job (`run_daily_ingest`, scheduled
  outside the killmail gate, switch `EVEREF_STRUCTURES_ENABLED`, still stopped
  by `BACKGROUND_JOBS_ENABLED=false`) and a one-off `backfill(file_urls)`.
  Sequential requests, 2 s apart, one file in memory at a time.
- Estimate: only for ids above every calibration point. The latest date becomes
  min(extrapolated latest, the id's own first_seen, the 3rd-smallest
  non-censored first_seen among ids at or above it). The midpoint can only move
  earlier (bound minus the measured lag) and the earliest date widens with it.
  Method stays `extrapolate`; never `exact`. Estimates inside the calibrated
  range are unchanged, and user anchors work as before.
- Admin > Structure Age shows last ingest, staleness, row counts and a shadow
  comparison against the frozen triff points.
- The old triff scrape and top-up scheduler blocks are removed. The scraper
  module and the manual admin trigger remain.

## PR 2 (later)

Delete the triff scraper and admin scrape routes and use EVE Ref everywhere.

## Running the backfill (operator, dev first)

    python -c "import asyncio; from app.intel.everef_structures import backfill; \
    print(asyncio.run(backfill([...v2 snapshot URLs...])))"

Only `https://data.everef.net/structures/` `*.v2.json[.bz2]` URLs are accepted.
