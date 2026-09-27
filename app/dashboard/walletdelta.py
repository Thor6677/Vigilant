"""T-076: the 7-day wallet-change arrow shown beside every pilot's wallet in
every dashboard mode (Compact rows, Cards, Detailed, Table).

`load_wallet_baselines()` is the one query for the whole page — but see
T-080: a `ROW_NUMBER() OVER (PARTITION BY character_id ...)` window over
every `wallet_snapshots` row at or before the cutoff forces SQLite to rank
the WHOLE filtered set before it can pick row 1 of each partition, which the
planner measured at 8.27s for 24 pilots against a real install's ~1.9M-row
table (30 pilots x six months, kept a year). The fix keeps it to ONE
statement while making SQLite do a separate, tiny, INDEX-backed seek per
character: `UNION ALL` of one `SELECT <cid>, (<the per-character LIMIT-1
lookup>)` branch per id. Each branch's subquery is a plain `character_id =
<cid> AND recorded_at <= :cutoff ORDER BY recorded_at DESC LIMIT 1`, which
`ix_wallet_snapshots_char_recorded` answers as a single index seek — the
planner measured 24 individual per-character lookups like this one at
0.7ms *combined* against the 1.9M-row production copy. On our own
~300k-row fixture (a smaller table, so smaller absolute numbers either
way), the OLD ROW_NUMBER() query measures ~63ms and this NEW UNION ALL
query measures ~1.3ms — see tests/test_dashboard_walletdelta_perf.py for
both the EXPLAIN QUERY PLAN and the timing. `build_wallet_deltas()` is
pure: it just diffs those baselines against each pilot's already-loaded
current wallet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WalletSnapshot

WALLET_DELTA_WINDOW = timedelta(days=7)


def _build_wallet_baselines_stmt(character_ids: list[int], cutoff: datetime):
    """The query itself, split out of load_wallet_baselines() so
    tests/test_dashboard_walletdelta_perf.py can run EXPLAIN QUERY PLAN on
    exactly what production executes. `character_ids` must be non-empty —
    the public function handles the empty-list short-circuit.

    One UNION ALL branch per character id, each carrying its own
    independent (uncorrelated) "latest balance at or before cutoff" scalar
    subquery — every branch is its own index seek on
    ix_wallet_snapshots_char_recorded, never a scan of the whole table.
    """
    branches = [
        select(
            literal(cid).label("character_id"),
            (
                select(WalletSnapshot.balance)
                .where(WalletSnapshot.character_id == cid)
                .where(WalletSnapshot.recorded_at <= cutoff)
                .order_by(WalletSnapshot.recorded_at.desc())
                .limit(1)
                .scalar_subquery()
            ).label("balance"),
        )
        for cid in character_ids
    ]
    return union_all(*branches)


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

    rows = (await db.execute(_build_wallet_baselines_stmt(character_ids, cutoff))).all()
    return {cid: balance for cid, balance in rows if balance is not None}


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
