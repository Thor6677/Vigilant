"""Deferred and automatic updates: decide when, then hand off to the sidecar.

Two features share one execution path and nothing else:

  B. "Schedule this update"  — one-shot, a tag the operator saw and approved.
  C. "Automatic updates"     — a standing policy, applying whatever is latest
                               when the window arrives.

B is not a special case of C. B pins a tag; C cannot, because applying whatever
is newest IS the point.

**The scheduler lives here, in the app, and not in the sidecar.** The
supervisor's entire security argument is that it performs exactly two operations
and knows nothing else; teaching it about weekdays, timezones and policy would
widen the root-equivalent component to hold logic with no reason to be
privileged. It still just claims whatever request appears.

Everything above the `# ── Impure half` banner is pure: no clock, no database,
no filesystem. That is what lets the window arithmetic — the part with the DST
and idempotency traps in it — be tested exhaustively without a running app.
"""
import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.db.models import (AdminAuditLog, AsyncSessionLocal, UpdatePolicy,
                           UpdateSchedule, UpdateStatus)
from app.ops import updater as updater_client
from app.ops.version import parse_version

logger = logging.getLogger(__name__)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")

# Shared with app/ops/update_check.py. NOTE for the operator: this means an
# admin who opts OUT of "a new release exists" notices also opts out of
# auto-update FAILURE reports — the compensating control for unattended
# deploys. Documented in README rather than silently split into a second type,
# because a second type nobody knows to enable is worse.
ALERT_TYPE = "update_available"

# How late a window may be honoured. The app is BOTH the scheduler and the thing
# being recreated, so it will sometimes be down when a window passes. Firing
# late within a bound is kinder than skipping; firing unboundedly late is worse
# than either — an update scheduled for 4am Sunday going off at 9am Monday when
# someone notices the box was off is exactly the unattended-at-a-surprising-time
# behaviour a schedule exists to prevent.
GRACE_SECONDS = 2 * 60 * 60

# The loop's own cadence. One minute is fine for a decision measured in hours,
# and keeps a missed window's lateness bounded by the grace above rather than by
# the tick.
TICK_SECONDS = 60


# ── Pure half ────────────────────────────────────────────────────────────────


def valid_timezone(name) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def parse_local_time(raw) -> tuple[int, int] | None:
    """"HH:MM" -> (hour, minute), or None.

    Deliberately strict: a silently-misparsed schedule is an update at the wrong
    hour, unattended.
    """
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _local_now(tz_name: str, now_utc: datetime) -> datetime:
    return now_utc.astimezone(ZoneInfo(tz_name))


def most_recent_window(weekday: int, local_time: str, tz_name: str,
                       now_utc: datetime) -> datetime | None:
    """The latest occurrence of (weekday, local_time) at or before now, local.

    Resolved through zoneinfo at EVALUATION time rather than stored as a UTC
    hour. A stored hour drifts by one twice a year, silently — "Sunday 4am"
    quietly becoming 3am or 5am is the kind of bug nobody notices until an
    unattended deploy runs at the wrong time.
    """
    hm = parse_local_time(local_time)
    if hm is None or not valid_timezone(tz_name) or not (0 <= weekday <= 6):
        return None
    hour, minute = hm
    tz = ZoneInfo(tz_name)
    local_now = _local_now(tz_name, now_utc)

    # Walk back at most 8 days: enough to find the weekday even when today IS
    # the weekday but the time has not arrived yet.
    for back in range(0, 8):
        day = (local_now - timedelta(days=back)).date()
        if day.weekday() != weekday:
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if candidate <= local_now:
            return candidate
    return None


def next_window(weekday: int, local_time: str, tz_name: str,
                now_utc: datetime) -> datetime | None:
    """The next occurrence at or after now — for showing "next run" in the UI."""
    hm = parse_local_time(local_time)
    if hm is None or not valid_timezone(tz_name) or not (0 <= weekday <= 6):
        return None
    hour, minute = hm
    tz = ZoneInfo(tz_name)
    local_now = _local_now(tz_name, now_utc)
    for forward in range(0, 8):
        day = (local_now + timedelta(days=forward)).date()
        if day.weekday() != weekday:
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if candidate >= local_now:
            return candidate
    return None


def window_key(window_local: datetime) -> str:
    """The idempotency key: the window's LOCAL date.

    Guards the worst failure mode in the feature — the app fires an update, the
    update recreates the app, the app comes back, re-evaluates, finds the window
    still open, and fires again. Local date rather than UTC so one window is one
    key regardless of which side of midnight UTC it falls on.
    """
    return window_local.date().isoformat()


def is_patch_upgrade(current: str | None, latest: str | None) -> bool:
    """Whether `latest` differs from `current` only in the patch component."""
    a, b = parse_version(current), parse_version(latest)
    if a is None or b is None:
        return False
    return a[0] == b[0] and a[1] == b[1] and b[2] > a[2]


def schedule_is_due(run_at_utc: datetime | None, now_utc: datetime,
                    grace_seconds: int = GRACE_SECONDS) -> tuple[bool, str]:
    """Whether a one-shot schedule should fire now. Returns (due, reason)."""
    if run_at_utc is None:
        return False, "no run time"
    if run_at_utc.tzinfo is None:
        run_at_utc = run_at_utc.replace(tzinfo=timezone.utc)
    delta = (now_utc - run_at_utc).total_seconds()
    if delta < 0:
        return False, "not yet"
    if delta > grace_seconds:
        # Deliberately not fired. See GRACE_SECONDS.
        return False, "missed the window"
    return True, "due"


def policy_decision(policy, now_utc: datetime, latest_tag: str | None,
                    current_tag: str | None,
                    grace_seconds: int = GRACE_SECONDS) -> tuple[bool, str | None, str]:
    """Should the standing policy fire? Returns (fire, window_key, reason).

    Every refusal carries a reason so the UI and the log can say WHY nothing
    happened, which is the question anyone asks of a scheduler that did nothing.
    """
    if policy is None or not policy.enabled:
        return False, None, "disabled"
    if policy.paused_reason:
        return False, None, f"paused: {policy.paused_reason}"

    window = most_recent_window(policy.weekday, policy.local_time,
                                policy.timezone, now_utc)
    if window is None:
        return False, None, "policy is not resolvable (bad time or timezone)"

    key = window_key(window)

    # Idempotency BEFORE lateness, deliberately. Both refuse, so the safety is
    # the same either way — but this window has already been handled, and
    # reporting "outside the window" for it implies the next tick might still
    # fire, which is exactly the wrong thing to tell someone reading the log
    # after a restart landed near the edge of the grace period.
    if policy.last_fired_window == key:
        # Not an error — the normal state for the rest of the window after a
        # successful fire, and the guard against the restart loop.
        return False, key, "already fired for this window"

    late = (_local_now(policy.timezone, now_utc) - window).total_seconds()
    if late > grace_seconds:
        return False, key, "outside the window"
    if not latest_tag:
        return False, key, "no release information yet"
    if latest_tag == current_tag:
        return False, key, "already up to date"
    if not updater_client.validate_tag_advisory(latest_tag):
        # Belt and braces: validate_tag's regex admits no hyphen, so a
        # prerelease is structurally ineligible for unattended application.
        return False, key, f"{latest_tag} is not an exact release tag"
    if policy.patch_only and not is_patch_upgrade(current_tag, latest_tag):
        return False, key, f"{latest_tag} is not a patch release"
    return True, key, "due"


# ── Impure half ──────────────────────────────────────────────────────────────


async def get_policy(db) -> UpdatePolicy:
    """The singleton policy row, created disabled on first access."""
    policy = (await db.execute(
        select(UpdatePolicy).where(UpdatePolicy.id == 1))).scalar_one_or_none()
    if policy is None:
        policy = UpdatePolicy(id=1, enabled=False)
        db.add(policy)
        await db.commit()
    return policy


async def pending_schedule(db) -> UpdateSchedule | None:
    return (await db.execute(
        select(UpdateSchedule)
        .where(UpdateSchedule.state == "pending")
        .order_by(UpdateSchedule.run_at)
    )).scalars().first()


async def _reconcile_outcome(db, policy) -> None:
    """Report how a previous automatic run ended, once it has ended.

    Runs here rather than at submit time because the app is recreated by the
    very update it triggers: whatever was in memory when the request was written
    is gone by the time there is an outcome to report. awaiting_request_id is in
    the database precisely so this survives that restart.

    KNOWN GAP: if someone deploys manually before this reconciles, status.json
    is overwritten with the manual run's id and the automatic run's outcome is
    never reported — awaiting_request_id then stays set until the next automatic
    fire replaces it. Narrow (it needs a manual deploy inside the minute after
    an automatic one finishes) and it fails toward silence rather than toward a
    wrong report, so it is recorded rather than fixed with more bookkeeping.
    """
    if not policy.awaiting_request_id:
        return
    status = updater_client.read_status()
    if not isinstance(status, dict):
        return
    if status.get("id") != policy.awaiting_request_id:
        return
    if status.get("state") not in ("success", "failed"):
        return

    ok = status.get("state") == "success"
    reverted = status.get("reverted_to")
    tag = status.get("to_tag")

    if ok:
        message = f"Automatic update to {tag} succeeded."
    elif reverted:
        message = (f"Automatic update to {tag} FAILED and was rolled back to "
                   f"{reverted}. Automatic updates are paused until you "
                   f"re-enable them.")
    else:
        message = (f"Automatic update to {tag} FAILED. Automatic updates are "
                   f"paused until you re-enable them.")

    policy.awaiting_request_id = None
    if not ok:
        # Pause rather than retry. Without this the same bad release is
        # attempted again every week, unattended, forever.
        policy.enabled = False
        policy.paused_reason = f"automatic update to {tag} failed"
    await db.commit()

    logger.info("updater: %s", message)
    try:
        from app.notify.discord import send_discord_alert
        # KEYWORDS, not positional. The signature is
        # (title, body, alert_type, key=None) — passing these positionally in
        # the obvious "type first" order silently makes alert_type the message
        # body, which then matches nothing in DISCORD_ALERT_TYPES and is dropped
        # without an error. Caught in review before this shipped; the test binds
        # against the real signature so an argument-order change breaks loudly.
        await send_discord_alert(
            title=f"Vigilant auto-update {'succeeded' if ok else 'FAILED'}",
            body=message,
            alert_type=ALERT_TYPE,
            key="auto-update",
        )
    except Exception as e:
        # An unattended change with no human-visible trail is the real hazard
        # here, so a failure to notify is worth a loud log line — but it must
        # not take the loop down.
        logger.error("updater: could not send auto-update notification: %s", e)


async def _fire(db, action: str, tag: str, *, policy=None, schedule=None,
                key: str | None = None) -> bool:
    """Submit one request and record that we did, atomically with the decision.

    The commit ordering is the whole point. last_fired_window is written in the
    SAME transaction as the decision to submit, so an app that is recreated
    seconds later cannot come back, re-evaluate an open window and fire a second
    time. update_status.notified_tag solves the identical problem for Discord
    announcements the same way.
    """
    try:
        request_id = updater_client.submit(action, tag, None)
    except (updater_client.UpdaterUnavailable, updater_client.UpdaterBusy,
            updater_client.InvalidRequest) as e:
        logger.warning("updater: scheduled %s to %s not submitted: %s", action, tag, e)
        return False

    now = datetime.now(timezone.utc)
    # An unattended deploy needs a durable record. Discord is the notification,
    # but it is gated on an opt-in type and may not be configured at all, so the
    # audit log is the one place this is guaranteed to be written down. Null
    # user id: nobody asked — that IS the fact being recorded.
    db.add(AdminAuditLog(
        user_id=None,
        event_type="auto_update_requested" if policy is not None else "scheduled_update_requested",
        detail=(f"{action} to {tag} (request {request_id}"
                + (f", window {key}" if key else "") + ")"),
    ))
    if schedule is not None:
        schedule.state = "fired"
        schedule.fired_at = now
        schedule.fired_request_id = request_id
    if policy is not None:
        policy.last_fired_window = key
        policy.last_fired_tag = tag
        policy.awaiting_request_id = request_id
    await db.commit()
    logger.info("updater: scheduled %s to %s submitted (id=%s)", action, tag, request_id)
    return True


async def tick(now_utc: datetime | None = None) -> str:
    """One evaluation. Returns a short reason, for logs and tests."""
    now = now_utc or datetime.now(timezone.utc)

    if not updater_client.is_available():
        return "no updater"

    async with AsyncSessionLocal() as db:
        policy = await get_policy(db)
        await _reconcile_outcome(db, policy)

        # Never stack a request on top of a run, and never race the sidecar's
        # pickup. The supervisor's lock is the real mutex; this keeps the app
        # from queueing work it knows will be refused.
        if updater_client.current_run_state() != updater_client.IDLE:
            return "busy"
        if updater_client.has_pending_request():
            return "a request is already queued"

        beat = updater_client.read_heartbeat() or {}
        current_tag = beat.get("current_tag")

        # One-shot first: the operator named this tag explicitly, so it outranks
        # a standing policy that would apply something else.
        sched = await pending_schedule(db)
        if sched is not None:
            due, reason = schedule_is_due(sched.run_at, now)
            if due:
                if sched.target_tag == current_tag:
                    sched.state = "superseded"
                    await db.commit()
                    return "scheduled tag already running"
                return ("fired schedule" if await _fire(db, "update", sched.target_tag,
                                                        schedule=sched)
                        else "schedule submit failed")
            if reason == "missed the window":
                sched.state = "superseded"
                await db.commit()
                logger.warning("updater: scheduled update to %s missed its window",
                               sched.target_tag)
                return "schedule missed its window"

        # What the hourly checker last saw, so up to an hour stale — and
        # staler than that if the checker has been failing (see
        # update_status.last_error). Acceptable against a WEEKLY window, and
        # re-polling GitHub here would double the API traffic to make a
        # once-a-week decision marginally fresher. The tag is re-validated on
        # the privileged side at pickup either way.
        row = (await db.execute(
            select(UpdateStatus).where(UpdateStatus.id == 1))).scalar_one_or_none()
        latest_tag = row.latest_tag if row else None

        fire, key, reason = policy_decision(policy, now, latest_tag, current_tag)
        if not fire:
            return reason
        return ("fired policy" if await _fire(db, "update", latest_tag,
                                              policy=policy, key=key)
                else "policy submit failed")


async def run_scheduler() -> None:
    """Background loop. Started from app.main alongside the update checker."""
    logger.info("update scheduler: starting (tick=%ss)", TICK_SECONDS)
    while True:
        try:
            result = await tick()
            if result in ("fired schedule", "fired policy"):
                logger.info("update scheduler: %s", result)
        except Exception:
            # One bad tick must not end the loop — the feature would silently
            # stop scheduling until the next container restart.
            logger.exception("update scheduler: tick failed")
        await asyncio.sleep(TICK_SECONDS)
