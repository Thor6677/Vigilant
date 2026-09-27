"""T-071: needs-attention strip — pure rule engine.

`build_attention()` turns per-character cached-data dicts into a sorted list
of `AttentionItem`. It does no I/O and reads no wall clock of its own (the
caller passes `now`), so it can be tested with arbitrary times. The route
(`app/routes/dashboard_attention.py`) is the only place that touches the
database, the session or the real clock.

Every threshold here is a deliberate copy of an existing one elsewhere in the
app (skill queue: `app.routes.characters.skill_warning`; PI: `pi._recompute_
expiry`; staleness: `app.routes.dashboard.STALE_*_SECONDS`) rather than an
import, because those originals read `datetime.now()` internally and would
make this module's output depend on when the test runs instead of the `now`
it's given. `tests/test_dashboard_attention.py` pins every constant below
against the module it was copied from, so drift there fails a test instead of
silently forking the two definitions.

## pilot input shape

`build_attention` takes `pilots: list[dict]`, one dict per character, built
by the route from `Character` + `CharacterDashboardCache` rows already in
hand — no ESI calls, no per-pilot query. Each dict:

    character_id       int
    character_name     str
    needs_reauth       bool                          — perm_status.token_failed(sync_warnings)
    skillqueue         list[dict] | "no_scope" | None — raw skillqueue_json (ESI shape: skill_id,
                                                         finished_level, start_date, finish_date)
    pi                 list[dict] | "no_scope" | None — raw pi_json planets (planet_id, system_name,
                                                         expiry_time ISO str or None; expiry_warning
                                                         is ignored — recomputed here from expiry_time,
                                                         same reason as pi.py's own _recompute_expiry)
    industry_jobs      list[dict] | "no_scope" | None — raw industry_json jobs (activity_id,
                                                         blueprint_type_id, product_type_id, runs, status)
    industry_synced_at datetime | None (aware UTC)    — field_synced_json["industry"]; see the jobs-ready
                                                         row in the rules table below for why this is used
    sync_status        str                            — "idle" | "syncing" | "error" (the route folds a
                                                         character queued-but-not-started into "syncing",
                                                         matching dashboard()'s own sync_statuses map)
    sync_error         str | None
    last_synced         datetime | None (aware UTC)

`"no_scope"` for a field means the character never shared the permission that
feeds it — matches `app.routes.dashboard._build_data_from_caches`'s own
convention — and produces no item for that signal. `None` means the scope is
held but nothing has synced yet; also no item (nothing to warn about yet).

## Rules

| Severity | Signal | Condition | Action | Ages out? |
|---|---|---|---|---|
| red | Re-auth | `needs_reauth` | Renew -> `/account/permissions/{cid}` | never |
| red | Empty skill queue | raw `skillqueue` list is empty | Skills -> `/character/{cid}/skills` | never |
| red | PI expired | any planet's `expiry_time` <= now | Planets -> `/industry/planetary` | yes, to grey after `AGE_OUT_DAYS` |
| amber | PI ending soon | any planet's `expiry_time` within 24h (pi.py's "critical" <1h band and "warning" <24h band both collapse to this one amber signal here — the brief's own 24h amber threshold already covers pi.py's tighter 1h one) | Planets -> `/industry/planetary` | no (resolves into red or clears on its own) |
| amber | Skill queue critical | `skill_warning` == "critical" (<=7 days by whole-day floor, i.e. up to but not including 8 days) | Skills | no |
| gold | Skill queue warning | `skill_warning` == "warning" (<=14 days, i.e. up to but not including 15 days) | Skills | no |
| gold | Jobs ready to deliver | any cached job has `status == "ready"` | Jobs -> `/industry/jobs` | yes, to grey after `AGE_OUT_DAYS` |
| grey | Skill queue paused | `skill_warning` == "paused" (queue non-empty but no pending entry has a finish date, e.g. every entry already finished) | Skills | n/a (already lowest) |
| grey | Sync stale / erroring | `sync_status == "error"`, or staleness is "critical" or "never" (own copy of `STALE_*_SECONDS`); suppressed entirely when the same pilot already has the red re-auth item (redundant), or while `sync_status == "syncing"` | Sync -> POST `/dashboard/sync/{cid}` | n/a |

Sort: severity (red, amber, gold, grey), then `since` oldest first, then
character name, then key (stable tiebreak). A `since` of `None` (onset not
tracked — most of the point conditions above have no persisted "when did
this start" data) sorts **last** within its severity tier: we'd rather
under-claim age than fabricate it.

## `since` per signal

Only PI-expired and sync-staleness have a real, persisted onset. The rest are
best-effort:

- PI expired: the earliest `expiry_time` among the expired planets (exact).
- PI ending soon: soonest `expiry_time` minus 24h (when the amber window
  opened, exact).
- Skill queue critical/warning: `queue_end` minus 8 / 15 days (when the
  queue crossed into that band, exact given `skill_warning`'s own thresholds).
- Sync stale: `last_synced` (the last time we know things were fine).
- Reauth, empty queue, paused queue: `None` — nothing records when the
  authorization died or the queue ran dry.
- Jobs ready: `industry_synced_at`, i.e. the last time the industry field was
  synced — a **lower bound**, not the job's actual completion time, because
  the cached job dict has neither `job_id` nor `end_date` (see the docstring
  note in the route module). Documented as an out-of-scope gap in the T-071
  report: the cache trim in `app.routes.dashboard.fetch_industry_jobs_data`
  would need both fields for a precise timestamp and for a `job_id`-based
  fingerprint instead of the tuple-based one used here.

## Fingerprints and age-out

`fingerprint` hashes only the state that produced the item (planet ids +
expiry times, `queue_end`, the sync error text, ...), never the severity or
the display text. Age-out changes severity and text but **never** `key` or
`fingerprint` — a dismissal recorded while an item was red/gold still applies
after it quietly ages to grey, because the underlying state hasn't
structurally changed, only gotten older.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ── Thresholds — each one is a documented copy, not an import; see the
#    module docstring for why, and tests/test_dashboard_attention.py for the
#    pin against the original. ────────────────────────────────────────────────

# app.routes.characters.skill_warning
SKILL_QUEUE_CRITICAL_DAYS = 7   # <=7 whole days remaining -> "critical"
SKILL_QUEUE_WARNING_DAYS = 14   # <=14 whole days remaining -> "warning"

# app.routes.pi._recompute_expiry (its "critical" <1h and "warning" <24h bands
# both fall inside this module's single amber PI signal — see rules table)
PI_EXPIRY_AMBER_SECONDS = 86400  # 24h

# app.routes.dashboard.STALE_WARNING_SECONDS / STALE_CRITICAL_SECONDS
STALE_WARNING_SECONDS = 900    # 15 min
STALE_CRITICAL_SECONDS = 1800  # 30 min

# How long an event-like item (PI expired, job ready) stays at its original
# severity before dropping to grey. Named per the brief's ageing-rules ask.
AGE_OUT_DAYS = 7
AGE_OUT = timedelta(days=AGE_OUT_DAYS)

_SEVERITY_ORDER = {"red": 0, "amber": 1, "gold": 2, "grey": 3}

# Sort sentinel: a `since` of None sorts after every known `since` within the
# same severity tier (see module docstring).
_UNKNOWN_SINCE = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class AttentionItem:
    key: str
    fingerprint: str
    severity: str  # red | amber | gold | grey
    character_id: int
    character_name: str
    text: str
    action_label: str
    action_url: str
    action_method: str  # "get" | "post"
    since: datetime | None


# ── Small pure helpers ───────────────────────────────────────────────────────

def _fp(*parts: str) -> str:
    """Short, stable hash of the state that produced an item."""
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _parse_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ago(now: datetime, then: datetime) -> str:
    age = (now - then).total_seconds()
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago"
    return f"{int(age // 86400)}d ago"


def _remaining(now: datetime, until: datetime) -> str:
    secs = (until - now).total_seconds()
    if secs <= 0:
        return "now"
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    minutes = int((secs % 3600) // 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def staleness(now: datetime, last_synced: datetime | None) -> str:
    """Own copy of app.routes.dashboard._staleness — see module docstring."""
    if last_synced is None:
        return "never"
    age = (now - last_synced).total_seconds()
    if age > STALE_CRITICAL_SECONDS:
        return "critical"
    if age > STALE_WARNING_SECONDS:
        return "warning"
    return "fresh"


def skill_queue_state(raw_queue: list | None, now: datetime) -> tuple[str, datetime | None]:
    """Own copy of the (queue, queue_end) -> warning logic in
    app.routes.characters._process_skillqueue + skill_warning. Returns
    (warning, queue_end); warning is one of empty|paused|critical|warning|ok.

    Deliberately checks emptiness on the RAW queue, not the pending-only
    list: a queue whose every entry has already finished is "paused" on the
    dashboard card, not "empty" — see module docstring.
    """
    if not raw_queue:
        return "empty", None

    now_naive_ok = now  # just documents that `now` must be tz-aware
    pending = []
    for entry in raw_queue:
        finish_raw = entry.get("finish_date")
        if finish_raw:
            try:
                finish_dt = _parse_iso(finish_raw)
            except (ValueError, TypeError):
                pending.append(entry)
                continue
            if finish_dt <= now_naive_ok:
                continue  # already completed
        pending.append(entry)

    queue_end = None
    if pending:
        finish_raw = pending[-1].get("finish_date")
        if finish_raw:
            try:
                queue_end = _parse_iso(finish_raw)
            except (ValueError, TypeError):
                queue_end = None

    if queue_end is None:
        return "paused", None

    days = int((queue_end - now).total_seconds() // 86400)
    if days <= SKILL_QUEUE_CRITICAL_DAYS:
        return "critical", queue_end
    if days <= SKILL_QUEUE_WARNING_DAYS:
        return "warning", queue_end
    return "ok", queue_end


def pi_expiry_state(expiry_time_raw: str | None, now: datetime) -> tuple[str | None, datetime | None]:
    """Own copy of app.routes.pi._recompute_expiry's thresholds. Returns
    (warning, expiry_dt); warning is expired|critical|warning|ok|None."""
    if not expiry_time_raw:
        return None, None
    try:
        expiry_dt = _parse_iso(expiry_time_raw)
    except (ValueError, TypeError):
        return None, None
    delta = (expiry_dt - now).total_seconds()
    if delta <= 0:
        return "expired", expiry_dt
    if delta < 3600:
        return "critical", expiry_dt
    if delta < PI_EXPIRY_AMBER_SECONDS:
        return "warning", expiry_dt
    return "ok", expiry_dt


# ── Main entry point ─────────────────────────────────────────────────────────

def build_attention(pilots: list[dict], now: datetime) -> list[AttentionItem]:
    items: list[AttentionItem] = []

    for p in pilots:
        cid = p["character_id"]
        name = p["character_name"]

        # ── red: authorization expired ──────────────────────────────────────
        if p.get("needs_reauth"):
            items.append(AttentionItem(
                key=f"reauth:{cid}",
                fingerprint=_fp("reauth"),
                severity="red",
                character_id=cid, character_name=name,
                text="authorization expired, EVE no longer accepts this token",
                action_label="Renew", action_url=f"/account/permissions/{cid}", action_method="get",
                since=None,
            ))

        # ── skill queue: empty (red) / critical (amber) / warning (gold) /
        #    paused (grey) ────────────────────────────────────────────────────
        sq = p.get("skillqueue")
        if sq not in ("no_scope", None):
            warning, queue_end = skill_queue_state(sq, now)
            if warning == "empty":
                items.append(AttentionItem(
                    key=f"queue_empty:{cid}", fingerprint=_fp("empty"),
                    severity="red", character_id=cid, character_name=name,
                    text="skill queue is empty",
                    action_label="Skills", action_url=f"/character/{cid}/skills", action_method="get",
                    since=None,
                ))
            elif warning == "critical":
                since = queue_end - timedelta(days=SKILL_QUEUE_CRITICAL_DAYS + 1) if queue_end else None
                items.append(AttentionItem(
                    key=f"queue_critical:{cid}",
                    fingerprint=_fp("critical", queue_end.isoformat() if queue_end else ""),
                    severity="amber", character_id=cid, character_name=name,
                    text=f"skill queue ends in {_remaining(now, queue_end)}",
                    action_label="Skills", action_url=f"/character/{cid}/skills", action_method="get",
                    since=since,
                ))
            elif warning == "warning":
                since = queue_end - timedelta(days=SKILL_QUEUE_WARNING_DAYS + 1) if queue_end else None
                items.append(AttentionItem(
                    key=f"queue_warning:{cid}",
                    fingerprint=_fp("warning", queue_end.isoformat() if queue_end else ""),
                    severity="gold", character_id=cid, character_name=name,
                    text=f"skill queue ends in {_remaining(now, queue_end)}",
                    action_label="Skills", action_url=f"/character/{cid}/skills", action_method="get",
                    since=since,
                ))
            elif warning == "paused":
                qlen = len(sq)
                items.append(AttentionItem(
                    key=f"queue_paused:{cid}", fingerprint=_fp("paused", str(qlen)),
                    severity="grey", character_id=cid, character_name=name,
                    text=f"skill queue is paused ({qlen} queued)",
                    action_label="Skills", action_url=f"/character/{cid}/skills", action_method="get",
                    since=None,
                ))
            # "ok" -> no item

        # ── PI: expired (red, ages to grey) / ending soon (amber) ──────────
        pi = p.get("pi")
        if pi not in ("no_scope", None):
            expired: list[tuple[int | None, datetime, str]] = []
            urgent: list[tuple[int | None, datetime, str]] = []
            for planet in pi:
                warn, exp_dt = pi_expiry_state(planet.get("expiry_time"), now)
                if warn is None:
                    continue
                label = planet.get("system_name") or f"Planet {planet.get('planet_id')}"
                if warn == "expired":
                    expired.append((planet.get("planet_id"), exp_dt, label))
                elif warn in ("critical", "warning"):
                    urgent.append((planet.get("planet_id"), exp_dt, label))

            if expired:
                expired.sort(key=lambda t: t[1])
                oldest = expired[0][1]
                fp = _fp("expired", *sorted(f"{pid}:{dt.isoformat()}" for pid, dt, _ in expired))
                aged = (now - oldest) > AGE_OUT
                if aged:
                    sev = "grey"
                    text = (f"PI extractor expired on {expired[0][2]} ({_ago(now, oldest)})"
                            if len(expired) == 1
                            else f"{len(expired)} PI extractors expired, oldest {_ago(now, oldest)}")
                else:
                    sev = "red"
                    text = (f"PI extractor expired on {expired[0][2]}"
                            if len(expired) == 1
                            else f"{len(expired)} PI extractors expired")
                items.append(AttentionItem(
                    key=f"pi_expired:{cid}", fingerprint=fp, severity=sev,
                    character_id=cid, character_name=name, text=text,
                    action_label="Planets", action_url="/industry/planetary", action_method="get",
                    since=oldest,
                ))

            if urgent:
                urgent.sort(key=lambda t: t[1])
                soonest = urgent[0][1]
                fp = _fp("urgent", *sorted(f"{pid}:{dt.isoformat()}" for pid, dt, _ in urgent))
                since = soonest - timedelta(hours=24)
                text = (f"PI extractor on {urgent[0][2]} ends in {_remaining(now, soonest)}"
                        if len(urgent) == 1
                        else f"{len(urgent)} PI extractors end within 24h")
                items.append(AttentionItem(
                    key=f"pi_critical:{cid}", fingerprint=fp, severity="amber",
                    character_id=cid, character_name=name, text=text,
                    action_label="Planets", action_url="/industry/planetary", action_method="get",
                    since=since,
                ))

        # ── industry jobs ready to deliver (gold, ages to grey) ─────────────
        jobs = p.get("industry_jobs")
        if jobs not in ("no_scope", None):
            ready = [j for j in jobs if j.get("status") == "ready"]
            if ready:
                fp = _fp("ready", *sorted(
                    f"{j.get('activity_id')}:{j.get('blueprint_type_id')}:"
                    f"{j.get('product_type_id')}:{j.get('runs')}"
                    for j in ready
                ))
                since = p.get("industry_synced_at")
                aged = since is not None and (now - since) > AGE_OUT
                sev = "grey" if aged else "gold"
                if aged:
                    text = (f"job ready to deliver ({_ago(now, since)})"
                            if len(ready) == 1
                            else f"{len(ready)} jobs ready to deliver ({_ago(now, since)})")
                else:
                    text = ("job ready to deliver"
                            if len(ready) == 1
                            else f"{len(ready)} jobs ready to deliver")
                items.append(AttentionItem(
                    key=f"jobs_ready:{cid}", fingerprint=fp, severity=sev,
                    character_id=cid, character_name=name, text=text,
                    action_label="Jobs", action_url="/industry/jobs", action_method="get",
                    since=since,
                ))

        # ── sync stale / erroring (grey) ────────────────────────────────────
        # Suppressed when the pilot already carries the red re-auth item
        # (redundant — a dead token IS why sync is failing), and while a sync
        # is actively in flight (not "stale", just busy).
        if not p.get("needs_reauth") and p.get("sync_status") != "syncing":
            status = p.get("sync_status", "idle")
            last_synced = p.get("last_synced")
            stale = staleness(now, last_synced)
            if status == "error" or stale in ("critical", "never"):
                err = (p.get("sync_error") or "")[:120]
                if status == "error":
                    text = "sync failing" + (f": {err}" if err else "")
                elif stale == "never":
                    text = "never synced"
                else:
                    text = f"data is stale ({_ago(now, last_synced)})"
                items.append(AttentionItem(
                    key=f"sync_stale:{cid}",
                    fingerprint=_fp("sync", status, err, stale),
                    severity="grey", character_id=cid, character_name=name, text=text,
                    action_label="Sync", action_url=f"/dashboard/sync/{cid}", action_method="post",
                    since=last_synced,
                ))

    items.sort(key=lambda it: (
        _SEVERITY_ORDER[it.severity],
        it.since or _UNKNOWN_SINCE,
        it.character_name.lower(),
        it.key,
    ))
    return items
