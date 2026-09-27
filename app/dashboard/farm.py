"""T-076: the Dashboard's "Skill farm · N injectors ready" link and each farm
pilot's Detailed-mode row.

Deliberately thin: it calls `app.skillfarm.rows.build_pilot_row` (with every
price set to None, since this only needs `ready_now`/`status`, never a
priced ISK figure) rather than reimplementing the SP math, so a parallel fix
to that module's unallocated-SP handling (T-076 was built alongside one)
flows through automatically. Nothing here edits app.skillfarm.*.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.character_skills import skill_summary
from app.db.models import Character
from app.skillfarm import pilots as farm_pilots
from app.skillfarm import rows as farm_rows


async def load_farm_summary(
    db: AsyncSession, user_id: int, characters: list[Character], char_caches: dict,
) -> dict | None:
    """None when the user has no farm pilots at all (hides the link and
    every per-pilot Farm row). Else:
        {"total_pilots": int, "ready_now": int,
         "by_character": {character_id: {"injectors_ready": int}}}
    """
    farm_rows_db = await farm_pilots.list_pilots(db, user_id)
    if not farm_rows_db:
        return None

    char_by_id = {c.character_id: c for c in characters}
    by_character: dict[int, dict] = {}
    ready_total = 0
    for p in farm_rows_db:
        char = char_by_id.get(p.character_id)
        cache = char_caches.get(p.character_id)
        summary = skill_summary(char, cache) if char is not None else "no_scope"
        row = farm_rows.build_pilot_row(
            pilot_id=p.id,
            character_id=p.character_id,
            character_name=char.character_name if char is not None else "",
            base_sp=p.base_sp,
            summary=summary,
            skillqueue=None,
            lsi_price=None,
            extractor_price=None,
            plex_price=None,
            sales_tax_pct=0.0,
            plex_per_month=0,
        )
        ready = row.get("ready_now", 0) if row.get("status") == "ok" else 0
        by_character[p.character_id] = {"injectors_ready": ready}
        ready_total += ready

    return {
        "total_pilots": len(farm_rows_db),
        "ready_now": ready_total,
        "by_character": by_character,
    }
