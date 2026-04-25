"""Player-count historical backfill orchestrator.

Loads third-party archives into player_count_snapshots. Idempotent — the
unique constraint on (source, recorded_at) makes re-runs safe.

Sources implemented:
- eve-offline-net (Chribba) via app/intel/eve_offline_net_scraper.py
  Two modes: 'coarse' (one-shot embedded fulldata, ~800 rows) or
  'fine' (chunked zoom-endpoint walk, ~8M rows at 1-min resolution).
- eve-offline-com (Adminor) — placeholder; needs DevTools recon to identify
  the JSON endpoint before the parser can be written. See plan in
  ~/.claude/plans/piped-finding-cray.md.

Cross-validation: when both sources have rows for the same UTC date, we
compute the relative delta and flag mismatches > 2%. The validation report
is returned alongside insert counts so the admin endpoint can surface it.

Fine backfill is long-running (~50 min for the full archive) — kicked off
as a background asyncio task by the admin endpoint. Progress logs land in
the app log, queryable via `docker logs`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.models import AsyncSessionLocal, PlayerCountSnapshot
from app.intel.eve_offline_net_scraper import (
    fetch_chribba_archive,
    fetch_chribba_archive_fine,
)

log = logging.getLogger(__name__)

# Module-level state for the fine backfill so the admin endpoint can poll
# progress without juggling task handles.
_fine_backfill_state: dict = {
    "running": False,
    "started_at": None,
    "windows_done": 0,
    "rows_inserted": 0,
    "last_window_end": None,
    "error": None,
}


async def _bulk_upsert_chunk(rows: list[dict]) -> int:
    """Insert one batch with INSERT OR IGNORE. Returns the newly-inserted
    row count via before/after diff (SQLite doesn't reliably report rowcount
    for batched on-conflict-do-nothing)."""
    if not rows:
        return 0
    async with AsyncSessionLocal() as db:
        # Cheaper than scanning the whole table: use the (source,
        # recorded_at) index range over the rows we're about to insert.
        rows_min = min(r["recorded_at"] for r in rows)
        rows_max = max(r["recorded_at"] for r in rows)
        src = rows[0]["source"]
        before = (
            await db.execute(
                select(func.count())
                .where(PlayerCountSnapshot.source == src)
                .where(PlayerCountSnapshot.recorded_at >= rows_min)
                .where(PlayerCountSnapshot.recorded_at <= rows_max)
            )
        ).scalar() or 0
        # SQLite has param-count limits; chunk the bulk insert
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            stmt = sqlite_insert(PlayerCountSnapshot).values(chunk)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["source", "recorded_at"]
            )
            await db.execute(stmt)
        await db.commit()
        after = (
            await db.execute(
                select(func.count())
                .where(PlayerCountSnapshot.source == src)
                .where(PlayerCountSnapshot.recorded_at >= rows_min)
                .where(PlayerCountSnapshot.recorded_at <= rows_max)
            )
        ).scalar() or 0
        return after - before


async def _run_fine_backfill_inner() -> None:
    """Long-running task body. Walks the archive day-by-day, batched-inserts
    each window's rows, updates module-level progress state for /admin/...
    polling."""
    state = _fine_backfill_state
    state.update({
        "running": True,
        "started_at": datetime.utcnow(),
        "windows_done": 0,
        "rows_inserted": 0,
        "last_window_end": None,
        "error": None,
    })
    log.info("eve-offline-net fine backfill: starting")
    try:
        async for batch in fetch_chribba_archive_fine():
            inserted = await _bulk_upsert_chunk(batch)
            state["windows_done"] += 1
            state["rows_inserted"] += inserted
            state["last_window_end"] = batch[-1]["recorded_at"] if batch else None
            if state["windows_done"] % 50 == 0:
                log.info(
                    "eve-offline-net fine backfill: %d windows, %d rows, last=%s",
                    state["windows_done"], state["rows_inserted"], state["last_window_end"],
                )
    except Exception as e:
        log.exception("eve-offline-net fine backfill failed")
        state["error"] = str(e)
    finally:
        state["running"] = False
        log.info(
            "eve-offline-net fine backfill: done. windows=%d rows=%d error=%s",
            state["windows_done"], state["rows_inserted"], state["error"],
        )


async def validate_overlap() -> dict:
    """Cross-validate eve-offline-net vs eve-offline-com on overlapping
    UTC dates. Flags relative deltas > 2%. Returns a summary dict."""
    async with AsyncSessionLocal() as db:
        net_rows = (
            await db.execute(
                select(PlayerCountSnapshot.recorded_at, PlayerCountSnapshot.player_count)
                .where(PlayerCountSnapshot.source == "eve-offline-net")
            )
        ).all()
        com_rows = (
            await db.execute(
                select(PlayerCountSnapshot.recorded_at, PlayerCountSnapshot.player_count)
                .where(PlayerCountSnapshot.source == "eve-offline-com")
            )
        ).all()

    # Group by UTC date — multiple samples on the same day get averaged.
    def group_by_date(rows):
        out: dict[date, list[int]] = defaultdict(list)
        for ra, pc in rows:
            out[ra.date()].append(pc)
        return {d: round(sum(v) / len(v)) for d, v in out.items()}

    net_by_date = group_by_date(net_rows)
    com_by_date = group_by_date(com_rows)
    overlap = sorted(set(net_by_date) & set(com_by_date))

    mismatches: list[dict] = []
    for d in overlap:
        n, c = net_by_date[d], com_by_date[d]
        denom = max(n, c, 1)
        delta_pct = abs(n - c) / denom * 100.0
        if delta_pct > 2.0:
            mismatches.append({
                "date": d.isoformat(),
                "net": n,
                "com": c,
                "delta_pct": round(delta_pct, 2),
            })

    return {
        "net_rows": len(net_rows),
        "com_rows": len(com_rows),
        "overlap_dates": len(overlap),
        "mismatches_gt_2pct_count": len(mismatches),
        "mismatches_gt_2pct": mismatches[:50],  # cap so JSON response stays small
    }


async def run_backfill(source: str = "all", mode: str = "fine") -> dict:
    """Triggered by the admin endpoint.

    `mode='coarse'` runs the embedded-fulldata one-shot path (fast, ~800 rows).
    `mode='fine'` kicks off the chunked zoom-endpoint walk as a background
    task and returns immediately. Caller can poll fine_backfill_state().

    Returns a summary dict.
    """
    summary: dict = {"sources_run": [], "errors": [], "mode": mode}

    if source in ("all", "net"):
        if mode == "coarse":
            try:
                net_rows = await fetch_chribba_archive()
                inserted = await _bulk_upsert_chunk(net_rows)
                summary["sources_run"].append({
                    "source": "eve-offline-net",
                    "fetched": len(net_rows),
                    "newly_inserted": inserted,
                    "mode": "coarse",
                })
            except Exception as e:
                log.exception("backfill: eve-offline-net coarse failed")
                summary["errors"].append({"source": "eve-offline-net", "error": str(e)})
        else:
            if _fine_backfill_state["running"]:
                summary["errors"].append({
                    "source": "eve-offline-net",
                    "error": "fine backfill already running — see fine_backfill_state",
                })
            else:
                asyncio.create_task(_run_fine_backfill_inner())
                summary["sources_run"].append({
                    "source": "eve-offline-net",
                    "mode": "fine",
                    "status": "started in background — poll /admin/player-count/status",
                })

    if source in ("all", "com"):
        summary["errors"].append({
            "source": "eve-offline-com",
            "error": "Adminor scraper not yet implemented (DevTools recon pending)",
        })

    summary["validation"] = await validate_overlap()
    summary["fine_state"] = dict(_fine_backfill_state)
    return summary


def fine_backfill_state() -> dict:
    """Public snapshot of the fine-backfill task state for /admin polling."""
    return dict(_fine_backfill_state)
