"""How scheduled and automatic updates ended, and who gets told.

Every unattended run — a one-shot schedule or the weekly policy — produces one
report: succeeded, failed, reverted, or skipped (its grace ran out before a
request ever reached the updater). Nobody was watching either kind, so the
report is the only way anyone finds out.

Delivery is layered so that nothing depends on an external service:

  1. An admin audit-log row, written in the SAME commit as the report row and
     before anything is pushed. The one channel that cannot fail on bad config.
  2. The report row itself, which the admin banner and the updater panel's
     history read. It survives the app restart the update causes.
  3. A bell event for every admin, for a tab that happens to be open.
  4. Optional push: Discord (existing env webhook, alert type `auto_update`)
     and one generic webhook (JSON, or ntfy's plain-text format).

A push that fails is recorded against the report and on the channel's "last
delivery" line. It never affects the update, the report or the other channels.
Nothing about whether an update runs is gated on any of this.

Everything above the `# ── Impure half` banner is pure.
"""
import asyncio
import ipaddress
import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.db.models import AdminAuditLog, UpdateNotifySettings, UpdateRunReport, User

logger = logging.getLogger(__name__)

SCHEDULED = "scheduled"
AUTOMATIC = "automatic"

SUCCEEDED = "succeeded"
FAILED = "failed"
REVERTED = "reverted"
SKIPPED = "skipped"
OUTCOMES = (SUCCEEDED, FAILED, REVERTED, SKIPPED)
# What "problems only" lets through. Everything that needs a person.
PROBLEMS = (FAILED, REVERTED, SKIPPED)

POLICY_ALL = "all"
POLICY_PROBLEMS = "problems"
POLICIES = (POLICY_ALL, POLICY_PROBLEMS)

FORMAT_JSON = "json"
FORMAT_NTFY = "ntfy"
FORMATS = (FORMAT_JSON, FORMAT_NTFY)

# Its own Discord type, NOT app/ops/update_check.py's `update_available`. The
# report is the compensating control for a deploy nobody watched; sharing a
# type meant opting out of "a new release exists" notices silently opted out
# of the failure reports as well. Also the bell event's type, so an admin can
# mute it there like any other.
ALERT_TYPE = "auto_update"

# Delivery states recorded per channel on each report.
SENT = "sent"
DELIVERY_FAILED = "failed"
FILTERED = "filtered"      # the channel's policy is "problems only"
OFF = "off"                # configured, but not able to send (see error)

WEBHOOK_TIMEOUT_SECONDS = 5.0
URL_MAX_LENGTH = 512

# A success notice lapses from the banner on its own after this long; anything
# that needs a person stays until an admin acknowledges it.
SUCCESS_BANNER_DAYS = 7

_NTFY_TAGS = {
    SUCCEEDED: "white_check_mark",
    FAILED: "x",
    REVERTED: "rewind",
    SKIPPED: "warning",
}


# ── Pure half ────────────────────────────────────────────────────────────────


def outcome_of(status: dict) -> str:
    """succeeded, reverted or failed, from a terminal status.json."""
    if status.get("state") == "success":
        return SUCCEEDED
    return REVERTED if status.get("reverted_to") else FAILED


def wants(policy: str | None, outcome: str) -> bool:
    """Whether a channel with this report policy should hear about `outcome`."""
    return policy != POLICY_PROBLEMS or outcome in PROBLEMS


def headline(kind: str, outcome: str, to_tag: str | None) -> str:
    """One line, ASCII only.

    ASCII because it doubles as ntfy's Title header, and httpx refuses header
    values that are not latin-1 — an arrow or a dash from a release name would
    turn a delivery into an exception.
    """
    if kind not in (SCHEDULED, AUTOMATIC):
        return "Vigilant: test notification"
    who = "Scheduled" if kind == SCHEDULED else "Automatic"
    target = f" to {to_tag}" if to_tag else ""
    verb = {
        SUCCEEDED: "succeeded",
        FAILED: "FAILED",
        REVERTED: "FAILED and was rolled back",
        SKIPPED: "was skipped",
    }.get(outcome, outcome)
    line = f"Vigilant: {who.lower()} update{target} {verb}"
    return line.encode("ascii", "replace").decode("ascii")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_payload(report) -> dict:
    """The generic webhook's JSON body. A stable, documented shape."""
    return {
        "event": "vigilant.update",
        "kind": report.kind,
        "outcome": report.outcome,
        "from_tag": report.from_tag,
        "to_tag": report.to_tag,
        "at": _iso(report.created_at),
        "detail": report.detail,
    }


def ntfy_request(report) -> tuple[str, dict]:
    """(body, headers) for ntfy's publish-by-POST format.

    ntfy takes the message as the plain-text body and everything else as
    headers, so the rich text goes in the body and the headers stay ASCII.
    Problems are sent at high priority so they break through a phone's quiet
    defaults; a success does not deserve that.
    """
    title = headline(report.kind, report.outcome, report.to_tag)
    headers = {
        "Title": title,
        "Priority": "high" if report.outcome in PROBLEMS else "default",
        "Tags": f"{_NTFY_TAGS.get(report.outcome, 'information_source')},vigilant",
    }
    return (report.detail or title), headers


def validate_webhook_url(raw) -> tuple[str | None, str | None]:
    """(url, None) if usable, else (None, reason).

    http and https only: anything else is either not a webhook or a way to make
    the app open something that is not one.
    """
    url = (raw or "").strip() if isinstance(raw, str) else ""
    if not url:
        return None, "enter a URL"
    if len(url) > URL_MAX_LENGTH:
        return None, f"the URL is longer than {URL_MAX_LENGTH} characters"
    if any(c.isspace() for c in url):
        return None, "the URL contains spaces"
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, "that is not a URL"
    if parts.scheme not in ("http", "https"):
        return None, "the URL must start with http:// or https://"
    if not parts.hostname:
        return None, "the URL has no host"
    return url, None


def address_problem(addr: str) -> str | None:
    """Why a webhook may not be sent to this IP address, or None if it may.

    Only globally reachable unicast addresses: no loopback, private, shared
    (CGNAT), link-local (which includes cloud metadata endpoints), reserved,
    unspecified or multicast. An IPv4 address written as IPv6 is judged as the
    IPv4 address it is.
    """
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return "its host does not resolve to an IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        return "its host is a private, local or reserved address"
    return None


def redact_url(url: str | None) -> str:
    """Scheme, host and a masked tail — never the path.

    For ntfy the topic IS the credential: anyone who has it can read and post.
    So the URL is never rendered back into the page (which every admin's tab
    re-fetches every few seconds) and never logged; this is what stands in.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unreadable URL)"
    host = parts.hostname or "?"
    path = (parts.path or "").rstrip("/")
    tail = f"/…{path[-3:]}" if len(path) > 4 else ("/…" if path else "")
    return f"{parts.scheme}://{host}{tail}"


def scrub(text: str, url: str | None) -> str:
    """Remove the webhook URL, and its path on its own, from an error message."""
    if not text:
        return text
    if url:
        text = text.replace(url, redact_url(url))
        try:
            path = urlsplit(url).path
        except ValueError:
            path = ""
        if path and len(path) > 1:
            text = text.replace(path, "/…")
    return text[:255]


def deliveries_of(report) -> dict:
    """The per-channel delivery record, decoded. {} if none or unreadable."""
    raw = getattr(report, "deliveries", None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ── Impure half ──────────────────────────────────────────────────────────────


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_notify_settings(db) -> UpdateNotifySettings:
    """The singleton settings row, created with every push channel off."""
    row = (await db.execute(
        select(UpdateNotifySettings).where(UpdateNotifySettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = UpdateNotifySettings(id=1, discord_policy=POLICY_ALL,
                                   webhook_format=FORMAT_JSON, webhook_policy=POLICY_ALL)
        db.add(row)
        await db.commit()
    return row


async def already_reported(db, request_id: str) -> bool:
    return (await db.execute(
        select(UpdateRunReport.id).where(UpdateRunReport.request_id == request_id)
    )).first() is not None


async def failed_here(db, tag: str | None) -> datetime | None:
    """When `tag` last failed or was rolled back on this install, or None.

    Only run reports count, not status.json: the sidecar also publishes a
    "failed" status for refusals (a lock it could not take, an unreadable
    request) that say nothing about the release, and one of those must not
    blacklist a tag for good.
    """
    if not tag:
        return None
    return (await db.execute(
        select(UpdateRunReport.created_at)
        .where(UpdateRunReport.to_tag == tag)
        .where(UpdateRunReport.outcome.in_((FAILED, REVERTED)))
        .order_by(UpdateRunReport.id.desc()).limit(1)
    )).scalar()


async def admin_user_ids(db) -> list[int]:
    """Everyone require_admin lets into the updater panel: admin and manager."""
    rows = await db.execute(select(User.id).where(User.role.in_(("admin", "manager"))))
    return [r[0] for r in rows]


async def record(db, *, kind: str, outcome: str, from_tag: str | None,
                 to_tag: str | None, detail: str,
                 request_id: str | None = None) -> UpdateRunReport | None:
    """Write the audit row and the report row in ONE commit. Returns the report.

    Also commits whatever else the caller has pending on this session — which is
    how the scheduler makes "pause the policy" and "report the failure" a
    single atomic step.

    Returns None if this request id was already reported (the unique index is
    the last line of defence; callers check first).
    """
    prefix = "auto" if kind == AUTOMATIC else "scheduled"
    detail = (detail or "")[:512]
    db.add(AdminAuditLog(
        user_id=None,
        event_type=f"{prefix}_update_{outcome}",
        detail=(f"{from_tag or '?'} -> {to_tag or '?'}: {detail}"
                + (f" (request {request_id})" if request_id else "")),
    ))
    report = UpdateRunReport(
        created_at=_now_naive(), kind=kind, outcome=outcome, from_tag=from_tag,
        to_tag=to_tag, detail=detail, request_id=request_id,
    )
    db.add(report)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.info("update report: request %s was already reported", request_id)
        return None
    logger.info("update report: %s", headline(kind, outcome, to_tag))
    return report


def _delivery(state: str, error: str | None = None) -> dict:
    return {"state": state, "at": _iso(datetime.now(timezone.utc)), "error": error}


async def _send_discord(title: str, body: str, key: str) -> dict:
    """One Discord post through the existing relay, as a delivery record."""
    from app.notify import discord as discord_notify
    from app.notify.discord import send_discord_alert

    try:
        # KEYWORDS, not positional: the signature is (title, body, alert_type,
        # key) and the obvious "type first" positional order silently makes
        # alert_type the message body, which matches nothing and is dropped.
        result = await send_discord_alert(title=title, body=body,
                                          alert_type=ALERT_TYPE, key=key)
    except Exception as e:     # the relay promises not to raise; belt and braces
        return _delivery(DELIVERY_FAILED, f"{type(e).__name__}: {e}"[:255])
    if result is None or result == discord_notify.SENT:
        return _delivery(SENT)
    if isinstance(result, str) and result.startswith("not sent"):
        return _delivery(OFF, result)
    return _delivery(DELIVERY_FAILED, str(result)[:255])


async def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def webhook_target_problem(url: str) -> str | None:
    """None if the webhook's host resolves only to public addresses, else why not.

    The webhook is sent from inside the deployment, so a URL naming the host
    itself, the Docker network or the LAN would have Vigilant make requests
    there, and the status or error it reports back says what answered. Every
    address the name resolves to must pass, and it is checked both when the URL
    is saved and before each send, because what a name resolves to can change.
    """
    parts = urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "the URL has an invalid port"
    if not parts.hostname:
        return "the URL has no host"
    try:
        addrs = await _resolve(parts.hostname, port)
    except Exception:
        return "its host name does not resolve"
    if not addrs:
        return "its host name does not resolve"
    for addr in addrs:
        problem = address_problem(addr)
        if problem:
            return problem
    return None


async def post_webhook(url: str, fmt: str, report) -> dict:
    """One webhook POST, as a delivery record. Never raises.

    Redirects are NOT followed: a webhook that answers with one is either
    misconfigured or pointing somewhere it should not, and following it would
    send the report to a URL nobody entered. Five seconds, because this runs in
    the scheduler's tick.
    """
    from app.config import user_agent

    host = urlsplit(url).hostname or "?"
    problem = await webhook_target_problem(url)
    if problem:
        logger.warning("update report: webhook to %s refused: %s", host, problem)
        return _delivery(DELIVERY_FAILED, f"not sent: {problem}")
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS,
                                     follow_redirects=False) as client:
            if fmt == FORMAT_NTFY:
                body, headers = ntfy_request(report)
                resp = await client.post(url, content=body.encode("utf-8"),
                                         headers={**headers, "User-Agent": user_agent()})
            else:
                resp = await client.post(url, json=json_payload(report),
                                         headers={"User-Agent": user_agent()})
    except Exception as e:
        error = scrub(f"{type(e).__name__}: {e}", url)
        logger.warning("update report: webhook to %s failed: %s", host, error)
        return _delivery(DELIVERY_FAILED, error)
    if 200 <= resp.status_code < 300:
        return _delivery(SENT)
    if 300 <= resp.status_code < 400:
        error = f"HTTP {resp.status_code}: redirect not followed"
    else:
        error = f"HTTP {resp.status_code}"
    logger.warning("update report: webhook to %s answered %s", host, error)
    return _delivery(DELIVERY_FAILED, error)


def _note_last_delivery(settings: UpdateNotifySettings, channel: str, result: dict) -> None:
    """Keep the channel's "last delivery" line current. Only real attempts
    count: a filtered report says nothing about whether the channel works."""
    if result.get("state") not in (SENT, DELIVERY_FAILED):
        return
    ok = result["state"] == SENT
    at = _now_naive()
    if channel == "discord":
        settings.discord_last_at, settings.discord_last_ok = at, ok
        settings.discord_last_error = None if ok else result.get("error")
    elif channel == "webhook":
        settings.webhook_last_at, settings.webhook_last_ok = at, ok
        settings.webhook_last_error = None if ok else result.get("error")


def _discord_configured() -> bool:
    # Through the relay's own settings lookup, so this and delivers() can never
    # disagree about whether a webhook is set.
    from app.notify import discord as discord_notify
    return bool(discord_notify.get_settings().discord_webhook_url)


async def _to_bell(db, report) -> None:
    """Queue a bell event for every admin. The report dispatcher owns Discord,
    so this goes in WITHOUT the relay _emit_notification would otherwise add."""
    from app.routes import dashboard

    event = {"type": ALERT_TYPE,
             "title": headline(report.kind, report.outcome, report.to_tag),
             "body": report.detail or ""}
    for uid in await admin_user_ids(db):
        dashboard._emit_notification(uid, dict(event), relay=False)


async def dispatch(db, report: UpdateRunReport) -> dict:
    """Push one recorded report everywhere it is configured to go.

    Never raises: the report is already written, and nothing here may take the
    scheduler loop down with it. Returns the per-channel results it stored.
    """
    results: dict = {}
    try:
        await _to_bell(db, report)
    except Exception as e:
        logger.warning("update report: could not queue the bell event: %s", e)

    try:
        settings = await get_notify_settings(db)
    except Exception as e:
        logger.error("update report: could not read notification settings: %s", e)
        return results

    title = headline(report.kind, report.outcome, report.to_tag)
    if wants(settings.discord_policy, report.outcome):
        # Asked even when no webhook is set: the relay is the authority on
        # that, and says so. A channel that is not configured at all is left
        # out of the record rather than listed as "off" on every report.
        # One key per report — the relay suppresses a repeated key for 30
        # minutes, which would swallow a skip reported soon after a failure.
        result = await _send_discord(title, report.detail or "",
                                     key=f"update-report-{report.id}")
        from app.notify.discord import NOT_SENT_NO_WEBHOOK
        if result.get("error") != NOT_SENT_NO_WEBHOOK:
            results["discord"] = result
            _note_last_delivery(settings, "discord", result)
    elif _discord_configured():
        results["discord"] = _delivery(FILTERED)

    if settings.webhook_url:
        if wants(settings.webhook_policy, report.outcome):
            results["webhook"] = await post_webhook(
                settings.webhook_url, settings.webhook_format or FORMAT_JSON, report)
        else:
            results["webhook"] = _delivery(FILTERED)
        _note_last_delivery(settings, "webhook", results["webhook"])

    report.deliveries = json.dumps(results) if results else None
    try:
        await db.commit()
    except Exception as e:
        logger.error("update report: could not store delivery results: %s", e)
        await db.rollback()
    return results


BANNER_LIMIT = 5
HISTORY_LIMIT = 10


async def banner_reports(db) -> list[UpdateRunReport]:
    """What the admin banner shows: unacknowledged reports, newest first.

    A success lapses on its own after SUCCESS_BANNER_DAYS — it is news, not a
    task. Failed, reverted and skipped stay until an admin acknowledges them,
    however old, because each one means something did not happen that someone
    expected to.
    """
    cutoff = _now_naive() - timedelta(days=SUCCESS_BANNER_DAYS)
    return list((await db.execute(
        select(UpdateRunReport)
        .where(UpdateRunReport.acknowledged_at.is_(None))
        .where(or_(UpdateRunReport.outcome != SUCCEEDED,
                   UpdateRunReport.created_at >= cutoff))
        .order_by(UpdateRunReport.id.desc())
        .limit(BANNER_LIMIT)
    )).scalars().all())


async def recent_reports(db, limit: int = HISTORY_LIMIT) -> list[UpdateRunReport]:
    return list((await db.execute(
        select(UpdateRunReport).order_by(UpdateRunReport.id.desc()).limit(limit)
    )).scalars().all())


def report_view(report) -> dict:
    """A report flattened for a template: plain values only, so a detached row
    can never lazy-load mid-render, and the delivery JSON already decoded."""
    return {
        "id": report.id,
        "kind": report.kind,
        "outcome": report.outcome,
        "problem": report.outcome in PROBLEMS,
        "from_tag": report.from_tag,
        "to_tag": report.to_tag,
        "detail": report.detail or "",
        "headline": headline(report.kind, report.outcome, report.to_tag),
        "at": report.created_at.strftime("%Y-%m-%d %H:%M") if report.created_at else "",
        "acknowledged": report.acknowledged_at is not None,
        "deliveries": deliveries_of(report),
    }


def notify_view(row: UpdateNotifySettings) -> dict:
    """The notification settings as the panel shows them. The webhook URL is
    reduced to redact_url() here, so the full secret never reaches a template."""
    from app.notify.discord import delivers

    def last(at, ok, error):
        if at is None:
            return None
        return {"at": at.strftime("%Y-%m-%d %H:%M"), "ok": bool(ok), "error": error}

    return {
        "discord_webhook_set": _discord_configured(),
        "discord_on": delivers(ALERT_TYPE),
        "discord_policy": row.discord_policy or POLICY_ALL,
        "discord_last": last(row.discord_last_at, row.discord_last_ok, row.discord_last_error),
        "webhook_set": bool(row.webhook_url),
        "webhook_shown": redact_url(row.webhook_url),
        "webhook_format": row.webhook_format or FORMAT_JSON,
        "webhook_policy": row.webhook_policy or POLICY_ALL,
        "webhook_last": last(row.webhook_last_at, row.webhook_last_ok, row.webhook_last_error),
    }


class _TestReport:
    """Stands in for a report when an admin presses "Send test notification"."""

    kind = "test"
    outcome = "test"
    from_tag = None
    to_tag = None

    def __init__(self):
        self.created_at = _now_naive()
        self.detail = ("Test notification from Vigilant. If you can read this, "
                       "update reports will reach you here.")


async def send_test(db, channel: str) -> dict:
    """Send a test message on one channel, ignoring its report policy, and
    record the attempt on that channel's last-delivery line."""
    settings = await get_notify_settings(db)
    fake = _TestReport()
    if channel == "discord":
        if not _discord_configured():
            return _delivery(OFF, "DISCORD_WEBHOOK_URL is not set")
        # A key per press: the relay suppresses a repeat of the same key for
        # 30 minutes, which would swallow a second test silently.
        result = await _send_discord(headline(fake.kind, fake.outcome, None), fake.detail,
                                     key=f"update-test-{datetime.now(timezone.utc).timestamp()}")
    elif channel == "webhook":
        if not settings.webhook_url:
            return _delivery(OFF, "no webhook is set")
        result = await post_webhook(settings.webhook_url,
                                    settings.webhook_format or FORMAT_JSON, fake)
    else:
        return _delivery(OFF, "unknown channel")
    _note_last_delivery(settings, channel, result)
    await db.commit()
    return result
