"""Per-user /dashboard view preferences (T-070), stored on the account in
`user_dashboard_prefs.prefs_json` (see app.db.models.UserDashboardPrefs).

Everything a stored or posted preferences blob might contain is validated
here, never trusted as-is: a browser can post anything, and a stored row can
predate a schema change. `load_prefs()` and `save_prefs()` are the only way
the rest of the app should touch this table.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserDashboardPrefs

# Phase 1 (T-070) renders only "compact" and "cards". A stored/posted mode of
# "detailed" or "table" is accepted and round-trips (so phase 2 doesn't need a
# migration), but the dashboard route falls back to Cards for anything it
# doesn't yet know how to render.
MODES = ("compact", "cards", "detailed", "table")

# The dashboard's lower sections, matched to what dashboard.html actually
# renders below the pilot cards: Wealth by Character, Contracts, Major Fleet
# Battles in New Eden (battles feature), Activity (killmails feature),
# Pilot Pulse (killmails feature), Your Pilots Combat Profile (killmails
# feature), and Recent Kills - zKillboard.
SECTION_KEYS = ("wealth", "contracts", "battles", "activity", "kill_pulse",
                "combat_profile", "recent_kills")

# The three sections that are about New Eden generally, not any one of the
# user's own pilots — what Compact mode hides by default (T-070 task 5).
GAME_WIDE_SECTIONS = ("battles", "activity", "kill_pulse")

# Phase 2's Table view column picker. Defined now so the stored-prefs schema
# doesn't need a migration when that stream ships.
TABLE_COLUMNS = ("pilot", "account", "corporation", "system", "ship", "wallet",
                  "wallet_7d", "net_worth", "queue_end", "training", "pi",
                  "jobs", "orders", "escrow", "clones", "last_sync", "tags",
                  "can_fly")

DEFAULT_PREFS: dict[str, Any] = {
    "mode": "cards",
    "collapsed_groups": [],
    "collapsed_sections": [],
    "hidden_sections": [],
    "table_columns": list(TABLE_COLUMNS),
    "table_sort": {"key": "pilot", "dir": "asc"},
    "tag_filter": [],
}

_MAX_COLLAPSED_GROUPS = 64
_MAX_GROUP_NAME_LEN = 100
_MAX_TAGS = 16
_MAX_TAG_LEN = 24


def _clean_str_list(value: Any, max_items: int, max_len: int) -> list[str] | None:
    """None means "not a list at all" (key ignored/defaulted). A list input
    always yields a list back — non-string items are dropped, over-long
    strings are truncated, and the list itself is capped at `max_items`."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        s = item[:max_len]
        if not s:
            continue
        out.append(s)
        if len(out) >= max_items:
            break
    return out


def _clean_subset(value: Any, allowed: tuple[str, ...]) -> list[str] | None:
    """Order-preserving, de-duplicated subset of `allowed`. None means the
    input wasn't a list at all."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str) and item in allowed and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _clean_table_sort(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    key, direction = value.get("key"), value.get("dir")
    if key in TABLE_COLUMNS and direction in ("asc", "desc"):
        return {"key": key, "dir": direction}
    return None


# One validator per key. Each returns the cleaned value, or None if the
# posted/stored value for that key doesn't validate at all (missing, wrong
# type, out of range) — the caller decides what None means: sanitize() falls
# back to the default, save_prefs() leaves the previously-saved value alone.
_VALIDATORS = {
    "mode": lambda v: v if v in MODES else None,
    "collapsed_groups": lambda v: _clean_str_list(v, _MAX_COLLAPSED_GROUPS, _MAX_GROUP_NAME_LEN),
    "collapsed_sections": lambda v: _clean_subset(v, SECTION_KEYS),
    "hidden_sections": lambda v: _clean_subset(v, SECTION_KEYS),
    "table_columns": lambda v: _clean_subset(v, TABLE_COLUMNS),
    "table_sort": _clean_table_sort,
    "tag_filter": lambda v: _clean_str_list(v, _MAX_TAGS, _MAX_TAG_LEN),
}


def sanitize(raw: Any) -> dict:
    """Turn anything (a dict of unknown shape, a list, None, garbage) into a
    fully valid prefs dict layered over DEFAULT_PREFS. Unknown keys in `raw`
    are dropped; a known key with an invalid value falls back to its default
    rather than failing the whole load."""
    if not isinstance(raw, dict):
        raw = {}
    out = copy.deepcopy(DEFAULT_PREFS)
    for key, validator in _VALIDATORS.items():
        cleaned = validator(raw.get(key))
        if cleaned is not None:
            out[key] = cleaned
    return out


def apply_patch(base: dict, patch: Any) -> dict:
    """Merge a patch onto an already-sanitized prefs dict. Unknown keys are
    dropped; a known key with an invalid value is dropped too (the existing
    value in `base` is left untouched) rather than resetting that key to its
    default — a bad single field shouldn't blow away the rest of a save."""
    result = copy.deepcopy(base)
    if not isinstance(patch, dict):
        return result
    for key, value in patch.items():
        validator = _VALIDATORS.get(key)
        if validator is None:
            continue  # unknown key
        cleaned = validator(value)
        if cleaned is not None:
            result[key] = cleaned
    return result


async def _get_row(db: AsyncSession, user_id: int) -> UserDashboardPrefs | None:
    result = await db.execute(
        select(UserDashboardPrefs).where(UserDashboardPrefs.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def load_prefs(db: AsyncSession, user_id: int) -> dict:
    """The user's saved dashboard prefs, merged over DEFAULT_PREFS. Tolerates
    a missing row, `prefs_json` that isn't valid JSON, and JSON of the wrong
    shape (a list, a string, null) — all of these just mean "use defaults"."""
    row = await _get_row(db, user_id)
    raw: Any = {}
    if row and row.prefs_json:
        try:
            raw = json.loads(row.prefs_json)
        except (ValueError, TypeError):
            raw = {}
    return sanitize(raw)


async def save_prefs(db: AsyncSession, user_id: int, patch: dict) -> dict:
    """Apply `patch` on top of the user's currently-saved prefs and persist
    the result. Returns the full saved prefs dict (not just the patched
    keys), so the caller can re-render immediately."""
    current = await load_prefs(db, user_id)
    merged = apply_patch(current, patch)

    row = await _get_row(db, user_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None:
        row = UserDashboardPrefs(user_id=user_id, prefs_json=json.dumps(merged), updated_at=now)
        db.add(row)
    else:
        row.prefs_json = json.dumps(merged)
        row.updated_at = now
    await db.commit()
    return merged
