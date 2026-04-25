"""eve-offline.com (Adminor.net) historical PCU scraper.

Recon notes (captured 2026-04-25 via static analysis of the Next.js bundle
chunk `/_next/static/chunks/ff83f648ff5d6788.js`):

- Endpoint: `https://eve-offline.com/api/history`
- Params:
    range:   one of '36h' | '1w' | '2w' | '1m' | '3m' | '6m' | '1y' | 'all'
             (the SPA passes 'all' lowercase; UI label is "All")
    server:  'tranquility' | 'singularity' | 'serenity' | 'buckingham' |
             'multiplicity'
    include: optional, 'newborns' (tranquility only — appends a 3rd column
             with CCP's "newborn" pilot count)
- Response: JSON array of `[unix_ms, players]` or `[unix_ms, players, newborns]`
  triples. ms is at the start of the row's averaging window.
- Granularity is window-dependent (server-side downsampling):
    36h / 1w / 1m → ~1-minute (≈ 50k rows per month)
    6m / 1y / all → ~30-hour resampling (≈ 110k rows for full archive)

To get fine-grained 1-min data across the full archive, walk monthly chunks
of `range=1m` and pin the start date — but the API doesn't accept arbitrary
date ranges; it always returns the trailing N from "now". So fine archival
backfill via this source is impossible right now, only the coarse 'all' is.
For minute-level historical data, use Chribba's eve-offline.net /data/?start=&end=
endpoint (eve_offline_net_scraper.fetch_chribba_archive_fine).

This module covers two cases:
1. `fetch_adminor_archive()` — single GET of `range=all` returning the full
   ~110k-row daily-ish archive going back to 2003-05-28.
2. `fetch_adminor_recent()` — fine-grained recent slices for cross-validation
   against ESI's live samples ('36h' / '1w' / '1m').
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

ADMINOR_BASE = "https://eve-offline.com/api/history"
SCRAPER_UA = "Vigilant/1.0 backfill (happyfun.fatman@gmail.com)"

VALID_RANGES = {"36h", "1w", "2w", "1m", "3m", "6m", "1y", "all"}


async def _fetch_history(rng: str, include_newborns: bool = True) -> list:
    if rng not in VALID_RANGES:
        raise ValueError(f"adminor: range must be one of {sorted(VALID_RANGES)}")
    params = {"range": rng, "server": "tranquility"}
    if include_newborns:
        params["include"] = "newborns"
    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"User-Agent": SCRAPER_UA, "Accept": "application/json"},
    ) as client:
        r = await client.get(ADMINOR_BASE, params=params)
        r.raise_for_status()
        return r.json()


def _normalize(raw: list) -> list[dict]:
    """Convert raw [[ts_ms, players, newborns?], ...] to dicts ready for
    PlayerCountSnapshot upsert. Drops invalid/zero rows."""
    out: list[dict] = []
    for entry in raw:
        if not (isinstance(entry, list) and len(entry) >= 2):
            continue
        ts_ms, pc = entry[0], entry[1]
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
        out.append({
            "recorded_at": recorded_at,
            "player_count": int(pc),
            "source": "eve-offline-com",
            "granularity": "minute",  # API resampled but the underlying data is per-minute
        })
    return out


async def fetch_adminor_archive() -> list[dict]:
    """Single GET of `range=all`. Returns ~110k rows from 2003-05-28 to now.
    The API server-side downsamples for the 'all' window to ~30-hour buckets,
    so this is daily-ish granularity, not minute-level. For fine recent
    data use fetch_adminor_recent() with smaller ranges."""
    raw = await _fetch_history("all", include_newborns=True)
    rows = _normalize(raw)
    log.info(
        "eve-offline-com: archive fetched %d rows (range %s to %s)",
        len(rows),
        rows[0]["recorded_at"] if rows else "—",
        rows[-1]["recorded_at"] if rows else "—",
    )
    return rows


async def fetch_adminor_recent(rng: str = "1m") -> list[dict]:
    """Fine-grained recent slice for cross-validation. `rng` ∈ 36h / 1w / 1m
    return ~1-minute granularity; longer windows are downsampled."""
    if rng not in VALID_RANGES:
        raise ValueError(f"adminor: range must be one of {sorted(VALID_RANGES)}")
    raw = await _fetch_history(rng, include_newborns=True)
    rows = _normalize(raw)
    log.info(
        "eve-offline-com: %s slice fetched %d rows (range %s to %s)",
        rng,
        len(rows),
        rows[0]["recorded_at"] if rows else "—",
        rows[-1]["recorded_at"] if rows else "—",
    )
    return rows
