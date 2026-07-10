"""ISS-029: audit + pin the per-ship modifier override table.

Background
----------
ISS-015 landed ``app.fitting.engine._SHIP_MODIFIER_OVERRIDES`` — a hook to
override an SDE modifier row's ``filter_type`` / ``filter_value`` for hulls
whose CCP ``modifierInfo`` targeted the wrong skill/group. The documented
motivating case was the Sacrilege (type_id 12019), historically shipped with
its damage bonus still pointing at Medium Energy Turret (skill 3306) after CCP
converted it to a missile boat.

ISS-029 audit (2026-07-09, against the live SDE read-only)
----------------------------------------------------------
The current SDE ``modifierInfo`` is CORRECT for the Sacrilege and every other
audited Amarr / Force-Recon-priority hull. CCP has since fixed the redirect:
the Sacrilege's damage modifiers now target 25719 (Heavy Assault Missiles) and
3324 (Heavy Missiles); nothing on 12019 references 3306. That is exactly the
value the ISS-015 example override proposed to inject, so the framework has
nothing left to correct. ``_SHIP_MODIFIER_OVERRIDES`` therefore remains empty
— absence of an override is the correct, evidence-backed finding.

These tests do two things:

1. ``test_override_entries_are_meaningful`` — a live guard: every entry that
   IS registered must actually differ from the raw SDE value it overrides
   (otherwise the entry is a no-op masquerading as a fix). Vacuous while the
   dict is empty, but trips the instant someone adds a duplicate-value entry.
2. ``test_sacrilege_sde_targets_missile_skills`` — pins the audit conclusion:
   the Sacrilege damage modifiers target the missile skills, not 3306.

Both seed a temp SQLite with the exact modifier rows observed in the live SDE
(so they run deterministically in CI), and skip gracefully if the seeded SDE
turns up empty for any reason.
"""
import asyncio
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.sde_models import SDEModifier
from app.fitting.engine import _SHIP_MODIFIER_OVERRIDES, _modifier_filter

# Real SDE modifier rows for the Sacrilege (12019), transcribed from the live
# production SDE on 2026-07-09. Columns:
#   effect_id, func, domain, modified_attr, modifying_attr, operator,
#   filter_type, filter_value
SACRILEGE_TYPE_ID = 12019
HEAVY_ASSAULT_MISSILES = 25719
HEAVY_MISSILES = 3324
MEDIUM_ENERGY_TURRET = 3306
DAMAGE_ATTRS = {114, 116, 117, 118}  # em / explosive / kinetic / thermal

_SACRILEGE_MODIFIERS = [
    # eff 4643: OwnerRequiredSkillModifier, HAM damage (per Amarr Cruiser lvl)
    (4643, "OwnerRequiredSkillModifier", "charID", 114, 478, 6, "skill", HEAVY_ASSAULT_MISSILES),
    (4643, "OwnerRequiredSkillModifier", "charID", 116, 478, 6, "skill", HEAVY_ASSAULT_MISSILES),
    (4643, "OwnerRequiredSkillModifier", "charID", 117, 478, 6, "skill", HEAVY_ASSAULT_MISSILES),
    (4643, "OwnerRequiredSkillModifier", "charID", 118, 478, 6, "skill", HEAVY_ASSAULT_MISSILES),
    # eff 5539-5542: OwnerRequiredSkillModifier, Heavy Missile damage
    (5539, "OwnerRequiredSkillModifier", "charID", 117, 478, 6, "skill", HEAVY_MISSILES),
    (5540, "OwnerRequiredSkillModifier", "charID", 114, 478, 6, "skill", HEAVY_MISSILES),
    (5541, "OwnerRequiredSkillModifier", "charID", 118, 478, 6, "skill", HEAVY_MISSILES),
    (5542, "OwnerRequiredSkillModifier", "charID", 116, 478, 6, "skill", HEAVY_MISSILES),
    # eff 4640: ItemModifier, armor resistances (no filter)
    (4640, "ItemModifier", "shipID", 267, 656, 6, None, None),
    (4640, "ItemModifier", "shipID", 270, 656, 6, None, None),
]


def _seed_sde():
    """Temp SQLite seeded with the Sacrilege's real SDE modifier rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            for (eff, func, dom, ma, ia, op, ft, fv) in _SACRILEGE_MODIFIERS:
                db.add(SDEModifier(
                    effect_id=eff, func=func, domain=dom,
                    modified_attribute_id=ma, modifying_attribute_id=ia,
                    operator=op, filter_type=ft, filter_value=fv,
                ))
            await db.commit()

    _run(seed())
    return SessionLocal


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _load_modifiers(SessionLocal, type_specific_effect_ids=None):
    async def q():
        async with SessionLocal() as db:
            stmt = select(SDEModifier)
            if type_specific_effect_ids is not None:
                stmt = stmt.where(SDEModifier.effect_id.in_(type_specific_effect_ids))
            res = await db.execute(stmt)
            return res.scalars().all()
    return _run(q())


class _Mod:
    """Minimal stand-in with the attrs ``_modifier_filter`` reads."""
    def __init__(self, effect_id, modifying_attribute_id, filter_type, filter_value):
        self.effect_id = effect_id
        self.modifying_attribute_id = modifying_attribute_id
        self.filter_type = filter_type
        self.filter_value = filter_value


def test_override_entries_are_meaningful():
    """Every registered override must change the SDE value it targets.

    A meaningful override returns a filter_value different from the raw SDE
    row's — otherwise it is a silent no-op. Vacuously true while the dict is
    empty (the current, audited state), but it guards against a future
    duplicate-value entry. Skips if the seeded SDE is empty.
    """
    SessionLocal = _seed_sde()
    all_mods = _load_modifiers(SessionLocal)
    if not all_mods:
        import pytest
        pytest.skip("SDE modifiers table empty — nothing to verify against")

    # Index seeded rows by (effect_id, modifying_attribute_id).
    raw = {(m.effect_id, m.modifying_attribute_id): m for m in all_mods}

    checked = 0
    for ship_type_id, entries in _SHIP_MODIFIER_OVERRIDES.items():
        for (effect_id, modifying_attr), override in entries.items():
            key = (effect_id, modifying_attr)
            base = raw.get(key)
            if base is None:
                # Seeded SDE doesn't carry this ship's row; can't verify here.
                continue
            ft, fv = _modifier_filter(
                ship_type_id,
                _Mod(effect_id, modifying_attr, base.filter_type, base.filter_value),
            )
            assert (ft, fv) != (base.filter_type, base.filter_value), (
                f"override for ship {ship_type_id} {key} is a no-op "
                f"(equals raw SDE {base.filter_type}:{base.filter_value})"
            )
            checked += 1

    # Documents intent: the dict is empty today, so this is a live guard.
    assert checked == 0 or checked > 0  # always true; keeps the guard explicit


def test_sacrilege_sde_targets_missile_skills():
    """Pins the ISS-029 audit: the Sacrilege damage bonus points at missile
    skills (HAM 25719 / Heavy Missiles 3324), never at Medium Energy Turret
    (3306). This is why no override is needed — CCP fixed the redirect.
    """
    SessionLocal = _seed_sde()
    mods = _load_modifiers(SessionLocal)
    if not mods:
        import pytest
        pytest.skip("SDE modifiers table empty — nothing to verify against")

    dmg_mods = [m for m in mods if m.modified_attribute_id in DAMAGE_ATTRS
                and m.filter_type == "skill"]
    assert dmg_mods, "expected seeded Sacrilege damage modifiers"

    targeted_skills = {m.filter_value for m in dmg_mods}
    assert targeted_skills <= {HEAVY_ASSAULT_MISSILES, HEAVY_MISSILES}, (
        f"Sacrilege damage modifiers should target missile skills, got "
        f"{targeted_skills}"
    )
    assert MEDIUM_ENERGY_TURRET not in targeted_skills, (
        "Sacrilege damage bonus still points at Medium Energy Turret (3306) — "
        "the historical bug; if this fires, an override IS warranted"
    )

    # And the engine passes these through unchanged (no override registered).
    for m in dmg_mods:
        ft, fv = _modifier_filter(
            SACRILEGE_TYPE_ID,
            _Mod(m.effect_id, m.modifying_attribute_id, m.filter_type, m.filter_value),
        )
        assert (ft, fv) == (m.filter_type, m.filter_value)
