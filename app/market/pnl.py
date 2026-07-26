"""Trading P&L — FIFO transaction matching (Phase 5 Task 5).

PURE module: no I/O, no DB, no ESI. Everything here is a deterministic function
of the input transaction list, so the matcher can be tested exhaustively
against hand-computed numbers (see tests/test_pnl.py).

The realized-profit model FIFO-matches buys against sells **per type_id**:

  * Buys enter a per-type FIFO queue as lots ``(qty, unit_price, date, source)``.
  * Sells consume the oldest lots first. A sell larger than the head lot spans
    multiple lots; a sell smaller than the head lot splits it (the remainder
    stays at the front of the queue).
  * A sell with no available lot (the buy predates our synced history) is
    "unmatched" — its quantity is counted and **excluded** from P&L rather than
    guessed at.

Lots carry a ``source``: "trade" (default) or "build". A trade lot is stock you
bought off the market — you paid a broker's fee to place that buy order, so its
cost basis is marked up by ``(1 + broker_fee)``. A build lot is stock you
manufactured (injected later from completed industry jobs) — there is no
acquisition broker fee, so it enters the queue at its raw ``unit_price`` (the
job's per-unit build cost). Sell-side fees are symmetric across both sources.
This asymmetry is the whole point of the source tag: mis-charging a broker fee
on manufactured stock would understate build profit.

Fees/taxes are NOT taken from the journal (linking a fee row to a specific fill
is unreliable). Instead we apply flat, configurable rates — the same assumption
surfaced prominently on the page:

  * BROKER_FEE_RATE  (1.5%) applies on BOTH sides — you pay broker's fee to
    place a buy order and again to place a sell order.
  * SALES_TAX_RATE   (3.37%) applies to SELLS only.

Per matched unit:
    buy_cost_eff  = buy_price  * (1 + broker_buy)
    sell_rev_eff  = sell_price * (1 - sales_tax - broker_sell)
    profit        = (sell_rev_eff - buy_cost_eff) * qty

"Avg margin %" is a *cost-weighted* margin — total realized profit divided by
total effective buy cost basis — NOT a naive average of per-match percentages
(a plain mean over-weights tiny lots). This is the correct figure for P&L.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# ── Flat-rate fee/tax assumptions (documented on /market/pnl) ────────────────
BROKER_FEE_RATE = 0.015    # 1.5% each side (buy order placement + sell order)
SALES_TAX_RATE = 0.0337    # 3.37% on sells only


@dataclass
class _Lot:
    """A remaining chunk of a buy, sitting in the FIFO queue."""
    qty: int
    unit_price: float
    date: object  # datetime | str — opaque to the matcher, echoed into rows
    source: str = "trade"  # "trade" (bought) | "build" (manufactured)


def _sort_key(t: dict):
    """Chronological order, tie-broken by transaction_id (monotonic with time).

    Fixtures without a transaction_id fall back to 0; Python's stable sort then
    preserves their input order as a last resort.
    """
    return (t["date"], t.get("transaction_id", 0))


def match_fifo(
    transactions: list[dict],
    *,
    broker_fee: float = BROKER_FEE_RATE,
    sales_tax: float = SALES_TAX_RATE,
) -> dict:
    """FIFO-match buys against sells per type_id.

    Each transaction dict needs: ``type_id``, ``quantity`` (>0), ``unit_price``,
    ``is_buy`` (bool), ``date``. ``transaction_id`` is optional (tiebreaker) and
    ``source`` is optional ("trade" default | "build"); build lots skip the
    acquisition broker fee (see module docstring).

    Returns ``{type_id: {"lots": [...remaining buys...], "realized": [...match
    rows...], "unmatched_sell_qty": int}}``. Each match row:
        {type_id, qty, buy_price, sell_price, buy_date, sell_date,
         cost_basis, proceeds, profit, lot_source}
    where cost_basis/proceeds are the fee-adjusted effective totals for that
    matched quantity, profit = proceeds - cost_basis, and lot_source echoes the
    consumed lot's source. Leftover-lot dicts echo ``source`` only when it is
    non-default ("build"), keeping trade-only output byte-identical.
    """
    # Per-type FIFO queue of buy lots + accumulated match rows.
    queues: dict[int, deque[_Lot]] = {}
    realized: dict[int, list[dict]] = {}
    unmatched: dict[int, int] = {}

    def _bucket(type_id: int):
        if type_id not in queues:
            queues[type_id] = deque()
            realized[type_id] = []
            unmatched[type_id] = 0

    for t in sorted(transactions, key=_sort_key):
        type_id = t["type_id"]
        qty = int(t["quantity"])
        if qty <= 0:
            continue
        _bucket(type_id)
        price = float(t["unit_price"])

        if t["is_buy"]:
            queues[type_id].append(_Lot(
                qty=qty, unit_price=price, date=t["date"],
                source=t.get("source", "trade")))
            continue

        # Sell — consume oldest lots first.
        remaining = qty
        q = queues[type_id]
        while remaining > 0 and q:
            lot = q[0]
            take = min(remaining, lot.qty)
            # Build lots enter at raw build cost (no acquisition broker fee);
            # trade lots carry the broker markup you paid to place the buy order.
            buy_cost_eff = (
                lot.unit_price if lot.source == "build"
                else lot.unit_price * (1 + broker_fee)
            )
            sell_rev_eff = price * (1 - sales_tax - broker_fee)
            cost_basis = buy_cost_eff * take
            proceeds = sell_rev_eff * take
            realized[type_id].append({
                "type_id": type_id,
                "qty": take,
                "buy_price": lot.unit_price,
                "sell_price": price,
                "buy_date": lot.date,
                "sell_date": t["date"],
                "cost_basis": cost_basis,
                "proceeds": proceeds,
                "profit": proceeds - cost_basis,
                "lot_source": lot.source,
            })
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                q.popleft()
        if remaining > 0:
            # Sell with no matching buy in our history (pre-sync) — excluded.
            unmatched[type_id] += remaining

    def _lot_out(lot: _Lot) -> dict:
        # Echo ``source`` only for non-default (build) lots so trade-only output
        # stays byte-identical to the pre-source-tag shape.
        out = {"qty": lot.qty, "unit_price": lot.unit_price, "date": lot.date}
        if lot.source != "trade":
            out["source"] = lot.source
        return out

    return {
        type_id: {
            "lots": [_lot_out(lot) for lot in queues[type_id]],
            "realized": realized[type_id],
            "unmatched_sell_qty": unmatched[type_id],
        }
        for type_id in queues
    }


def aggregate_by_type(result: dict) -> list[dict]:
    """Per-type roll-up of a `match_fifo` result, sorted by realized profit desc.

    Each row: {type_id, realized_isk, qty_flipped, cost_basis, proceeds,
    margin_pct, unmatched_sell_qty}. ``margin_pct`` is cost-weighted
    (realized_isk / cost_basis * 100), None when nothing matched.
    """
    rows = []
    for type_id, data in result.items():
        matches = data["realized"]
        realized_isk = sum(m["profit"] for m in matches)
        cost_basis = sum(m["cost_basis"] for m in matches)
        proceeds = sum(m["proceeds"] for m in matches)
        qty_flipped = sum(m["qty"] for m in matches)
        trade_profit = sum(m["profit"] for m in matches if m["lot_source"] == "trade")
        build_profit = sum(m["profit"] for m in matches if m["lot_source"] == "build")
        margin_pct = (realized_isk / cost_basis * 100) if cost_basis > 0 else None
        rows.append({
            "type_id": type_id,
            "realized_isk": realized_isk,
            "qty_flipped": qty_flipped,
            "cost_basis": cost_basis,
            "proceeds": proceeds,
            "margin_pct": margin_pct,
            "trade_profit": trade_profit,
            "build_profit": build_profit,
            "unmatched_sell_qty": data["unmatched_sell_qty"],
        })
    rows.sort(key=lambda r: r["realized_isk"], reverse=True)
    return rows


def _month_key(d) -> str:
    """Bucket a match's sell_date into a YYYY-MM label. Accepts datetime or ISO
    string (the matcher echoes whatever the caller passed through)."""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m")
    return str(d)[:7]


def aggregate_monthly(result: dict) -> list[dict]:
    """Realized profit bucketed by the SELL month, oldest-first.

    Each row: {month: "YYYY-MM", realized_isk, qty_flipped}. Buckets are keyed
    on the realizing (sell) side — profit is booked when the flip closes.
    """
    buckets: dict[str, dict] = {}
    for data in result.values():
        for m in data["realized"]:
            key = _month_key(m["sell_date"])
            b = buckets.setdefault(key, {
                "month": key, "realized_isk": 0.0, "qty_flipped": 0,
                "trade_profit": 0.0, "build_profit": 0.0,
            })
            b["realized_isk"] += m["profit"]
            b["qty_flipped"] += m["qty"]
            if m["lot_source"] == "build":
                b["build_profit"] += m["profit"]
            else:
                b["trade_profit"] += m["profit"]
    return [buckets[k] for k in sorted(buckets)]


def totals(result: dict) -> dict:
    """Account-wide totals across every type_id.

    {realized_isk, qty_flipped, cost_basis, unmatched_sell_qty, types_traded}.
    """
    realized_isk = 0.0
    qty_flipped = 0
    cost_basis = 0.0
    unmatched = 0
    trade_profit = 0.0
    build_profit = 0.0
    for data in result.values():
        for m in data["realized"]:
            realized_isk += m["profit"]
            qty_flipped += m["qty"]
            cost_basis += m["cost_basis"]
            if m["lot_source"] == "build":
                build_profit += m["profit"]
            else:
                trade_profit += m["profit"]
        unmatched += data["unmatched_sell_qty"]
    return {
        "realized_isk": realized_isk,
        "qty_flipped": qty_flipped,
        "cost_basis": cost_basis,
        "trade_profit": trade_profit,
        "build_profit": build_profit,
        "unmatched_sell_qty": unmatched,
        "types_traded": sum(1 for d in result.values() if d["realized"]),
    }
