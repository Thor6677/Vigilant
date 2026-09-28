"""ISS-069: per-pilot sync staleness, judged against the pilot's own cadence.

The Dashboard badge and the needs-attention strip both ask "is this pilot's
data stale?". They used to answer it from the age of the last sync alone, so a
pilot who shares only hourly fields (or none, i.e. zkill only) showed "stale"
for half of every hour although nothing was wrong: the scheduler
(`app.routes.dashboard._any_field_stale`) only queues a pilot when one of its
GRANTED fields passes its cache window, so such a pilot legitimately syncs
hourly.

Staleness is now how overdue the pilot's MOST-overdue granted field is:

    max over granted f of (now - field_synced[f] - FIELD_CACHE_SECONDS[f])

with the same 15 / 30 minute grace on top as warning / critical. A granted
field that has never synced makes the pilot "never".

This module is pure: no I/O, no clock of its own (callers pass `now`), and it
imports nothing from `app.routes.*`, so `app.dashboard.attention` can use it
without depending on the route layer. `app.routes.dashboard` re-exports the
tables below (`FIELD_CACHE_SECONDS`, `FIELD_SCOPES`, `STALE_*_SECONDS`) so
existing imports keep working; this module is their single home.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.auth import scopes as perms

# ── ESI cache timers (seconds) — from ESI swagger Cache-Control: max-age ─────
# https://esi.evetech.net/latest/swagger.json
FIELD_CACHE_SECONDS: dict[str, int] = {
    "wallet":        120,   # ESI max-age: 120s
    "location":       60,   # ESI max-age:   5s  — 60s is adequate for a dashboard
    "clones":       3600,   # ESI max-age: 3600s
    "notifications": 600,   # ESI max-age: 600s — feeds structure-alert banners
    "contracts":     300,   # ESI max-age: 300s
    "pi":            600,   # ESI max-age: 600s
    "skillqueue":    120,   # ESI max-age: 120s
    "skills":       3600,   # T-073: total SP / trained levels change slowly
    "zkill":        3600,   # zkillboard — 1h is plenty
    "assets":       3600,   # ESI max-age: 3600s
    "roles":        3600,   # corp roles — rarely change, cached for permission checks
    "transactions": 3600,   # wallet fills — immutable; hourly incremental page-back is plenty
    "orders":       3600,   # ESI max-age: 1200s; hourly is enough for net-worth escrow
    "industry":     3600,   # ESI max-age: 300s; hourly is enough for WIP valuation
}

FIELD_SCOPES: dict[str, str | None] = {
    "wallet":        perms.WALLET,
    "location":      perms.LOCATION,
    "clones":        perms.CLONES,
    "notifications": perms.NOTIFICATIONS,
    "contracts":     perms.CONTRACTS,
    "pi":            perms.PLANETS,
    "skillqueue":    perms.SKILLQUEUE,
    "skills":        perms.SKILLS,   # T-073
    "zkill":         None,   # no ESI scope required
    "assets":        perms.ASSETS,
    "roles":         perms.CORP_ROLES,
    "transactions":  perms.WALLET,   # same scope as wallet balance
    "orders":        perms.ORDERS,
    "industry":      perms.JOBS,
}

# Grace on top of a field's own cache window before the badge changes colour.
STALE_WARNING_SECONDS = 900   # 15 min: yellow indicator
STALE_CRITICAL_SECONDS = 1800  # 30 min: red indicator + manual resync button


def has_permissions(scopes: str | None) -> bool:
    """False for a pilot that shares no permissions at all (empty `scopes`)."""
    return bool((scopes or "").strip())


def granted_fields(scopes: str | None) -> list[str]:
    """Fields the scheduler syncs for a pilot holding `scopes` (zkill needs none)."""
    held = scopes or ""
    return [f for f in FIELD_CACHE_SECONDS
            if not FIELD_SCOPES[f] or FIELD_SCOPES[f] in held]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_field_synced(raw: str | dict | None) -> dict[str, datetime]:
    """`CharacterDashboardCache.field_synced_json` -> {field: aware datetime}.
    Unparseable entries are left out (they read as "never synced")."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, datetime] = {}
    for field, val in raw.items():
        if isinstance(val, datetime):
            out[field] = _aware(val)
        elif isinstance(val, str) and val:
            try:
                out[field] = _aware(datetime.fromisoformat(val))
            except ValueError:
                continue
    return out


def classify(overdue_seconds: float) -> str:
    """Map "seconds past the allowed window" to fresh | warning | critical."""
    if overdue_seconds > STALE_CRITICAL_SECONDS:
        return "critical"
    if overdue_seconds > STALE_WARNING_SECONDS:
        return "warning"
    return "fresh"


def staleness(now: datetime, scopes: str | None,
              field_synced: str | dict | None,
              last_synced: datetime | None = None) -> str:
    """fresh | warning | critical | never, from the pilot's granted fields.

    `field_synced` is the raw field_synced_json (str or dict). A granted field
    with no stamp (newly added, or field_synced_json was reset) falls back to
    `last_synced` (the cache row's column; naive means UTC) as its stamp, so
    such a pilot ages normally. "never" only when a granted field has no stamp
    AND there is no `last_synced` either (nothing has ever synced)."""
    synced = parse_field_synced(field_synced)
    fallback = _aware(last_synced) if last_synced is not None else None
    worst = None
    for field in granted_fields(scopes):
        ts = synced.get(field) or fallback
        if ts is None:
            return "never"
        overdue = (now - ts).total_seconds() - FIELD_CACHE_SECONDS[field]
        if worst is None or overdue > worst:
            worst = overdue
    return classify(worst) if worst is not None else "fresh"
