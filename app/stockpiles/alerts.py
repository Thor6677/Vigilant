"""Stockpile-low alert checker (Phase 5 Task 3).

Hooked into `app.routes.dashboard._background_scheduler` (the same daily/periodic
tick that drives the net-worth snapshot). Each run compares every user's summed
holdings against their stockpile targets and emits a `stockpile_low`
notification through the single choke point `_emit_notification`, so the browser
alert dropdown AND the Discord relay both pick it up with no extra wiring.

**Dedup — "at most once per 24h per target" (module dict idiom, same shape as
`app/notify/discord.py`'s `_last_sent`):** a target that stays under-stocked
across many ticks alerts once, then is suppressed for 24h. When holdings recover
to/above target the suppression key is dropped, so a fresh drop below target
alerts immediately (crossing-below semantics) rather than waiting out a stale
24h window. Suppression is keyed on `(user_id, type_id)`.

The scheduler runs this hourly (matching the hourly asset-cache refresh cadence)
rather than daily, so the 24h suppression window actually does work — a daily
tick alone would make the dedup a no-op.

Seams for testing: `check_user_targets` is pure w.r.t. the DB (takes pre-built
rows), and both the suppression dict and the `emit` callable are injectable, so
"emits once / suppressed second / above-target doesn't emit" is deterministic
without touching the clock or the real notification queue. `now` is a plain
monotonic-seconds float the caller supplies.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.db.models import AsyncSessionLocal, StockpileTarget
from app.sde import lookup as sde
from app.stockpiles.holdings import (
    build_rows,
    holdings_for_user,
    list_targets,
)

logger = logging.getLogger(__name__)

_SUPPRESS_SECONDS = 24 * 60 * 60  # 24h per-(user, type) dedup window

# (user_id, type_id) -> last-emitted monotonic timestamp. Module-global so
# suppression persists across scheduler ticks (like discord.py's _last_sent).
_last_alert: dict[tuple[int, int], float] = {}


def _default_emit(user_id: int, event: dict) -> None:
    """Route an event through the single notification choke point.

    Imported lazily via the module (not `from ... import _emit_notification`) so
    a test monkeypatching `dashboard._emit_notification` actually intercepts the
    call, and to avoid a circular import at module load (dashboard imports a lot).
    """
    from app.routes import dashboard
    dashboard._emit_notification(user_id, event)


def _event_for(row: dict) -> dict:
    """Build the notification event dict for one under-stocked target row."""
    return {
        "type": "stockpile_low",
        "title": "Stockpile Low",
        "body": (
            f"{row['type_name']}: {row['current']:,} / {row['target_qty']:,} "
            f"(short {row['deficit']:,})"
        ),
    }


def check_user_targets(
    user_id: int,
    rows: list[dict],
    *,
    now: float,
    suppress: dict | None = None,
    emit=None,
) -> int:
    """Emit `stockpile_low` for under-stocked rows, with 24h per-target dedup.

    `rows` are the render dicts from `holdings.build_rows` (each has
    `type_id`, `type_name`, `current`, `target_qty`, `deficit`, `under`). Pure
    w.r.t. the DB — the caller supplies rows, the suppression dict, and the emit
    callable (defaults: the module-global dict + `_emit_notification`). Returns
    the number of alerts emitted this call.

    Semantics:
      * under-stocked + not suppressed  -> emit, record now in `suppress`.
      * under-stocked + suppressed (<24h since last) -> skip.
      * at/above target -> drop any suppression key so a later drop re-alerts.
    """
    if suppress is None:
        suppress = _last_alert
    if emit is None:
        emit = _default_emit

    emitted = 0
    for row in rows:
        key = (user_id, row["type_id"])
        if row["under"]:
            last = suppress.get(key)
            if last is not None and (now - last) < _SUPPRESS_SECONDS:
                continue  # already alerted within the suppression window
            suppress[key] = now
            emit(user_id, _event_for(row))
            emitted += 1
        else:
            # Recovered to/above target — forget it so a fresh drop alerts again.
            suppress.pop(key, None)
    return emitted


async def run_stockpile_check(now: float | None = None) -> dict:
    """Background entry point — own session, checks every user with targets.

    Piggybacked on the `_background_scheduler` tick. Loads the distinct set of
    users who have any stockpile target, sums each one's active-character
    holdings, and runs `check_user_targets`. Returns a small summary for the log.
    """
    now = now if now is not None else time.monotonic()
    async with AsyncSessionLocal() as db:
        user_ids = (await db.execute(
            select(StockpileTarget.user_id).distinct()
        )).scalars().all()

        emitted = 0
        for uid in user_ids:
            targets = await list_targets(db, uid)
            if not targets:
                continue
            holdings = await holdings_for_user(db, uid)
            names = await sde.type_ids_to_names(
                db, [t.type_id for t in targets]
            )
            rows = build_rows(targets, holdings, names)
            emitted += check_user_targets(uid, rows, now=now)

    result = {"users": len(user_ids), "emitted": emitted}
    logger.info("stockpile alert check: %s", result)
    return result
