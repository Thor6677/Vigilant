"""Pure helpers for a character's synced skill levels (T-073).

``skills_json`` on ``CharacterDashboardCache`` is written by the "skills"
sync field (``fetch_skills_data`` / ``_sync_fields`` wiring in
``app/routes/dashboard.py``), never by anything in this module. These two
functions only read that already-cached JSON, so a later dashboard stream
(per-fit "can I fly this" badges) can call them straight from a loaded
Character + CharacterDashboardCache pair with no DB or ESI access of its own.
"""
from __future__ import annotations

import json

from app.auth import scopes as perms


def skill_summary(char, cache) -> dict | str | None:
    """Return the character's parsed skills payload.

    * ``"no_scope"`` — the character has never shared
      ``esi-skills.read_skills.v1``, so there is nothing to show but a
      permission prompt.
    * ``None`` — the scope is present but the field hasn't synced yet (no
      cache row, no ``skills_json``, or corrupt JSON — treated the same as
      "not synced" rather than raising).
    * otherwise — ``{"total_sp": int, "unallocated_sp": int,
      "levels": {"<skill_id>": active_skill_level}}``, exactly the shape
      ``fetch_skills_data`` stores.

    ``char`` is anything with a ``.scopes`` string attribute (``Character``);
    ``cache`` is anything with a ``.skills_json`` attribute
    (``CharacterDashboardCache``), or ``None`` if no cache row exists yet.
    """
    scopes = getattr(char, "scopes", "") or ""
    if perms.SKILLS not in scopes:
        return "no_scope"

    raw = getattr(cache, "skills_json", None) if cache is not None else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def skill_levels(summary) -> dict[int, int]:
    """``{skill_id: active_skill_level}`` from a ``skill_summary()`` result.

    Empty for ``"no_scope"``, ``None``, or a summary with no levels — callers
    (e.g. a can-fly check) can treat "no data" and "nothing trained" the same
    way without special-casing the sentinel values above.
    """
    if not isinstance(summary, dict):
        return {}
    levels = summary.get("levels")
    if not isinstance(levels, dict):
        return {}
    out: dict[int, int] = {}
    for skill_id, level in levels.items():
        try:
            out[int(skill_id)] = int(level)
        except (TypeError, ValueError):
            continue
    return out
