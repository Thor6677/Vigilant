"""
EVE-Scout Thera / Turnur wormhole connections.

EVE-Scout scans and publishes the public wormhole signatures that connect the
two "roaming hub" systems — Thera (a J-space system) and Turnur (a low-sec
system) — to the rest of New Eden. Those connections act as free routing
shortcuts: a trip that is 23 gate jumps through k-space might be 6 jumps if you
duck through a Thera hole.

This module is the single, tested home for fetching + parsing that feed:

  * ``get_signatures()``   — TTL-cached raw signature list (10-min TTL,
    single-flight via an ``asyncio.Lock``, stale-on-error fallback).
  * ``parse_connections()`` — pure transform from the raw API rows to
    ``(src_system_id, dst_system_id, meta)`` edge tuples suitable for splicing
    into a routing graph.
  * ``get_connections()``  — convenience wrapper (fetch + parse).

No database table — the data is ephemeral (holes collapse within hours) and the
in-process cache is sufficient. The star-map background poller mirrors the raw
list into its own stats cache so ``/api/map/stats`` keeps serving it unchanged;
that endpoint's output shape is intentionally left untouched.

The route-planner UI (React) already consumes the connections client-side via a
"Include Thera / Turnur" toggle and splices the edges into its graphology graph;
this module only owns the server-side fetch/parse/cache.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

EVE_SCOUT_URL = "https://api.eve-scout.com/v2/public/signatures"

# EVE-Scout updates infrequently (holes live for hours); a 10-minute cache is
# plenty fresh and keeps us well clear of any rate limiting.
CACHE_TTL_SECONDS = 600

# CCP / third-party etiquette: identify with a contactable User-Agent. Matches
# the contact-email pattern used by app/intel/player_count.py.
USER_AGENT = "Vigilant/1.0 (happyfun.fatman@gmail.com)"

FETCH_TIMEOUT_SECONDS = 10

# The two anchor systems EVE-Scout scans. Every published signature has one of
# these as its "out" (k-space-facing) endpoint. Verified against the live API
# response 2026-07-04.
THERA_SYSTEM_ID = 31000005
TURNUR_SYSTEM_ID = 30002086
ANCHOR_SYSTEM_IDS = {THERA_SYSTEM_ID: "Thera", TURNUR_SYSTEM_ID: "Turnur"}


@dataclass(frozen=True)
class TheraConnection:
    """One Thera/Turnur wormhole connection as an undirected routing edge.

    ``src``/``dst`` are solar-system IDs. ``via`` names the anchor hub the edge
    routes through ("Thera" or "Turnur") so route legs can be labelled.
    """
    src: int
    dst: int
    via: str
    wh_type: str
    life_hours: int | None
    signature: str


# ── Module-level cache ────────────────────────────────────────────────────────
_cache_data: list[dict] | None = None
_cache_fetched_at: datetime | None = None
_lock = asyncio.Lock()


def _now() -> datetime:
    """Indirection so tests can monkeypatch the clock."""
    return datetime.now(timezone.utc)


async def _fetch_raw() -> list[dict]:
    """Fetch the raw signature list from EVE-Scout. Raises on transport error
    or non-200 so the caller can fall back to stale cache. Isolated in its own
    function so tests can monkeypatch it without hitting the network."""
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(EVE_SCOUT_URL)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"EVE-Scout returned {type(data).__name__}, expected list")
    return data


def last_updated() -> datetime | None:
    """Timestamp of the last successful fetch, or None if never fetched."""
    return _cache_fetched_at


async def get_signatures(*, force: bool = False) -> list[dict]:
    """Return the raw EVE-Scout signature list, TTL-cached.

    * Serves the in-memory cache while it is younger than ``CACHE_TTL_SECONDS``.
    * Refreshes under a single-flight ``asyncio.Lock`` so concurrent callers
      collapse onto one HTTP request.
    * On a fetch error, logs a warning and returns the last-known-good cache
      (stale-on-error). Returns ``[]`` only if there is no cache at all.
    """
    global _cache_data, _cache_fetched_at

    if not force and _cache_data is not None and _cache_fetched_at is not None:
        if (_now() - _cache_fetched_at).total_seconds() < CACHE_TTL_SECONDS:
            return _cache_data

    async with _lock:
        # Re-check after acquiring the lock — another waiter may have refreshed.
        if not force and _cache_data is not None and _cache_fetched_at is not None:
            if (_now() - _cache_fetched_at).total_seconds() < CACHE_TTL_SECONDS:
                return _cache_data
        try:
            data = await _fetch_raw()
        except Exception as e:
            log.warning("EVE-Scout fetch failed: %s", e)
            # Stale-on-error: keep serving whatever we last had.
            return _cache_data if _cache_data is not None else []
        _cache_data = data
        _cache_fetched_at = _now()
        log.debug("EVE-Scout refreshed: %d signatures", len(data))
        return _cache_data


def parse_connections(signatures: list[dict]) -> list[TheraConnection]:
    """Transform raw EVE-Scout rows into undirected routing edges.

    Keeps only fully-scanned wormhole signatures that have both endpoints and
    whose "out" side is a known anchor (Thera/Turnur). Non-wormhole rows,
    incomplete scans, and rows missing a system id are dropped.
    """
    out: list[TheraConnection] = []
    seen: set[tuple[int, int]] = set()
    for sig in signatures or []:
        if sig.get("signature_type") != "wormhole":
            continue
        src = sig.get("out_system_id")
        dst = sig.get("in_system_id")
        if not isinstance(src, int) or not isinstance(dst, int):
            continue
        if src == dst:
            continue
        via = ANCHOR_SYSTEM_IDS.get(src)
        if via is None:
            # Only edges anchored at Thera/Turnur are useful shortcuts.
            continue
        # Dedupe on the undirected pair — EVE-Scout can list the same hole twice.
        pair = (min(src, dst), max(src, dst))
        if pair in seen:
            continue
        seen.add(pair)
        life = sig.get("remaining_hours")
        out.append(TheraConnection(
            src=src,
            dst=dst,
            via=via,
            wh_type=sig.get("wh_type") or "",
            life_hours=life if isinstance(life, int) else None,
            signature=sig.get("out_signature") or "",
        ))
    return out


async def get_connections(*, force: bool = False) -> list[TheraConnection]:
    """Fetch (TTL-cached) and parse EVE-Scout connections in one call."""
    return parse_connections(await get_signatures(force=force))
