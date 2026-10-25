"""Best-effort reader for saved br.evetools.org reports (T-082).

A saved report (`/br/<24 hex>`) has no documented API. The site's own front
end calls a JSON endpoint that returns exactly what the loot finder needs: one
(system, start, end) window per system, plus the kill ids and hashes. It is
undocumented, sits behind Cloudflare and may change without notice, so this
module is a kill-switched convenience, not a dependency:

  * it runs ONCE, when a link is pasted. The caller turns the result into a
    canonical `/intel/loot/windows?...` URL, so viewing a report fetches
    nothing from evetools;
  * the host and path are constants, only a 24-hex id is interpolated; no
    redirects are followed; the body is streamed under a 16 MB cap;
  * only integers (and 40-hex hashes) are extracted. No string from the
    response is ever kept or rendered;
  * results are cached for an hour (an owner can edit a report) as windows and
    a kill count only, and fetches are rate limited per user and per process,
    run at most two at a time, and share one total deadline;
  * any failure becomes a message telling the user to paste each system's
    'link to related' instead (a supported, parse-only path).

`parse_composition` is pure, so the response shapes can be tested without HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from app.config import get_settings, user_agent
from app.intel.battle_loot import (
    DEFAULT_HOURS_AFTER, DEFAULT_HOURS_BEFORE, MAX_EPOCH, MAX_WINDOWS, MAX_WINDOW_SECS,
    SORRY_PASTE_RELATED, BrError, Scope, Window, parse_stamp, normalize_scope,
)

log = logging.getLogger(__name__)

EVETOOLS_API = "https://br.evetools.org/newapi"
FETCH_TIMEOUT_SECONDS = 10      # per read
TOTAL_DEADLINE_SECONDS = 20     # the whole fetch, however slowly the bytes trickle
MAX_CONCURRENT_FETCHES = 2
MAX_BODY_BYTES = 16 * 1024 * 1024
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 128
USER_LIMIT, USER_WINDOW_SECONDS = 6, 60
PROCESS_LIMIT, PROCESS_WINDOW_SECONDS = 60, 3600

_OLD_KEY_RE = re.compile(r"^(\d{1,10})/(\d{12})$")
_EPOCH_MIN = 1_000_000_000   # 2001, in seconds
_MS_FLOOR = 100_000_000_000  # at or above this a timestamp is milliseconds


@dataclass(frozen=True)
class EvetoolsScope:
    windows: Scope
    kms_count: int


@dataclass(frozen=True)
class EvetoolsError:
    message: str


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _windows(doc: dict) -> list[Window]:
    timings = doc.get("timings")
    if isinstance(timings, list) and timings:
        out = []
        for t in timings:
            if not isinstance(t, dict):
                raise BrError("unreadable")
            sid, start, end = t.get("systemID"), t.get("start"), t.get("end")
            if not (_is_int(sid) and _is_int(start) and _is_int(end)):
                raise BrError("unreadable")
            if not (_EPOCH_MIN <= start < MAX_EPOCH and _EPOCH_MIN <= end < MAX_EPOCH):
                raise BrError("unreadable")
            out.append(Window(sid, start, end))
        return out
    # Older reports: one related stamp per system, no explicit window. Assumed
    # to span the same -1h/+2h the related views use (unverified).
    relateds = doc.get("relateds")
    if isinstance(relateds, list) and relateds:
        out = []
        for r in relateds:
            key = r.get("relatedKey") if isinstance(r, dict) else None
            m = _OLD_KEY_RE.match(key) if isinstance(key, str) else None
            if not m:
                raise BrError("unreadable")
            stamp = parse_stamp(m.group(2))
            out.append(Window(int(m.group(1)), stamp - DEFAULT_HOURS_BEFORE * 3600,
                              stamp + DEFAULT_HOURS_AFTER * 3600))
        return out
    raise BrError("unreadable")


def _count_kills(doc: dict) -> int:
    n = 0
    relateds = doc.get("relateds")
    for rel in relateds if isinstance(relateds, list) else ():
        kms = rel.get("kms") if isinstance(rel, dict) else None
        if isinstance(kms, list):
            n += sum(1 for km in kms if isinstance(km, dict) and _is_int(km.get("id")) and km["id"] > 0)
    return n


def parse_composition(doc) -> EvetoolsScope | EvetoolsError:
    """A decoded `/br/composition/<id>` document -> windows and a kill count.

    Pure: no I/O and no system lookup (the caller checks system ids against the
    SDE). Whatever the document looks like, this returns; it never raises."""
    try:
        return _parse_composition(doc)
    except Exception:       # RecursionError, odd types: all "couldn't read it"
        return EvetoolsError(SORRY_PASTE_RELATED)


def _parse_composition(doc) -> EvetoolsScope | EvetoolsError:
    if not isinstance(doc, dict):
        return EvetoolsError(SORRY_PASTE_RELATED)
    status = doc.get("status")
    if status == "processing":
        return EvetoolsError("evetools is still building that report. Try again shortly.")
    if status == "error":
        return EvetoolsError("evetools has no report with that link. Check it and try again.")
    if status != "success":
        return EvetoolsError(SORRY_PASTE_RELATED)
    try:
        windows = _windows(doc)
        if len(windows) > MAX_WINDOWS:
            return EvetoolsError(f"That report covers more than {MAX_WINDOWS} systems. "
                                 "Paste each system's 'link to related' instead.")
        if any(w.end - w.start > MAX_WINDOW_SECS for w in windows):
            return EvetoolsError("That report spans more than a day in one system. "
                                 "Paste each system's 'link to related' instead.")
        scope = normalize_scope(windows)
    except BrError:
        return EvetoolsError(SORRY_PASTE_RELATED)
    count = doc.get("kmsCount")
    return EvetoolsScope(scope, count if _is_int(count) and count >= 0 else _count_kills(doc))


def _seconds(v) -> int | None:
    if not _is_int(v) or v < _EPOCH_MIN:
        return None
    return v // 1000 if v >= _MS_FLOOR else v


def parse_battle(doc) -> EvetoolsScope | EvetoolsError:
    """A decoded `/br/battle/<n>` document: `{battle{solar_system,
    battle_start, battle_end}, kms[...]}` -> one window. Times are taken as
    epoch seconds or milliseconds (unverified which). Never raises."""
    try:
        battle = doc.get("battle") if isinstance(doc, dict) else None
        if not isinstance(battle, dict):
            return EvetoolsError(SORRY_PASTE_RELATED)
        sid = battle.get("solar_system")
        start, end = _seconds(battle.get("battle_start")), _seconds(battle.get("battle_end"))
        if not _is_int(sid) or start is None or end is None:
            return EvetoolsError(SORRY_PASTE_RELATED)
        if end - start > MAX_WINDOW_SECS:
            return EvetoolsError("That battle spans more than a day. "
                                 "Paste each system's 'link to related' instead.")
        scope = normalize_scope([Window(sid, start, end)])
        kms = doc.get("kms")
        count = sum(1 for km in kms if isinstance(km, dict) and _is_int(km.get("id"))) \
            if isinstance(kms, list) else 0
        return EvetoolsScope(scope, count)
    except Exception:
        return EvetoolsError(SORRY_PASTE_RELATED)


# ── Fetching ─────────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, EvetoolsScope]] = {}
_user_hits: dict[int, deque] = {}
_process_hits: deque = deque()


def src_cache_key(src: str) -> str:
    """The cache key for a canonical `src` value (`evetools:<id>` or
    `evetools-battle:<n>`)."""
    kind, _, ident = src.partition(":")
    return f"battle:{ident}" if kind == "evetools-battle" else ident


def cached_resolution(key: str) -> EvetoolsScope | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _retry_after(hits: deque, limit: int, window: int, now: float) -> int:
    while hits and now - hits[0] >= window:
        hits.popleft()
    return int(window - (now - hits[0])) + 1 if len(hits) >= limit else 0


def _rate_limited(user_id: int) -> int:
    """Seconds to wait before another fetch is allowed, 0 if it is."""
    now = time.monotonic()
    user_hits = _user_hits.setdefault(user_id, deque())
    wait = max(_retry_after(user_hits, USER_LIMIT, USER_WINDOW_SECONDS, now),
               _retry_after(_process_hits, PROCESS_LIMIT, PROCESS_WINDOW_SECONDS, now))
    if not wait:
        user_hits.append(now)
        _process_hits.append(now)
    if len(_user_hits) > 256:
        for uid in [u for u, h in _user_hits.items() if not h or now - h[-1] >= USER_WINDOW_SECONDS]:
            del _user_hits[uid]
    return wait


_fetch_gate: asyncio.Semaphore | None = None
_fetch_gate_loop = None


def _gate() -> asyncio.Semaphore:
    global _fetch_gate, _fetch_gate_loop
    loop = asyncio.get_running_loop()
    if _fetch_gate is None or _fetch_gate_loop is not loop:
        _fetch_gate, _fetch_gate_loop = asyncio.Semaphore(MAX_CONCURRENT_FETCHES), loop
    return _fetch_gate


async def _read_capped(resp: httpx.Response) -> bytes | None:
    """The decoded body, or None once it would pass MAX_BODY_BYTES.

    The wire bytes are read raw and gunzipped here with a per-call output
    limit, so one small compressed chunk cannot inflate past the cap before it
    is counted. Only identity and gzip are accepted."""
    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("identity", "gzip"):
        return None
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    buf = bytearray()
    async for chunk in resp.aiter_raw():
        pending = chunk
        while pending:
            if inflater is None:
                buf += pending
                pending = b""
            else:
                buf += inflater.decompress(pending, MAX_BODY_BYTES + 1 - len(buf))
                pending = inflater.unconsumed_tail
            if len(buf) > MAX_BODY_BYTES:
                return None
    if inflater is not None:
        buf += inflater.flush(MAX_BODY_BYTES + 1 - len(buf))
        if len(buf) > MAX_BODY_BYTES:
            return None
    return bytes(buf)


async def _fetch_body(path: str) -> bytes | None:
    """One GET to the constant host plus `path`. None on any failure (non-200,
    a redirect, a timeout, a body past the cap)."""
    url = f"{EVETOOLS_API}{path}"
    headers = {"User-Agent": user_agent(), "Accept": "application/json", "Accept-Encoding": "gzip"}
    try:
        async with _gate():
            async with asyncio.timeout(TOTAL_DEADLINE_SECONDS):
                async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False,
                                             trust_env=False) as client:
                    async with client.stream("GET", url, headers=headers) as resp:
                        if resp.status_code != 200:
                            log.info("evetools: HTTP %s", resp.status_code)
                            return None
                        body = await _read_capped(resp)
                        if body is None:
                            log.info("evetools: body over the cap or encoded oddly")
                        return body
    except Exception as e:
        log.info("evetools: fetch failed: %s", type(e).__name__)
        return None


def _store(key: str, result: EvetoolsScope) -> None:
    now = time.monotonic()
    for k in [k for k, (t, _r) in _cache.items() if now - t >= CACHE_TTL_SECONDS]:
        del _cache[k]
    if len(_cache) >= CACHE_MAX_ENTRIES:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[key] = (now, result)


async def _resolve(key: str, path: str, parse, user_id: int, system_exists) -> EvetoolsScope | EvetoolsError:
    try:
        if not get_settings().battle_loot_evetools_resolve:
            return EvetoolsError(SORRY_PASTE_RELATED)
        hit = cached_resolution(key)
        if hit is not None:
            return hit
        wait = _rate_limited(user_id)
        if wait:
            return EvetoolsError(f"Too many evetools lookups. Try again in {wait} s.")
        body = await _fetch_body(path)
        if body is None:
            return EvetoolsError(SORRY_PASTE_RELATED)
        try:
            doc = json.loads(body)
        except (ValueError, RecursionError):
            return EvetoolsError(SORRY_PASTE_RELATED)
        result = parse(doc)
        if isinstance(result, EvetoolsError):
            return result
        if system_exists is not None:
            ids = sorted({w.system_id for w in result.windows})
            if set(ids) - await system_exists(ids):
                return EvetoolsError(SORRY_PASTE_RELATED)
        _store(key, result)
        return result
    except Exception:
        log.exception("evetools resolve failed")
        return EvetoolsError(SORRY_PASTE_RELATED)


async def resolve_br(br_id: str, user_id: int,
                     system_exists: Callable[[list[int]], Awaitable[set[int]]] | None = None,
                     ) -> EvetoolsScope | EvetoolsError:
    """Saved evetools report -> windows. Never raises.

    `system_exists(ids)` returns the subset of ids that are real solar systems;
    a report naming any other is refused."""
    return await _resolve(br_id, f"/br/composition/{br_id}", parse_composition, user_id, system_exists)


async def resolve_battle(battle_id: int, user_id: int,
                         system_exists: Callable[[list[int]], Awaitable[set[int]]] | None = None,
                         ) -> EvetoolsScope | EvetoolsError:
    """An evetools auto-detected battle (`/battle/<n>`) -> one window. Same
    safeguards as resolve_br; `battle_id` is an int by construction."""
    return await _resolve(f"battle:{battle_id}", f"/br/battle/{int(battle_id)}", parse_battle,
                          user_id, system_exists)
