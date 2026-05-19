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
  source, line, col, policy, disposition}.
- File-size cap at 10 MB with one-level rotation
  (csp-violations.jsonl.1) so the log can't fill the disk during a
  migration soak that ramps up violations transiently.

The matching `report-uri /csp-report` directive lives in
app/middleware/csp_nonce.py:_CSP_TEMPLATE.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Response

router = APIRouter()
log = logging.getLogger(__name__)

_LOG_DIR = Path("/data/logs")
_LOG_PATH = _LOG_DIR / "csp-violations.jsonl"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB before rotation


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

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()

        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")
        ts = datetime.now(timezone.utc).isoformat()

        # Cap line length so a hostile client can't bloat the log with
        # one massive policy string.
        def truncate(value, n=512):
            if value is None:
                return None
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            s = str(value)
            return s if len(s) <= n else s[:n] + "…"

        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            for v in violations:
                row = {
                    "ts": ts,
                    "ip": client_ip,
                    "ua": truncate(user_agent, 256),
                    "doc": truncate(v.get("document-uri") or v.get("documentURL")),
                    "directive": truncate(
                        v.get("violated-directive") or v.get("effectiveDirective"), 64
                    ),
                    "blocked": truncate(v.get("blocked-uri") or v.get("blockedURL")),
                    "source": truncate(v.get("source-file") or v.get("sourceFile")),
                    "line": v.get("line-number") or v.get("lineNumber"),
                    "col": v.get("column-number") or v.get("columnNumber"),
                    "disposition": truncate(v.get("disposition"), 16),
                }
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as e:  # noqa: BLE001 — best-effort sink
        log.warning(f"csp-report write failed: {e}")

    return Response(status_code=204)
