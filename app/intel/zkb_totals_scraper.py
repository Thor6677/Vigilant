"""zKillboard daily totals scraper.

Fetches the public bulk-history aggregate at
https://zkillboard.com/api/history/totals.json — a single 110 KB JSON file
mapping `YYYYMMDD` → integer kill count for that day. Covers 2007-12-05
through ~yesterday.

This is the cheapest way to get a long-tail "destruction activity" line on
the historical chart. Per-day kill detail (with ISK values) would require
walking r2z2.zkillboard.com/history/<date>.json + ESI-fetching every kill —
~110M ESI calls over the full archive, infeasible.

Single-shot ingest. Idempotent — the (source='zkb-totals', date) unique
constraint dedups across re-runs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from app.db.models import KillmailDailyAggregate

log = logging.getLogger(__name__)

ZKB_TOTALS_URL = "https://zkillboard.com/api/history/totals.json"
SCRAPER_UA = "Vigilant/1.0 backfill (happyfun.fatman@gmail.com)"


async def fetch_zkb_totals() -> list[dict]:
    """Single GET, returns rows shaped for KillmailDailyAggregate upsert.

    Rows: {"date": date, "source": "zkb-totals", "kill_count": int,
           "total_isk_destroyed": None}
    """
    async with httpx.AsyncClient(
        timeout=60.0,
        headers={
            "User-Agent": SCRAPER_UA,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    ) as client:
        r = await client.get(ZKB_TOTALS_URL)
        r.raise_for_status()
        data = r.json()

    rows: list[dict] = []
    for k, v in data.items():
        if not (isinstance(k, str) and len(k) == 8 and k.isdigit()):
            continue
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        try:
            d = datetime.strptime(k, "%Y%m%d").date()
        except ValueError:
            continue
        rows.append({
            "date": d,
            "source": "zkb-totals",
            "kill_count": int(v),
            "total_isk_destroyed": None,
        })

    if rows:
        rows.sort(key=lambda r: r["date"])
        log.info(
            "zkb-totals: parsed %d daily totals (range %s to %s)",
            len(rows), rows[0]["date"], rows[-1]["date"],
        )
    return rows
