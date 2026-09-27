"""Keep webhook credentials out of the HTTP client's own log lines.

The three webhook senders (app/notify/discord.py, app/notify/user_discord.py,
app/ops/update_reports.py) all promise that a webhook URL is never logged in
full, and each keeps that promise in its own log lines. httpx does not: it
logs every completed request at INFO as

    HTTP Request: POST https://discord.com/api/webhooks/<id>/<token> "HTTP/1.1 204 No Content"

and the app logs httpx at INFO on purpose, because that line is how ESI
traffic is seen in the container logs. So every webhook send wrote the whole
credential to the logs, once per send.

The fix is a logging.Filter on the "httpx" and "httpcore" loggers that
rewrites a record in two situations:

  * Inside `redact_request_urls()`, which every sender wraps around its POST,
    any URL in the record is cut down to scheme + host + "/…". The context is
    carried by a ContextVar, so it follows the task that made the request and
    nothing else: an ESI request made concurrently in another task is logged
    unchanged, path and all.
  * Always, with or without the context, a Discord webhook path
    (`/api/webhooks/<digits>/<token>`) is cut to `/api/webhooks/…`. This is
    the belt to the context manager's braces, for a future sender that
    forgets to wrap its POST.

The filter never raises. Whatever a record carries — a non-string msg, args
that are None, a tuple or a dict, an httpx.URL object — it either rewrites
what it can or leaves the record alone; a logging filter that raised would
take the request down with it.

Installation happens once, at import of this module, and is idempotent; each
sender imports the module so the filter is in place before its first send.
Only the logging line is changed. httpx still gets the real URL, and the
senders' own lines are untouched (they never held the URL to begin with).
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from typing import Iterator

# The senders' own "never in full" form (see update_reports.redact_url).
REDACTION_MARKER = "…"

# httpx logs on "httpx"; httpcore logs on these children, and a Logger's
# filters only see records logged on that logger itself, never records
# propagating up from a child. So each child is named, not just the parent.
LOGGER_NAMES = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)

_redact_request_urls: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "vigilant_redact_request_urls", default=False)

# scheme://host[:port] and whatever follows up to whitespace or a quote.
_URL_RE = re.compile(r"(?P<origin>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'/?#]+)(?P<rest>[^\s\"']*)")
# A Discord webhook path wherever it appears, context or not.
_WEBHOOK_PATH_RE = re.compile(r"/api/webhooks/\d+/[^\s\"'/?#]+(?:[?#][^\s\"']*)?")

# Values a %-format can carry that never hold a URL and must stay as they are
# (`%d` on a stringified int would raise inside the formatter).
_PLAIN_TYPES = (int, float, bool, bytes, type(None))


@contextlib.contextmanager
def redact_request_urls() -> Iterator[None]:
    """While active in this task, every URL httpx/httpcore log is cut to its
    origin. Wrap exactly the HTTP call that carries a credential in its URL."""
    token = _redact_request_urls.set(True)
    try:
        yield
    finally:
        _redact_request_urls.reset(token)


def redacting() -> bool:
    """Whether the current context is inside redact_request_urls()."""
    return _redact_request_urls.get()


def _cut_url(m: re.Match) -> str:
    return m.group("origin") + "/" + REDACTION_MARKER if m.group("rest") else m.group("origin")


def redact_text(text: str, everything: bool) -> str:
    """`text` with credentials taken out of its URLs.

    With `everything`, every URL becomes scheme://host/… ; otherwise only a
    Discord webhook path is cut, and any other URL is returned as it came.
    """
    if everything:
        text = _URL_RE.sub(_cut_url, text)
    return _WEBHOOK_PATH_RE.sub("/api/webhooks/" + REDACTION_MARKER, text)


def _redact_value(value, everything: bool):
    """One format argument. Strings are rewritten; anything else (httpx.URL,
    for one) is rewritten through its str() only if that changes something,
    so an object that carries no URL keeps its type for the formatter."""
    if isinstance(value, str):
        return redact_text(value, everything)
    if isinstance(value, _PLAIN_TYPES):
        return value
    try:
        text = str(value)
    except Exception:
        return value
    cleaned = redact_text(text, everything)
    return cleaned if cleaned != text else value


class RedactRequestUrlFilter(logging.Filter):
    """Rewrites msg and args in place; always lets the record through."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            everything = _redact_request_urls.get()
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg, everything)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(_redact_value(a, everything) for a in args)
            elif isinstance(args, dict):
                record.args = {k: _redact_value(v, everything) for k, v in args.items()}
            elif args is not None:
                record.args = _redact_value(args, everything)
        except Exception:
            # A filter that raises would fail the request that logged. Let the
            # record through as it is: still better than no request at all.
            pass
        return True


_FILTER = RedactRequestUrlFilter()


def install() -> None:
    """Attach the filter to every httpx/httpcore logger, once."""
    for name in LOGGER_NAMES:
        log = logging.getLogger(name)
        if not any(f is _FILTER for f in log.filters):
            log.addFilter(_FILTER)


install()
