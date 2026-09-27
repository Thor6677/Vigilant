"""T-076 Table mode: one row-builder per pilot, keyed by
`app.dashboard.prefs.TABLE_COLUMNS`. Pure — no I/O, no `datetime.now()` —
takes values the route already computed (a `build_pilot_summaries()` entry,
a Detailed-mode `detail` dict from `app.dashboard.detail`, a tags row, a
wallet-delta entry, and the raw `queue_end` datetime `skill_map` already
carries) and reshapes them into one row of `{"text", "sort"}` cells.

Every cell carries both a display `text` and a `sort` value so the
page-level script can client-side-sort by `data-sort-value` without ever
re-deriving anything from `text` (which may contain HTML-escaped entities,
locale formatting, etc. — `sort` is always a plain str/int/float).
"""
from __future__ import annotations

from datetime import datetime


def _isk(amount: float | None) -> str:
    """Local copy of the ISK abbreviation used across this codebase — see
    app.routes.dashboard._format_isk_py / app.skillfarm.rows.isk_str for the
    other two. Duplicated rather than imported to avoid a route -> table ->
    route import cycle (dashboard.py imports this module)."""
    if amount is None:
        return "—"
    amount = float(amount)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_000_000_000_000:
        return f"{sign}{amount / 1_000_000_000_000:.2f}T ISK"
    if amount >= 1_000_000_000:
        return f"{sign}{amount / 1_000_000_000:.2f}B ISK"
    if amount >= 1_000_000:
        return f"{sign}{amount / 1_000_000:.2f}M ISK"
    return f"{sign}{amount:,.0f} ISK"


def _lower(s: str | None) -> str:
    return (s or "").lower()


# A large but finite epoch timestamp (year 3000) for "no queue end" / "never
# synced" sort values. `float("inf")` round-trips through Jinja's `tojson`
# as the *string* "Infinity", which `parseFloat` reads back as `NaN` in the
# page's client-side sort — a finite sentinel avoids that entirely.
_FAR_FUTURE_TS = 32_503_680_000.0


def _delta_text(delta: dict | None) -> str:
    if not delta:
        return ""
    if delta["direction"] == "flat":
        return "±0"
    arrow = "▲" if delta["direction"] == "up" else "▼"
    return f"{arrow} {_isk(delta['amount'])}"


def _delta_sort(delta: dict | None) -> float:
    if not delta:
        return 0.0
    amt = delta["amount"]
    return -amt if delta["direction"] == "down" else amt


def build_table_row(
    summary: dict,
    detail: dict | None,
    tags_row: dict | None,
    wallet_delta: dict | None,
    queue_end: datetime | None,
    last_synced: datetime | None = None,
) -> dict:
    """`summary` is one build_pilot_summaries() entry. `detail` is that
    pilot's app.dashboard.detail extras (None outside Detailed/Table, or for
    a pilot with nothing synced yet — every column below tolerates that).
    `tags_row` is a load_character_tags() entry or None. `queue_end` is the
    raw datetime app.routes.characters.group_skill_data's skill_map already
    carries per character (`skill_map[cid]["queue_end"]`) — used only for a
    correct chronological sort, since `training.finish_str` is text.
    `last_synced` is the raw datetime off CharacterDashboardCache.last_synced
    (the same value `app.routes.dashboard._age_str` formats into
    `sync.last_str`) — same reason: sorting "Last Sync" by its display text
    ("5m ago" vs "2h ago" vs "1d ago") would sort lexically, not
    chronologically.
    """
    tags_row = tags_row or {"tags": [], "note": None}
    detail = detail or {}
    training = summary.get("training") or {}
    clones = summary.get("clones") or {}
    sync = summary.get("sync") or {}
    pi = detail.get("pi")
    industry = detail.get("industry")
    market = detail.get("market")
    net_worth = detail.get("net_worth")
    wallet = summary.get("wallet")

    training_text = "—"
    if training.get("skill"):
        training_text = f"{training['skill']} {training['level']}"
    elif training.get("warning") == "paused":
        training_text = "Paused"
    elif training.get("warning") == "empty":
        training_text = "Empty queue"
    elif training.get("warning") == "no_scope":
        training_text = "—"

    tags_list = tags_row.get("tags") or []

    cells = {
        "pilot": {"text": summary.get("name", ""), "sort": _lower(summary.get("name"))},
        "account": {"text": summary.get("account", ""), "sort": _lower(summary.get("account"))},
        "corporation": {"text": summary.get("corporation_name") or "—", "sort": _lower(summary.get("corporation_name"))},
        "system": {"text": summary.get("system_name") or "Unknown", "sort": _lower(summary.get("system_name"))},
        "ship": {"text": summary.get("ship_name") or "—", "sort": _lower(summary.get("ship_name"))},
        "wallet": {"text": _isk(wallet) if wallet is not None else "—", "sort": wallet if wallet is not None else -1.0},
        "wallet_7d": {"text": _delta_text(wallet_delta), "sort": _delta_sort(wallet_delta)},
        "net_worth": {"text": _isk(net_worth) if net_worth is not None else "—", "sort": net_worth if net_worth is not None else -1.0},
        "queue_end": {
            "text": training.get("finish_str") or "—",
            "sort": queue_end.timestamp() if queue_end else _FAR_FUTURE_TS,
        },
        "training": {"text": training_text, "sort": _lower(training.get("skill"))},
        "pi": {"text": pi["expiry_str"] if pi else "—", "sort": pi["colonies"] if pi else 0},
        "jobs": {"text": f"{industry['active']} active" if industry else "—", "sort": industry["active"] if industry else 0},
        "orders": {"text": f"{market['open_orders']} open" if market else "—", "sort": market["open_orders"] if market else 0},
        "escrow": {"text": _isk(market["escrow"]) if market else "—", "sort": market["escrow"] if market else 0.0},
        "clones": {
            "text": f"{clones['count']} clone{'s' if clones.get('count') != 1 else ''}" if clones.get("count") is not None else "—",
            "sort": clones.get("count") or 0,
        },
        "last_sync": {
            "text": sync.get("last_str") or "never",
            "sort": last_synced.timestamp() if last_synced else -1.0,
        },
        "tags": {"text": ", ".join(tags_list), "sort": _lower(", ".join(tags_list))},
        "can_fly": {"text": "…", "sort": 0},
    }

    return {
        "character_id": summary["character_id"],
        "account": summary.get("account", ""),
        "tags": tags_list,
        "flags": summary.get("flags", []),
        "needs_reauth": summary.get("needs_reauth", False),
        "cells": cells,
    }


def sort_table_rows(rows: list[dict], table_sort: dict) -> list[dict]:
    """Stable sort by one column's precomputed `sort` value. Unknown/missing
    keys fall back to "pilot" so a stale persisted sort (e.g. a column that
    was removed from the picker) never raises."""
    key = table_sort.get("key") or "pilot"
    reverse = table_sort.get("dir") == "desc"

    def _key(row):
        cell = row["cells"].get(key) or row["cells"]["pilot"]
        return cell["sort"]

    return sorted(rows, key=_key, reverse=reverse)
