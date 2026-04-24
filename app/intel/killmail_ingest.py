"""Killmail backfill worker — paginates zKillboard's character history and
stores each kill via killmail_store.

Memory-safe design (from reverted attempt's lessons):
- Processes one page at a time, one character per scheduler call.
- Stampede guard: no fetches in first 10 min of process uptime.
- Rate-limited: zKB Semaphore(3) + paced pages; ESI Semaphore(3) in fetcher.
- Idempotent: killmail_store skips existing PKs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db.models import AsyncSessionLocal, Character, CharacterKillIngest
from app.intel.killmail_store import fetch_killmail, store_killmail, get_our_char_ids

log = logging.getLogger(__name__)

ZKB_BASE = "https://zkillboard.com/api"
ZKB_HEADERS = {
    "User-Agent": "Vigilant/1.0 EVE Dashboard (personal use)",
    "Accept-Encoding": "gzip",
    "Accept": "application/json",
}

_PROCESS_STARTED_AT = time.time()
STARTUP_GRACE_SECONDS = 600  # 10 min before backfill starts
MAX_PAGES = 10               # zKill caps at ~10 pages / 2000 kills
PAGE_SLEEP_SECONDS = 5.0     # between zKB pages
# CCP's `killmail` route group gives 3600 tokens per 15 min; a successful
# 2xx response costs 2 tokens, so the sustainable budget is ~2 req/s. Pace
# to stay under it: 3 concurrent fetches per 1.5s wave = 2 req/s average.
ESI_BATCH_SIZE = 3           # per-wave size for ESI hydration
ESI_BATCH_SLEEP = 1.5        # seconds between ESI waves
FIRST_RUN_PAGE_LIMIT = 3     # cap pages on first-backfill to 600 kills


def within_startup_grace() -> bool:
    return (time.time() - _PROCESS_STARTED_AT) < STARTUP_GRACE_SECONDS


async def find_pending_backfill_chars(limit: int = 1) -> list[int]:
    """Return character IDs that need backfill (never synced, or stale by >6h).
    Caps the scheduler to one at a time so we don't stampede.

    Ordering priority (most starved first):
      1. Never-touched characters (no row in character_kill_ingest yet).
      2. Incomplete backfills, oldest last_synced first.
      3. Complete-but-stale refreshes, oldest last_synced first.
    Within each tier we fall back to character_id for determinism."""
    async with AsyncSessionLocal() as db:
        all_chars = await db.execute(select(Character.character_id))
        all_ids = [r[0] for r in all_chars.all()]
        if not all_ids:
            return []

        state = await db.execute(select(CharacterKillIngest))
        state_map = {r.character_id: r for r in state.scalars().all()}

        stale_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=6)

        def _priority(cid: int):
            s = state_map.get(cid)
            # Sentinel far in the past so None-last_synced sorts before any real time.
            ls = s.last_synced if (s and s.last_synced) else datetime.min
            if s is None:
                return (0, ls, cid)
            if not s.backfill_complete:
                return (1, ls, cid)
            if s.last_synced is None or s.last_synced < stale_cutoff:
                return (2, ls, cid)
            return (99, ls, cid)  # up-to-date — excluded below

        candidates = sorted(
            (cid for cid in all_ids if _priority(cid)[0] < 99),
            key=_priority,
        )
        return candidates[:limit]


async def _fetch_zkb_page(http: httpx.AsyncClient, character_id: int, page: int) -> list[dict]:
    url = f"{ZKB_BASE}/characterID/{character_id}/page/{page}/"
    try:
        resp = await http.get(url, headers=ZKB_HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
        elif resp.status_code == 429:
            log.warning("killmail_ingest: zKB 429 for char %s page %s", character_id, page)
            await asyncio.sleep(5)
    except Exception as e:
        log.warning("killmail_ingest: zKB error char=%s page=%s: %s", character_id, page, e)
    return []


async def backfill_character(character_id: int) -> dict:
    """Backfill a single character. Safe to call periodically — idempotent.

    Returns {pages, inserted} for logging.
    """
    if within_startup_grace():
        log.debug("killmail_ingest: within startup grace, skipping backfill")
        return {"pages": 0, "inserted": 0, "skipped": "startup_grace"}

    async with AsyncSessionLocal() as db:
        row = await db.get(CharacterKillIngest, character_id)
        if row is None:
            row = CharacterKillIngest(character_id=character_id)
            db.add(row)
            await db.commit()
        first_run = not row.backfill_complete and (row.last_backfill_page or 0) == 0
        start_page = max(1, (row.last_backfill_page or 0) + 1) if not row.backfill_complete else 1
        last_seen = row.last_seen_killmail_id

    max_pages_this_run = FIRST_RUN_PAGE_LIMIT if first_run else MAX_PAGES
    our_ids = await get_our_char_ids()
    pages_walked = 0
    inserted_total = 0
    new_last_seen = last_seen

    async with httpx.AsyncClient() as http:
        for page in range(start_page, start_page + max_pages_this_run):
            if page > MAX_PAGES:
                # zKillboard only serves up to MAX_PAGES — treat this as a
                # successful terminal state so the scheduler stops re-picking
                # us, rather than looping forever on an instant-break no-op.
                async with AsyncSessionLocal() as db:
                    r = await db.get(CharacterKillIngest, character_id)
                    if r:
                        r.backfill_complete = True
                        r.last_synced = datetime.now(timezone.utc).replace(tzinfo=None)
                        if new_last_seen and (r.last_seen_killmail_id is None or new_last_seen > r.last_seen_killmail_id):
                            r.last_seen_killmail_id = new_last_seen
                        await db.commit()
                break
            page_data = await _fetch_zkb_page(http, character_id, page)
            pages_walked += 1
            if not page_data:
                async with AsyncSessionLocal() as db:
                    r = await db.get(CharacterKillIngest, character_id)
                    if r:
                        r.backfill_complete = True
                        r.last_synced = datetime.now(timezone.utc).replace(tzinfo=None)
                        if new_last_seen and (r.last_seen_killmail_id is None or new_last_seen > r.last_seen_killmail_id):
                            r.last_seen_killmail_id = new_last_seen
                        await db.commit()
                break

            # Collect page kill-ids and filter out any we've already stored
            # (disk-first), so we don't burn ESI tokens re-fetching. On warm
            # caches most of pages 1-N will be no-ops.
            candidates: list[tuple[int, str, dict]] = []
            for km in page_data:
                kid = km.get("killmail_id")
                zkb = km.get("zkb", {}) or {}
                khash = zkb.get("hash")
                if not (kid and khash):
                    continue
                if new_last_seen is None or kid > new_last_seen:
                    new_last_seen = kid
                candidates.append((kid, khash, zkb))

            already_stored: set[int] = set()
            if candidates:
                from app.db.models import Killmail
                async with AsyncSessionLocal() as db:
                    rows = await db.execute(
                        select(Killmail.killmail_id).where(
                            Killmail.killmail_id.in_([c[0] for c in candidates])
                        )
                    )
                    already_stored = {r[0] for r in rows.all()}

            to_fetch = [c for c in candidates if c[0] not in already_stored]

            for i in range(0, len(to_fetch), ESI_BATCH_SIZE):
                batch = to_fetch[i:i + ESI_BATCH_SIZE]

                async def _one(kid, khash, zkb):
                    full = await fetch_killmail(kid, khash)
                    if full:
                        if await store_killmail(full, zkb, our_ids):
                            return 1
                    return 0

                results = await asyncio.gather(
                    *[_one(k, h, z) for k, h, z in batch],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, int):
                        inserted_total += r
                if i + ESI_BATCH_SIZE < len(to_fetch):
                    await asyncio.sleep(ESI_BATCH_SLEEP)

            async with AsyncSessionLocal() as db:
                r = await db.get(CharacterKillIngest, character_id)
                if r:
                    r.last_backfill_page = page
                    r.last_synced = datetime.now(timezone.utc).replace(tzinfo=None)
                    if new_last_seen:
                        r.last_seen_killmail_id = new_last_seen
                    await db.commit()

            if page < start_page + max_pages_this_run - 1:
                await asyncio.sleep(PAGE_SLEEP_SECONDS)

    log.info(
        "killmail_ingest: char=%s pages=%d inserted=%d first_run=%s",
        character_id, pages_walked, inserted_total, first_run,
    )
    return {"pages": pages_walked, "inserted": inserted_total, "first_run": first_run}
