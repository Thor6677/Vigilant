"""Permission helpers for scoped SkillPlans (personal/corp/alliance/custom).

Resolves a user's on-chain identity (characters → corps → alliances → corp roles)
from cached tables and answers three questions about a plan:

    can_view(plan, identities)   — "Can this user see the plan at all?"
    can_edit(plan, identities)   — "Can they add/remove skills, rename, etc?"
    can_admin(plan, identities)  — "Can they change ACLs or delete?"

The identity resolver is cheap (two indexed queries: Characters + CharacterCorpRoles)
so a single call per request is fine even when checking many plans.

Role semantics:
- Corp edit: Communications_Officer, Director, or CEO in the owner corp.
- Alliance edit (option B): Director or CEO in ANY corp the user has a
  character in, where that corp is a member of the owner alliance.
- Custom edit: ACL entry with permission 'edit' or 'admin' matching the user's
  character, corp, or alliance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Character, CharacterCorpRoles, SkillPlan, SkillPlanACL,
)


# Roles that grant "I can create/edit this scope's plans" — matches EVE's in-game
# permission to create corp bookmarks (Communications_Officer / Director / CEO).
CORP_EDIT_ROLES: set[str] = {"Communications_Officer", "Director", "CEO"}

# Alliance edit (option B): Director/CEO in any alliance corp.
ALLIANCE_EDIT_ROLES: set[str] = {"Director", "CEO"}

# Admin on a corp-scoped plan = CEO only (tighter than edit — can delete/reassign).
CORP_ADMIN_ROLES: set[str] = {"CEO"}
ALLIANCE_ADMIN_ROLES: set[str] = {"CEO"}


@dataclass
class Identities:
    """A snapshot of one user's on-chain identity for permission checks."""
    user_id: int
    character_ids: set[int] = field(default_factory=set)
    corp_ids: set[int] = field(default_factory=set)
    alliance_ids: set[int] = field(default_factory=set)
    # Map corp_id → set of role strings (union across characters in that corp).
    roles_by_corp: dict[int, set[str]] = field(default_factory=dict)
    # Map corp_id → alliance_id (so we can find alliance corps the user is in).
    alliance_of_corp: dict[int, int] = field(default_factory=dict)

    def roles_in_corp(self, corp_id: int) -> set[str]:
        return self.roles_by_corp.get(corp_id, set())

    def corps_in_alliance(self, alliance_id: int) -> set[int]:
        return {cid for cid, aid in self.alliance_of_corp.items() if aid == alliance_id}


async def resolve_identities(db: AsyncSession, user_id: int) -> Identities:
    """Load the user's characters + cached corp roles into one Identities struct.

    Two indexed queries; safe to call once per request.
    """
    ident = Identities(user_id=user_id)

    char_rows = (await db.execute(
        select(Character).where(Character.user_id == user_id)
    )).scalars().all()
    if not char_rows:
        return ident

    for c in char_rows:
        ident.character_ids.add(c.character_id)
        if c.corporation_id:
            ident.corp_ids.add(c.corporation_id)
            if c.alliance_id:
                ident.alliance_of_corp[c.corporation_id] = c.alliance_id
                ident.alliance_ids.add(c.alliance_id)

    # Fetch cached roles for each character; union into per-corp sets.
    char_ids = list(ident.character_ids)
    if char_ids:
        role_rows = (await db.execute(
            select(CharacterCorpRoles).where(
                CharacterCorpRoles.character_id.in_(char_ids)
            )
        )).scalars().all()
        char_by_id = {c.character_id: c for c in char_rows}
        for r in role_rows:
            char = char_by_id.get(r.character_id)
            if not char or not char.corporation_id:
                continue
            try:
                roles = set(json.loads(r.roles_json) or [])
            except Exception:
                roles = set()
            ident.roles_by_corp.setdefault(char.corporation_id, set()).update(roles)

    return ident


def _acl_matches(acl_entries: Iterable[SkillPlanACL], ident: Identities,
                 allowed_perms: set[str]) -> bool:
    """True if any ACL entry grants `ident` one of `allowed_perms`."""
    for e in acl_entries:
        if e.permission not in allowed_perms:
            continue
        if e.subject_type == "character" and e.subject_id in ident.character_ids:
            return True
        if e.subject_type == "corporation" and e.subject_id in ident.corp_ids:
            return True
        if e.subject_type == "alliance" and e.subject_id in ident.alliance_ids:
            return True
    return False


def can_view(plan: SkillPlan, ident: Identities) -> bool:
    """True if the user may see the plan."""
    if plan.user_id == ident.user_id:
        return True
    vis = plan.visibility or "personal"
    if vis == "personal":
        return False  # only owner — handled by the user_id check above
    if vis == "corporation":
        return plan.owner_corp_id in ident.corp_ids
    if vis == "alliance":
        return plan.owner_alliance_id in ident.alliance_ids
    if vis == "custom":
        return _acl_matches(plan.acl_entries or [], ident,
                            {"view", "edit", "admin"})
    return False


def can_edit(plan: SkillPlan, ident: Identities) -> bool:
    """True if the user may add/remove/reorder/rename skills."""
    if plan.user_id == ident.user_id:
        return True
    vis = plan.visibility or "personal"
    if vis == "personal":
        return False
    if vis == "corporation":
        if plan.owner_corp_id not in ident.corp_ids:
            return False
        return bool(CORP_EDIT_ROLES & ident.roles_in_corp(plan.owner_corp_id))
    if vis == "alliance":
        if plan.owner_alliance_id not in ident.alliance_ids:
            return False
        # Option B: Director/CEO in any corp the user has a char in that's in the alliance
        for corp_id in ident.corps_in_alliance(plan.owner_alliance_id):
            if ALLIANCE_EDIT_ROLES & ident.roles_in_corp(corp_id):
                return True
        return False
    if vis == "custom":
        return _acl_matches(plan.acl_entries or [], ident, {"edit", "admin"})
    return False


def can_admin(plan: SkillPlan, ident: Identities) -> bool:
    """True if the user may manage ACLs or delete the plan.

    Always granted to the owner. Tighter than edit: only CEO on corp/alliance
    scopes and ACL 'admin' on custom.
    """
    if plan.user_id == ident.user_id:
        return True
    vis = plan.visibility or "personal"
    if vis == "personal":
        return False
    if vis == "corporation":
        if plan.owner_corp_id not in ident.corp_ids:
            return False
        return bool(CORP_ADMIN_ROLES & ident.roles_in_corp(plan.owner_corp_id))
    if vis == "alliance":
        if plan.owner_alliance_id not in ident.alliance_ids:
            return False
        for corp_id in ident.corps_in_alliance(plan.owner_alliance_id):
            if ALLIANCE_ADMIN_ROLES & ident.roles_in_corp(corp_id):
                return True
        return False
    if vis == "custom":
        return _acl_matches(plan.acl_entries or [], ident, {"admin"})
    return False


# ── Eligibility helpers for UI (which corps/alliances can this user target?) ──

def eligible_corps_for_create(ident: Identities) -> set[int]:
    """Corps where the user has at least one character with a corp-edit role."""
    return {cid for cid, roles in ident.roles_by_corp.items()
            if CORP_EDIT_ROLES & roles}


def eligible_alliances_for_create(ident: Identities) -> set[int]:
    """Alliances where the user has at least one character in a corp with Director/CEO."""
    out: set[int] = set()
    for corp_id, alliance_id in ident.alliance_of_corp.items():
        if alliance_id and (ALLIANCE_EDIT_ROLES & ident.roles_in_corp(corp_id)):
            out.add(alliance_id)
    return out
