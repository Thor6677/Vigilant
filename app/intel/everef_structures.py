"""EVE Ref public-structure snapshots -> structure_first_seen (T-090, PR 1).

Write side only. Each snapshot (data.everef.net/structures/history/<year>/
<date>/*.v2.json.bz2, or structures-latest.v2.json) is a JSON object keyed by
structure id. We keep four things per id: first_seen, type_id and the last
time it was listed, and the MINIMUM first_seen over every file (upstream drops
structures unseen for 30 days, so a later file can only lower it).

first_seen is an upper bound on when a structure was anchored. Nothing here
writes structure_age_calibration: rows in that table are shown as "exact".

Failure rules: any HTTP, bz2, JSON or schema problem skips that one file and
leaves nothing behind; the next run retries it. Each file is parsed outside
the database, then written in ONE transaction together with its import record,
so an import is all or nothing. Only one file is held in memory at a time and
requests are sequential with a pause between them.
"""
from __future__ import annotations

import asyncio
import bz2
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median

import httpx
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import user_agent
from app.db import models
from app.db.models import StructureFirstSeen, StructureFirstSeenImport

log = logging.getLogger(__name__)

HISTORY_BASE = "https://data.everef.net/structures/history"
LATEST_URL = "https://data.everef.net/structures/structures-latest.v2.json"
ALLOWED_PREFIX = "https://data.everef.net/structures/"
REQUEST_GAP_SECONDS = 2.0
LOOKBACK_DAYS = 4                    # day directories tried, newest first
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
EARLIEST_FIRST_SEEN = datetime(2016, 4, 26)      # Citadel release; 1970 is a placeholder
CHUNK = 400                          # rows per INSERT (5 columns, well under SQLite's variable cap)
CENSOR_MIN_COUNT = 50
CENSOR_FACTOR = 5

_V2_NAME = re.compile(r"^structures-[0-9T_:\-]+\.v2\.json(\.bz2)?$")

_running = False


def is_running() -> bool:
    return _running


class SnapshotError(Exception):
    """A file that can't be used; skipped and retried on the next run."""


@dataclass(frozen=True)
class Row:
    structure_id: int
    first_seen: datetime
    last_listed: datetime | None
    type_id: int | None


def _naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_time(v) -> datetime | None:
    """A snapshot timestamp as naive UTC, or None when missing/unparseable."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v, tz=timezone.utc)
        elif isinstance(v, str) and v.strip():
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _unpack(raw: bytes) -> bytes:
    if not raw.startswith(b"BZh"):
        return raw
    d = bz2.BZ2Decompressor()
    try:
        out = d.decompress(raw, MAX_UNPACKED_BYTES + 1)
    except (OSError, ValueError, EOFError) as e:
        raise SnapshotError(f"bad bz2: {e}") from e
    if len(out) > MAX_UNPACKED_BYTES or not d.eof:
        raise SnapshotError("bz2 payload truncated or too large")
    return out


def parse_snapshot(raw: bytes, *, now: datetime | None = None) -> list[Row]:
    """Rows from a v2 snapshot (bz2 or plain JSON). Ids with a missing,
    unparseable, pre-Citadel (1970 placeholder) or future first_seen are
    dropped. Raises SnapshotError when the file is not a snapshot at all."""
    now = now or _naive_utc()
    try:
        data = json.loads(_unpack(raw))
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise SnapshotError(f"bad JSON: {e}") from e
    if not isinstance(data, dict):
        raise SnapshotError("snapshot is not an object keyed by structure id")
    limit = now + timedelta(days=1)
    rows: list[Row] = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        try:
            sid = int(val.get("structure_id", key))
        except (TypeError, ValueError):
            continue
        if sid <= 0 or sid > 2**63 - 1:
            continue
        first = _parse_time(val.get("first_seen"))
        if first is None or first < EARLIEST_FIRST_SEEN or first > limit:
            continue
        listed = [t for t in (_parse_time(val.get("last_seen_public_structure")),
                              _parse_time(val.get("last_structure_get"))) if t]
        tid = val.get("type_id")
        rows.append(Row(sid, first, max(listed) if listed else None,
                        tid if isinstance(tid, int) and not isinstance(tid, bool) else None))
    return rows


def censored_dates(counts: dict[date, int]) -> set[date]:
    """Dates shared by far more ids than a normal day: more than
    max(CENSOR_MIN_COUNT, CENSOR_FACTOR x the median daily count). Those are
    upstream sweeps or resets, not creation days."""
    if not counts:
        return set()
    limit = max(CENSOR_MIN_COUNT, CENSOR_FACTOR * median(counts.values()))
    return {d for d, n in counts.items() if n > limit}


async def refresh_censored(db) -> int:
    """Recompute structure_first_seen.censored from the whole table. Runs
    inside the importing transaction. Returns the number of censored dates."""
    day = func.date(StructureFirstSeen.first_seen)
    rows = (await db.execute(select(day, func.count()).group_by(day))).all()
    counts = {date.fromisoformat(d): n for d, n in rows if d}
    bad = {d.isoformat() for d in censored_dates(counts)}
    await db.execute(update(StructureFirstSeen).values(censored=day.in_(bad)) if bad
                     else update(StructureFirstSeen).values(censored=False))
    return len(bad)


async def import_rows(db, name: str, rows: list[Row], *, etag: str | None, size: int | None) -> None:
    """Upsert one file's rows and its import record into `db`; the caller
    commits. Keeps the MINIMUM first_seen and the newest last_listed. Every
    row carries `source` explicitly (the file name)."""
    t = StructureFirstSeen.__table__
    for i in range(0, len(rows), CHUNK):
        ins = sqlite_insert(t).values([
            {"structure_id": r.structure_id, "first_seen": r.first_seen, "censored": False,
             "source": name, "last_listed": r.last_listed, "type_id": r.type_id}
            for r in rows[i:i + CHUNK]])
        ex = ins.excluded
        older = ex.first_seen < t.c.first_seen
        await db.execute(ins.on_conflict_do_update(
            index_elements=["structure_id"],
            set_={
                "first_seen": case((older, ex.first_seen), else_=t.c.first_seen),
                "source": case((older, ex.source), else_=t.c.source),
                "last_listed": case(
                    (ex.last_listed.is_(None), t.c.last_listed),
                    (t.c.last_listed.is_(None), ex.last_listed),
                    (ex.last_listed > t.c.last_listed, ex.last_listed),
                    else_=t.c.last_listed),
                "type_id": func.coalesce(ex.type_id, t.c.type_id),
            }))
    await refresh_censored(db)
    imp = sqlite_insert(StructureFirstSeenImport.__table__).values(
        file_name=name, imported_at=_naive_utc(), rows=len(rows), etag=etag, size=size)
    await db.execute(imp.on_conflict_do_update(
        index_elements=["file_name"],
        set_={"imported_at": imp.excluded.imported_at, "rows": imp.excluded.rows,
              "etag": imp.excluded.etag, "size": imp.excluded.size}))


async def already_imported(name: str, etag: str | None) -> bool:
    async with models.AsyncSessionLocal() as db:
        rec = await db.get(StructureFirstSeenImport, name)
    return rec is not None and (etag is None or rec.etag == etag)


async def ingest_bytes(name: str, raw: bytes, *, etag: str | None = None) -> str:
    """Parse and store one file in a single transaction. 'imported' | 'skipped'
    (already recorded) | 'failed' (nothing written)."""
    if await already_imported(name, etag):
        return "skipped"
    try:
        rows = parse_snapshot(raw)
    except SnapshotError as e:
        log.warning("everef structures: %s unusable: %s", name, e)
        return "failed"
    try:
        async with models.AsyncSessionLocal() as db:
            await import_rows(db, name, rows, etag=etag, size=len(raw))
            await db.commit()
    except Exception as e:                       # rolled back by the context exit
        log.warning("everef structures: writing %s failed: %s", name, e)
        return "failed"
    log.info("everef structures: %s imported (%d rows)", name, len(rows))
    return "imported"


# ── HTTP ──────────────────────────────────────────────────────────────────

def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=60, follow_redirects=False,
                             headers={"User-Agent": user_agent("backfill")})


async def _get(client: httpx.AsyncClient, url: str, cap: int) -> bytes:
    """GET url, refusing anything but 200 or more than `cap` bytes."""
    if not url.startswith(ALLOWED_PREFIX):
        raise SnapshotError(f"refusing {url}")
    async with client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise SnapshotError(f"HTTP {resp.status_code} for {url}")
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > cap:
                raise SnapshotError(f"{url} larger than {cap} bytes")
        return bytes(buf)


def _entry_etag(entry: dict) -> str | None:
    parts = [f"{k}={entry[k]}" for k in ("sha256", "etag", "mtime", "size")
             if entry.get(k) is not None]
    return "|".join(parts)[:200] or None


def parse_index(raw: bytes) -> dict[str, str | None]:
    """{file name: etag} for the v2 snapshots a day's index.json lists. v1
    files (id lists) and anything else are ignored. Tolerant of a list or an
    object with a 'files' list, of string or object entries."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise SnapshotError(f"bad index.json: {e}") from e
    items = data.get("files") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SnapshotError("index.json has no file list")
    out: dict[str, str | None] = {}
    for it in items:
        entry = it if isinstance(it, dict) else {"name": it}
        name = entry.get("name") or entry.get("path")
        if not isinstance(name, str):
            continue
        name = name.rsplit("/", 1)[-1]
        if _V2_NAME.match(name):
            out[name] = _entry_etag(entry)
    return out


async def run_daily_ingest(*, transport: httpx.AsyncBaseTransport | None = None,
                           today: date | None = None, gap: float = REQUEST_GAP_SECONDS) -> dict:
    """Fetch the newest v2 snapshot (searching the last LOOKBACK_DAYS day
    directories), else structures-latest.v2.json, and import it unless it is
    already recorded. Never raises."""
    global _running
    if _running:
        return {"status": "already_running"}
    _running = True
    try:
        today = today or datetime.now(timezone.utc).date()
        async with _client(transport) as client:
            first = True

            async def fetch(url: str, cap: int) -> bytes:
                nonlocal first
                if not first:
                    await asyncio.sleep(gap)
                first = False
                return await _get(client, url, cap)

            for back in range(LOOKBACK_DAYS):
                day = today - timedelta(days=back)
                base = f"{HISTORY_BASE}/{day.year}/{day.isoformat()}"
                try:
                    files = parse_index(await fetch(f"{base}/index.json", 8 * 1024 * 1024))
                except SnapshotError as e:
                    log.info("everef structures: no index for %s (%s)", day, e)
                    continue
                except httpx.HTTPError as e:
                    log.warning("everef structures: index fetch failed: %s", e)
                    return {"status": "failed"}
                if not files:
                    continue
                name = max(files)
                if await already_imported(name, files[name]):
                    return {"status": "up_to_date", "file": name}
                try:
                    raw = await fetch(f"{base}/{name}", MAX_DOWNLOAD_BYTES)
                except (SnapshotError, httpx.HTTPError) as e:
                    log.warning("everef structures: %s fetch failed: %s", name, e)
                    return {"status": "failed", "file": name}
                return {"status": await ingest_bytes(name, raw, etag=files[name]), "file": name}
            # No usable day directory: the rolling latest file.
            try:
                raw = await fetch(LATEST_URL, MAX_DOWNLOAD_BYTES)
            except (SnapshotError, httpx.HTTPError) as e:
                log.warning("everef structures: latest fetch failed: %s", e)
                return {"status": "failed", "file": "structures-latest.v2.json"}
            import hashlib
            return {"status": await ingest_bytes(
                "structures-latest.v2.json", raw,
                etag="sha256=" + hashlib.sha256(raw).hexdigest()),
                "file": "structures-latest.v2.json"}
    except Exception as e:
        log.warning("everef structures: daily ingest error: %s", e)
        return {"status": "failed"}
    finally:
        _running = False


async def backfill(file_urls: list[str], *, transport: httpx.AsyncBaseTransport | None = None,
                   gap: float = REQUEST_GAP_SECONDS) -> list[dict]:
    """One-off: import the given snapshot URLs (https://data.everef.net/
    structures/...), oldest first by name, one at a time with a pause between
    requests. Files already recorded are skipped without a download; v1 files
    and foreign hosts are refused. For the operator to run on dev, e.g.
    asyncio.run(backfill([...]))."""
    global _running
    if _running:
        return [{"status": "already_running"}]
    _running = True
    results: list[dict] = []
    try:
        async with _client(transport) as client:
            first = True
            for url in sorted(set(file_urls), key=lambda u: u.rsplit("/", 1)[-1]):
                name = url.rsplit("/", 1)[-1]
                if not url.startswith(ALLOWED_PREFIX) or not _V2_NAME.match(name):
                    results.append({"file": name, "status": "refused"})
                    continue
                if await already_imported(name, None):
                    results.append({"file": name, "status": "skipped"})
                    continue
                if not first:
                    await asyncio.sleep(gap)
                first = False
                try:
                    raw = await _get(client, url, MAX_DOWNLOAD_BYTES)
                except (SnapshotError, httpx.HTTPError) as e:
                    log.warning("everef structures backfill: %s: %s", name, e)
                    results.append({"file": name, "status": "failed"})
                    continue
                results.append({"file": name, "status": await ingest_bytes(name, raw)})
                del raw
    finally:
        _running = False
    return results
