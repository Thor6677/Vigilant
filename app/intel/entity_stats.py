"""Killboard-grade combat stats for ANY entity (character / corporation /
alliance) computed from the LOCAL killmail archive.

Design rules (mirrors ``app/intel/kill_queries.py``):
- One session per top-level function (safe under asyncio.gather).
- Aggregate in SQL (GROUP BY / COUNT), never ``.all()``-then-Python-bin.
- Column-scoped selects only.

THE CARDINAL RULE — every query MUST carry a ``killmail_time >=`` lower bound.
The window is mandatory here (no ``days=None`` path): default 90d, options 30d
and 7d. Enforced structurally by ``_cutoff`` always returning a datetime and
every builder appending ``.where(Killmail.killmail_time >= cutoff)``.

Index reality (see report / Task 6 deploy gate): the entity columns
``victim_{character,corporation,alliance}_id`` (Killmail) and
``{character,corporation,alliance}_id`` (KillmailAttacker) are each
single-column indexed, and ``killmail_time`` is separately indexed, but there
is NO composite ``(entity, killmail_time)``. SQLite (no ANALYZE) takes the
entity-equality index, so these run the "entity-indexed path" the plan
blesses — bounded by the entity's own involvement count, not the 60M-row
table. For a very large alliance that is still a full-entity-history read to
count 90 days; a ``(victim_*_id, killmail_time)`` / ``({kind}_id,
killmail_time)`` composite would prune the window. Do NOT add one silently —
EXPLAIN corp/alliance queries on the VPS first.

Semantics:
- losses  = killmails where victim_{kind}_id == entity_id
- kills   = distinct killmails where the entity appears in killmail_attackers
            with matching {kind}_id, EXCLUDING self-victim killmails
            (mirrors character_summary's ``victim != id``). For a corp/alliance
            this drops awox kills from ``kills`` while they still count as a
            ``loss`` — defensible and consistent with the per-char surface.
- solo    = kills where attacker_count == 1 (only attacker was the entity).
- danger  = kills / (kills + losses); 0.0 when the entity has no activity.
- dow convention: ``strftime('%w')`` → 0=Sunday .. 6=Saturday.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, case, distinct, func, or_, select

from app.db.models import AsyncSessionLocal, Killmail, KillmailAttacker

# kind → (Killmail victim column, KillmailAttacker column)
_KIND_COLUMNS = {
    "character": (Killmail.victim_character_id, KillmailAttacker.character_id),
    "corporation": (Killmail.victim_corporation_id, KillmailAttacker.corporation_id),
    "alliance": (Killmail.victim_alliance_id, KillmailAttacker.alliance_id),
}

VALID_KINDS = tuple(_KIND_COLUMNS)  # whitelist for the route

# Windows the UI is allowed to request.
VALID_WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 90


def _cutoff(days: int) -> datetime:
    """Naive-UTC lower bound. Always returns a datetime — the window is never
    optional for entity stats (the CARDINAL RULE)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


def _cols(kind: str):
    try:
        return _KIND_COLUMNS[kind]
    except KeyError:
        raise ValueError(f"invalid entity kind: {kind!r}")


# ── Query builders (pure — no session; unit-testable for window enforcement) ─

def kills_solo_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    """distinct kills + distinct solo kills (attacker_count == 1) for the entity.
    Self-victim killmails excluded."""
    victim_col, attacker_col = _cols(kind)
    return (
        select(
            func.count(distinct(KillmailAttacker.killmail_id)).label("kills"),
            func.count(
                distinct(
                    case(
                        (Killmail.attacker_count == 1, KillmailAttacker.killmail_id),
                        else_=None,
                    )
                )
            ).label("solo"),
        )
        .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
        .where(attacker_col == entity_id)
        .where(or_(victim_col.is_(None), victim_col != entity_id))
        .where(Killmail.killmail_time >= cutoff)
    )


def losses_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    victim_col, _ = _cols(kind)
    return (
        select(func.count().label("losses"))
        .where(victim_col == entity_id)
        .where(Killmail.killmail_time >= cutoff)
    )


def heatmap_kills_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    """(dow, hour, distinct-kill-count) buckets on the attacker side."""
    victim_col, attacker_col = _cols(kind)
    dow = func.strftime("%w", Killmail.killmail_time)
    hour = func.strftime("%H", Killmail.killmail_time)
    return (
        select(dow, hour, func.count(distinct(KillmailAttacker.killmail_id)))
        .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
        .where(attacker_col == entity_id)
        .where(or_(victim_col.is_(None), victim_col != entity_id))
        .where(Killmail.killmail_time >= cutoff)
        .group_by(dow, hour)
    )


def heatmap_losses_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    victim_col, _ = _cols(kind)
    dow = func.strftime("%w", Killmail.killmail_time)
    hour = func.strftime("%H", Killmail.killmail_time)
    return (
        select(dow, hour, func.count())
        .where(victim_col == entity_id)
        .where(Killmail.killmail_time >= cutoff)
        .group_by(dow, hour)
    )


def top_ships_query(kind: str, entity_id: int, cutoff: datetime, limit: int) -> Select:
    """Ships the entity used on kills, ranked by distinct killmails."""
    victim_col, attacker_col = _cols(kind)
    n = func.count(distinct(KillmailAttacker.killmail_id))
    return (
        select(KillmailAttacker.ship_type_id, n.label("n"))
        .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
        .where(attacker_col == entity_id)
        .where(KillmailAttacker.ship_type_id.is_not(None))
        .where(or_(victim_col.is_(None), victim_col != entity_id))
        .where(Killmail.killmail_time >= cutoff)
        .group_by(KillmailAttacker.ship_type_id)
        .order_by(n.desc())
        .limit(limit)
    )


def top_systems_kills_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    victim_col, attacker_col = _cols(kind)
    n = func.count(distinct(KillmailAttacker.killmail_id))
    return (
        select(Killmail.solar_system_id, n.label("n"))
        .join(KillmailAttacker, KillmailAttacker.killmail_id == Killmail.killmail_id)
        .where(attacker_col == entity_id)
        .where(or_(victim_col.is_(None), victim_col != entity_id))
        .where(Killmail.killmail_time >= cutoff)
        .group_by(Killmail.solar_system_id)
    )


def top_systems_losses_query(kind: str, entity_id: int, cutoff: datetime) -> Select:
    victim_col, _ = _cols(kind)
    return (
        select(Killmail.solar_system_id, func.count().label("n"))
        .where(victim_col == entity_id)
        .where(Killmail.killmail_time >= cutoff)
        .group_by(Killmail.solar_system_id)
    )


# ── Public async stat functions ─────────────────────────────────────────────

async def entity_summary(kind: str, entity_id: int, days: int = DEFAULT_WINDOW) -> dict:
    """{kills, losses, solo, solo_ratio, danger} over the window."""
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        kills, solo = (await db.execute(kills_solo_query(kind, entity_id, cutoff))).one()
        (losses,) = (await db.execute(losses_query(kind, entity_id, cutoff))).one()

    kills = int(kills or 0)
    solo = int(solo or 0)
    losses = int(losses or 0)
    total = kills + losses
    return {
        "kills": kills,
        "losses": losses,
        "solo": solo,
        "solo_ratio": (solo / kills) if kills else 0.0,
        "danger": (kills / total) if total else 0.0,
    }


async def entity_heatmap(kind: str, entity_id: int, days: int = DEFAULT_WINDOW) -> list[dict]:
    """Combined kills+losses activity per (dow, hour). dow: 0=Sun..6=Sat.
    Only non-zero cells returned."""
    cutoff = _cutoff(days)
    counts: dict[tuple[int, int], int] = {}
    async with AsyncSessionLocal() as db:
        for row in (await db.execute(heatmap_kills_query(kind, entity_id, cutoff))).all():
            dow, hour, c = row
            key = (int(dow), int(hour))
            counts[key] = counts.get(key, 0) + int(c or 0)
        for row in (await db.execute(heatmap_losses_query(kind, entity_id, cutoff))).all():
            dow, hour, c = row
            key = (int(dow), int(hour))
            counts[key] = counts.get(key, 0) + int(c or 0)
    return [{"dow": d, "hour": h, "count": c} for (d, h), c in counts.items()]


async def entity_top_ships(
    kind: str, entity_id: int, days: int = DEFAULT_WINDOW, limit: int = 5
) -> list[dict]:
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(top_ships_query(kind, entity_id, cutoff, limit))).all()
    return [{"ship_type_id": tid, "count": int(n or 0)} for tid, n in rows]


async def entity_top_systems(
    kind: str, entity_id: int, days: int = DEFAULT_WINDOW, limit: int = 5
) -> list[dict]:
    """Systems with the most entity activity (kills + losses combined)."""
    cutoff = _cutoff(days)
    counts: dict[int, int] = {}
    async with AsyncSessionLocal() as db:
        for sid, n in (await db.execute(top_systems_kills_query(kind, entity_id, cutoff))).all():
            if sid is None:
                continue
            counts[sid] = counts.get(sid, 0) + int(n or 0)
        for sid, n in (await db.execute(top_systems_losses_query(kind, entity_id, cutoff))).all():
            if sid is None:
                continue
            counts[sid] = counts.get(sid, 0) + int(n or 0)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"system_id": sid, "count": c} for sid, c in ranked]
