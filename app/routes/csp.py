"""CSP violation report sink (ISS-023).

Accepts browser-submitted CSP violation reports at POST /csp-report and
appends them as JSON Lines to /data/logs/csp-violations.jsonl. Used by
the T-031 / T-033 migration work to diff violation counts before/after
template changes — without a server-side sink, violations only surface
in DevTools, which doesn't scale across 50+ templates.

Behavior:
- Always returns 204 (per spec, browsers ignore the response anyway).
- Best-effort logging — failures here never propagate to the user.
- Each line is one JSON object: {ts, ip, ua, doc, directive, blocked,
  source, line, col, policy, disposition, count}.
- File-size cap at 10 MB with one-level rotation
  (csp-violations.jsonl.1) so the log can't fill the disk during a
  migration soak that ramps up violations transiently.
- Repeats are collapsed, and total writes are rate-capped. See below.

## Why the sink dedupes (2026-09-21)

A browser reports EVERY violation, and a page that violates on a timer
reports on that timer forever. The admin Overview re-fetches itself every
10 seconds; a stale session there turned each tick into six refused inline
scripts, so one forgotten tab wrote ~2,400 identical rows an hour and
rotated the 10 MB log daily — burying the signal the sink exists to
provide and making the size cap meaningless, since the interesting rows
are always the ones that just got rotated away.

Two bounds, both in-process and both cheap:

1. **Dedupe window.** Reports are keyed on
   (doc, directive, blocked, source, line, col) — the identity of the
   violation, deliberately excluding ts/ip/ua so the same violation from
   one client collapses. The first report of a key is written straight
   away (the sink stays useful for a reload-and-tail check), and repeats
   inside `_DEDUPE_WINDOW_S` are counted rather than written. When the
   window closes, one rollup line carrying the window's full `count` is
   written, so no report is ever silently lost — only merged.

2. **Per-minute write cap.** A hard ceiling of `_MAX_WRITES_PER_MIN`
   lines, whatever the dedupe does, because the key includes line/column
   and a pathological page could mint unique keys indefinitely. Dropped
   reports are counted and surfaced as a single `{"dropped": N}` line
   once the minute rolls over, so the log says so rather than quietly
   under-reporting.

Both counters live in module state, which means per worker process — the
bounds multiply by worker count. That is deliberate: a shared counter
would need a lock or a store for a diagnostic log, and the point is to
turn thousands of rows into a handful, not to be exact.

The matching `report-uri /csp-report` directive lives in
app/middleware/csp_nonce.py:_CSP_TEMPLATE.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Response

router = APIRouter()
log = logging.getLogger(__name__)

_LOG_DIR = Path("/data/logs")
_LOG_PATH = _LOG_DIR / "csp-violations.jsonl"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB before rotation

# Collapse identical reports seen inside this many seconds into one line.
_DEDUPE_WINDOW_S = 600  # 10 minutes

# Ceiling on lines written per minute, across all keys. 20/min is ~7.5 MB a
# day at the observed row size — comfortably under the 10 MB rotation point,
# so a misbehaving client can no longer rotate the log out from under an
# investigation.
_MAX_WRITES_PER_MIN = 20

# Ceiling on how many distinct violations are tracked at once. The key
# includes line and column, so a page minting fresh coordinates could grow
# this without bound; evicting the oldest keeps the memory flat and costs
# only dedupe quality for a client that is already pathological.
_MAX_WINDOWS = 2000

# key -> [window_opened_at, reports_seen_in_window, last_row]
# `last_row` is kept so the rollup line describes the same violation with a
# fresh timestamp rather than replaying the first sighting's. Insertion
# order is window-open order, which is what makes eviction "oldest first".
_windows: dict[tuple, list] = {}

# [minute_started_at, lines_written_this_minute, reports_dropped_this_minute]
_rate = [0.0, 0, 0]


def _now() -> float:
    """Monotonic clock, isolated so tests can drive the windows directly."""
    return time.monotonic()


def _rotate_if_needed() -> None:
    """One-level rotation. Keeps disk pressure bounded during ramp."""
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size >= _MAX_BYTES:
            backup = _LOG_PATH.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            _LOG_PATH.rename(backup)
    except OSError as e:
        log.warning(f"csp-report log rotation failed: {e}")


def _key(row: dict) -> tuple:
    """Violation identity. ts/ip/ua are excluded on purpose — the same
    violation reported a thousand times is one fact, not a thousand."""
    return (row.get("doc"), row.get("directive"), row.get("blocked"),
            row.get("source"), row.get("line"), row.get("col"))


def _truncate(value, n=512):
    """Cap field length so a hostile client can't bloat the log with one
    massive policy string."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        value = json.dumps(value, separators=(",", ":"))
    s = str(value)
    return s if len(s) <= n else s[:n] + "…"


def _expired_rollups(now: float) -> list[dict]:
    """Close every dedupe window older than `_DEDUPE_WINDOW_S`.

    Returns one rollup row per window that actually suppressed something.
    Windows that saw a single report are simply forgotten — that report was
    already written when the window opened.

    Sweeping on each incoming report (rather than on a timer) means a key
    that stops firing has its rollup written by the next report of ANY key,
    which is the only traffic this endpoint gets. A sink that goes quiet
    keeps its last rollup pending, which is the right trade for a log with
    no background task of its own.
    """
    rollups = []
    for key, state in list(_windows.items()):
        opened, seen, row = state
        if now - opened < _DEDUPE_WINDOW_S:
            continue
        del _windows[key]
        if seen > 1:
            rollups.append({**row, "ts": _iso_now(), "count": seen,
                            "window_s": _DEDUPE_WINDOW_S})
    return rollups


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(rows: list[dict]) -> None:
    """Append rows as JSON Lines, honouring the per-minute write cap.

    The cap is applied here rather than at the dedupe layer so that rollups,
    first-sightings and the drop notice all draw on one budget — otherwise
    the cap bounds only part of the traffic and the guarantee is hollow.
    """
    if not rows:
        return

    now = _now()
    if now - _rate[0] >= 60:
        dropped = _rate[2]
        _rate[0], _rate[1], _rate[2] = now, 0, 0
        if dropped:
            # State the loss rather than under-reporting silently; a reader
            # diffing counts across a migration needs to know the sink was
            # saturated.
            rows.insert(0, {"ts": _iso_now(), "dropped": dropped,
                            "note": "per-minute write cap"})

    allowed = []
    for row in rows:
        if _rate[1] >= _MAX_WRITES_PER_MIN:
            _rate[2] += 1
            continue
        _rate[1] += 1
        allowed.append(row)
    if not allowed:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed()
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        for row in allowed:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


@router.post("/csp-report", status_code=204)
async def csp_report(request: Request) -> Response:
    try:
        body = await request.body()
        # Browsers send either application/csp-report (legacy report-uri,
        # body is {"csp-report": {...}}) or application/reports+json
        # (modern Reporting API, body is [{"type": "csp-violation", ...}]).
        # We accept whatever shape arrives and log the violation object(s).
        if not body:
            return Response(status_code=204)

        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=204)

        violations: list[dict] = []
        if isinstance(payload, dict) and "csp-report" in payload:
            violations.append(payload["csp-report"])
        elif isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict):
                    violations.append(entry.get("body") or entry)
        elif isinstance(payload, dict):
            violations.append(payload)

        if not violations:
            return Response(status_code=204)

        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        ts = _iso_now()
        now = _now()

        rows = _expired_rollups(now)
        for v in violations:
            row = {
                "ts": ts,
                "ip": client_ip,
                "ua": _truncate(user_agent, 256),
                "doc": _truncate(v.get("document-uri") or v.get("documentURL")),
                "directive": _truncate(
                    v.get("violated-directive") or v.get("effectiveDirective"), 64
                ),
                "blocked": _truncate(v.get("blocked-uri") or v.get("blockedURL")),
                "source": _truncate(v.get("source-file") or v.get("sourceFile")),
                "line": v.get("line-number") or v.get("lineNumber"),
                "col": v.get("column-number") or v.get("columnNumber"),
                "disposition": _truncate(v.get("disposition"), 16),
            }
            key = _key(row)
            window = _windows.get(key)
            if window is None:
                while len(_windows) >= _MAX_WINDOWS:
                    # Oldest-opened first: dicts iterate in insertion order.
                    evicted = _windows.pop(next(iter(_windows)))
                    if evicted[1] > 1:
                        rows.append({**evicted[2], "ts": ts,
                                     "count": evicted[1],
                                     "window_s": _DEDUPE_WINDOW_S})
                # First sighting: write it now, so a "change a template and
                # tail the log" check still answers immediately, and open a
                # window for the repeats that are about to follow.
                _windows[key] = [now, 1, row]
                rows.append({**row, "count": 1})
            else:
                window[1] += 1

        _emit(rows)
    except Exception as e:  # noqa: BLE001 — best-effort sink
        log.warning(f"csp-report write failed: {e}")

    return Response(status_code=204)
