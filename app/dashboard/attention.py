"""T-071: needs-attention strip — pure rule engine.

`build_attention()` turns per-character cached-data dicts into a sorted list
of `AttentionItem`. It does no I/O and reads no wall clock of its own (the
caller passes `now`), so it can be tested with arbitrary times. The route
(`app/routes/dashboard_attention.py`) is the only place that touches the
database, the session or the real clock.

The skill-queue and PI thresholds here are deliberate copies of existing ones
elsewhere in the app (skill queue: `app.routes.characters.skill_warning`; PI:
`pi._recompute_expiry`) rather than imports, because those originals read
`datetime.now()` internally and would make this module's output depend on when
the test runs instead of the `now` it's given. `tests/test_dashboard_
attention.py` pins those constants against the module they were copied from,
so drift there fails a test instead of silently forking the two definitions.

Sync staleness is NOT a copy any more (ISS-069): both this module and the
Dashboard route use the one pure implementation in `app.dashboard.staleness`,
which takes `now` explicitly and imports nothing from `app.routes.*`.

## pilot input shape

`build_attention` takes `pilots: list[dict]`, one dict per character, built
by the route from `Character` + `CharacterDashboardCache` rows already in
hand — no ESI calls, no per-pilot query. Each dict:

    character_id       int
    character_name     str
    account_group      str | None                    — Character.account_group, with the UI's own
                                                         "Ungrouped" default folded to None by the route
                                                         (see "Account grouping" below)
    needs_reauth       bool                          — perm_status.token_failed(sync_warnings)
    skillqueue         list[dict] | "no_scope" | None — raw skillqueue_json (ESI shape: skill_id,
                                                         finished_level, start_date, finish_date)
    pi                 list[dict] | "no_scope" | None — raw pi_json planets (planet_id, system_name,
                                                         expiry_time ISO str or None; expiry_warning
                                                         is ignored — recomputed here from expiry_time,
                                                         same reason as pi.py's own _recompute_expiry)
    industry_jobs      list[dict] | "no_scope" | None — raw industry_json jobs (activity_id,
                                                         blueprint_type_id, product_type_id, runs, status,
                                                         and since ISS-064 job_id + end_date, which rows
                                                         cached earlier lack until their next hourly sync)
    industry_synced_at datetime | None (aware UTC)    — field_synced_json["industry"]; see the jobs-ready
                                                         row in the rules table below for why this is used
    sync_status        str                            — "idle" | "syncing" | "error" (the route folds a
                                                         character queued-but-not-started into "syncing",
                                                         matching dashboard()'s own sync_statuses map)
    sync_error         str | None
    last_synced         datetime | None (aware UTC)
    staleness          str (optional)                 — fresh | warning | critical | never, computed by the
                                                         route via app.dashboard.staleness from the pilot's
                                                         granted fields; absent -> judged from last_synced
                                                         alone (the pre-ISS-069 rule)
    no_perms           bool (optional)                — the pilot shares no permissions at all

`"no_scope"` for a field means the character never shared the permission that
feeds it — matches `app.routes.dashboard._build_data_from_caches`'s own
convention — and produces no item for that signal. `None` means the scope is
held but nothing has synced yet; also no item (nothing to warn about yet).

## Account grouping

EVE only lets one pilot per account train at a time, so with N alts on one
account, N-1 of them show an idle skill queue *by design* — that isn't a
per-pilot problem, it's normal. Per-pilot "queue empty"/"queue paused" items
would flood a multi-account owner's strip with one false alarm per idle alt
(the owner this shipped for: 24 pilots on 8 accounts, ~16 idle alts on a
healthy day). So the queue-idle signal is evaluated **per account**, not per
pilot, using the same `account_group` the Dashboard itself already reasons
about training-account counts with (`group_skill_data` in
`app.routes.characters`):

- Pilots sharing a truthy `account_group` are one account.
- A pilot with no `account_group` (`None` — the route folds the UI's default
  "Ungrouped" to this) is its own account, keyed by its `character_id`. This
  is deliberate: several different real EVE accounts can all sit at the
  default "Ungrouped" bucket before their owner organizes them, and treating
  that string as one account would wrongly fold unrelated accounts together.
- Within an account, only pilots that *have* the skill queue scope
  (`skillqueue` not in `("no_scope", None)`) count. An account where every
  pilot lacks the scope produces nothing (nothing to warn about).
- An account is **training** if any counted pilot's `skill_queue_state` is
  `ok`, `warning`, or `critical`. A training account produces no idle item —
  amber/gold still fire per training pilot exactly as before, they're just no
  longer gated on the whole-account check.
- An account with **no** training pilot produces exactly **one** red
  `account_idle` item, never one per idle pilot.

## Rules

| Severity | Signal | Condition | Action | Ages out? |
|---|---|---|---|---|
| red | Re-auth | `needs_reauth` | Renew -> `/account/permissions/{cid}` | never |
| red | Account idle | no pilot on the account is training (see "Account grouping") | Skills -> the paused pilot's page if there is one, else the first counted pilot's | never |
| red | PI expired | any planet's `expiry_time` <= now, and not expired more than `PI_ABANDONED_DAYS` ago | Planets -> `/industry/planetary` | yes, to grey after `AGE_OUT_DAYS`; dropped entirely (no item at all) once past `PI_ABANDONED_DAYS` — an abandoned colony, not something to act on |
| amber | PI ending soon | any planet's `expiry_time` within 24h (pi.py's "critical" <1h band and "warning" <24h band both collapse to this one amber signal here — the brief's own 24h amber threshold already covers pi.py's tighter 1h one) | Planets -> `/industry/planetary` | no (resolves into red or clears on its own) |
| amber | Skill queue critical | `skill_warning` == "critical" (<=7 days by whole-day floor, i.e. up to but not including 8 days) | Skills | no |
| gold | Skill queue warning | `skill_warning` == "warning" (<=14 days, i.e. up to but not including 15 days) | Skills | no |
| gold | Jobs ready to deliver | any cached job has `status == "ready"`, or is "active" with an `end_date` that has passed (finished between hourly syncs) | Jobs -> `/industry/jobs` | yes, to grey after `AGE_OUT_DAYS` |
| grey | No permissions | `no_perms` (the pilot shares nothing, so nothing syncs) | Permissions -> `/account/permissions/{cid}`; never folded into the stale collapse, and such a pilot never produces the stale item | n/a |
| grey | Sync stale / erroring | `sync_status == "error"`, or staleness is "critical" or "never" (`app.dashboard.staleness`: how overdue the most-overdue granted field is); suppressed entirely when the same pilot already has the red re-auth item (redundant), or while `sync_status == "syncing"` | Sync -> POST `/dashboard/sync/{cid}`, unless `SYNC_STALE_COLLAPSE_MIN` or more pilots qualify at once, in which case they collapse into one `sync_stale:many` item naming up to 3 pilots + a count of the rest, dismiss-only (no action button — a per-pilot sync button doesn't fit one row) | n/a |

Sort: severity (red, amber, gold, grey), then `since` oldest first, then
character name, then key (stable tiebreak). A `since` of `None` (onset not
tracked — most of the point conditions above have no persisted "when did
this start" data) sorts **last** within its severity tier: we'd rather
under-claim age than fabricate it.

Display truncation (showing at most 8 items, with the rest behind a
`<details>` "Show all N") is a route/template concern, not this module's —
see `app.routes.dashboard_attention` and `partials/dashboard_attention.html`.
This module always returns the full, untruncated list.

## `since` per signal

Only PI-expired and sync-staleness have a real, persisted onset. The rest are
best-effort:

- PI expired: the earliest `expiry_time` among the expired planets (exact).
- PI ending soon: soonest `expiry_time` minus 24h (when the amber window
  opened, exact).
- Skill queue critical/warning: `queue_end` minus 8 / 15 days (when the
  queue crossed into that band, exact given `skill_warning`'s own thresholds).
- Sync stale: `last_synced` (the last time we know things were fine); the
  collapsed `sync_stale:many` item is `None` — there is no single onset for
  a set of pilots that individually went stale at different times.
- Reauth, account idle: `None` — nothing records when the authorization died
  or an account's last pilot stopped training.
- Jobs ready: the earliest `end_date` among the ready jobs (exact). For cache
  rows written before ISS-064 (no `job_id`/`end_date` yet) it falls back to
  `industry_synced_at`, the last industry sync — a lower bound — and to the
  old tuple-based fingerprint, until the next hourly sync rewrites the row.
  The job_id fingerprint differs from the tuple one, so each previously
  dismissed "jobs ready" item reappears once after that sync.

## Fingerprints and age-out

`fingerprint` hashes only the state that produced the item (planet ids +
expiry times, `queue_end`, the sync error text, ...), never the severity or
the display text. Age-out changes severity and text but **never** `key` or
`fingerprint` — a dismissal recorded while an item was red/gold still applies
after it quietly ages to grey, because the underlying state hasn't
structurally changed, only gotten older.

One exception: PI abandonment (`PI_ABANDONED_DAYS`) DOES change the
fingerprint on a multi-planet `pi_expired` item, because the abandoned
planet is filtered out of the hashed set entirely, not just re-labelled —
unlike the red-to-grey age-out above, this is a real change to which
planets are being aggregated. An "until it changes" dismissal on such an
item lapses the moment one of its planets crosses into abandoned, and the
item reappears (with a smaller count) for whatever's left.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.dashboard import staleness as _shared_staleness
from app.dashboard.staleness import STALE_CRITICAL_SECONDS, STALE_WARNING_SECONDS  # noqa: F401  (re-exported)

# ── Thresholds — each one is a documented copy, not an import (except the
#    staleness ones, which come from app.dashboard.staleness); see the module
#    docstring for why, and tests/test_dashboard_attention.py for the pin
#    against the original. ────────────────────────────────────────────────

# app.routes.characters.skill_warning
SKILL_QUEUE_CRITICAL_DAYS = 7   # <=7 whole days remaining -> "critical"
SKILL_QUEUE_WARNING_DAYS = 14   # <=14 whole days remaining -> "warning"

# app.routes.pi._recompute_expiry (its "critical" <1h and "warning" <24h bands
# both fall inside this module's single amber PI signal — see rules table)
PI_EXPIRY_AMBER_SECONDS = 86400  # 24h

# How long an event-like item (PI expired, job ready) stays at its original
# severity before dropping to grey. Named per the brief's ageing-rules ask.
AGE_OUT_DAYS = 7
AGE_OUT = timedelta(days=AGE_OUT_DAYS)

# PI extractors that expired more than this many days ago are abandoned
# colonies, not attention items -- dropped before aggregation, never shown
# at any severity (not even grey). Distinct from AGE_OUT_DAYS, which only
# steps a still-counted expired extractor down to grey; this is the point
# past which it stops being surfaced at all. T-077.
PI_ABANDONED_DAYS = 14
PI_ABANDONED = timedelta(days=PI_ABANDONED_DAYS)

# 3 or more pilots sharing the grey "sync stale/erroring" signal at once
# collapse into a single dismiss-only summary item instead of one row each
# -- a per-pilot sync button doesn't fit that row, and this is exactly the
# "31 items" complaint the T-077 brief was filed for. See
# _sync_stale_candidates / _sync_stale_items.
SYNC_STALE_COLLAPSE_MIN = 3

_SEVERITY_ORDER = {"red": 0, "amber": 1, "gold": 2, "grey": 3}

# Sort sentinel: a `since` of None sorts after every known `since` within the
# same severity tier (see module docstring).
_UNKNOWN_SINCE = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class AttentionItem:
    key: str
    fingerprint: str
    severity: str  # red | amber | gold | grey
    # None only for the collapsed "N pilots have stale data" item — it has
    # no single character behind it.
    character_id: int | None
    character_name: str
    text: str
    # None/None/None for the collapsed "N pilots have stale data" item only
    # (SYNC_STALE_COLLAPSE_MIN+ pilots) -- it has no single target to act on,
    # so the partial renders dismiss controls only, no action button.
    action_label: str | None
    action_url: str | None
    action_method: str | None  # "get" | "post" | None
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
    """Age-only staleness for a pilot dict that carries no precomputed
    `staleness` (the pre-ISS-069 rule). Same thresholds and classifier as
    app.dashboard.staleness, which the route uses for the real per-field
    answer."""
    if last_synced is None:
        return "never"
    return _shared_staleness.classify((now - last_synced).total_seconds())


def _pilot_staleness(p: dict, now: datetime) -> str:
    pre = p.get("staleness")
    if pre in ("fresh", "warning", "critical", "never"):
        return pre
    return staleness(now, p.get("last_synced"))


def _parse_job_end(job: dict) -> datetime | None:
    raw = job.get("end_date")
    if not raw:
        return None
    try:
        return _parse_iso(str(raw))
    except (ValueError, TypeError):
        return None


def _ready_jobs(jobs: list, now: datetime) -> list[dict]:
    """Jobs to deliver: ESI's "ready", or "active" past its end_date (it
    finished between hourly syncs)."""
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        status = j.get("status")
        if status == "ready":
            out.append(j)
        elif status == "active":
            end = _parse_job_end(j)
            if end is not None and end <= now:
                out.append(j)
    return out


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


def _account_idle_items(pilots: list[dict], now: datetime) -> list[AttentionItem]:
    """One red item per account with no training pilot (see "Account
    grouping" in the module docstring). Pilots without the skill queue scope
    don't count either way; an account where none of them hold it produces
    nothing."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for p in pilots:
        grp = p.get("account_group")
        key = grp if grp else f"\x00solo:{p['character_id']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    out: list[AttentionItem] = []
    for key in order:
        members = groups[key]
        grp = members[0].get("account_group")

        scoped: list[tuple[dict, str]] = []
        for m in members:
            sq = m.get("skillqueue")
            if sq in ("no_scope", None):
                continue
            label, _ = skill_queue_state(sq, now)
            scoped.append((m, label))

        if not scoped:
            continue  # nobody on this account shares the skill queue scope
        if any(label in ("ok", "warning", "critical") for _, label in scoped):
            continue  # somebody on this account is training

        paused = next((m for m, label in scoped if label == "paused"), None)
        target = paused or scoped[0][0]
        fp = _fp("idle", *sorted(f"{m['character_id']}:{label}" for m, label in scoped))

        out.append(AttentionItem(
            key=f"account_idle:{grp or target['character_id']}",
            fingerprint=fp,
            severity="red",
            character_id=target["character_id"],
            character_name=grp or target["character_name"],
            text="no pilot on this account is training",
            action_label="Skills", action_url=f"/character/{target['character_id']}/skills",
            action_method="get",
            since=None,
        ))
    return out


def _sync_stale_candidates(pilots: list[dict], now: datetime) -> list[dict]:
    """Pilots whose sync data is stale or erroring right now (the grey
    signal), minus any already carrying the red re-auth item (redundant —
    a dead token IS why sync is failing) or currently mid-sync (not stale,
    just busy). Each candidate carries its own resolved `text`/`since` so
    both the per-pilot and collapsed renderings below share one
    computation."""
    out: list[dict] = []
    for p in pilots:
        if p.get("needs_reauth") or p.get("sync_status") == "syncing":
            continue
        if p.get("no_perms"):
            continue  # nothing is shared, so nothing syncs (own grey item)
        status = p.get("sync_status", "idle")
        last_synced = p.get("last_synced")
        stale = _pilot_staleness(p, now)
        if status != "error" and stale not in ("critical", "never"):
            continue
        err = (p.get("sync_error") or "")[:120]
        if status == "error":
            text = "sync failing" + (f": {err}" if err else "")
        elif stale == "never":
            text = "never synced"
        else:
            text = f"data is stale ({_ago(now, last_synced)})"
        out.append({
            "character_id": p["character_id"],
            "character_name": p["character_name"],
            "status": status, "err": err, "stale": stale,
            "text": text, "since": last_synced,
        })
    return out


def _sync_stale_items(pilots: list[dict], now: datetime) -> list[AttentionItem]:
    """One grey item per stale/erroring pilot, unless SYNC_STALE_COLLAPSE_MIN
    or more qualify at once, in which case they collapse into a single
    dismiss-only item (key `sync_stale:many`) naming up to 3 pilots plus a
    count of the rest. No action button on the collapsed item — per-pilot
    sync buttons don't fit one summary row."""
    candidates = _sync_stale_candidates(pilots, now)
    if len(candidates) < SYNC_STALE_COLLAPSE_MIN:
        return [
            AttentionItem(
                key=f"sync_stale:{c['character_id']}",
                fingerprint=_fp("sync", c["status"], c["err"], c["stale"]),
                severity="grey", character_id=c["character_id"], character_name=c["character_name"],
                text=c["text"],
                action_label="Sync", action_url=f"/dashboard/sync/{c['character_id']}", action_method="post",
                since=c["since"],
            )
            for c in candidates
        ]

    names = sorted(c["character_name"] for c in candidates)
    shown = names[:3]
    remaining = len(names) - len(shown)
    text = f"{len(candidates)} pilots have stale data — {', '.join(shown)}"
    if remaining > 0:
        text += f" and {remaining} more"
    fp = _fp("sync_many", *sorted(str(c["character_id"]) for c in candidates))
    return [AttentionItem(
        key="sync_stale:many",
        fingerprint=fp,
        severity="grey", character_id=None, character_name="",
        text=text,
        action_label=None, action_url=None, action_method=None,
        since=None,
    )]


# ── Main entry point ─────────────────────────────────────────────────────────

def build_attention(pilots: list[dict], now: datetime) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    items.extend(_account_idle_items(pilots, now))

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

        # ── skill queue: critical (amber) / warning (gold), per training
        #    pilot. "empty"/"paused"/"ok" produce no per-pilot item — the
        #    account-level idle check below covers the whole-account case. ──
        sq = p.get("skillqueue")
        if sq not in ("no_scope", None):
            warning, queue_end = skill_queue_state(sq, now)
            if warning == "critical":
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
            # "empty"/"paused"/"ok" -> handled per-account below, not here

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
                    if (now - exp_dt) > PI_ABANDONED:
                        continue  # abandoned colony -- not an attention item
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
            ready = _ready_jobs(jobs, now)
            if ready:
                if all(j.get("job_id") is not None for j in ready):
                    # ISS-064: fingerprint on the job ids, date from end_date.
                    fp = _fp("ready", *sorted(f"job:{j['job_id']}" for j in ready))
                    ends = [e for e in (_parse_job_end(j) for j in ready) if e is not None]
                    since = min(ends) if ends else p.get("industry_synced_at")
                else:
                    # Row cached before ISS-064: status-only, as before.
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

    # ── no permissions (grey) ───────────────────────────────────────────────
    for p in pilots:
        if p.get("no_perms"):
            items.append(AttentionItem(
                key=f"no_perms:{p['character_id']}",
                fingerprint=_fp("no_perms"),
                severity="grey",
                character_id=p["character_id"], character_name=p["character_name"],
                text="shares no permissions",
                action_label="Permissions",
                action_url=f"/account/permissions/{p['character_id']}", action_method="get",
                since=None,
            ))

    # ── sync stale / erroring (grey) ────────────────────────────────────────
    # A separate pass over all pilots (not inside the per-pilot loop above)
    # because whether this collapses into one item depends on how many
    # pilots qualify at once -- see _sync_stale_items.
    items.extend(_sync_stale_items(pilots, now))

    items.sort(key=lambda it: (
        _SEVERITY_ORDER[it.severity],
        it.since or _UNKNOWN_SINCE,
        it.character_name.lower(),
        it.key,
    ))
    return items
