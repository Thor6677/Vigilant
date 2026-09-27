"""Per-user Discord webhook relay for alerts (T-075).

Every alert goes through `app.routes.dashboard._emit_notification`. Besides the
instance-wide relay in app/notify/discord.py (one webhook from the
environment, the operator's), that choke point also schedules
`send_user_discord_alert` here, fire-and-forget, so a user who pasted their
own webhook on the Account page gets the alert types they opted in to — PI
extractor expiry included — in their own channel.

The webhook URL is a credential: whoever holds it can post to the channel.
So it is encrypted at rest (EncryptedText), never rendered back into a page
(the Account page shows `mask_webhook_url`), and never logged in full
(`webhook_log_ref` is host plus webhook id). And because the send is made
from inside the deployment on a user's behalf, the URL is validated to the
shape of a Discord webhook at save time AND again before every send, and the
connection is made only to vetted public addresses, through the same pinned
transport ISS-055 built for update-report webhooks.

Never-raise contract: `send_user_discord_alert` catches everything. A failure
is one `logger.warning` line without a traceback — the deployment's
monitoring counts traceback lines and pages on them — and is recorded on the
user's row (`last_ok`, `last_error`, no URL) where the Account page shows it.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import httpx
from sqlalchemy import select

from app.db.models import AsyncSessionLocal, UserNotifySettings
# Importing log_redaction installs the filter that keeps httpx's own request
# line from printing the URL; redact_request_urls() marks the send.
from app.notify.log_redaction import redact_request_urls
# The vetting and the pinned transport are ISS-055's; they are reused as they
# are so both webhook senders dial exactly the same way and one set of tests
# (tests/test_webhook_targets.py) covers the socket layer for both.
from app.ops.update_reports import _PinnedTransport, vetted_addresses

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0
_SUPPRESS_SECONDS = 30 * 60        # per-(user, type, key) dedup window
_SETTINGS_TTL_SECONDS = 60.0       # per-user settings cache
_RATE_LIMIT = 20                   # messages per user ...
_RATE_WINDOW_SECONDS = 10 * 60     # ... per this window; the excess is dropped
_MUTE_MAX_SECONDS = 15 * 60        # cap on how long a Discord 429 mutes a user
_DEDUP_PRUNE_AT = 5000             # entries; prune expired ones past this

URL_MAX_LENGTH = 300
ALLOWED_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})
_PATH_RE = re.compile(r"^/api/webhooks/(\d{1,25})/([A-Za-z0-9_-]{1,100})$")
_DIGITS_RE = re.compile(r"^\d{1,25}$")

# What send_user_discord_alert() reports back, like app/notify/discord.py.
SENT = "sent"
NOT_SENT_NO_WEBHOOK = "not sent: no webhook saved"
NOT_SENT_DISABLED = "not sent: the webhook is paused"
NOT_SENT_TYPE_OFF = "not sent: alert type not opted in"
NOT_SENT_SUPPRESSED = "not sent: repeat within the suppression window"
NOT_SENT_RATE_CAPPED = "not sent: per-user rate cap"
NOT_SENT_MUTED = "not sent: Discord asked for a pause"

GONE_ERROR = "Discord says this webhook no longer exists — paste a new one"
INVALID_STORED_ERROR = "the saved webhook URL is no longer accepted — paste a new one"

# ── Alert types ───────────────────────────────────────────────────────────────
# Every "type" _emit_notification is handed, grouped for the Account page. The
# relay only ever sends a type that is in here AND in the user's opt-in list.
# `auto_update` is deliberately absent: its emitter passes relay=False and
# delivers to the operator's channels itself, so a checkbox for it would never
# fire. Legacy `structure_alert` events follow structure_attack, as they do in
# static/js/notifications.js.
ALERT_TYPE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Structures & POS", (
        ("structure_attack", "Under attack, shields or armor lost, destroyed (structures, POS, skyhooks)"),
        ("structure_fuel", "Fuel low, services offline (structures and POS)"),
        ("structure_change", "Anchoring, unanchoring, online"),
        ("sovereignty", "Sovereignty structure reinforced, command node events"),
        ("moonmining", "Moon mining extractions and fractures"),
        ("poco", "Customs office attacked or reinforced"),
    )),
    ("Planetary industry", (
        ("pi_expiring", "Extractors expiring or expired"),
    )),
    ("Skills", (
        ("skill_complete", "Skill training complete"),
    )),
    ("Industry & market thresholds", (
        ("inventory_low", "Corp inventory below the low threshold"),
        ("inventory_critical", "Corp inventory below the critical threshold"),
        ("contract_low", "Corp contracts below the low threshold"),
        ("contract_critical", "Corp contracts below the critical threshold"),
        ("stockpile_low", "Stockpile below its target"),
    )),
    ("Kill alerts", (
        ("kill_alert", "A watched pilot, corp, alliance or system in a kill"),
    )),
)
ALERT_TYPES: tuple[str, ...] = tuple(key for _, items in ALERT_TYPE_GROUPS for key, _ in items)
ALERT_LABELS: dict[str, str] = {key: label for _, items in ALERT_TYPE_GROUPS for key, label in items}
DEFAULT_ALERT_TYPES: tuple[str, ...] = ("structure_attack", "structure_fuel", "pi_expiring", "stockpile_low")
_TYPE_ALIASES = {"structure_alert": "structure_attack"}


def parse_alert_types(raw: str | None) -> list[str]:
    """The opted-in types in a stored CSV, in allowlist order, unknowns dropped."""
    chosen = {t.strip() for t in (raw or "").split(",") if t.strip()}
    return [t for t in ALERT_TYPES if t in chosen]


def serialize_alert_types(types) -> str:
    """CSV for storage: allowlist order, unknowns dropped, no duplicates."""
    chosen = set(types or ())
    return ",".join(t for t in ALERT_TYPES if t in chosen)


# ── URL validation, masking and logging ───────────────────────────────────────

def validate_discord_webhook_url(url: str) -> tuple[bool, str | None]:
    """(True, None) if `url` is exactly a Discord webhook URL, else (False, why).

    Pure: no lookups. The shape is pinned down rather than parsed loosely —
    https, one of Discord's own hosts (no lookalikes, IP literals, userinfo,
    ports other than 443, trailing dots or encoded tricks), the webhook path
    with its two components, at most Discord's own `thread_id` and `wait`
    query parameters, no fragment — because the app will connect to it on a
    user's behalf. Whitespace, control and non-ASCII characters are refused
    outright: nothing in a real webhook URL needs them and every homograph or
    smuggling trick starts with one.
    """
    if not isinstance(url, str) or not url:
        return False, "enter a webhook URL"
    if len(url) > URL_MAX_LENGTH:
        return False, f"the URL is longer than {URL_MAX_LENGTH} characters"
    if any(not (33 <= ord(c) <= 126) for c in url):
        return False, "the URL contains whitespace or characters a Discord webhook URL never has"
    if "#" in url:
        return False, "the URL must not have a #fragment"
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "that is not a URL"
    if parts.scheme != "https":
        return False, "the URL must start with https://"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False, "the URL must not contain a username or password"
    try:
        port = parts.port
    except ValueError:
        return False, "the URL has an invalid port"
    if port not in (None, 443):
        return False, "the URL must use port 443"
    host = parts.hostname or ""
    if host not in ALLOWED_HOSTS:
        return False, "the host must be discord.com (or discordapp.com, ptb.discord.com, canary.discord.com)"
    if not _PATH_RE.match(parts.path or ""):
        return False, "the path must look like /api/webhooks/<id>/<token>"
    if parts.query:
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False, "the URL has an unusable query string"
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen:
                return False, f"the query repeats {key}"
            seen.add(key)
            if key == "thread_id" and _DIGITS_RE.match(value):
                continue
            if key == "wait" and value == "true":
                continue
            return False, "only ?thread_id=<id> and ?wait=true are allowed after the token"
    return True, None


def _parts(url: str | None) -> tuple[str, str] | None:
    """(host, webhook id) of a URL that validates, else None."""
    if not url:
        return None
    ok, _ = validate_discord_webhook_url(url)
    if not ok:
        return None
    parts = urlsplit(url)
    m = _PATH_RE.match(parts.path or "")
    return (parts.hostname or "", m.group(1)) if m else None


def mask_webhook_url(url: str | None) -> str:
    """What the Account page shows for a saved URL: host, the start of the
    webhook id, and dots for the token. Never the token, never the whole id."""
    parts = _parts(url)
    if parts is None:
        return "(unreadable URL)" if url else ""
    host, hook_id = parts
    return f"{host}/api/webhooks/{hook_id[:4]}…/••••"


def webhook_log_ref(url: str | None) -> str:
    """The only form of a webhook URL that ever reaches a log line."""
    parts = _parts(url)
    return f"{parts[0]}/{parts[1]}" if parts else "(invalid url)"


# ── In-process state ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Config:
    url: str | None
    enabled: bool
    types: frozenset[str]


_config_cache: dict[int, tuple[float, _Config]] = {}
_last_sent: dict[tuple[int, str, str], float] = {}
_sent_times: dict[int, deque[float]] = {}
_dropped: dict[int, int] = {}          # rate-capped drops per user, for the report
_muted_until: dict[int, float] = {}    # Discord 429: don't try again before this


def invalidate(user_id: int) -> None:
    """Forget the cached settings after a save or remove."""
    _config_cache.pop(user_id, None)


def _reset_state() -> None:
    """Tests only."""
    _config_cache.clear()
    _last_sent.clear()
    _sent_times.clear()
    _dropped.clear()
    _muted_until.clear()


async def _load_config(user_id: int) -> _Config:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(UserNotifySettings).where(
            UserNotifySettings.user_id == user_id))).scalar_one_or_none()
        if row is None:
            return _Config(None, False, frozenset())
        return _Config(row.discord_webhook_url, bool(row.enabled),
                       frozenset(parse_alert_types(row.alert_types)))


async def _config_for(user_id: int) -> _Config:
    now = time.monotonic()
    hit = _config_cache.get(user_id)
    if hit is not None and now - hit[0] < _SETTINGS_TTL_SECONDS:
        return hit[1]
    cfg = await _load_config(user_id)
    _config_cache[user_id] = (now, cfg)
    return cfg


def _suppressed(user_id: int, alert_type: str, key: str, now: float) -> bool:
    if len(_last_sent) > _DEDUP_PRUNE_AT:
        for k in [k for k, t in _last_sent.items() if now - t >= _SUPPRESS_SECONDS]:
            del _last_sent[k]
    last = _last_sent.get((user_id, alert_type, key))
    if last is not None and now - last < _SUPPRESS_SECONDS:
        return True
    _last_sent[(user_id, alert_type, key)] = now
    return False


def _rate_capped(user_id: int, now: float) -> bool:
    times = _sent_times.setdefault(user_id, deque())
    while times and now - times[0] >= _RATE_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= _RATE_LIMIT:
        _dropped[user_id] = _dropped.get(user_id, 0) + 1
        return True
    times.append(now)
    return False


def dropped_count(user_id: int) -> int:
    """How many alerts the rate cap has dropped for this user since start-up."""
    return _dropped.get(user_id, 0)


# ── The message ──────────────────────────────────────────────────────────────

def neutralise_mentions(text: str) -> str:
    """Break every @mention so alert text can't ping a channel. Belt: the
    payload's allowed_mentions is the braces."""
    return (text or "").replace("@", "@​")


def _kill_alert_text(event: dict) -> tuple[str, str, str]:
    """(title, body, key) for a kill_alert event, whose emitter passes no title."""
    label = str(event.get("matched_label") or "watched entity")
    kind = str(event.get("kind") or "kill")
    system = str(event.get("system_name") or event.get("system_id") or "?")
    value = float(event.get("total_value") or 0)
    isk = f"{value / 1e9:.2f}B ISK" if value >= 1e9 else f"{value / 1e6:.1f}M ISK"
    body = f"{label} — {kind} in {system} ({isk})"
    if event.get("zkb_url"):
        body += f"\n{event['zkb_url']}"
    key = f"km:{event.get('killmail_id') or ''}:{label}"
    return "Kill alert", body, key


def build_payload(title: str, body: str, alert_type: str) -> dict:
    label = ALERT_LABELS.get(alert_type) or alert_type
    return {
        "username": "Vigilant",
        "embeds": [{
            "title": neutralise_mentions(title)[:256],
            "description": neutralise_mentions(body)[:2048],
            "footer": {"text": f"Vigilant · {label}"[:2048]},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "allowed_mentions": {"parse": []},
    }


@dataclass
class Outcome:
    ok: bool
    error: str | None = None   # short, never contains the URL
    gone: bool = False         # Discord says the webhook no longer exists
    mute_seconds: float = 0.0  # Discord asked for a pause (429)


def _retry_after(resp) -> float:
    for header in ("retry-after", "x-ratelimit-reset-after"):
        raw = resp.headers.get(header)
        if raw:
            try:
                return max(0.0, min(float(raw), _MUTE_MAX_SECONDS))
            except ValueError:
                continue
    return 60.0


async def deliver(url: str, title: str, body: str, alert_type: str) -> Outcome:
    """One POST to `url`, with every check re-applied. Never raises.

    Only the addresses vetted here are dialled (see _PinnedTransport); no
    redirects, no environment proxy, five seconds.
    """
    from app.config import user_agent

    ok, problem = validate_discord_webhook_url(url)
    if not ok:
        return Outcome(False, INVALID_STORED_ERROR, gone=True)
    problem, addrs = await vetted_addresses(url)
    if problem:
        return Outcome(False, f"not sent: {problem}")
    try:
        with redact_request_urls():
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False,
                                         trust_env=False,
                                         transport=_PinnedTransport(addrs)) as client:
                resp = await client.post(url, json=build_payload(title, body, alert_type),
                                         headers={"User-Agent": user_agent()})
    except Exception as e:
        return Outcome(False, f"{type(e).__name__}"[:255])
    status = resp.status_code
    if 200 <= status < 300:
        return Outcome(True)
    if status in (401, 404):
        return Outcome(False, GONE_ERROR, gone=True)
    if status == 429:
        return Outcome(False, "HTTP 429: Discord is rate limiting this webhook",
                       mute_seconds=_retry_after(resp))
    if 300 <= status < 400:
        return Outcome(False, f"HTTP {status}: redirect not followed")
    return Outcome(False, f"HTTP {status}")


async def record_outcome(db, user_id: int, outcome: Outcome) -> None:
    """Write the delivery result to the user's row, on the caller's session."""
    row = (await db.execute(select(UserNotifySettings).where(
        UserNotifySettings.user_id == user_id))).scalar_one_or_none()
    if row is None:
        return
    row.last_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.last_ok = outcome.ok
    row.last_error = None if outcome.ok else (outcome.error or "failed")[:255]
    if outcome.gone:
        row.enabled = False
    await db.commit()
    if outcome.gone:
        invalidate(user_id)


async def _record(user_id: int, outcome: Outcome) -> None:
    async with AsyncSessionLocal() as db:
        await record_outcome(db, user_id, outcome)


async def _send(user_id: int, title: str, body: str, alert_type: str,
                key: str | None, event: dict | None) -> str:
    cfg = await _config_for(user_id)
    if not cfg.url:
        return NOT_SENT_NO_WEBHOOK
    if not cfg.enabled:
        return NOT_SENT_DISABLED
    alert_type = _TYPE_ALIASES.get(alert_type, alert_type)
    if alert_type not in cfg.types:
        return NOT_SENT_TYPE_OFF
    if alert_type == "kill_alert" and event:
        title, body, key = _kill_alert_text(event)

    now = time.monotonic()
    if now < _muted_until.get(user_id, 0.0):
        return NOT_SENT_MUTED
    if _suppressed(user_id, alert_type, key or title, now):
        return NOT_SENT_SUPPRESSED
    if _rate_capped(user_id, now):
        return NOT_SENT_RATE_CAPPED

    outcome = await deliver(cfg.url, title, body, alert_type)
    if outcome.mute_seconds:
        _muted_until[user_id] = now + outcome.mute_seconds
    if not outcome.ok:
        logger.warning("user discord alert: user=%s type=%s webhook=%s %s%s",
                       user_id, alert_type, webhook_log_ref(cfg.url), outcome.error,
                       " (webhook disabled)" if outcome.gone else "")
    await _record(user_id, outcome)
    return SENT if outcome.ok else f"failed: {outcome.error}"


async def send_user_discord_alert(user_id: int, title: str, body: str, alert_type: str,
                                  key: str | None = None, *, event: dict | None = None) -> str:
    """Post one alert to this user's own webhook, if they opted in to its type.

    Never raises, whether scheduled from _emit_notification or awaited
    directly. Skips silently when the user has no webhook, paused it, or did
    not opt in to `alert_type`; dedups per (user, type, key) for 30 minutes;
    drops anything over the per-user rate cap; and never sleeps on a 429 —
    the user is muted until Discord's retry-after instead.

    `event` is the full notification event, for types whose emitter passes no
    usable title (kill alerts). Returns SENT, a NOT_SENT_* reason or
    "failed: <why>", like the instance-wide relay.
    """
    try:
        return await _send(user_id, title, body, alert_type, key, event)
    except Exception as e:
        logger.warning("user discord alert: user=%s type=%s failed: %s",
                       user_id, alert_type, type(e).__name__)
        return f"failed: {type(e).__name__}"


async def send_test_message(db, user_id: int, url: str) -> Outcome:
    """The Account page's "Send test message": one synchronous delivery to
    `url`, past the opt-in list, the dedup window and the rate cap, recorded
    on the user's row through the caller's session. Never raises."""
    try:
        outcome = await deliver(
            url, "Test message",
            "Vigilant can reach this channel. Alerts you opted in to will arrive here.",
            "test")
    except Exception as e:
        outcome = Outcome(False, type(e).__name__)
    if not outcome.ok:
        logger.warning("user discord alert: user=%s test to webhook=%s %s",
                       user_id, webhook_log_ref(url), outcome.error)
    await record_outcome(db, user_id, outcome)
    return outcome
