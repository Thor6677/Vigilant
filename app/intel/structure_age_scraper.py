"""Structure age calibration scraper.

Builds a local interpolation table by collecting structure IDs with confirmed
anchor dates from triff.tools (method='exact'). Two phases:

Phase 1 — EVERef history
  Downloads all daily structure snapshots from data.everef.net (2023-present),
  extracts unique structure IDs, queries triff.tools for each, stores any
  'exact' results. ~2-5k IDs, ~1 hour.

Phase 2 — Adaptive gap search
  Sorts the Phase 1 exact IDs and samples evenly within each gap between
  adjacent known IDs. Any 'exact' hit splits that gap, reducing the window
  for future passes. Repeats until no new IDs found across a full pass.
  Each pass: ~40k triff.tools calls @ 10 concurrent ≈ 10 min/pass.

triff.tools API (public, no auth required):
  GET https://triff.tools/api/structure-intel/age/{structure_id}
  Returns: {id, method, midISO, lowISO, highISO, daysWide}
  method: "exact" = in their calibration dataset
          "interpolate" = between two known IDs
          "extrapolate-tail" = beyond their dataset
"""

from __future__ import annotations

import asyncio
import bz2
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, func

from app.db.models import AsyncSessionLocal, StructureAgeCalibration

log = logging.getLogger(__name__)

TRIFF_BASE = "https://triff.tools/api/structure-intel/age"
EVEREF_BASE = "https://data.everef.net/structures/history"
EVEREF_YEARS = ["2023", "2024", "2025", "2026"]

# Phase 2 config
RANGE_LO = 1_021_000_000_000   # oldest known Upwell structure ID
RANGE_HI = 1_055_000_000_000   # beyond most recent known IDs
MIN_GAP = 100_000               # don't subdivide gaps smaller than this
SAMPLES_PER_GAP = 20            # evenly-spaced probes per gap per pass
CONCURRENCY = 10                # concurrent triff.tools requests

_scraper_running = False


def is_running() -> bool:
    return _scraper_running


# ── Shared helpers ────────────────────────────────────────────────────────

async def _triff_lookup(client: httpx.AsyncClient, structure_id: int) -> dict | None:
    try:
        resp = await client.get(f"{TRIFF_BASE}/{structure_id}", timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.debug("structure_age: lookup failed %s: %s", structure_id, e)
    return None


async def _batch_lookup(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    ids: list[int],
) -> list[dict]:
    async def _one(sid: int) -> dict | None:
        async with sem:
            return await _triff_lookup(client, sid)

    results = await asyncio.gather(*[_one(sid) for sid in ids], return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def _store_exact(results: list[dict]) -> int:
    """Upsert 'exact' results into StructureAgeCalibration using INSERT OR IGNORE.
    Retries up to 3 times on lock contention. Returns insert count."""
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

    exact = [r for r in results if r.get("method") == "exact"]
    if not exact:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for r in exact:
        try:
            rows.append({
                "structure_id": int(r["id"]),
                "anchor_mid": datetime.fromisoformat(r["midISO"].replace("Z", "+00:00")).replace(tzinfo=None),
                "anchor_low": datetime.fromisoformat(r["lowISO"].replace("Z", "+00:00")).replace(tzinfo=None),
                "anchor_high": datetime.fromisoformat(r["highISO"].replace("Z", "+00:00")).replace(tzinfo=None),
                "days_wide": float(r.get("daysWide", 6.0)),
                "fetched_at": now,
            })
        except (ValueError, KeyError):
            continue

    if not rows:
        return 0

    for attempt in range(3):
        try:
            async with AsyncSessionLocal() as db:
                stmt = _sqlite_insert(StructureAgeCalibration).values(rows)
                stmt = stmt.on_conflict_do_nothing(index_elements=["structure_id"])
                await db.execute(stmt)
                await db.commit()
            return len(rows)
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                log.warning("structure_age: _store_exact failed after 3 attempts: %s", e)
    return 0

    return inserted


async def _get_existing_ids() -> set[int]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(StructureAgeCalibration.structure_id))
        return {r[0] for r in rows.all()}


async def _get_sorted_exact_ids() -> list[int]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(StructureAgeCalibration.structure_id)
            .order_by(StructureAgeCalibration.structure_id)
        )
        return [r[0] for r in rows.all()]


# ── Phase 1: EVERef history ───────────────────────────────────────────────

async def _list_snapshot_urls(client: httpx.AsyncClient) -> list[str]:
    """Scrape EVERef directory listings to build a list of all snapshot file URLs."""
    urls: list[str] = []
    sem = asyncio.Semaphore(10)

    async def _fetch_day(year: str, date: str) -> str | None:
        async with sem:
            try:
                resp = await client.get(f"{EVEREF_BASE}/{year}/{date}/", timeout=20)
                # Prefer v2 format; fall back to v1
                m = re.search(r'href="([^"]*\.v2\.json\.bz2)"', resp.text) or \
                    re.search(r'href="([^"]*\.json\.bz2)"', resp.text)
                if m:
                    href = m.group(1)
                    if href.startswith("/"):
                        return f"https://data.everef.net{href}"
                    return href
            except Exception:
                pass
            return None

    for year in EVEREF_YEARS:
        try:
            resp = await client.get(f"{EVEREF_BASE}/{year}/", timeout=20)
            dates = sorted(set(re.findall(
                rf'/structures/history/{year}/(\d{{4}}-\d{{2}}-\d{{2}})/', resp.text
            )))
            day_results = await asyncio.gather(*[_fetch_day(year, d) for d in dates])
            urls.extend(u for u in day_results if u)
            log.info("structure_age phase1: %s — %d snapshots found", year, sum(1 for u in day_results if u))
        except Exception as e:
            log.warning("structure_age phase1: failed to list %s: %s", year, e)

    return urls


async def _ids_from_snapshot(url: str, client: httpx.AsyncClient) -> set[int]:
    """Download and parse one snapshot file, returning all structure IDs."""
    try:
        resp = await client.get(url, timeout=60)
        if resp.status_code != 200:
            return set()
        data = json.loads(bz2.decompress(resp.content))
        if isinstance(data, list):
            return {int(x) for x in data if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
        if isinstance(data, dict):
            return {int(k) for k in data if k.isdigit()}
    except Exception as e:
        log.debug("structure_age phase1: parse error %s: %s", url, e)
    return set()


async def run_phase1() -> dict:
    """Phase 1: collect structure IDs from EVERef history and query triff.tools."""
    log.info("structure_age phase1: starting")
    headers = {"User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)"}

    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        urls = await _list_snapshot_urls(http)
        log.info("structure_age phase1: %d snapshot files to process", len(urls))

        # Download all snapshots concurrently (10 at a time) and collect unique IDs
        sem_dl = asyncio.Semaphore(10)
        all_ids: set[int] = set()

        async def _dl(url: str) -> set[int]:
            async with sem_dl:
                return await _ids_from_snapshot(url, http)

        for i in range(0, len(urls), 100):
            batch = urls[i:i + 100]
            batch_results = await asyncio.gather(*[_dl(u) for u in batch])
            for ids in batch_results:
                all_ids |= ids
            log.info("structure_age phase1: processed %d/%d snapshots, %d unique IDs so far",
                     min(i + 100, len(urls)), len(urls), len(all_ids))

        existing = await _get_existing_ids()
        new_ids = sorted(all_ids - existing)
        log.info("structure_age phase1: %d unique IDs total, %d new to query triff.tools",
                 len(all_ids), len(new_ids))

        sem_api = asyncio.Semaphore(CONCURRENCY)
        total_inserted = 0
        CHUNK = 100

        for i in range(0, len(new_ids), CHUNK):
            chunk = new_ids[i:i + CHUNK]
            results = await _batch_lookup(http, sem_api, chunk)
            n = await _store_exact(results)
            total_inserted += n
            if i % 500 == 0 and i > 0:
                log.info("structure_age phase1: queried %d/%d, %d exact stored so far",
                         i, len(new_ids), total_inserted)

    log.info("structure_age phase1: done — %d exact IDs stored", total_inserted)
    return {"snapshots": len(urls), "unique_ids": len(all_ids), "new_exact": total_inserted}


# ── Phase 2: Adaptive gap search ─────────────────────────────────────────

def _build_sample_points(sorted_ids: list[int], existing: set[int]) -> list[int]:
    """For each gap between adjacent exact IDs, generate SAMPLES_PER_GAP
    evenly-spaced probe points. Skips gaps smaller than MIN_GAP."""
    boundaries = [RANGE_LO] + sorted_ids + [RANGE_HI]
    points: list[int] = []
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if hi - lo < MIN_GAP:
            continue
        step = (hi - lo) // (SAMPLES_PER_GAP + 1)
        for j in range(1, SAMPLES_PER_GAP + 1):
            sid = lo + step * j
            if sid not in existing:
                points.append(sid)
    return points


async def run_phase2(max_passes: int = 20) -> dict:
    """Phase 2: sample gaps between known exact IDs, collect new 'exact' hits,
    repeat until no new hits found or max_passes reached."""
    log.info("structure_age phase2: starting (max %d passes)", max_passes)
    headers = {"User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)"}
    total_found = 0
    passes_run = 0

    async with httpx.AsyncClient(headers=headers, timeout=15) as http:
        sem = asyncio.Semaphore(CONCURRENCY)

        for pass_num in range(1, max_passes + 1):
            passes_run = pass_num
            sorted_ids = await _get_sorted_exact_ids()
            existing = set(sorted_ids)
            sample_points = _build_sample_points(sorted_ids, existing)

            if not sample_points:
                log.info("structure_age phase2: no gaps to search, done")
                break

            log.info("structure_age phase2: pass %d — %d gaps, %d sample points",
                     pass_num, len(sorted_ids) + 1, len(sample_points))

            pass_found = 0
            CHUNK = 200
            for i in range(0, len(sample_points), CHUNK):
                chunk = sample_points[i:i + CHUNK]
                results = await _batch_lookup(http, sem, chunk)
                n = await _store_exact(results)
                pass_found += n

            total_found += pass_found
            log.info("structure_age phase2: pass %d complete — %d new exact IDs (total %d)",
                     pass_num, pass_found, total_found)

            if pass_found == 0:
                log.info("structure_age phase2: no new IDs found, stopping")
                break

    log.info("structure_age phase2: done — %d total new exact IDs across %d passes",
             total_found, passes_run)
    return {"passes": passes_run, "total_found": total_found}


# ── Full scrape ───────────────────────────────────────────────────────────

async def run_full_scrape() -> dict:
    """Run Phase 1 then Phase 2. Sets _scraper_running for the duration."""
    global _scraper_running
    _scraper_running = True
    try:
        p1 = await run_phase1()
        log.info("structure_age: phase1 complete — %s", p1)
        p2 = await run_phase2()
        log.info("structure_age: phase2 complete — %s", p2)
        return {"phase1": p1, "phase2": p2}
    finally:
        _scraper_running = False
