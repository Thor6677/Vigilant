"""The ESI permission catalog — what Vigilant may ask EVE SSO for, and why.

This module is the single source of truth for every user-facing notion of an
ESI scope: the permission picker shown before the SSO redirect, the Account
page, the "missing permission" notices on feature pages, the data purge that
runs when a permission is withdrawn, and the scope list sent to SSO itself.
Feature code imports the scope constants from here rather than spelling the
strings out again.

Vocabulary, used consistently in the UI:
  * a **permission** is one entry below — a user-facing bundle of one or more
    ESI scopes ("Wallet & transactions").
  * an **in-game role** is a corporation role held in EVE (Accountant,
    Director, …). Several corporation permissions are useless without one: EVE
    SSO grants the scope regardless, and ESI then answers 403. The required
    roles come from the spec's x-required-roles (see app/esi/scope_map.py).
  * Vigilant's own user/manager/admin setting is an *account role*; nothing
    here touches it.

What a character has actually granted is ``Character.scopes`` (read back from
the token after SSO). What the user explicitly said no to is
``Character.declined_scopes``. A catalog scope in neither set is "new": the
character was authorized before Vigilant asked for it, so the UI offers it
rather than nagging.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ── Scope constants ──────────────────────────────────────────────────────────
# Character
WALLET = "esi-wallet.read_character_wallet.v1"
ORDERS = "esi-markets.read_character_orders.v1"
ASSETS = "esi-assets.read_assets.v1"
LOCATION = "esi-location.read_location.v1"
SHIP = "esi-location.read_ship_type.v1"
ONLINE = "esi-location.read_online.v1"
SKILLS = "esi-skills.read_skills.v1"
SKILLQUEUE = "esi-skills.read_skillqueue.v1"
CLONES = "esi-clones.read_clones.v1"
IMPLANTS = "esi-clones.read_implants.v1"
JOBS = "esi-industry.read_character_jobs.v1"
BLUEPRINTS = "esi-characters.read_blueprints.v1"
MINING = "esi-industry.read_character_mining.v1"
PLANETS = "esi-planets.manage_planets.v1"
CONTRACTS = "esi-contracts.read_character_contracts.v1"
NOTIFICATIONS = "esi-characters.read_notifications.v1"
MAIL = "esi-mail.read_mail.v1"
FITTINGS = "esi-fittings.read_fittings.v1"
STRUCTURE_NAMES = "esi-universe.read_structures.v1"
STRUCTURE_SEARCH = "esi-search.search_structures.v1"
WAYPOINT = "esi-ui.write_waypoint.v1"
# Corporation
CORP_ROLES = "esi-characters.read_corporation_roles.v1"
CORP_MEMBERS = "esi-corporations.read_corporation_membership.v1"
CORP_WALLETS = "esi-wallet.read_corporation_wallets.v1"
CORP_ORDERS = "esi-markets.read_corporation_orders.v1"
CORP_JOBS = "esi-industry.read_corporation_jobs.v1"
CORP_STRUCTURES = "esi-corporations.read_structures.v1"
CORP_CONTRACTS = "esi-contracts.read_corporation_contracts.v1"
CORP_ASSETS = "esi-assets.read_corporation_assets.v1"
CORP_BLUEPRINTS = "esi-corporations.read_blueprints.v1"

CHARACTER = "character"
CORPORATION = "corporation"


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    group: str                      # CHARACTER | CORPORATION
    summary: str                    # what Vigilant shows with it, in plain words
    scopes: tuple[str, ...]
    powers: tuple[str, ...] = ()    # pages/features that need it
    in_game_roles: tuple[str, ...] = ()  # any ONE of these is required in game
    writes: bool = False            # changes something in the game client
    note: str | None = None         # extra caveat shown under the summary
    icon: str = "•"


PERMISSIONS: tuple[Permission, ...] = (
    # ── Character ────────────────────────────────────────────────────────
    Permission(
        "wallet", "Wallet & transactions", CHARACTER,
        "Your ISK balance, wallet journal and market transactions.",
        (WALLET,), ("Dashboard ISK", "Net worth", "Profit & loss", "Wallet journal"), icon="◈"),
    Permission(
        "orders", "Market orders", CHARACTER,
        "Your open buy and sell orders.",
        (ORDERS,), ("Net worth (escrow)", "Order tracking"), icon="⇄"),
    Permission(
        "assets", "Assets", CHARACTER,
        "Every item you own and where it is.",
        (ASSETS,), ("Assets", "Net worth", "Stockpiles", "Fitting tool: owned modules"), icon="▣"),
    Permission(
        "location", "Location, ship & online status", CHARACTER,
        "Which system you are in, the ship you are flying, and whether you are logged in.",
        (LOCATION, SHIP, ONLINE), ("Dashboard location", "Wormhole tracker", "Star map: you-are-here"), icon="⌖"),
    Permission(
        "skills", "Skills & skill queue", CHARACTER,
        "Your trained skills, attributes and training queue.",
        (SKILLS, SKILLQUEUE), ("Skill queue warnings", "Skill plans", "Ship mastery", "Fitting skill checks"), icon="✦"),
    Permission(
        "clones", "Clones & implants", CHARACTER,
        "Your jump clones and the implants in your active clone.",
        (CLONES, IMPLANTS), ("Character page", "Fitting tool: your implants"), icon="⚕"),
    Permission(
        "industry", "Industry jobs & blueprints", CHARACTER,
        "Your manufacturing and research jobs and the blueprints you own.",
        (JOBS, BLUEPRINTS), ("Industry jobs", "Blueprints", "Net worth (work in progress)"), icon="⚙"),
    Permission(
        "mining", "Mining ledger", CHARACTER,
        "What you mined, where and when (last 30 days, as EVE keeps it).",
        (MINING,), ("Mining ledger",), icon="⛏"),
    Permission(
        "planets", "Planetary industry", CHARACTER,
        "Your colonies, extractors and factory layouts.",
        (PLANETS,), ("Planetary industry",),
        note="EVE names this scope “manage planets”, but it is read-only — "
             "Vigilant cannot change your colonies.", icon="◍"),
    Permission(
        "contracts", "Contracts", CHARACTER,
        "Contracts you issued or that were issued to you.",
        (CONTRACTS,), ("Contract alerts", "Character page"), icon="✎"),
    Permission(
        "notifications", "In-game notifications", CHARACTER,
        "The notification feed EVE shows you (structure attacks, war declarations, …).",
        (NOTIFICATIONS,), ("Structure attack alerts", "Timer board"), icon="✉"),
    Permission(
        "mail", "EVE Mail", CHARACTER,
        "Your in-game mail: headers, and a message body when you open it.",
        (MAIL,), ("Character page: mail",), icon="✉"),
    Permission(
        "fittings", "Saved fittings", CHARACTER,
        "The ship fittings saved in your EVE client.",
        (FITTINGS,), ("Fitting tool: import from EVE",), icon="⛭"),
    Permission(
        "structure_names", "Player structure names", CHARACTER,
        "Looks up the names of player-owned structures you have access to.",
        (STRUCTURE_NAMES,), ("Assets, jobs and location show structure names instead of numbers",), icon="⌂"),
    Permission(
        "structure_search", "In-game search", CHARACTER,
        "Lets Vigilant run your character's in-game search to find corporation "
        "and alliance names as you type.",
        (STRUCTURE_SEARCH,), ("Timer board: owner lookup", "Timer access lists"),
        note="EVE files this under “search structures”. Only your own searches use "
             "your character — never other users'.", icon="⌕"),
    Permission(
        "waypoints", "Set autopilot waypoints", CHARACTER,
        "Lets Vigilant send a route to your in-game autopilot when you ask it to.",
        (WAYPOINT,), ("Star map: send route to EVE",), writes=True,
        note="The only permission that changes anything in the game, and only when you click it.",
        icon="➚"),
    # ── Corporation ──────────────────────────────────────────────────────
    Permission(
        "corp_roles", "Your corporation roles", CORPORATION,
        "Which in-game roles you hold in your corporation.",
        (CORP_ROLES,), ("Corp pages: explain which role is missing", "Skill plan sharing", "Timer board"),
        note="Included automatically with any other corporation permission, so "
             "Vigilant can tell a missing role from a missing permission.", icon="♜"),
    Permission(
        "corp_members", "Corporation members", CORPORATION,
        "Your corporation's member list.",
        (CORP_MEMBERS,), ("Corporation page: members",), icon="☰"),
    Permission(
        "corp_wallets", "Corporation wallets", CORPORATION,
        "Corporation wallet balances and journals.",
        (CORP_WALLETS,), ("Corporation page: wallets", "Corp wallet history"),
        in_game_roles=("Accountant", "Junior_Accountant"), icon="◈"),
    Permission(
        "corp_orders", "Corporation market orders", CORPORATION,
        "Your corporation's open market orders.",
        (CORP_ORDERS,), ("Corporation page: orders",),
        in_game_roles=("Accountant", "Trader"), icon="⇄"),
    Permission(
        "corp_industry", "Corporation industry jobs", CORPORATION,
        "Jobs running on your corporation's behalf.",
        (CORP_JOBS,), ("Industry jobs: corporation", "Corporation page: industry"),
        in_game_roles=("Factory_Manager",), icon="⚙"),
    Permission(
        "corp_structures", "Corporation structures", CORPORATION,
        "Your corporation's structures, fuel and services.",
        (CORP_STRUCTURES,), ("Corporation page: structures", "Structure alerts"),
        in_game_roles=("Station_Manager",), icon="⌂"),
    Permission(
        "corp_contracts", "Corporation contracts", CORPORATION,
        "Contracts issued by or to your corporation.",
        (CORP_CONTRACTS,), ("Corp contracts", "Corporation page: contracts"), icon="✎"),
    Permission(
        "corp_assets", "Corporation assets", CORPORATION,
        "Everything in your corporation's hangars.",
        (CORP_ASSETS,), ("Corp inventory", "Inventory alerts"),
        in_game_roles=("Director",), icon="▣"),
    Permission(
        "corp_blueprints", "Corporation blueprints", CORPORATION,
        "Blueprints owned by your corporation.",
        (CORP_BLUEPRINTS,), ("Blueprints: corporation",),
        in_game_roles=("Director",), icon="⚙"),
)

BY_KEY: dict[str, Permission] = {p.key: p for p in PERMISSIONS}
_BY_SCOPE: dict[str, Permission] = {s: p for p in PERMISSIONS for s in p.scopes}

# Every scope Vigilant may request, in catalog order. This is also the
# complete set the EVE SSO application must have enabled.
ALL_SCOPES: tuple[str, ...] = tuple(s for p in PERMISSIONS for s in p.scopes)

PRESETS: dict[str, tuple[str, ...]] = {
    "everything": tuple(p.key for p in PERMISSIONS),
    "personal": tuple(p.key for p in PERMISSIONS if p.group == CHARACTER),
    "minimal": (),
}
PRESET_LABELS = (
    ("everything", "Everything", "All character and corporation data. The full dashboard."),
    ("personal", "Personal only", "Everything about your character; nothing from your corporation."),
    ("minimal", "Minimal", "Just log in. Vigilant knows your public name, corporation and alliance, nothing more."),
)
DEFAULT_PRESET = "everything"

assert len(set(ALL_SCOPES)) == len(ALL_SCOPES), "a scope is listed under two permissions"
assert set(PRESETS) == {k for k, _, _ in PRESET_LABELS}


def parse_scopes(value: str | Iterable[str] | None) -> set[str]:
    """Space-separated scope string (as stored and as EVE returns it) -> set."""
    if not value:
        return set()
    if isinstance(value, str):
        return set(value.split())
    return set(value)


def join_scopes(scopes: Iterable[str]) -> str:
    """Set of scopes -> stored/SSO string, in catalog order for stable diffs."""
    wanted = set(scopes)
    ordered = [s for s in ALL_SCOPES if s in wanted]
    ordered += sorted(wanted - set(ordered))
    return " ".join(ordered)


def normalize_keys(keys: Iterable[str]) -> list[str]:
    """Validate a permission selection and apply the catalog's own rules.

    Unknown keys are dropped (the picker is a plain GET form, so anything can
    arrive). Any corporation permission pulls in ``corp_roles``: without the
    character's roles Vigilant cannot tell "you did not allow this" from "your
    character lacks the in-game role", which is the whole point of the notices.
    """
    chosen = {k for k in keys if k in BY_KEY}
    if any(BY_KEY[k].group == CORPORATION for k in chosen):
        chosen.add("corp_roles")
    return [p.key for p in PERMISSIONS if p.key in chosen]


def scopes_for(keys: Iterable[str]) -> list[str]:
    """The ESI scopes to request for a (normalized) permission selection."""
    chosen = set(normalize_keys(keys))
    return [s for p in PERMISSIONS if p.key in chosen for s in p.scopes]


def keys_for_scopes(scopes: Iterable[str]) -> list[str]:
    """Permissions fully covered by a set of granted scopes."""
    have = parse_scopes(scopes)
    return [p.key for p in PERMISSIONS if set(p.scopes) <= have]


def permission_for_scope(scope: str) -> Permission | None:
    return _BY_SCOPE.get(scope)


def extra_scopes(granted: Iterable[str]) -> set[str]:
    """Scopes a token carries that the catalog no longer asks for (for example
    esi-industry.read_corporation_mining.v1, requested by older releases and
    never used). The Account page offers to drop them."""
    return parse_scopes(granted) - set(ALL_SCOPES)


# ── Per-character state ──────────────────────────────────────────────────────
GRANTED = "granted"
DECLINED = "declined"      # the user said no at their last authorization
NEW = "new"                # never offered: authorized before Vigilant asked
PARTIAL = "partial"        # some of the permission's scopes but not all


def state_of(perm: Permission, granted: Iterable[str], declined: Iterable[str]) -> str:
    have = parse_scopes(granted)
    said_no = parse_scopes(declined)
    held = [s for s in perm.scopes if s in have]
    if len(held) == len(perm.scopes):
        return GRANTED
    if any(s in said_no for s in perm.scopes):
        return DECLINED
    if held:
        return PARTIAL
    return NEW


def has(char, scope: str) -> bool:
    """True iff the character's token carries ``scope``. Accepts anything with
    a ``scopes`` attribute (Character rows, plain dicts built for templates)."""
    raw = char.get("scopes") if isinstance(char, dict) else getattr(char, "scopes", "")
    return scope in parse_scopes(raw)


def declined_after(requested: Iterable[str]) -> set[str]:
    """What a user declined by authorizing with ``requested``: every catalog
    scope they were offered and left unticked."""
    return set(ALL_SCOPES) - parse_scopes(requested)
