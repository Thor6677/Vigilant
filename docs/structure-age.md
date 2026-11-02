# Structure age: EVE Ref first sightings (T-090)

The estimator's calibration table (`structure_age_calibration`) holds the frozen
baseline points (read-only, `source='triff'`, no longer fetched from anywhere)
and admin-approved user anchors (`source='user'`). Any row there shows as
"exact", so EVE Ref data is never written to it.

## PR 1 (done): shadow ingest and a tail bound

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
- The old triff scrape and top-up scheduler blocks are removed.

## PR 2 (done): EVE Ref everywhere, triff.tools gone

- Deleted: the scraper module (`structure_age_scraper.py`, including the old v1
  id-list refresh, which the daily v2 ingest supersedes), the admin routes
  `POST /admin/structure-age/scrape` and `GET /admin/structure-age/status`, and
  their tests. `tests/test_no_triff_tools.py` fails if "triff.tools" in any
  host or URL form appears under `app/`. The frozen rows stay and keep
  `source='triff'`; that value is a label, not a call.
- Admin wording: "triff baseline" is now "data baseline".
- ID-rate fit: uses every non-user point (today the frozen rows), so user
  points still cannot move it.
- In-range estimates (between two calibration points, neither on the id) use
  EVE Ref too. B = min(the id's own first_seen, the 3rd-smallest non-censored
  first_seen among ids inside this gap, i.e. at or above the id and below the
  upper neighbour). Rules:
  - An id with a calibration row of its own (frozen or user) is unchanged, and
    stays `exact`.
  - If B is no earlier than the estimate's latest date, nothing changes.
  - Otherwise the latest date moves to B and the window slides earlier with it,
    keeping its width. It never grows, never moves later, and never goes below
    the lowest neighbouring anchor date (or the estimate's own earliest date,
    if lower). It is never narrower than 2 days (MIN_REFINED_DAYS), so a very
    early B holds the window at the lower neighbour with a note saying so.
  - The midpoint only moves earlier. The method stays `interpolate`, the
    source becomes `everef`, and the result is never `exact`.
- Ids between the newest frozen row and a user point above it are refined the
  same way (the user-point branch), with the note extended rather than replaced.
- The lag used above the range is not used inside it.

## Running the backfill (operator, dev first)

    python -c "import asyncio; from app.intel.everef_structures import backfill; \
    print(asyncio.run(backfill([...v2 snapshot URLs...])))"

Only `https://data.everef.net/structures/` `*.v2.json[.bz2]` URLs are accepted.
