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
# A Director passes every ESI corporation role check (in EVE a director holds
# all roles), although the spec's x-required-roles never lists it. Checking
# the listed roles literally told Directors they lacked "Accountant" for data
# they could read (ISS-050).
DIRECTOR = "Director"


def _attr(char, name):
    return char.get(name) if isinstance(char, dict) else getattr(char, name, None)


def pretty_role(role: str) -> str:
    return role.replace("_", " ")


def roles_text(roles: Iterable[str]) -> str:
    """("Accountant", "Junior_Accountant") -> "Accountant, Junior Accountant or
    Director" — Director always qualifies, so it is always named."""
    names = [pretty_role(r) for r in roles]
    if DIRECTOR not in roles:
        names.append(DIRECTOR)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def holds_role(roles: set[str] | None, required: Iterable[str]) -> bool | None:
    """Whether a character's in-game roles satisfy a requirement: True, False,
    or None when its roles are unknown (never synced)."""
    required = set(required)
    if not required:
        return True
    if roles is None:
        return None
    return DIRECTOR in roles or bool(roles & required)


def rank_candidates(chars, required: Iterable[str], roles_by_char: dict[int, set[str]],
                    dead_ids: Iterable[int] = ()) -> list:
    """Order characters for a corporation ESI call, most likely to succeed
    first: holds a qualifying role, then roles unknown, then known to lack it;
    characters whose authorization EVE rejected go last in every tier. Stable,
    so the original order breaks ties. Nobody is dropped — role data can be up
    to an hour stale, and trying costs one request."""
    required = tuple(required)
    dead = set(dead_ids)

    def key(c):
        cid = _attr(c, "character_id")
        held = holds_role(roles_by_char.get(cid), required)
        return (cid in dead, 0 if held is True else 1 if held is None else 2)
    return sorted(chars, key=key)


async def dead_token_ids(db: AsyncSession, character_ids: Iterable[int]) -> set[int]:
    """Characters whose last sync found their authorization rejected."""
    from app.db.models import CharacterDashboardCache
    ids = [int(c) for c in character_ids]
    if not ids:
        return set()
    rows = (await db.execute(select(
        CharacterDashboardCache.character_id, CharacterDashboardCache.sync_warnings_json
    ).where(CharacterDashboardCache.character_id.in_(ids)))).all()
    out = set()
    for cid, raw in rows:
        try:
            if raw and token_failed(json.loads(raw)):
                out.add(cid)
        except (ValueError, TypeError):
            continue
    return out


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
    return perms.GRANTED if holds_role(roles_by_char[cid], perm.in_game_roles) else ROLE_MISSING


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


# What app/routes/dashboard.py:_client_for records when EVE rejects the refresh
# (TokenRevoked: SSO answered 400/401). That is definitive — the authorization
# is gone until the user renews it. Its other warning, "token_refresh_failed:
# <Exception>", is a network or SSO outage and clears on its own, so it must
# not tell anyone to re-authorize.
TOKEN_REVOKED = "token_revoked"


def token_failed(warnings: dict | None) -> bool:
    """True iff the last sync found this character's authorization rejected.

    ISS-048: this used to look for "token_refresh_failed", which the sync
    layer only writes for transient errors, so a genuinely dead token never
    surfaced — characters sat silently stale for months."""
    return any(str(v).startswith(TOKEN_REVOKED) for v in (warnings or {}).values())
