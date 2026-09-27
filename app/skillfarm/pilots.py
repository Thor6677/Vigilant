"""Skill-farm settings + pilot CRUD (T-073).

Every write here is scoped to the caller's own user_id -- app/routes/
skill_farm.py never trusts a posted id past what these functions check,
which is the IDOR boundary the ticket calls out as the main risk: adding a
pilot checks the character belongs to the caller, and editing/removing a
pilot row filters by (id, user_id) together so a crafted id from another
account's row is simply not found (404), never acted on.
"""
from __future__ import annotations

import math as _math
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Character, SkillFarmPilot, SkillFarmSettings
from app.skillfarm.constants import DEFAULT_SALES_TAX_PCT, SKILL_FLOOR_SP

TAX_MIN, TAX_MAX = 0.0, 100.0
PLEX_MONTH_MIN, PLEX_MONTH_MAX = 0, 10_000
BASE_SP_MIN, BASE_SP_MAX = 0, 1_000_000_000
VALID_PRICE_SOURCES = ("sell", "buy")


class ValidationError(ValueError):
    """A user-facing input error (out of range, bad price source). Routes
    catch this and re-render the page with a message instead of a 500."""


class NotOwned(ValidationError):
    """The referenced character or pilot row does not belong to the caller
    -- the IDOR boundary itself, not an input-range problem. Kept as its own
    exception so a route can turn THIS into a 404 (per the v1.7.0 convention:
    an ownership check fails closed with 404) while a plain ValidationError
    still renders the page with a message. Subclasses ValidationError so a
    caller that only checks for that still catches it."""


def _check_range(value: float, lo: float, hi: float, label: str) -> None:
    # math.isfinite also rejects NaN/inf -- a bare `value < lo or value > hi`
    # lets `nan` straight through (every comparison with NaN is False), and
    # FastAPI's Form(...) happily parses the literal string "nan" as a float.
    if not _math.isfinite(value) or value < lo or value > hi:
        raise ValidationError(f"{label} must be between {lo:g} and {hi:g}")


# ── Settings ──────────────────────────────────────────────────────────────

async def get_settings(db: AsyncSession, user_id: int) -> SkillFarmSettings:
    """The user's settings row, created on first read with the documented
    defaults so callers never special-case "no row yet"."""
    row = (await db.execute(
        select(SkillFarmSettings).where(SkillFarmSettings.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        row = SkillFarmSettings(
            user_id=user_id,
            sales_tax_pct=DEFAULT_SALES_TAX_PCT,
            plex_per_month=500,
            price_source="sell",
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def save_settings(
    db: AsyncSession, user_id: int, sales_tax_pct: float, plex_per_month: int,
    price_source: str,
) -> SkillFarmSettings:
    _check_range(float(sales_tax_pct), TAX_MIN, TAX_MAX, "Sales tax %")
    _check_range(float(plex_per_month), PLEX_MONTH_MIN, PLEX_MONTH_MAX, "PLEX/month")
    if price_source not in VALID_PRICE_SOURCES:
        raise ValidationError(f"Price source must be one of {VALID_PRICE_SOURCES}")

    row = await get_settings(db, user_id)
    row.sales_tax_pct = float(sales_tax_pct)
    row.plex_per_month = int(plex_per_month)
    row.price_source = price_source
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return row


# ── Pilots ────────────────────────────────────────────────────────────────

async def list_pilots(db: AsyncSession, user_id: int) -> list[SkillFarmPilot]:
    rows = (await db.execute(
        select(SkillFarmPilot)
        .where(SkillFarmPilot.user_id == user_id)
        .order_by(SkillFarmPilot.id)
    )).scalars().all()
    return list(rows)


async def _owns_character(db: AsyncSession, user_id: int, character_id: int) -> bool:
    row = (await db.execute(
        select(Character.character_id).where(
            Character.character_id == character_id, Character.user_id == user_id,
        )
    )).scalar_one_or_none()
    return row is not None


async def eligible_characters(db: AsyncSession, user_id: int) -> list[Character]:
    """The user's own characters not already added as a farm pilot -- feeds
    the "add farm pilot" select."""
    added = {p.character_id for p in await list_pilots(db, user_id)}
    rows = (await db.execute(
        select(Character)
        .where(Character.user_id == user_id)
        .order_by(Character.character_name)
    )).scalars().all()
    return [c for c in rows if c.character_id not in added]


async def add_pilot(
    db: AsyncSession, user_id: int, character_id: int, base_sp: int | None = None,
) -> SkillFarmPilot:
    """Add one of the caller's own characters as a farm pilot.

    Raises NotOwned if the character isn't the caller's own (the IDOR
    check -- also covers a nonexistent character_id, indistinguishably, so a
    crafted id can't be used to probe which ids exist) or ValidationError if
    base_sp is out of range. Adding an already-added character is idempotent
    -- returns the existing row rather than duplicating it (also guards the
    UniqueConstraint race if two submits land at once).
    """
    if not await _owns_character(db, user_id, character_id):
        raise NotOwned("That character does not belong to you")
    base_sp = SKILL_FLOOR_SP if base_sp is None else int(base_sp)
    _check_range(float(base_sp), BASE_SP_MIN, BASE_SP_MAX, "Base SP")

    existing = (await db.execute(
        select(SkillFarmPilot).where(
            SkillFarmPilot.user_id == user_id, SkillFarmPilot.character_id == character_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    pilot = SkillFarmPilot(
        user_id=user_id, character_id=character_id, base_sp=base_sp,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(pilot)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a second concurrent add of the same pilot.
        await db.rollback()
        existing = (await db.execute(
            select(SkillFarmPilot).where(
                SkillFarmPilot.user_id == user_id, SkillFarmPilot.character_id == character_id,
            )
        )).scalar_one_or_none()
        if existing is not None:
            return existing
        raise
    await db.refresh(pilot)
    return pilot


async def update_base_sp(
    db: AsyncSession, user_id: int, pilot_id: int, base_sp: int,
) -> SkillFarmPilot:
    """Update one of the caller's own pilot rows.

    Raises NotOwned (the route maps this to 404) when pilot_id isn't the
    caller's own -- checked BEFORE validating base_sp, so a crafted id never
    gets a different error shape than a genuinely out-of-range value would.
    """
    pilot = (await db.execute(
        select(SkillFarmPilot).where(
            SkillFarmPilot.id == pilot_id, SkillFarmPilot.user_id == user_id,
        )
    )).scalar_one_or_none()
    if pilot is None:
        raise NotOwned("That farm pilot was not found")
    _check_range(float(base_sp), BASE_SP_MIN, BASE_SP_MAX, "Base SP")
    pilot.base_sp = int(base_sp)
    pilot.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return pilot


async def remove_pilot(db: AsyncSession, user_id: int, pilot_id: int) -> bool:
    """Delete one of the caller's own pilot rows. Returns True iff a row was
    removed -- scoped to (id, user_id) together so a crafted id belonging to
    another account is a no-op, not a cross-account delete."""
    res = await db.execute(
        sa_delete(SkillFarmPilot).where(
            SkillFarmPilot.id == pilot_id, SkillFarmPilot.user_id == user_id,
        )
    )
    await db.commit()
    return bool(res.rowcount)
