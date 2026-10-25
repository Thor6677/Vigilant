"""Jita sell pricing for the battle loot finder (T-082).

"Jita sell" is the lowest sell order at Jita 4-4, the same definition the
Appraisal and Compression pages use. Dropped items are priced there for the
top JITA_TYPE_CAP types by value at the EVE average; the rest keep the average
(labelled as such). Fetching is a background job, because a cold report needs
up to 150 market calls (25-40 s) and the page must render at once on the
average:

  * derived prices are cached in process for 30 min (60 s for a failed fetch),
    so the raw order books never reach the ESI cache table;
  * ONE job runs process-wide (a second report's job waits its turn), joined
    by key if the same report is opened twice;
  * pacing follows the bulk-fetch pattern: Semaphore(3), batches of 10, 1 s
    between batches;
  * the feature spends at most BUDGET_CALLS calls per 15 min (10% of the
    market-order bucket it shares with Appraisal and Compression);
  * get_public absorbs the first 429 (it sets the app-wide throttle, waits and
    retries once), so a recovered 429 looks like a normal price. The job
    therefore watches rate_limit_tracker's rejection count between batches and
    stops, marking the rest as failed fetches, if it rises.

Everything here is in memory and lost on restart, which is harmless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass, field

from app.esi.client import ESIClient
from app.esi.market import APPRAISAL_HUBS, get_hub_sell_quote
from app.esi.rate_limit import rate_limit_tracker

log = logging.getLogger(__name__)

JITA = APPRAISAL_HUBS["jita"]
JITA_TYPE_CAP = 150
TTL_OK_SECONDS = 1800        # a price, or "no sell order at Jita 4-4"
TTL_ERROR_SECONDS = 60       # a failed fetch or a budget stop
BATCH_SIZE = 10
CONCURRENCY = 3
BATCH_PAUSE_SECONDS = 1.0
BUDGET_CALLS = 600
BUDGET_WINDOW_SECONDS = 900
MAX_JOB_RECORDS = 32
CRASH_BACKOFF_SECONDS = 60   # after a crashed job, don't start another for the same report

# type_id -> (stored_at, status, price); status ok | none | error | budget
_cache: dict[int, tuple[float, str, float | None]] = {}
_budget_calls: deque = deque()
_jobs: dict[str, "JobState"] = {}
_crashed_until: dict[str, float] = {}
_tasks: set = set()
_job_lock: asyncio.Lock | None = None
_job_lock_loop = None


def _now() -> float:
    return time.monotonic()


@dataclass
class JobState:
    state: str = "queued"          # queued | running | done
    total: int = 0
    done: int = 0
    failed: int = 0
    budget_hit: bool = False
    budget_resume_at: float | None = None   # epoch seconds
    stopped_early: bool = False             # ended because ESI started rejecting
    crashed: bool = False
    fetched_at: float | None = None         # epoch seconds, when the job finished
    types: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Candidates:
    types: tuple[int, ...]       # the types to look up, best first, at most JITA_TYPE_CAP
    total: int                   # distinct dropped types in the report
    coverage: float              # share of dropped value (at the average) the capped types cover


def rank_candidates(items: dict[int, list[tuple]], avg: dict, blueprints: set,
                    cap: int = JITA_TYPE_CAP) -> Candidates:
    """Distinct dropped, non-blueprint types, ranked by dropped quantity x EVE
    average. Types without an average go last so a new item still gets a look
    when there is room."""
    value: dict[int, float] = {}
    qty: dict[int, int] = {}
    for rows in items.values():
        for type_id, _destroyed, dropped, _single, _flag in rows:
            if dropped and type_id not in blueprints:
                value[type_id] = value.get(type_id, 0.0) + dropped * (avg.get(type_id) or 0.0)
                qty[type_id] = qty.get(type_id, 0) + dropped
    # Equal values (no average at all, e.g. the price list failed to load) fall
    # back to dropped quantity, so the cap isn't decided by type id.
    ranked = sorted(value, key=lambda t: (-value[t], -qty[t], t))
    chosen = ranked[:cap]
    total_value = sum(value.values())
    covered = sum(value[t] for t in chosen)
    coverage = (covered / total_value) if total_value else 1.0
    return Candidates(tuple(chosen), len(ranked), coverage)


def cached_prices(type_ids) -> dict[int, tuple[str, float | None]]:
    """Fresh derived prices for the given types: {type_id: (status, price)}."""
    now = _now()
    out = {}
    for t in type_ids:
        hit = _cache.get(t)
        if hit is None:
            continue
        ttl = TTL_OK_SECONDS if hit[1] in ("ok", "none") else TTL_ERROR_SECONDS
        if now - hit[0] < ttl:
            out[t] = (hit[1], hit[2])
    return out


def priced_at(type_ids) -> float | None:
    """Epoch seconds of the oldest fresh price among the types, for the
    "prices as of" line; None if none is cached."""
    stored = [_cache[t][0] for t in cached_prices(type_ids)]
    return time.time() - (_now() - min(stored)) if stored else None


def _rejected_since(start: datetime) -> bool:
    """Whether ESI rejected (429/420) anything after `start`. Compares the
    newest rejection's timestamp, not the count: the tracker prunes old entries
    and caps its deque, so a count can stay level while a new one arrives."""
    rejections = rate_limit_tracker.recent_rejections
    return bool(rejections) and rejections[-1] > start


def _budget_allowance() -> int:
    now = _now()
    while _budget_calls and now - _budget_calls[0] >= BUDGET_WINDOW_SECONDS:
        _budget_calls.popleft()
    return max(0, BUDGET_CALLS - len(_budget_calls))


def _store(type_id: int, status: str, price: float | None) -> None:
    _cache[type_id] = (_now(), status, price)
    if len(_cache) > 20000:
        for t in sorted(_cache, key=lambda k: _cache[k][0])[:5000]:
            del _cache[t]


async def _fetch_batch(client: ESIClient, batch: list[int], sem: asyncio.Semaphore,
                       job: JobState) -> None:
    async def one(type_id: int) -> None:
        async with sem:
            _budget_calls.append(_now())
            status, price = await get_hub_sell_quote(client, JITA["region_id"], JITA["station_id"], type_id)
        _store(type_id, status, price)
        job.done += 1
        if status == "error":
            job.failed += 1
    await asyncio.gather(*(one(t) for t in batch))


async def _run_job(lock: asyncio.Lock, key: str, job: JobState) -> None:
    try:
        async with lock:
            job.state = "running"
            todo = [t for t in job.types if t not in cached_prices([t])]
            job.done = job.total - len(todo)
            client = ESIClient("", cache_enabled=False)
            sem = asyncio.Semaphore(CONCURRENCY)
            started = datetime.now(timezone.utc)
            pos = 0
            while pos < len(todo):
                if pos and _rejected_since(started):
                    job.stopped_early = True
                    for t in todo[pos:]:
                        _store(t, "error", None)
                        job.done += 1
                        job.failed += 1
                    break
                allowance = _budget_allowance()
                if allowance <= 0:
                    job.budget_hit = True
                    job.budget_resume_at = time.time() + (
                        BUDGET_WINDOW_SECONDS - (_now() - _budget_calls[0]) if _budget_calls else 0)
                    for t in todo[pos:]:
                        _store(t, "budget", None)
                        job.done += 1
                    break
                batch = todo[pos:pos + min(BATCH_SIZE, allowance)]
                if pos:
                    await asyncio.sleep(BATCH_PAUSE_SECONDS)
                await _fetch_batch(client, batch, sem, job)
                pos += len(batch)
            job.fetched_at = time.time()
            job.state = "done"
    except Exception:
        log.exception("battle loot pricing job failed")
        job.crashed = True
        for t in job.types:
            if t not in cached_prices([t]):
                _store(t, "error", None)
        _crashed_until[key] = _now() + CRASH_BACKOFF_SECONDS
    finally:
        job.state = "done"


def get_job(key: str) -> JobState | None:
    return _jobs.get(key)


def ensure_job(key: str, type_ids) -> JobState | None:
    """Start (or join) the pricing job for a report. None when every candidate
    already has a fresh price. Single-flight per key: no await between the
    membership check and the insert, so don't add one."""
    global _job_lock, _job_lock_loop
    types = tuple(type_ids)
    if not types or all(t in cached_prices([t]) for t in types):
        return None
    running = _jobs.get(key)
    if running is not None and running.state != "done":
        return running
    if _crashed_until.get(key, 0) > _now():
        return None
    loop = asyncio.get_running_loop()
    if _job_lock is None or _job_lock_loop is not loop:
        _job_lock, _job_lock_loop = asyncio.Lock(), loop
    job = JobState(total=len(types), types=types)
    _jobs[key] = job
    while len(_jobs) > MAX_JOB_RECORDS:
        del _jobs[next(iter(_jobs))]
    task = loop.create_task(_run_job(_job_lock, key, job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job
