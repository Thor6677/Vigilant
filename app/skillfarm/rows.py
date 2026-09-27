"""Combine one farm pilot's synced skills + settings + prices into a single
render-ready row (T-073). Pure -- no DB, no ESI -- so the edge cases (below
5.5M SP, exactly on the floor, base_sp above the floor, unallocated SP
excluded, not training, a missing price, ...) are directly unit-testable
against crafted summary/skillqueue/price fixtures, independent of the route
and template.
"""
from __future__ import annotations

from app.skillfarm import math as farm_math


def isk_str(value: float | None) -> str:
    """Negative-safe ISK abbreviation (`corporations._format_isk`'s shape,
    duplicated locally per this codebase's existing per-module convention —
    see app/routes/dashboard.py:_format_isk_py and
    app/routes/corporations.py:_format_isk for the other two copies)."""
    if value is None:
        return "—"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e12:
        return f"{sign}{value / 1e12:.2f}T ISK"
    if value >= 1e9:
        return f"{sign}{value / 1e9:.2f}B ISK"
    if value >= 1e6:
        return f"{sign}{value / 1e6:.2f}M ISK"
    return f"{sign}{value:,.0f} ISK"


def duration_str(hours: float | None) -> str:
    if hours is None:
        return "—"
    total_minutes = int(hours * 60)
    if total_minutes <= 0:
        return "now"
    days, rem = divmod(total_minutes, 1440)
    hrs, mins = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hrs}h"
    if hrs > 0:
        return f"{hrs}h {mins}m"
    return f"{mins}m"


def build_pilot_row(
    *,
    pilot_id: int,
    character_id: int,
    character_name: str,
    base_sp: int,
    summary,  # app.character_skills.skill_summary() result: "no_scope" | None | dict
    skillqueue: list[dict] | None,
    lsi_price: float | None,
    extractor_price: float | None,
    plex_price: float | None,
    sales_tax_pct: float,
    plex_per_month: int,
) -> dict:
    """One render-ready row. `status` is "no_scope" | "waiting" | "ok" -- the
    template shows a permission/sync message for the first two and skips the
    numeric fields entirely (they're not present on the returned dict)."""
    row = {
        "id": pilot_id,
        "character_id": character_id,
        "character_name": character_name,
        "base_sp": base_sp,
    }

    if summary == "no_scope":
        row["status"] = "no_scope"
        return row
    if summary is None:
        row["status"] = "waiting"
        return row

    total_sp = int(summary.get("total_sp") or 0)
    unallocated_sp = int(summary.get("unallocated_sp") or 0)
    # Extraction can only pull from ALLOCATED sp -- unallocated is banked but
    # untrained and never counted toward injectors_ready (T-073 spec).
    allocated_sp = total_sp - unallocated_sp

    ready_now = farm_math.injectors_ready(allocated_sp, base_sp)
    sp_needed = farm_math.sp_until_next_injector(allocated_sp, base_sp)
    rate = farm_math.sp_per_hour(skillqueue)
    eta_hours = farm_math.hours_until_next_injector(sp_needed, rate)
    sp_month = farm_math.sp_per_month(rate)
    inj_month = farm_math.injectors_per_month(rate)
    profit, notes = farm_math.monthly_profit(
        rate, lsi_price, extractor_price, sales_tax_pct, plex_per_month, plex_price,
    )
    if rate <= 0:
        notes = ["not currently training"] + notes
    ready_isk = ready_now * lsi_price if lsi_price is not None else None

    row.update({
        "status": "ok",
        "total_sp": total_sp,
        "unallocated_sp": unallocated_sp,
        "allocated_sp": allocated_sp,
        "ready_now": ready_now,
        "ready_isk": ready_isk,
        "ready_isk_str": isk_str(ready_isk),
        "sp_needed": sp_needed,
        "rate_per_hour": rate,
        "eta_hours": eta_hours,
        "eta_str": duration_str(eta_hours),
        "sp_per_month": sp_month,
        "injectors_per_month": inj_month,
        "profit": profit,
        "profit_str": isk_str(profit),
        "notes": notes,
    })
    return row


def totals_row(rows: list[dict]) -> dict:
    """Account-wide totals across every "ok" pilot row. A pilot still
    waiting on its first sync or missing the skills scope contributes
    nothing (not a 0 that would understate "how many are actually ready")."""
    ok = [r for r in rows if r.get("status") == "ok"]
    ready_now = sum(r["ready_now"] for r in ok)
    priced = [r["ready_isk"] for r in ok if r.get("ready_isk") is not None]
    profit_priced = [r["profit"] for r in ok if r.get("profit") is not None]
    sp_month = sum(r["sp_per_month"] for r in ok)
    return {
        "ready_now": ready_now,
        "ready_isk": sum(priced) if priced else None,
        "sp_per_month": sp_month,
        "profit": sum(profit_priced) if profit_priced else None,
    }
