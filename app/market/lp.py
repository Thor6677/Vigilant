"""LP store ROI calculator (Phase 4 Task 3).

**Investigation (done up front, per the plan):** LP store offers are NOT in
the imported SDE — the bsd/fsd SDE dumps this app imports don't carry
`invLoyaltyOffers` at all. So offer catalogs and the NPC corp roster itself
go straight to ESI:

  * `/loyalty/stores/{corporation_id}/offers/` — public, one corp's offer
    catalog. Cached 24h per corp, module-dict-keyed idiom identical to
    `app.market.orders.get_orders` (fetch-once, single-flight `asyncio.Lock`
    per key, stale-on-error fallback) — only the TTL differs. Offer catalogs
    move roughly with balance patches, not minute-to-minute like an order
    book, so 24h (matching `app.market.history.HISTORY_TTL`) is plenty fresh
    without hammering ESI on every corp switch.
  * `/corporations/npccorps/` — public, the full ~380 NPC corporation IDs.
    This is static game data that never changes at runtime, so it's cached
    *forever* at module scope (no TTL at all) rather than reusing the 24h
    idiom — resolved to names once via a single batched `/universe/names/`
    POST (ESI allows 1000 ids/call, comfortably covering the whole roster).

**Faction grouping is different**: it comes from the SDE's
`npcCorporations.jsonl` (`sde_npc_corps` table, loaded by
`app.sde.loader`), NOT ESI. An earlier version of this module used ESI's
`GET /corporations/{id}/` `faction_id` field per corp — that field means
FACTIONAL-WARFARE ENLISTMENT, not NPC faction ownership, so it only ever
returned a value for the single corp per empire actively enlisted in FW,
dumping the other ~270+ corps into "Other". See `get_corps_by_faction` /
`_fetch_corp_faction_map_sde` below.

Item prices (both the awarded item's sell value and each required item's
cost) come from the global `/markets/prices/` list
(`app.esi.market.get_market_prices`), the same source Task 1/2's sibling
`app.routes.industry._get_price_map` already leans on. That endpoint is
already 5-min TTL cached by `ESIClient`'s db-backed cache
(`TTL["market_prices"]` in `app/db/cache.py`), so `get_price_map` below adds
no further caching layer — it just reshapes the ~30k-row response into a
`{type_id: price}` lookup dict fresh each call. `average_price` is used with
`adjusted_price` as fallback (average_price is None for very illiquid
types) — same fallback order as `industry._get_price_map`.

**ISK/LP formula** (documented once, here — see `offer_economics()`):

    isk_per_lp = (sell_value - isk_cost - materials_cost) / lp_cost
    sell_value = awarded_item_unit_price * quantity_awarded

Guards, both of which set `isk_per_lp = None` (offer still shown in the
table, just excluded from the ranking and displayed as "—"):

  * `lp_cost <= 0` — can't divide; also not a real LP-store offer.
  * the awarded item is unpriced (not in the global price list — most
    commonly blueprints, which is exactly the case the plan calls out:
    "Blueprint offers: item sell value = the BLUEPRINT's market price if it
    has one; if unpriced, show '—' and exclude from ranking"). No
    manufacture-value math is attempted for unpriced blueprints — out of
    scope for this task, noted in the page footnote.
  * ANY required item is unpriced. We deliberately do NOT treat an unpriced
    required item as "costs 0 ISK" — some LP offers require untradeable
    tags/certificates that could stand in for real value, and silently
    zeroing them would inflate the offer's apparent ROI. Treating the whole
    offer as unpriced errs toward under-ranking instead, which is the safer
    direction for a "what should I buy?" tool.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.sde_models import SDENpcCorp
from app.esi import market as esi_market
from app.esi.client import ESIClient


def _now() -> datetime:
    """Clock seam — monkeypatched in tests to exercise TTL expiry."""
    return datetime.now(timezone.utc)


# ── NPC corp roster (forever cache — static game data) ────────────────────────

_npc_corp_ids: list[int] | None = None
_npc_corp_names: dict[int, str] = {}
_npc_corp_lock = asyncio.Lock()


async def _fetch_npc_corp_ids_esi() -> list[int]:
    """Monkeypatched in tests — the network is never hit there."""
    client = ESIClient("", cache_enabled=False)
    data = await client.get_public("/corporations/npccorps/")
    return data if isinstance(data, list) else []


async def _fetch_names_esi(ids: list[int]) -> dict[int, str]:
    """Monkeypatched in tests. Batches at 1000 ids/call (ESI's own cap on
    `/universe/names/`); a single failed chunk is skipped rather than
    aborting the whole resolution, so a partial roster still renders with
    whatever names came back."""
    if not ids:
        return {}
    client = ESIClient("", cache_enabled=False)
    names: dict[int, str] = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        try:
            result = await client.post_public("/universe/names/", chunk)
        except Exception:
            continue
        if isinstance(result, list):
            for r in result:
                names[r["id"]] = r["name"]
    return names


async def get_npc_corps() -> list[dict]:
    """Return `[{corporation_id, name}, ...]` sorted by name — the full NPC
    corporation roster (LP-store-owning or not; the plan explicitly scopes
    this to "the full npccorps list is fine" rather than pre-filtering to
    only corps that actually run a store — a corp with none just renders an
    empty offers table when selected).

    Cached forever at module scope behind a single-flight lock. A fetch
    failure is NOT cached (so the next call retries) — only a successful
    fetch populates `_npc_corp_ids`, since caching an empty roster from a
    transient ESI hiccup would strand the page with no corps to pick from
    until process restart.
    """
    global _npc_corp_ids, _npc_corp_names
    if _npc_corp_ids is not None:
        return _corps_list()

    async with _npc_corp_lock:
        if _npc_corp_ids is not None:
            return _corps_list()
        try:
            ids = await _fetch_npc_corp_ids_esi()
        except Exception:
            return []
        names = await _fetch_names_esi(ids)
        _npc_corp_ids = ids
        _npc_corp_names = names
    return _corps_list()


def _corps_list() -> list[dict]:
    return sorted(
        (
            {"corporation_id": cid, "name": _npc_corp_names.get(cid, str(cid))}
            for cid in (_npc_corp_ids or [])
        ),
        key=lambda c: c["name"],
    )


# ── Faction grouping (EVE-style pickers Task 3; forever cache like the roster
# it's built on top of) ────────────────────────────────────────────────────────

MAJOR_FACTIONS = (
    "Amarr Empire",
    "Caldari State",
    "Gallente Federation",
    "Minmatar Republic",
)

# corp_id -> faction_id, only for corps that HAVE a faction_id (a corp absent
# from this map falls to "Other"). `None` = not yet built.
_corp_faction_map: dict[int, int] | None = None
_faction_names: dict[int, str] = {}
_corp_faction_lock = asyncio.Lock()


async def _fetch_factions_esi() -> list[dict]:
    """Monkeypatched in tests. `GET /universe/factions/` — public, the full
    faction list `[{faction_id, name, ...}, ...]`."""
    client = ESIClient("", cache_enabled=False)
    data = await client.get_public("/universe/factions/")
    return data if isinstance(data, list) else []


async def _fetch_corp_faction_map_sde(db: AsyncSession) -> dict[int, int] | None:
    """`{corporation_id: faction_id}` from the SDE's `sde_npc_corps` table
    (loaded from `npcCorporations.jsonl` — see
    `app.sde.loader._parse_npc_corp_item`). This is the correct source for
    "which NPC faction owns this corp" — ESI's `GET /corporations/{id}/`
    `faction_id` means factional-warfare enlistment (only ever set for the
    single corp actively enlisted in FW per empire), NOT NPC ownership,
    which is why this module no longer calls it.

    Returns `None` if the table has zero rows at all — a pre-reimport /
    freshly-migrated deploy where the SDE loader hasn't run its
    `npcCorporations.jsonl` pass yet — so the caller can render an explicit
    "pending SDE reimport" degraded state instead of silently (and
    incorrectly) showing every corp under Other with no explanation. A
    populated table where a given corp simply has no `faction_id` (most NPC
    corps don't) is NOT this case — that corp just isn't in the returned
    dict and falls to "Other" normally.
    """
    result = await db.execute(select(SDENpcCorp.corporation_id, SDENpcCorp.faction_id))
    rows = result.all()
    if not rows:
        return None
    return {cid: fid for cid, fid in rows if fid is not None}


def _group_corps_by_faction(
    corps: list[dict],
    faction_map: dict[int, int],
    faction_names: dict[int, str],
) -> list[dict]:
    """`corps` (already name-sorted by `get_npc_corps`) bucketed by faction
    name, majors first alphabetically, then remaining named factions
    alphabetically, "Other" last. Corps within a bucket keep the incoming
    (name-sorted) order."""
    buckets: dict[str, list[dict]] = {}
    for c in corps:
        fid = faction_map.get(c["corporation_id"])
        fname = faction_names.get(fid, "Other") if fid is not None else "Other"
        buckets.setdefault(fname, []).append(c)

    def sort_key(name: str) -> tuple:
        if name in MAJOR_FACTIONS:
            return (0, MAJOR_FACTIONS.index(name))
        if name == "Other":
            return (2, "")
        return (1, name)

    return [
        {"faction_name": name, "corps": buckets[name]}
        for name in sorted(buckets, key=sort_key)
    ]


_SDE_PENDING_NOTE = (
    "NPC corp faction data not loaded yet — showing all corporations under "
    "Other until the next SDE reimport finishes. Reload to retry."
)


async def get_corps_by_faction(db: AsyncSession) -> list[dict]:
    """`[{"faction_name": str, "corps": [{"corporation_id", "name"}, ...]},
    ...]` — the NPC corp roster (`get_npc_corps`) grouped by faction, majors
    first alphabetically, then remaining named factions alphabetically,
    "Other" last.

    corp -> faction comes from the SDE (`_fetch_corp_faction_map_sde`, see
    its docstring for why — ESI's per-corp `faction_id` means FW enlistment,
    not NPC ownership); faction_id -> name still comes from ESI's
    `/universe/factions/` (that field means what it says there, and there's
    no SDE table for it).

    Backed by a module-scope `_corp_faction_map` / `_faction_names` cache
    built once behind a single-flight lock — same fetch-once,
    failure-not-cached discipline as `get_npc_corps` above (read that one
    first). Two independent conditions degrade the roster to a single
    "Other" group (cache left unpopulated either way, so the next call
    retries):

      * `sde_npc_corps` is empty (pending SDE reimport) — `degraded: True`
        with a `note` explaining the pending reimport.
      * the shared `/universe/factions/` fetch raises — `degraded: True`,
        no `note` (template falls back to its generic retry message).
    """
    global _corp_faction_map, _faction_names
    corps = await get_npc_corps()

    if _corp_faction_map is not None:
        return _group_corps_by_faction(corps, _corp_faction_map, _faction_names)

    async with _corp_faction_lock:
        if _corp_faction_map is not None:
            return _group_corps_by_faction(corps, _corp_faction_map, _faction_names)

        sde_map = await _fetch_corp_faction_map_sde(db)
        if sde_map is None:
            return [{
                "faction_name": "Other",
                "corps": corps,
                "degraded": True,
                "note": _SDE_PENDING_NOTE,
            }]

        try:
            factions = await _fetch_factions_esi()
        except Exception:
            return [{"faction_name": "Other", "corps": corps, "degraded": True}]
        names = {
            f["faction_id"]: f["name"]
            for f in factions
            if isinstance(f, dict) and "faction_id" in f and "name" in f
        }
        _corp_faction_map = sde_map
        _faction_names = names
    return _group_corps_by_faction(corps, _corp_faction_map, _faction_names)


# ── Offers cache (24h TTL, one entry per corp) ─────────────────────────────────

OFFERS_TTL = timedelta(hours=24)

_offers_cache: dict[int, tuple[datetime, list[dict]]] = {}
_offers_locks: dict[int, asyncio.Lock] = {}


def _get_offers_lock(corporation_id: int) -> asyncio.Lock:
    lock = _offers_locks.get(corporation_id)
    if lock is None:
        lock = asyncio.Lock()
        _offers_locks[corporation_id] = lock
    return lock


async def _fetch_offers_esi(corporation_id: int) -> list[dict]:
    """Monkeypatched in tests. Many NPC corp IDs run no LP store at all —
    ESI 404s for those, which `get_public` surfaces as a raised exception
    (`raise_for_status`); `get_offers` below treats that identically to any
    other fetch error (stale-on-error, else empty list)."""
    client = ESIClient("", cache_enabled=False)
    data = await client.get_public(f"/loyalty/stores/{corporation_id}/offers/")
    return data if isinstance(data, list) else []


async def get_offers(corporation_id: int) -> list[dict]:
    """Raw ESI offers for one corp, 24h TTL cached. Same fetch-once,
    single-flight-lock, stale-on-error idiom as `app.market.orders.get_orders`
    (see that module's docstring for the full reasoning) — only the TTL
    differs here."""
    cached = _offers_cache.get(corporation_id)
    if cached is not None and (_now() - cached[0]) < OFFERS_TTL:
        return cached[1]

    async with _get_offers_lock(corporation_id):
        cached = _offers_cache.get(corporation_id)
        if cached is not None and (_now() - cached[0]) < OFFERS_TTL:
            return cached[1]
        try:
            data = await _fetch_offers_esi(corporation_id)
        except Exception:
            return cached[1] if cached is not None else []
        _offers_cache[corporation_id] = (_now(), data)
        return data


# ── Prices ──────────────────────────────────────────────────────────────────────

async def get_price_map(db: AsyncSession) -> dict[int, float]:
    """`{type_id: unit price}` from the global `/markets/prices/` list —
    `average_price` with `adjusted_price` as fallback. See module docstring
    for why no extra caching layer is added here (the ESI client's own db
    cache already covers the network hit)."""
    try:
        client = ESIClient("", db=db)
        rows = await esi_market.get_market_prices(client)
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    price_map: dict[int, float] = {}
    for r in rows:
        tid = r.get("type_id")
        if tid is None:
            continue
        price = r.get("average_price")
        if price is None:
            price = r.get("adjusted_price")
        if price is not None:
            price_map[tid] = price
    return price_map


# ── Pure math ────────────────────────────────────────────────────────────────

def offer_economics(offer: dict, price_map: dict[int, float]) -> dict:
    """Pure math, no I/O — see module docstring for the ISK/LP formula and
    its guards. Kept separate from I/O so it's directly unit-testable
    against fixture offers.

    `offer` is one row of ESI's `/loyalty/stores/{corp}/offers/` shape:
      `{offer_id, type_id, quantity, lp_cost, isk_cost,
        required_items: [{type_id, quantity}, ...]}`
    """
    type_id = offer.get("type_id")
    quantity = offer.get("quantity") or 0
    lp_cost = offer.get("lp_cost") or 0
    isk_cost = offer.get("isk_cost") or 0
    required_items = offer.get("required_items") or []

    unit_price = price_map.get(type_id)
    sell_value = unit_price * quantity if unit_price is not None else None

    materials_cost = 0.0
    materials_priced = True
    for ri in required_items:
        rid = ri.get("type_id")
        rqty = ri.get("quantity") or 0
        rprice = price_map.get(rid)
        if rprice is None:
            materials_priced = False
        else:
            materials_cost += rprice * rqty
    if not materials_priced:
        materials_cost = None

    priced = sell_value is not None and materials_priced
    isk_per_lp = None
    if priced and lp_cost > 0:
        isk_per_lp = (sell_value - isk_cost - materials_cost) / lp_cost

    return {
        "offer_id": offer.get("offer_id"),
        "type_id": type_id,
        "quantity": quantity,
        "lp_cost": lp_cost,
        "isk_cost": isk_cost,
        "unit_price": unit_price,
        "materials_cost": materials_cost,
        "sell_value": sell_value,
        "isk_per_lp": isk_per_lp,
        "priced": priced,
    }


def rank_offers(offers: list[dict], price_map: dict[int, float]) -> list[dict]:
    """Compute economics for every offer and sort by ISK/LP descending.

    Unpriced/zero-lp offers (`isk_per_lp is None`) sort AFTER all priced
    offers rather than being dropped from the list — the caller still shows
    them (with "—") so a corp's full catalog is visible, they're just not
    part of the best-first ranking.
    """
    rows = [offer_economics(o, price_map) for o in offers]
    rows.sort(key=lambda r: (r["isk_per_lp"] is None, -(r["isk_per_lp"] or 0)))
    return rows
