"""Discord webhook relay for user-facing alert notifications.

Structure attacks, fuel alerts, and the other notification types surfaced
in the browser (see base.html's notification settings panel) can ALSO be
posted to a Discord webhook so they reach a sleeping/AFK player. This is
wired from the single in-process notification choke point,
`app.routes.dashboard._emit_notification` — every alert type (skill
completion, PI expiry, structure/POS/moon/sov alerts, inventory/contract
thresholds, kill alerts, ...) funnels through that one function regardless
of which route module raises it, so hooking there covers everything without
touching six separate call sites.

Fire-and-forget contract: `_emit_notification` schedules `send_discord_alert`
via `asyncio.create_task` rather than awaiting it, so a slow or unreachable
webhook can NEVER delay or break the sync path that generates alerts.
`send_discord_alert` itself also never raises — even if awaited directly
(e.g. from a test, or a future caller) any failure is caught, logged at
warning, and swallowed. This double guarantee means callers never need
their own try/except around it.
"""
import logging
import time

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_SUPPRESS_SECONDS = 30 * 60  # 30-minute per-(type, key) dedup window

# (alert_type, key) -> last-sent monotonic timestamp. Prevents a flapping
# or repeating alert (e.g. a structure staying "under attack" across
# multiple sync cycles) from spamming the channel. Recorded BEFORE the send
# attempt so a failing webhook doesn't retry-storm either.
_last_sent: dict[tuple[str, str], float] = {}


def _enabled_types(raw: str) -> set[str]:
    return {t.strip() for t in raw.split(",") if t.strip()}


async def send_discord_alert(title: str, body: str, alert_type: str, key: str | None = None) -> None:
    """POST an alert to the configured Discord webhook, if enabled.

    Args:
        title: Short alert headline (e.g. "Structure Under Attack").
        body: Longer detail line (e.g. structure/system name).
        alert_type: One of the notification-settings type strings
            (structure_attack, structure_fuel, inventory_low, ...) — gates
            delivery against the DISCORD_ALERT_TYPES opt-in list.
        key: Dedup key distinguishing this alert instance from others of the
            same type (e.g. a structure or item name) for the 30-min
            suppression window. Defaults to `title` when omitted, since most
            call sites only have title/body/type available.

    Never raises. No-ops silently when the webhook URL is unset or the
    alert_type isn't in the opt-in list; logs at warning and swallows HTTP
    errors, timeouts, and any other exception from the send itself.
    """
    settings = get_settings()
    webhook_url = settings.discord_webhook_url
    if not webhook_url:
        return  # relay not configured — silent no-op

    if alert_type not in _enabled_types(settings.discord_alert_types):
        return  # this alert type isn't opted in — silent no-op

    dedup_key = (alert_type, key or title)
    now = time.monotonic()
    last = _last_sent.get(dedup_key)
    if last is not None and (now - last) < _SUPPRESS_SECONDS:
        return  # suppressed: an identical alert already went out recently
    _last_sent[dedup_key] = now

    payload = {"content": f"**{title}**\n{body}" if body else f"**{title}**"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning(
                    "discord alert relay: HTTP %s posting type=%s", resp.status_code, alert_type
                )
    except Exception as e:
        logger.warning("discord alert relay: failed to send type=%s: %s", alert_type, e)
