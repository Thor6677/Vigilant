"""Per-character permission state for pages and notices.

Two different things can stop Vigilant showing corporation data, and the UI
must never blur them:

* a missing **permission** — the user did not let Vigilant read it (declined),
  or authorized before Vigilant asked for it (new). Fixed on the Account page.
* a missing **in-game role** — the permission was granted, but the character
  does not hold the corporation role ESI requires (Accountant, Director, …).
  Fixed in EVE, not in Vigilant.

Roles are read from CharacterCorpRoles (synced hourly from
/characters/{id}/roles/, which needs the ``corp_roles`` permission). Without
that permission the roles are unknown, and the notice says so rather than
guessing.
"""
from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import scopes as perms
from app.db.models import CharacterCorpRoles

ROLE_MISSING = "role_missing"
ROLES_UNKNOWN = "roles_unknown"


def _attr(char, name):
    return char.get(name) if isinstance(char, dict) else getattr(char, name, None)


def pretty_role(role: str) -> str:
    return role.replace("_", " ")


def roles_text(roles: Iterable[str]) -> str:
    """("Accountant", "Junior_Accountant") -> "Accountant or Junior Accountant"."""
    roles = [pretty_role(r) for r in roles]
    return " or ".join(roles)


async def corp_roles_for(db: AsyncSession, character_ids: Iterable[int]) -> dict[int, set[str]]:
    """character_id -> in-game roles, for characters whose roles have synced."""
    ids = [int(c) for c in character_ids]
    if not ids:
        return {}
    rows = (await db.execute(select(CharacterCorpRoles).where(
        CharacterCorpRoles.character_id.in_(ids)))).scalars().all()
    out: dict[int, set[str]] = {}
    for row in rows:
        try:
            out[row.character_id] = set(json.loads(row.roles_json or "[]"))
        except (ValueError, TypeError):
            continue
    return out


def character_state(char, key: str, roles_by_char: dict[int, set[str]] | None = None) -> str:
    """One of perms.GRANTED / DECLINED / NEW / PARTIAL, or ROLE_MISSING /
    ROLES_UNKNOWN for a granted corporation permission that still cannot work."""
    perm = perms.BY_KEY[key]
    state = perms.state_of(perm, _attr(char, "scopes"), _attr(char, "declined_scopes"))
    if state != perms.GRANTED or not perm.in_game_roles or roles_by_char is None:
        return state
    cid = _attr(char, "character_id")
    if cid not in roles_by_char:
        return ROLES_UNKNOWN if not perms.has(char, perms.CORP_ROLES) else perms.GRANTED
    return perms.GRANTED if roles_by_char[cid] & set(perm.in_game_roles) else ROLE_MISSING


def gaps(characters, key: str, roles_by_char: dict[int, set[str]] | None = None) -> dict[str, list]:
    """Characters grouped by why ``key`` does not work for them. Characters for
    which it works are left out; an empty dict means nothing to say."""
    out: dict[str, list] = {}
    for char in characters:
        state = character_state(char, key, roles_by_char)
        if state != perms.GRANTED:
            out.setdefault(state, []).append(char)
    return out


def summary(char) -> dict:
    """Everything the Account page shows about one character's permissions."""
    granted, declined = _attr(char, "scopes"), _attr(char, "declined_scopes")
    states = {p.key: perms.state_of(p, granted, declined) for p in perms.PERMISSIONS}
    return {
        "states": states,
        "granted": [k for k, s in states.items() if s == perms.GRANTED],
        "declined": [k for k, s in states.items() if s == perms.DECLINED],
        "new": [k for k, s in states.items() if s in (perms.NEW, perms.PARTIAL)],
        "extra_scopes": sorted(perms.extra_scopes(granted)),
    }


def token_failed(warnings: dict | None) -> bool:
    """The sync layer records a dead refresh token as a token_refresh_failed
    warning on the field it was syncing (app/routes/dashboard.py:_client_for)."""
    return any("token_refresh_failed" in str(v) for v in (warnings or {}).values())
