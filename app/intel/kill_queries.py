"""Pure-SQL analytics queries over killmails + killmail_attackers.

Design rules:
- One session per top-level function (safe to call from asyncio.gather).
- Prefer GROUP BY aggregates over Python-side counting.
- Never SELECT whole ORM objects when only a few columns are needed — use
  column-scoped selects so SQLAlchemy doesn't accidentally pull any columns
  we might add later.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, distinct, func, or_, select

from app.db.models import AsyncSessionLocal, Killmail, KillmailAttacker


def _cutoff(days: int | None) -> datetime | None:
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


# ── Per-character: heatmap ──────────────────────────────────────────────────

async def weekly_heatmap(character_id: int, days: int = 90) -> list[dict]:
    """Return 7x24 buckets of kill activity (wins + losses) for a character.
    Output: [{dow: int 0-6, hour: int 0-23, count: int}, ...] — only non-zero
    cells are returned to keep the payload small.
    """
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        attacker_ids = select(KillmailAttacker.killmail_id).where(
            KillmailAttacker.character_id == character_id
        )
        q = select(Killmail.killmail_time).where(
            or_(
                Killmail.victim_character_id == character_id,
                Killmail.killmail_id.in_(attacker_ids),
            )
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        rows = await db.execute(q)

        counts: dict[tuple[int, int], int] = {}
        for (t,) in rows.all():
            if t is None:
                continue
            # datetime.weekday(): Monday=0 ... Sunday=6
            key = (t.weekday(), t.hour)
            counts[key] = counts.get(key, 0) + 1
        return [{"dow": d, "hour": h, "count": c} for (d, h), c in counts.items()]


# ── Per-character: top ships/weapons/systems ────────────────────────────────

async def top_ships_used(character_id: int, days: int = 90, limit: int = 10) -> list[dict]:
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        q = (
            select(KillmailAttacker.ship_type_id, func.count().label("n"))
            .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
            .where(KillmailAttacker.character_id == character_id)
            .where(KillmailAttacker.ship_type_id.is_not(None))
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        q = q.group_by(KillmailAttacker.ship_type_id).order_by(func.count().desc()).limit(limit)
        rows = await db.execute(q)
        return [{"ship_type_id": tid, "count": n} for tid, n in rows.all()]


async def top_weapons_used(character_id: int, days: int = 90, limit: int = 10) -> list[dict]:
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        q = (
            select(KillmailAttacker.weapon_type_id, func.count().label("n"))
            .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
            .where(KillmailAttacker.character_id == character_id)
            .where(KillmailAttacker.weapon_type_id.is_not(None))
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        q = q.group_by(KillmailAttacker.weapon_type_id).order_by(func.count().desc()).limit(limit)
        rows = await db.execute(q)
        return [{"weapon_type_id": tid, "count": n} for tid, n in rows.all()]


async def top_systems(character_id: int, days: int = 90, limit: int = 10) -> list[dict]:
    """Systems where the character had the most kill activity (wins or losses)."""
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        attacker_ids = select(KillmailAttacker.killmail_id).where(
            KillmailAttacker.character_id == character_id
        )
        q = (
            select(Killmail.solar_system_id, func.count().label("n"))
            .where(
                or_(
                    Killmail.victim_character_id == character_id,
                    Killmail.killmail_id.in_(attacker_ids),
                )
            )
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        q = q.group_by(Killmail.solar_system_id).order_by(func.count().desc()).limit(limit)
        rows = await db.execute(q)
        return [{"system_id": sid, "count": n} for sid, n in rows.all()]


# ── Per-character: loss autopsy buckets ─────────────────────────────────────

async def loss_autopsy(character_id: int, days: int = 90) -> dict:
    """Classify the character's recent losses into buckets:
      solo (1 attacker), small_gang (<=5), fleet (>5), npc, smartbomb.
    """
    cutoff = _cutoff(days)
    buckets = {"solo": 0, "small_gang": 0, "fleet": 0, "npc": 0, "smartbomb": 0}
    async with AsyncSessionLocal() as db:
        q = (
            select(
                Killmail.killmail_id,
                Killmail.attacker_count,
                Killmail.is_npc,
            )
            .where(Killmail.victim_character_id == character_id)
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        losses = (await db.execute(q)).all()
        if not losses:
            return buckets
        loss_ids = [row[0] for row in losses]

        # Smartbomb detection via SDE type-name match on attacker weapons
        from app.db.sde_models import SDEType
        smartbomb_q = (
            select(distinct(KillmailAttacker.killmail_id))
            .join(SDEType, SDEType.type_id == KillmailAttacker.weapon_type_id)
            .where(KillmailAttacker.killmail_id.in_(loss_ids))
            .where(SDEType.type_name.ilike("%Smartbomb%"))
        )
        smartbomb_ids = {row[0] for row in (await db.execute(smartbomb_q)).all()}

    for km_id, att_count, is_npc in losses:
        if km_id in smartbomb_ids:
            buckets["smartbomb"] += 1
        elif is_npc:
            buckets["npc"] += 1
        elif (att_count or 1) == 1:
            buckets["solo"] += 1
        elif (att_count or 1) <= 5:
            buckets["small_gang"] += 1
        else:
            buckets["fleet"] += 1
    return buckets


# ── Per-character: summary totals (wins/losses/isk) ─────────────────────────

async def character_summary(character_id: int, days: int | None = 90) -> dict:
    """Kills, losses, ISK destroyed, ISK lost for one character."""
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        # Wins: kills where char was an attacker but not victim
        attacker_ids = select(distinct(KillmailAttacker.killmail_id)).where(
            KillmailAttacker.character_id == character_id
        )
        win_q = select(func.sum(Killmail.total_value), func.count()).where(
            and_(
                Killmail.killmail_id.in_(attacker_ids),
                Killmail.victim_character_id != character_id,
            )
        )
        loss_q = select(func.sum(Killmail.total_value), func.count()).where(
            Killmail.victim_character_id == character_id
        )
        if cutoff is not None:
            win_q = win_q.where(Killmail.killmail_time >= cutoff)
            loss_q = loss_q.where(Killmail.killmail_time >= cutoff)
        win_isk, win_n = (await db.execute(win_q)).one()
        loss_isk, loss_n = (await db.execute(loss_q)).one()

    return {
        "kills": int(win_n or 0),
        "losses": int(loss_n or 0),
        "isk_destroyed": float(win_isk or 0),
        "isk_lost": float(loss_isk or 0),
    }


# ── Cross-character: pulse / wingmen / hunters ──────────────────────────────

async def multi_character_summary(character_ids: list[int], days: int | None = 30) -> dict:
    """Single-query aggregate across N characters. Replaces the old per-char
    asyncio.gather fan-out.
    """
    if not character_ids:
        return {"isk_destroyed": 0.0, "isk_lost": 0.0, "kills": 0, "losses": 0}

    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        attacker_ids = select(distinct(KillmailAttacker.killmail_id)).where(
            KillmailAttacker.character_id.in_(character_ids)
        )
        win_q = select(func.sum(Killmail.total_value), func.count()).where(
            and_(
                Killmail.killmail_id.in_(attacker_ids),
                Killmail.victim_character_id.not_in(character_ids),
            )
        )
        loss_q = select(func.sum(Killmail.total_value), func.count()).where(
            Killmail.victim_character_id.in_(character_ids)
        )
        if cutoff is not None:
            win_q = win_q.where(Killmail.killmail_time >= cutoff)
            loss_q = loss_q.where(Killmail.killmail_time >= cutoff)
        win_isk, win_n = (await db.execute(win_q)).one()
        loss_isk, loss_n = (await db.execute(loss_q)).one()

    return {
        "isk_destroyed": float(win_isk or 0),
        "isk_lost": float(loss_isk or 0),
        "kills": int(win_n or 0),
        "losses": int(loss_n or 0),
    }


async def per_character_summary(character_ids: list[int], days: int | None = 30) -> dict[int, dict]:
    """Single GROUP BY query — returns per-char {kills, losses, isk_destroyed,
    isk_lost}. Replaces per-char asyncio.gather pattern.
    """
    if not character_ids:
        return {}

    cutoff = _cutoff(days)
    out: dict[int, dict] = {cid: {"kills": 0, "losses": 0, "isk_destroyed": 0.0, "isk_lost": 0.0} for cid in character_ids}
    async with AsyncSessionLocal() as db:
        # Wins per-character via attacker table join
        win_q = (
            select(
                KillmailAttacker.character_id,
                func.count(distinct(Killmail.killmail_id)).label("n"),
                func.sum(Killmail.total_value).label("isk"),
            )
            .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
            .where(KillmailAttacker.character_id.in_(character_ids))
            .where(
                or_(
                    Killmail.victim_character_id.is_(None),
                    Killmail.victim_character_id.not_in(character_ids),
                )
            )
            .group_by(KillmailAttacker.character_id)
        )
        if cutoff is not None:
            win_q = win_q.where(Killmail.killmail_time >= cutoff)
        for cid, n, isk in (await db.execute(win_q)).all():
            if cid in out:
                out[cid]["kills"] = int(n or 0)
                out[cid]["isk_destroyed"] = float(isk or 0)

        # Losses per-character via victim column
        loss_q = (
            select(
                Killmail.victim_character_id,
                func.count().label("n"),
                func.sum(Killmail.total_value).label("isk"),
            )
            .where(Killmail.victim_character_id.in_(character_ids))
            .group_by(Killmail.victim_character_id)
        )
        if cutoff is not None:
            loss_q = loss_q.where(Killmail.killmail_time >= cutoff)
        for cid, n, isk in (await db.execute(loss_q)).all():
            if cid in out:
                out[cid]["losses"] = int(n or 0)
                out[cid]["isk_lost"] = float(isk or 0)

    return out


async def frequent_wingmen(character_ids: list[int], days: int = 90, limit: int = 10) -> list[dict]:
    """Characters (not in our set) who attacked alongside us most often."""
    if not character_ids:
        return []
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        our_kms = select(distinct(KillmailAttacker.killmail_id)).where(
            KillmailAttacker.character_id.in_(character_ids)
        )
        q = (
            select(
                KillmailAttacker.character_id,
                func.count(distinct(KillmailAttacker.killmail_id)).label("n"),
            )
            .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
            .where(KillmailAttacker.killmail_id.in_(our_kms))
            .where(KillmailAttacker.character_id.is_not(None))
            .where(KillmailAttacker.character_id.not_in(character_ids))
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        q = q.group_by(KillmailAttacker.character_id).order_by(func.count(distinct(KillmailAttacker.killmail_id)).desc()).limit(limit)
        rows = await db.execute(q)
        return [{"character_id": cid, "count": n} for cid, n in rows.all()]


async def your_hunters(character_ids: list[int], days: int = 90, limit: int = 10) -> list[dict]:
    """Corps / alliances that have killed our characters most often."""
    if not character_ids:
        return []
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        our_losses = select(distinct(Killmail.killmail_id)).where(
            Killmail.victim_character_id.in_(character_ids)
        )
        q = (
            select(
                KillmailAttacker.corporation_id,
                KillmailAttacker.alliance_id,
                func.count(distinct(KillmailAttacker.killmail_id)).label("n"),
            )
            .join(Killmail, Killmail.killmail_id == KillmailAttacker.killmail_id)
            .where(KillmailAttacker.killmail_id.in_(our_losses))
            .where(KillmailAttacker.corporation_id.is_not(None))
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        q = q.group_by(KillmailAttacker.corporation_id, KillmailAttacker.alliance_id).order_by(func.count(distinct(KillmailAttacker.killmail_id)).desc()).limit(limit)
        rows = await db.execute(q)
        return [
            {"type": "corp", "id": corp_id, "alliance_id": ally_id, "count": n}
            for corp_id, ally_id, n in rows.all()
        ]


async def solo_gang_split(character_id: int, days: int = 90) -> dict:
    """Bucket the character's wins by gang size: solo / small (2-5) /
    medium (6-20) / fleet (21+). Ported from the v1 killmails-wip branch."""
    buckets = {"solo": 0, "small": 0, "medium": 0, "fleet": 0}
    cutoff = _cutoff(days)
    async with AsyncSessionLocal() as db:
        q = (
            select(Killmail.killmail_id, Killmail.attacker_count)
            .join(KillmailAttacker, KillmailAttacker.killmail_id == Killmail.killmail_id)
            .where(KillmailAttacker.character_id == character_id)
            .distinct()
        )
        if cutoff is not None:
            q = q.where(Killmail.killmail_time >= cutoff)
        result = await db.execute(q)
        for _kid, count in result.all():
            c = count or 0
            if c <= 1:
                buckets["solo"] += 1
            elif c <= 5:
                buckets["small"] += 1
            elif c <= 20:
                buckets["medium"] += 1
            else:
                buckets["fleet"] += 1
    return buckets


async def streaks(character_id: int) -> dict:
    """Current win streak, longest win streak, days since last loss.
    Ported from the v1 killmails-wip branch."""
    async with AsyncSessionLocal() as db:
        attacker_ids_q = select(KillmailAttacker.killmail_id).where(
            KillmailAttacker.character_id == character_id
        )
        q = (
            select(Killmail.killmail_time, Killmail.victim_character_id)
            .where(or_(
                Killmail.victim_character_id == character_id,
                Killmail.killmail_id.in_(attacker_ids_q),
            ))
            .order_by(Killmail.killmail_time.asc())
        )
        rows = (await db.execute(q)).all()

    current_win = 0
    longest_win = 0
    last_loss_time: datetime | None = None
    for kt, vid in rows:
        is_loss = vid == character_id
        if is_loss:
            longest_win = max(longest_win, current_win)
            current_win = 0
            last_loss_time = kt
        else:
            current_win += 1
    longest_win = max(longest_win, current_win)

    days_since_loss: int | None = None
    if last_loss_time:
        days_since_loss = max(
            0, (datetime.now(timezone.utc).replace(tzinfo=None) - last_loss_time).days
        )

    return {
        "current_win": current_win,
        "longest_win": longest_win,
        "days_since_loss": days_since_loss,
    }
