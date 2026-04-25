"""eve-offline.net (Chribba / OMG Labs) historical PCU scraper.

Two acquisition paths:

1. `fetch_chribba_archive()` — single GET of the rendered page, extracts the
   embedded `var fulldata = [[ts_ms, players], ...];`. Returns ~800 rows
   spread evenly over 23 years (~10-day intervals). Use this for a fast
   coarse seed.

2. `fetch_chribba_archive_fine()` — async-iterates 1-day windows of the
   JSONP zoom endpoint, yielding lists of fine-grained rows. Resolution is
   1-minute from ~2010 onward (Chribba's automation startup), sparse before.
   Total ~8.4M rows over 23 years. Use this for archival-quality backfill.

Recon notes (captured 2026-04-25):
- Page URL: https://eve-offline.net/?server=tranquility
- JSONP endpoint: /data/?server=tranquility&start=<ms>&end=<ms>&callback=?
- Endpoint returns `cb([[ts_ms, players], ...])` — strip callback wrapper
- Endpoint downsamples to ~1440 rows max per response. Querying day-sized
  windows yields the underlying minute-level data for post-2010 years;
  larger windows cause server-side downsampling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

EVE_OFFLINE_NET_URL = "https://eve-offline.net/?server=tranquility"
EVE_OFFLINE_NET_DATA_URL = "https://eve-offline.net/data/"
SCRAPER_UA = "Vigilant/1.0 backfill (happyfun.fatman@gmail.com)"

# Earliest data point Chribba records (we don't bother fetching pre-EVE-launch).
ARCHIVE_START = datetime(2003, 5, 1)

_JSONP_RE = re.compile(r"cb\(\s*(\[.*\])\s*\)\s*;?\s*$", re.DOTALL)

# Match `var fulldata = ([[...]]);` — the page wraps the array in extra
# parens (`= (...)`). Allow either form for forward-compat. The greedy match
# extends through the array body; the trailing `)?\s*;` consumes the close.
_FULLDATA_RE = re.compile(
    r"var\s+fulldata\s*=\s*\(?\s*(\[\[.*?\]\])\s*\)?\s*;",
    re.DOTALL,
)


async def fetch_chribba_archive() -> list[dict]:
    """Fetch + parse the Chribba archive. Returns a list of dicts ready to
    pass to PlayerCountSnapshot upsert:

        [{"recorded_at": datetime, "player_count": int,
          "source": "eve-offline-net", "granularity": "daily"}, ...]

    Raises on transport failure or parse failure (callers should surface the
    error; this is a one-shot, errors are loud).
    """
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": SCRAPER_UA, "Accept": "text/html"},
    ) as client:
        r = await client.get(EVE_OFFLINE_NET_URL)
        r.raise_for_status()
        body = r.text

    m = _FULLDATA_RE.search(body)
    if not m:
        raise RuntimeError(
            "eve-offline.net: could not locate `var fulldata = [...]` in page. "
            "Site structure may have changed — re-do recon."
        )
    raw = m.group(1)
    try:
        pairs = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"eve-offline.net: fulldata is not valid JSON: {e}") from e

    out: list[dict] = []
    for entry in pairs:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        ts_ms, pc = entry
        if not isinstance(ts_ms, (int, float)) or not isinstance(pc, (int, float)):
            continue
        if pc <= 0:
            # Drop the early sentinel zeros (first few points) — not real data.
            continue
        try:
            recorded_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            continue
        out.append({
            "recorded_at": recorded_at,
            "player_count": int(pc),
            "source": "eve-offline-net",
            "granularity": "weekly",
        })

    log.info(
        "eve-offline-net: fetched %d points (range %s to %s)",
        len(out),
        out[0]["recorded_at"] if out else "—",
        out[-1]["recorded_at"] if out else "—",
    )
    return out


async def _fetch_window(
    client: httpx.AsyncClient, start_ms: int, end_ms: int
) -> list[list]:
    """One JSONP fetch. Returns the raw [[ts_ms, players], ...] list, or []
    on failure (logged at debug — we don't want a single bad window to abort
    the whole archive backfill)."""
    url = f"{EVE_OFFLINE_NET_DATA_URL}?server=tranquility&start={start_ms}&end={end_ms}&callback=cb"
    try:
        r = await client.get(url)
        if r.status_code != 200:
            log.debug("eve-offline-net %s-%s: HTTP %s", start_ms, end_ms, r.status_code)
            return []
        m = _JSONP_RE.search(r.text)
        if not m:
            return []
        return json.loads(m.group(1))
    except Exception as e:
        log.debug("eve-offline-net %s-%s: %s", start_ms, end_ms, e)
        return []


async def fetch_chribba_archive_fine(
    start: datetime | None = None,
    end: datetime | None = None,
    chunk_days: int = 1,
    concurrency: int = 3,
) -> AsyncIterator[list[dict]]:
    """Async generator yielding chunks of fine-grained rows from Chribba's
    zoom endpoint. Walks `start`→`end` in `chunk_days`-sized windows.
    Default: full archive at 1-day chunks (1-minute resolution post-2010).

    Yields list[dict] per window, each dict shaped for PlayerCountSnapshot
    upsert. Caller is responsible for batching DB inserts.

    `concurrency` caps in-flight requests to be polite (Chribba is a
    community-run site). 3 is a reasonable middle ground between speed and
    courtesy. Lower for very long backfills; default-rate of ~3 req/s
    completes the 23-year archive in ~50 minutes.
    """
    start = start or ARCHIVE_START
    end = end or datetime.utcnow()
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"User-Agent": SCRAPER_UA, "Accept": "application/javascript, */*"},
    ) as client:
        cursor = start
        while cursor < end:
            window_end = min(cursor + timedelta(days=chunk_days), end)
            start_ms = int(cursor.replace(tzinfo=timezone.utc).timestamp() * 1000)
            end_ms = int(window_end.replace(tzinfo=timezone.utc).timestamp() * 1000)

            async with sem:
                pairs = await _fetch_window(client, start_ms, end_ms)

            rows: list[dict] = []
            for entry in pairs:
                if not (isinstance(entry, list) and len(entry) == 2):
                    continue
                ts_ms, pc = entry
                if not isinstance(ts_ms, (int, float)) or not isinstance(pc, (int, float)):
                    continue
                if pc <= 0:
                    continue
                try:
                    recorded_at = datetime.fromtimestamp(
                        ts_ms / 1000.0, tz=timezone.utc
                    ).replace(tzinfo=None)
                except (OverflowError, OSError, ValueError):
                    continue
                rows.append({
                    "recorded_at": recorded_at,
                    "player_count": int(pc),
                    "source": "eve-offline-net",
                    "granularity": "minute",
                })

            if rows:
                yield rows

            cursor = window_end
