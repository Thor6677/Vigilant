"""Hub Scan jobs. In-process: the registry and progress live in this module,
in the one app process (the same assumption the order cache and the route
memo make). After a restart there are no jobs; the saved result is what
remains.

One job per user. At most MAX_RUNNING run; the rest wait their turn. Every
job's upstream requests go through one shared `Pacer`, so two scans together
are each slower rather than pushing harder on EVE's API.

The registry keeps each user's latest job after it ends, so the page can say
"failed" or "cancelled". It is keyed by user id and a new scan replaces the
user's old entry, so it is bounded by the number of users, not by scans run.

A job: expand the list -> benchmark each item -> fill the five summaries per
item -> retry the items that failed, once -> compute rows -> save. Twenty
item failures in a row stop it early and save what it has as "incomplete".
Cancelling, or any unexpected error, saves nothing: the previous result stays.
A watchlist that is gone (or no longer the user's) by save time fails the job
with nothing saved.

The semaphore and pacer belong to one event loop; `reset()` rebuilds them and
`start()` creates them on first use.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db.models import AsyncSessionLocal
from app.esi.market import APPRAISAL_HUBS
from app.market import hub_cost, hub_route, scan_fetch, scan_lists, scan_results, watchlists
from app.market import lp as market_lp
from app.sde import lookup as sde

logger = logging.getLogger(__name__)

MAX_RUNNING = 2
ITEMS_AT_ONCE = 4            # items worked on together; the Pacer bounds the requests
STOP_AFTER_FAILURES = 20
ACTIVE = ("queued", "running")
LIST_GONE = "That list no longer exists."


@dataclass
class Job:
    user_id: int
    kind: str
    ref: int
    settings: scan_results.Settings
    state: str = "queued"        # queued | running | done | failed | cancelled
    done: int = 0
    total: int = 0
    label: str = ""
    message: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)


_jobs: dict[int, Job] = {}
_slots: asyncio.Semaphore | None = None
_pacer: scan_fetch.Pacer | None = None


def _now() -> datetime:
    """Clock seam, monkeypatched in tests. Call it when the time is needed."""
    return datetime.now(timezone.utc)


def reset() -> None:
    """Forget all jobs and rebuild the shared limits (tests; also safe at
    startup). Only call with no jobs running: a running job would keep the old
    limits and the new ones would not know about it."""
    global _slots, _pacer
    _jobs.clear()
    _slots = asyncio.Semaphore(MAX_RUNNING)
    _pacer = scan_fetch.Pacer()


def get(user_id: int) -> Job | None:
    return _jobs.get(user_id)


def _on_done(job: Job, task: asyncio.Task) -> None:
    """Fallback for a task that ended without setting a final state (e.g.
    cancelled before its first step, so its own handler never ran)."""
    if job.state in ACTIVE:
        if task.cancelled():
            job.state = "cancelled"
        else:
            job.state, job.message = "failed", "The scan failed. Scan again."


def start(user_id: int, kind: str, ref: int, settings: scan_results.Settings) -> Job:
    """Start a scan, or return the user's queued/running one. Must be called
    from the running event loop (a request handler)."""
    global _slots, _pacer
    current = _jobs.get(user_id)
    if current is not None and current.state in ACTIVE:
        return current
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_RUNNING)
    if _pacer is None:
        _pacer = scan_fetch.Pacer()
    job = Job(user_id, kind, ref, settings)
    coro = _run(job)
    try:
        task = asyncio.create_task(coro)
    except BaseException:
        coro.close()
        raise                       # nothing registered: no job stuck queued
    job.task = task
    task.add_done_callback(lambda t: _on_done(job, t))
    _jobs[user_id] = job
    return job


def cancel(user_id: int) -> bool:
    job = _jobs.get(user_id)
    if job is None or job.state not in ACTIVE or job.task is None:
        return False
    job.task.cancel()
    return True


def forget(user_id: int) -> None:
    """Drop the user's entry, cancelling a queued or running job first (an
    account being removed). A job cancelled before its save saves nothing."""
    job = _jobs.pop(user_id, None)
    if job is not None and job.state in ACTIVE and job.task is not None:
        job.task.cancel()


async def _wait_all(coros) -> list[asyncio.Task]:
    """Run coroutines together and return the finished tasks. Every child is
    finished, and its exception retrieved, before this returns or propagates:
    if this task is cancelled the unfinished children are cancelled and
    awaited, and children that had already failed are read so Python does not
    log "Task exception was never retrieved". (A plain gather also waits for
    cancelled children on current Python; the point here is retrieval and the
    guarantee that no child outlives the job.)"""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.wait(tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if any(t.cancelled() for t in tasks):
        raise asyncio.CancelledError()
    return tasks


async def _fill_item(type_id: int) -> None:
    """All five summaries for one item, each on its own session. Raises if any fails."""
    async def one(hub: dict) -> None:
        async with AsyncSessionLocal() as db:
            await scan_fetch.ensure_summary(db, hub["region_id"], hub["station_id"],
                                            type_id, _now(), _pacer)
    tasks = await _wait_all([one(h) for h in APPRAISAL_HUBS.values()])
    errors = [t.exception() for t in tasks]      # reads them all: none goes unretrieved
    for err in errors:
        if err is not None:
            raise err


async def _fill(job: Job | None, type_ids: list[int]) -> tuple[set[int], bool]:
    """(items that failed, stopped early?). Advances job.done per item when
    `job` is given (the main pass; the retry pass passes None)."""
    failed: set[int] = set()
    streak = 0
    stop = False

    async def item(type_id: int) -> None:
        try:
            await _fill_item(type_id)
        finally:
            if job is not None:
                job.done += 1

    for i in range(0, len(type_ids), ITEMS_AT_ONCE):
        chunk = type_ids[i:i + ITEMS_AT_ONCE]
        tasks = await _wait_all([item(t) for t in chunk])
        for type_id, task in zip(chunk, tasks):
            err = task.exception()
            if err is None:
                streak = 0
            else:
                logger.info("hub scan: item %s failed: %s", type_id, err)
                failed.add(type_id)
                streak += 1
                if streak >= STOP_AFTER_FAILURES:
                    stop = True      # consecutive in list order, checked per item
        if stop:
            failed.update(type_ids[i + ITEMS_AT_ONCE:])
            return failed, True
    return failed, False


async def _routes(db, settings: scan_results.Settings) -> dict:
    if settings.origin_system_id is None:
        return {}
    hubs = APPRAISAL_HUBS
    stations = await sde.stations_by_ids(db, [h["station_id"] for h in hubs.values()])
    systems = {k: stations[h["station_id"]]["system_id"]
               for k, h in hubs.items() if h["station_id"] in stations}
    return await hub_route.hub_routes(db, settings.origin_system_id, systems)


async def _run(job: Job) -> None:
    try:
        async with _slots:
            job.state = "running"
            async with AsyncSessionLocal() as db:
                try:
                    listing = await scan_lists.expand(db, job.user_id, job.kind, job.ref)
                except scan_lists.ListNotFound:
                    job.state, job.message = "failed", LIST_GONE
                    return
                job.label, job.total = listing.label, len(listing.type_ids)
                notes = list(listing.notes)
                if listing.total > len(listing.type_ids):
                    notes.append(f"{len(listing.type_ids)} of {listing.total} scanned; "
                                 "pick a narrower list to cover the rest.")
                # Once per scan; {} when EVE's list could not be fetched.
                price_map = await market_lp.get_price_map(db)
                if not price_map:
                    notes.append("EVE's price list was unavailable, so build costs are missing.")
                benchmarks = {}
                for type_id in listing.type_ids:
                    try:
                        benchmarks[type_id] = await hub_cost.benchmark(
                            db, type_id, price_map=price_map)
                    except Exception:
                        # One bad lookup is one item without a build cost.
                        logger.warning("hub scan: benchmark failed for %s", type_id,
                                       exc_info=True)
                        await db.rollback()
                try:
                    routes = await _routes(db, job.settings)
                except Exception:
                    logger.warning("hub scan: routes failed", exc_info=True)
                    await db.rollback()
                    # {} means no starting system; with one set, say it failed.
                    routes = ({} if job.settings.origin_system_id is None else
                              {k: hub_route.UNAVAILABLE for k in APPRAISAL_HUBS})

            failed, stopped = await _fill(job, listing.type_ids)
            if failed and not stopped:
                # One more try for the ones that failed, without advancing progress.
                failed, stopped = await _fill(
                    None, [t for t in listing.type_ids if t in failed])

            async with AsyncSessionLocal() as db:
                summaries = await scan_results.load_summaries(db, listing.type_ids)
                rows = scan_results.compute_rows(summaries, listing.type_ids, listing.names,
                                                 benchmarks, job.settings, failed, _now())
                # A watchlist deleted while it was being scanned gets no
                # result saved after the fact. (Deleting cancels a running
                # scan of the list first; this covers a delete that lands
                # between that and here.) Checked on the saving session.
                if (job.kind == "watchlist"
                        and await watchlists.get(db, job.user_id, job.ref) is None):
                    job.state, job.message = "failed", LIST_GONE
                    return
                await scan_results.save(
                    db, job.user_id, job.kind, job.ref, label=listing.label,
                    status="incomplete" if stopped else "complete",
                    item_total=listing.total,
                    item_scanned=len(listing.type_ids) - len(failed),
                    settings=job.settings, notes=notes, routes=routes, rows=rows,
                    now=_now())
                job.state = "done"      # saved: a cancel after this keeps the result
    except asyncio.CancelledError:
        if job.state != "done":
            job.state = "cancelled"
        raise
    except Exception:
        logger.exception("hub scan: job for user %s failed", job.user_id)
        job.state, job.message = "failed", "The scan failed. Scan again."
