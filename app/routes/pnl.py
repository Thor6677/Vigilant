"""Industry → Trading P&L (Phase 5 Task 5).

Realized-profit tracking via FIFO matching of synced wallet transactions
(buys → sells per type per character). All surfaces auth-gated:

  * `/market/pnl`  — per-type realized P&L table + monthly bar chart, with an
                     optional per-character filter.

Everything is computed **on request** from the `wallet_transactions` table
(filled by the dashboard sync's `transactions` field). The read is bounded by
what's synced — one `SELECT ... ORDER BY date` over a user's characters, fed
straight into the pure `app.market.pnl` engine — so there is no killmail-scale
scan here. The FIFO/fee math and the flat-rate broker/tax assumptions live in
`app/market/pnl.py`; this module is just query + present.

Industry (manufacture-cost) P&L is deliberately OUT OF SCOPE — the page is
titled "Trading P&L" and only reflects market flips. See the Phase 5 follow-up
ticket for industry P&L.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, WalletTransaction, get_db
from app.market import pnl as pnl_engine
from app.sde import lookup as sde

router = APIRouter(tags=["pnl"])
# MUST be named `templates` — main.py's sys.modules loop pushes the nav globals
# onto every Jinja2Templates instance named `templates` under app.routes.*.
templates = Jinja2Templates(directory="app/templates")


async def _user_characters(db: AsyncSession, user_id: int) -> list[Character]:
    rows = (await db.execute(
        select(Character).where(Character.user_id == user_id)
    )).scalars().all()
    return list(rows)


def _fmt_isk(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000_000_000:
        return f"{sign}{a / 1_000_000_000_000:.2f}T"
    if a >= 1_000_000_000:
        return f"{sign}{a / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"{sign}{a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}{a / 1_000:.1f}K"
    return f"{sign}{a:,.2f}"


@router.get("/market/pnl", response_class=HTMLResponse)
async def pnl_page(
    request: Request, character_id: int = 0, db: AsyncSession = Depends(get_db),
):
    """Trading P&L page. `character_id=0` = all of the user's characters."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    chars = await _user_characters(db, user_id)
    char_options = [{"character_id": c.character_id, "name": c.character_name} for c in chars]
    cid_set = {c.character_id for c in chars}
    # Guard the filter: only honour a character_id the user actually owns.
    selected = character_id if character_id in cid_set else 0
    target_cids = [selected] if selected else list(cid_set)

    if not target_cids:
        return templates.TemplateResponse(request, "pnl.html", {
            "char_options": char_options, "selected_character_id": 0,
            "has_rows": False, "rows": [], "monthly": [],
            "totals": None, "unmatched_total": 0,
            "assumptions": _assumptions(),
        })

    tx_rows = (await db.execute(
        select(
            WalletTransaction.transaction_id, WalletTransaction.date,
            WalletTransaction.type_id, WalletTransaction.quantity,
            WalletTransaction.unit_price, WalletTransaction.is_buy,
        )
        .where(WalletTransaction.character_id.in_(target_cids))
        .order_by(WalletTransaction.date)
    )).all()

    if not tx_rows:
        return templates.TemplateResponse(request, "pnl.html", {
            "char_options": char_options, "selected_character_id": selected,
            "has_rows": False, "rows": [], "monthly": [],
            "totals": None, "unmatched_total": 0,
            "assumptions": _assumptions(),
        })

    transactions = [{
        "transaction_id": tid, "date": d, "type_id": type_id,
        "quantity": qty, "unit_price": price, "is_buy": bool(is_buy),
    } for tid, d, type_id, qty, price, is_buy in tx_rows]

    result = pnl_engine.match_fifo(transactions)
    per_type = pnl_engine.aggregate_by_type(result)
    monthly = pnl_engine.aggregate_monthly(result)
    grand = pnl_engine.totals(result)

    name_map = await sde.type_ids_to_names(db, [r["type_id"] for r in per_type])

    # Only surface types that actually realized a flip (matched at least one
    # sell). A type with buys but no sells has no realized P&L to show.
    display_rows = [{
        "type_id": r["type_id"],
        "type_name": name_map.get(r["type_id"], f"Type {r['type_id']}"),
        "realized_isk": r["realized_isk"],
        "realized_isk_str": _fmt_isk(r["realized_isk"]),
        "qty_flipped": r["qty_flipped"],
        "qty_flipped_str": f"{r['qty_flipped']:,}",
        "margin_pct": r["margin_pct"],
        "margin_pct_str": (f"{r['margin_pct']:.1f}%" if r["margin_pct"] is not None else "—"),
        "profit_positive": r["realized_isk"] >= 0,
    } for r in per_type if r["qty_flipped"] > 0]

    monthly_chart = {
        "labels": [m["month"] for m in monthly],
        "realized": [m["realized_isk"] for m in monthly],
    }

    return templates.TemplateResponse(request, "pnl.html", {
        "char_options": char_options,
        "selected_character_id": selected,
        "has_rows": True,
        "rows": display_rows,
        "monthly": monthly_chart,
        "totals": {
            "realized_isk_str": _fmt_isk(grand["realized_isk"]),
            "realized_positive": grand["realized_isk"] >= 0,
            "qty_flipped_str": f"{grand['qty_flipped']:,}",
            "types_traded": grand["types_traded"],
        },
        "unmatched_total": grand["unmatched_sell_qty"],
        "assumptions": _assumptions(),
    })


def _assumptions() -> dict:
    """Flat-rate fee/tax figures surfaced in the page footnote, sourced from the
    single source of truth in the engine so page + math never drift."""
    return {
        "broker_pct": pnl_engine.BROKER_FEE_RATE * 100,
        "sales_tax_pct": pnl_engine.SALES_TAX_RATE * 100,
    }
