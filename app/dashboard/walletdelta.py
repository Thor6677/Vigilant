"""T-076: the 7-day wallet-change arrow shown beside every pilot's wallet in
every dashboard mode (Compact rows, Cards, Detailed, Table).

`load_wallet_baselines()` is the one query for the whole page: a single
SELECT (a `ROW_NUMBER() OVER (PARTITION BY character_id ...)` window,
filtered to `recorded_at <= now - 7d`) returns the latest-before-cutoff
`WalletSnapshot` balance for every character at once — never one query per
pilot. `build_wallet_deltas()` is pure: it just diffs those baselines
against each pilot's already-loaded current wallet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WalletSnapshot

WALLET_DELTA_WINDOW = timedelta(days=7)


async def load_wallet_baselines(
    db: AsyncSession, character_ids: list[int], now: datetime | None = None,
) -> dict[int, float]:
    """{character_id: balance} for the most recent WalletSnapshot at or
    before `now - 7d`, for every id in `character_ids`, in ONE statement.
    A character with no snapshot that old (new to Vigilant, or Vigilant
    itself younger than a week) is simply absent from the result."""
    if not character_ids:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = (now - WALLET_DELTA_WINDOW)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

    row_number = (
        func.row_number()
        .over(
            partition_by=WalletSnapshot.character_id,
            order_by=WalletSnapshot.recorded_at.desc(),
        )
        .label("rn")
    )
    subq = (
        select(WalletSnapshot.character_id, WalletSnapshot.balance, row_number)
        .where(WalletSnapshot.character_id.in_(character_ids))
        .where(WalletSnapshot.recorded_at <= cutoff)
        .subquery()
    )
    rows = (
        await db.execute(select(subq.c.character_id, subq.c.balance).where(subq.c.rn == 1))
    ).all()
    return {cid: balance for cid, balance in rows}


def build_wallet_deltas(
    current_wallets: dict[int, float | None], baselines: dict[int, float],
) -> dict[int, dict]:
    """Pure: {character_id: {"direction": "up"|"down"|"flat", "amount": float}}.
    A pilot with no baseline (no snapshot old enough) or no current wallet at
    all is simply absent from the result — "pilots with no history show
    nothing", never a synthesized zero."""
    out: dict[int, dict] = {}
    for cid, baseline in baselines.items():
        current = current_wallets.get(cid)
        if current is None:
            continue
        delta = current - baseline
        if delta == 0:
            direction = "flat"
        elif delta > 0:
            direction = "up"
        else:
            direction = "down"
        out[cid] = {"direction": direction, "amount": abs(delta)}
    return out
