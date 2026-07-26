"""In-app update checker: notify when a newer release exists. Never auto-update.

Modelled on Diun's notify-only behaviour rather than Watchtower's auto-pull
(which is archived, and is explicitly not what is wanted here): the operator
always decides when to deploy. Implemented in-app rather than as a host cron so
it can be exercised on the dev instance, reuse the existing Discord relay, and
drive an admin banner.

Polls the RELEASES API, not the tag list. A tag with no published Release is
therefore invisible — the deliberate escape hatch for building an image you do
not want to advertise yet. Unauthenticated GitHub allows 60 requests/hour/IP; at
one per hour this uses 24 per day.

The pure decision logic lives in `decide()` so it is unit-testable without a
database, an event loop, or a network.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.config import get_settings, user_agent
from app.db.models import AsyncSessionLocal, UpdateStatus
from app.notify.discord import send_discord_alert
from app.ops.version import is_newer

logger = logging.getLogger(__name__)

ALERT_TYPE = "update_available"
_INTERVAL_SECONDS = 60 * 60
_BACKOFF_SECONDS = 6 * 60 * 60   # after a rate-limit response
_TIMEOUT_SECONDS = 10.0
_BODY_LIMIT = 500


@dataclass
class Action:
    """What the caller should do about a polled release."""
    tag: str
    notify: bool


def decide(state, release: dict, settings) -> Action:
    """Pure: given the stored state and a polled release, should we announce?

    Fails closed via is_newer(), which returns False for an unparseable running
    version — including the "dev" that source builds report.
    """
    tag = (release or {}).get("tag_name") or ""
    if not tag:
        return Action(tag="", notify=False)
    if tag == state.notified_tag:
        return Action(tag=tag, notify=False)
    return Action(tag=tag, notify=is_newer(tag, settings.version))


async def _fetch_latest_release(repo: str) -> dict:
    """GET the latest published release. Raises on any transport/HTTP error."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": user_agent()}
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _load_state(db) -> UpdateStatus:
    row = (await db.execute(
        select(UpdateStatus).where(UpdateStatus.id == 1))).scalar_one_or_none()
    if row is None:
        row = UpdateStatus(id=1)
        db.add(row)
        await db.flush()
    return row


async def check_once() -> None:
    """One poll. Never raises — the loop below depends on that."""
    settings = get_settings()
    try:
        release = await _fetch_latest_release(settings.update_check_repo)
    except Exception as e:
        logger.warning("update check: fetch failed: %s", e)
        try:
            async with AsyncSessionLocal() as db:
                state = await _load_state(db)
                state.last_error = str(e)[:512]
                await db.commit()
        except Exception:
            # Recording the error is best-effort; a broken DB must not turn a
            # failed poll into a crashed task.
            pass
        return

    try:
        async with AsyncSessionLocal() as db:
            state = await _load_state(db)
            action = decide(state, release, settings)

            state.latest_tag = action.tag or state.latest_tag
            state.latest_url = release.get("html_url")
            state.latest_body = (release.get("body") or "")[:_BODY_LIMIT]
            state.checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
            state.last_error = None
            published = release.get("published_at")
            if published:
                try:
                    state.latest_published_at = datetime.fromisoformat(
                        published.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass

            if action.notify:
                # Recorded in the SAME transaction as the send decision, and
                # committed BEFORE the send: a crash between the two costs one
                # missed notification, whereas the other order would re-announce
                # the same release on every restart.
                state.notified_tag = action.tag
                await db.commit()
                await send_discord_alert(
                    title=f"Vigilant {action.tag} is available",
                    body=(f"Running {settings.version}.\n"
                          f"{release.get('html_url', '')}\n"
                          f"Deploy with: scripts/deploy.sh --tag {action.tag}"),
                    alert_type=ALERT_TYPE,
                    key=action.tag,
                )
                logger.info("update check: announced %s", action.tag)
            else:
                await db.commit()
    except Exception as e:
        logger.warning("update check: state update failed: %s", e)


async def run_update_check() -> None:
    """Hourly poll loop. Started from app.main.startup()."""
    settings = get_settings()
    if not settings.update_check_enabled:
        logger.info("update check: disabled by UPDATE_CHECK_ENABLED=false")
        return
    while True:
        delay = _INTERVAL_SECONDS
        try:
            await check_once()
            async with AsyncSessionLocal() as db:
                state = await _load_state(db)
                if state.last_error and "403" in state.last_error:
                    delay = _BACKOFF_SECONDS
                    logger.info("update check: rate limited, backing off 6h")
        except Exception as e:
            # Belt and braces: check_once() already swallows everything, but
            # this loop must outlive any bug in it or in the backoff read.
            logger.warning("update check: unexpected error: %s", e)
        await asyncio.sleep(delay)
