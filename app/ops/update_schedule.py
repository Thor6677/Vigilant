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
from app.ops import update_reports
from app.ops import updater as updater_client
from app.ops.version import parse_version

logger = logging.getLogger(__name__)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")

# The Discord alert type (and bell event type) for run reports. Defined, with
# the reason it is not `update_available`, in app/ops/update_reports.py.
ALERT_TYPE = update_reports.ALERT_TYPE

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

# When this process started. A policy window that closed before then passed
# while Vigilant was down, which is one of the two ways a window can be shown
# to have been missed (see _report_missed_window). Read at call time, so a test
# can move it.
STARTED_AT = datetime.now(timezone.utc)


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


def updater_not_ready(heartbeat) -> str | None:
    """Why the sidecar should not be handed an unattended request right now,
    or None when it may.

    Two things, both read from the heartbeat the sidecar already publishes:

    1. **It is replacing itself.** After every successful in-app update the
       sidecar pulls its own new image and hands off to a helper container
       that recreates it (`pulling`, then `handed_off`). Its poll loop is
       blocked for the whole of that, so a request written now is not claimed
       by the outgoing process — it waits for whichever sidecar polls next.
       The rename claim means it can never run twice, but it CAN be stranded:
       the handoff's documented worst case is the old container gone and the
       new one never started. The request then sits in /control, the window is
       already recorded as fired, and whoever next starts a sidecar — hours or
       days later — gets an unattended deploy with no grace bound on it, which
       is precisely what GRACE_SECONDS exists to prevent. A human clicking
       Update during a handoff is watching the panel; the scheduler is not.

    2. **Any of its environment checks is not "ok".** This is the rule the
       panel already applies to the Update and Rollback buttons, and it also
       covers the tail of a handoff: a freshly started sidecar publishes a
       `startup` placeholder check until its own checks have run, so this
       holds off until the replacement has proved it can actually deploy.

    Skipping is always safe here. Nothing is recorded, so the next tick simply
    asks again, and a handoff takes seconds to minutes against a two-hour
    grace. The one gap left is the few milliseconds between the sidecar
    publishing a run's success and publishing `pulling`: the heartbeat cannot
    show a handoff that has not been announced yet.
    """
    record = updater_client.self_update_record(heartbeat)
    if record is not None and record.get("state") in updater_client.SELF_UPDATE_IN_FLIGHT:
        return "updater is upgrading itself"
    checks = heartbeat.get("checks") if isinstance(heartbeat, dict) else None
    if isinstance(checks, dict):
        failing = sorted(name for name, result in checks.items() if result != "ok")
        if failing:
            return "updater checks not passing: " + ", ".join(failing)
    return None


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
    eligible, reason = release_eligible(policy, latest_tag, current_tag)
    return eligible, key, reason


def release_eligible(policy, latest_tag: str | None,
                     current_tag: str | None) -> tuple[bool, str]:
    """Whether the policy would apply `latest_tag` at all, window aside.

    Split out of policy_decision so a CLOSED window can be asked the same
    question: a window is only reported as skipped if there was something it
    would have applied.
    """
    if not latest_tag:
        return False, "no release information yet"
    if latest_tag == current_tag:
        return False, "already up to date"
    if not updater_client.validate_tag_advisory(latest_tag):
        # Belt and braces: validate_tag's regex admits no hyphen, so a
        # prerelease is structurally ineligible for unattended application.
        return False, f"{latest_tag} is not an exact release tag"
    if policy.patch_only and not is_patch_upgrade(current_tag, latest_tag):
        return False, f"{latest_tag} is not a patch release"
    return True, "due"


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
    """Report how the last scheduled or automatic run ended, once it has ended.

    Runs here rather than at submit time because the app is recreated by the
    very update it triggers: whatever was in memory when the request was written
    is gone by the time there is an outcome to report. awaiting_request_id and
    fired_request_id are in the database precisely so this survives that
    restart, and the report's unique request_id is what makes it once-only.

    Reads status.json whatever the heartbeat says: reporting an outcome needs
    nothing from the sidecar, and waiting for it (through a self-update, say)
    would only widen the gap below.

    KNOWN GAP: if someone deploys manually before this reconciles, status.json
    is overwritten with the manual run's id and that scheduled or automatic
    run's outcome is never reported — for an automatic run, awaiting_request_id
    then stays set until the next automatic fire replaces it. Narrow (it needs a
    manual deploy inside the minute after an unattended one finishes) and it
    fails toward silence rather than toward a wrong report, so it is recorded
    rather than fixed with more bookkeeping.
    """
    status = updater_client.read_status()
    if not isinstance(status, dict):
        return
    if status.get("state") not in ("success", "failed"):
        return
    rid = status.get("id")
    if not rid:
        return

    automatic = bool(policy.awaiting_request_id) and rid == policy.awaiting_request_id
    if not automatic:
        fired = (await db.execute(
            select(UpdateSchedule.id).where(UpdateSchedule.fired_request_id == rid)
        )).first()
        if fired is None:
            return          # a run somebody started by hand: not ours to report
    if await update_reports.already_reported(db, rid):
        if automatic:
            policy.awaiting_request_id = None
            await db.commit()
        return

    outcome = update_reports.outcome_of(status)
    kind = update_reports.AUTOMATIC if automatic else update_reports.SCHEDULED
    label = "Automatic" if automatic else "Scheduled"
    from_tag, to_tag = status.get("from_tag"), status.get("to_tag")
    reverted = status.get("reverted_to")

    if outcome == update_reports.SUCCEEDED:
        message = f"{label} update from {from_tag or '?'} to {to_tag} succeeded."
    elif outcome == update_reports.REVERTED:
        message = (f"{label} update to {to_tag} failed and was rolled back to "
                   f"{reverted}.")
    else:
        error = (status.get("error") or "").strip()
        message = f"{label} update to {to_tag} failed." + (f" {error}" if error else "")

    if automatic:
        policy.awaiting_request_id = None
        if outcome != update_reports.SUCCEEDED:
            # Pause rather than retry. Without this the same bad release is
            # attempted again every week, unattended, forever.
            policy.enabled = False
            policy.paused_reason = f"automatic update to {to_tag} failed"
            message += " Automatic updates are paused until you re-enable them."

    # One commit: the pause above, the audit row and the report row.
    report = await update_reports.record(
        db, kind=kind, outcome=outcome, from_tag=from_tag, to_tag=to_tag,
        detail=message, request_id=rid)
    if report is not None:
        await update_reports.dispatch(db, report)


async def _fire(db, action: str, tag: str, *, policy=None, schedule=None,
                key: str | None = None) -> bool:
    """Submit one request and record that we did, atomically with the decision.

    The commit ordering is the whole point. last_fired_window is written in the
    SAME transaction as the decision to submit, so an app that is recreated
    seconds later cannot come back, re-evaluate an open window and fire a second
    time. update_status.notified_tag solves the identical problem for Discord
    announcements the same way.

    A refused submit is recorded as a hold, like any other reason the request
    could not go in: if the grace runs out, it is what the skip report says.
    """
    try:
        request_id = updater_client.submit(action, tag, None)
    except (updater_client.UpdaterUnavailable, updater_client.UpdaterBusy,
            updater_client.InvalidRequest) as e:
        logger.warning("updater: scheduled %s to %s not submitted: %s", action, tag, e)
        await _note_hold(db, f"the request was refused: {e}",
                         policy=policy, schedule=schedule, key=key)
        return False

    now = datetime.now(timezone.utc)
    # An unattended deploy needs a durable record written when it is REQUESTED,
    # before the restart it causes. Null user id: nobody asked — that IS the
    # fact being recorded. How it ended is a second row, from the report.
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
        schedule.held_reason = None
    if policy is not None:
        policy.last_fired_window = key
        policy.last_fired_tag = tag
        policy.awaiting_request_id = request_id
        policy.held_window = None
        policy.held_reason = None
    await db.commit()
    logger.info("updater: scheduled %s to %s submitted (id=%s)", action, tag, request_id)
    return True


def _submission_hold(beat) -> str | None:
    """Why a request must not be written right now, or None when it may.

    Evaluated only at the point of submission, never before: a schedule or a
    window that is held for its whole grace still has to reach the code that
    reports it as skipped.
    """
    if not updater_client.is_available():
        return "no updater"
    # Never stack a request on top of a run, and never race the sidecar's
    # pickup. The supervisor's lock is the real mutex; this keeps the app
    # from queueing work it knows will be refused.
    if updater_client.current_run_state() != updater_client.IDLE:
        return "busy"
    if updater_client.has_pending_request():
        return "a request is already queued"
    return updater_not_ready(beat)


async def _note_hold(db, reason: str, *, policy=None, schedule=None,
                     key: str | None = None) -> None:
    """Remember why a due run was held back — written only when it changes, so
    a long hold costs one write, not one per tick."""
    reason = reason[:255]
    changed = False
    if schedule is not None and schedule.held_reason != reason:
        schedule.held_reason = reason
        changed = True
    if policy is not None and (policy.held_window != key or policy.held_reason != reason):
        policy.held_window = key
        policy.held_reason = reason
        changed = True
    if changed:
        await db.commit()


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def _report_missed_schedule(db, sched, current_tag) -> None:
    """The skip report for a one-shot schedule whose grace ran out. Also
    commits the caller's pending state change on the schedule."""
    grace_h = GRACE_SECONDS // 3600
    why = sched.held_reason or "Vigilant or its updater was not running"
    detail = (f"Scheduled update to {sched.target_tag}, due "
              f"{sched.run_at.strftime('%Y-%m-%d %H:%M')} UTC, was not started "
              f"within {grace_h}h: {why}. It has been dropped; schedule it again "
              f"if it is still wanted.")
    report = await update_reports.record(
        db, kind=update_reports.SCHEDULED, outcome=update_reports.SKIPPED,
        from_tag=current_tag, to_tag=sched.target_tag, detail=detail)
    if report is not None:
        await update_reports.dispatch(db, report)


async def _report_missed_window(db, policy, key: str | None, now: datetime,
                                status_row, current_tag) -> bool:
    """Report a policy window that closed without firing — once, and only with
    evidence that it should have fired.

    The release must STILL be one the policy would apply: if something else got
    there first (a one-shot schedule, an admin pressing Update, a CLI deploy),
    nothing was missed, whatever the ticks inside the window saw. Then one of:
      - a tick INSIDE the window wanted to fire and was held (held_window), or
      - no tick ever saw the window (observed_window), it closed before this
        process started, and the release had been published before it closed —
        Vigilant was down for the window, and an up-and-running one would have
        acted. "Started after the window" alone is not enough: any restart
        after a window (a rollback, say) satisfies it.

    Anything else stays silent. A skipped report is a banner someone has to
    acknowledge, so a false one costs more than a missing one.
    """
    if not key or key in (policy.last_fired_window, policy.last_skipped_window):
        return False
    latest_tag = status_row.latest_tag if status_row else None
    window = most_recent_window(policy.weekday, policy.local_time, policy.timezone, now)
    if window is None:
        return False
    closed_at = window.astimezone(timezone.utc) + timedelta(seconds=GRACE_SECONDS)

    eligible, _ = release_eligible(policy, latest_tag, current_tag)
    if not eligible:
        if policy.held_window is not None:
            # Held, but satisfied some other way. Forget it, once.
            policy.held_window = None
            policy.held_reason = None
            await db.commit()
        return False

    if policy.held_window == key:
        why = policy.held_reason or "the updater was not ready"
    else:
        published = _as_utc(status_row.latest_published_at) if status_row else None
        if policy.observed_window == key or STARTED_AT < closed_at:
            return False        # we were running; a due tick would have held it
        if published is None or published > closed_at:
            return False
        why = "Vigilant was not running during the window"

    policy.last_skipped_window = key
    policy.held_window = None
    policy.held_reason = None
    detail = (f"The automatic update window ({WEEKDAYS[policy.weekday]} "
              f"{policy.local_time} {policy.timezone}, {key}) closed without "
              f"updating to {latest_tag or 'the latest release'}: {why}. The "
              f"next window will try again.")
    # One commit: the skip marker, the audit row and the report row.
    report = await update_reports.record(
        db, kind=update_reports.AUTOMATIC, outcome=update_reports.SKIPPED,
        from_tag=current_tag, to_tag=latest_tag, detail=detail)
    if report is not None:
        await update_reports.dispatch(db, report)
    return True


async def tick(now_utc: datetime | None = None) -> str:
    """One evaluation. Returns a short reason, for logs and tests."""
    now = now_utc or datetime.now(timezone.utc)

    # No heartbeat file at all means the updater profile has never run on this
    # install — the default. Nothing to schedule against, so not even a
    # database read. A file that has merely gone STALE is different: the
    # updater has stopped, and a window missed because of that is exactly
    # what an admin needs to hear about.
    beat = updater_client.read_last_heartbeat()
    if not isinstance(beat, dict):
        return "no updater"

    async with AsyncSessionLocal() as db:
        policy = await get_policy(db)
        await _reconcile_outcome(db, policy)

        hold = _submission_hold(beat)
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
                if hold:
                    await _note_hold(db, hold, schedule=sched)
                    return hold
                return ("fired schedule" if await _fire(db, "update", sched.target_tag,
                                                        schedule=sched)
                        else "schedule submit failed")
            if reason == "missed the window":
                sched.state = "superseded"
                if sched.target_tag == current_tag:
                    # Deployed some other way meanwhile: nothing was missed.
                    await db.commit()
                    return "scheduled tag already running"
                logger.warning("updater: scheduled update to %s missed its window",
                               sched.target_tag)
                # pending -> superseded happens exactly once per schedule, so
                # this is the one place its skip is reported.
                await _report_missed_schedule(db, sched, current_tag)
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
        if key and reason != "outside the window" and policy.observed_window != key:
            policy.observed_window = key
            await db.commit()
        if fire:
            if hold:
                await _note_hold(db, hold, policy=policy, key=key)
                return hold
            return ("fired policy" if await _fire(db, "update", latest_tag,
                                                  policy=policy, key=key)
                    else "policy submit failed")
        if reason == "outside the window" and await _report_missed_window(
                db, policy, key, now, row, current_tag):
            return "window skipped"
        return reason


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
