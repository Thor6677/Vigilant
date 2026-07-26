"""Stockpile holdings computation + target CRUD (Phase 5 Task 3).

**Investigation — what "current holdings" can cheaply mean (done up front):**

Only ONE of the three candidate inputs is actually persisted in a sync cache
and thus free to read in a page render / background tick with no extra ESI
round trips:

  * **assets** — `CharacterAssetCache.assets_json`, a flat JSON list of resolved
    asset stacks (`{type_id, quantity, ...}`), refreshed hourly by the dashboard
    sync. This is the same source Task 1's net-worth valuation parses, so we
    reuse its parsing approach (`_parse_assets`).

The other two candidates are deliberately EXCLUDED for v1 (documented on the
page footnote, exactly as net-worth documents its exclusions):

  * **open sell-order quantities** — NOT synced. `CharacterDashboardCache.orders_json`
    is vestigial (never written; Task 1 confirmed no caller writes it), and
    `esi/market.get_character_orders` has no callers. Counting order stock would
    require a new per-character ESI sync field — out of scope here.
  * **in-progress manufacturing output** — fetched live per request in
    `industry_jobs.py`, never persisted. Same story: no cache to read.

So v1 holdings = summed asset stacks only, across the user's ACTIVE characters
(`Character.is_active`). This is a conscious, documented divergence from Task 1's
net-worth job, which snapshots ALL linked characters: a stockpile is about what
you can actually field/use, so a deactivated/removed alt's frozen assets
shouldn't paper over a real deficit. Holdings are summed account-wide (every
station and hangar) — targets carry no location, matching the model.
"""
from __future__ import annotations

import json

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, CharacterAssetCache, StockpileTarget


def _parse_assets(assets_json: str | None) -> list[dict]:
    """Decode one character's cached asset list; tolerate corrupt/absent JSON."""
    if not assets_json:
        return []
    try:
        data = json.loads(assets_json)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def sum_holdings(asset_lists: list[list[dict]]) -> dict[int, int]:
    """Pure math, no I/O. Sum `quantity` per `type_id` across many asset lists.

    Each list is one character's resolved asset stacks. Returns
    `{type_id: total_quantity}` summed account-wide. Rows with no `type_id` are
    ignored; a missing/None quantity counts as 0 (never inflate a stockpile from
    a malformed row). Directly unit-testable against fixture asset JSON.
    """
    totals: dict[int, int] = {}
    for assets in asset_lists:
        for a in assets or []:
            tid = a.get("type_id")
            if tid is None:
                continue
            qty = a.get("quantity") or 0
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                qty = 0
            totals[tid] = totals.get(tid, 0) + qty
    return totals


def compute_deficit(current: int, target: int) -> int:
    """Shortfall of `current` vs `target`, floored at 0 (never negative).

    A surplus is not a deficit — `compute_deficit(120, 100) == 0`.
    """
    d = target - current
    return d if d > 0 else 0


def build_rows(
    targets: list[StockpileTarget],
    holdings: dict[int, int],
    names: dict[int, str] | None = None,
) -> list[dict]:
    """Join targets against summed holdings into render-ready dicts.

    Pure — no DB, no I/O — so the current/deficit/under-stocked flags are
    unit-testable. `names` maps type_id -> display name (resolved by the caller
    from SDE); a missing name falls back to `Type <id>`. Rows are returned in the
    given order (the route sorts before calling).
    """
    names = names or {}
    rows = []
    for t in targets:
        current = int(holdings.get(t.type_id, 0))
        deficit = compute_deficit(current, t.target_qty)
        rows.append({
            "id": t.id,
            "type_id": t.type_id,
            "type_name": names.get(t.type_id) or f"Type {t.type_id}",
            "target_qty": t.target_qty,
            "current": current,
            "deficit": deficit,
            "under": deficit > 0,
            "note": t.note or "",
        })
    return rows


# ── DB helpers ──────────────────────────────────────────────────────────────

async def active_character_ids(db: AsyncSession, user_id: int) -> list[int]:
    """The user's active character_ids (holdings source of truth).

    `Character.is_active` defaults True; a NULL is treated as active too so a
    pre-migration row isn't silently dropped from a stockpile total.
    """
    rows = (await db.execute(
        select(Character.character_id).where(
            Character.user_id == user_id,
            (Character.is_active.is_(True)) | (Character.is_active.is_(None)),
        )
    )).scalars().all()
    return list(rows)


async def holdings_for_user(db: AsyncSession, user_id: int) -> dict[int, int]:
    """Sum every active character's cached assets into `{type_id: quantity}`.

    Two queries total (character ids, then their asset caches in one `IN`) — no
    per-character round trip. Returns an empty dict when the user has no active
    characters or no synced assets yet.
    """
    cids = await active_character_ids(db, user_id)
    if not cids:
        return {}
    rows = (await db.execute(
        select(CharacterAssetCache.assets_json).where(
            CharacterAssetCache.character_id.in_(cids)
        )
    )).scalars().all()
    return sum_holdings([_parse_assets(r) for r in rows])


async def list_targets(db: AsyncSession, user_id: int) -> list[StockpileTarget]:
    """A user's stockpile targets, newest first."""
    rows = (await db.execute(
        select(StockpileTarget)
        .where(StockpileTarget.user_id == user_id)
        .order_by(StockpileTarget.id.desc())
    )).scalars().all()
    return list(rows)


async def add_target(
    db: AsyncSession, user_id: int, type_id: int, target_qty: int,
    note: str | None = None,
) -> StockpileTarget:
    """Insert a new target for the user. Commits. `target_qty` is floored at 0."""
    t = StockpileTarget(
        user_id=user_id,
        type_id=int(type_id),
        target_qty=max(0, int(target_qty)),
        note=(note or "").strip() or None,
    )
    db.add(t)
    await db.commit()
    return t


async def delete_target(db: AsyncSession, user_id: int, target_id: int) -> bool:
    """Delete one of the user's targets by id. Commits.

    Scoped to `user_id` so a crafted id can't delete another account's row.
    Returns True iff a row was removed.
    """
    res = await db.execute(
        sa_delete(StockpileTarget).where(
            StockpileTarget.id == int(target_id),
            StockpileTarget.user_id == user_id,
        )
    )
    await db.commit()
    return bool(res.rowcount)
