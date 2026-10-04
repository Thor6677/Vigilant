"""Shared harness for the T-070 "no visual change" refactor proof.

Renders dashboard.html's `content` block directly, against a hand-built
context, with no database and no `datetime.now()` in the loop — the template
itself performs no time-dependent computation (that all happens in
app/routes/dashboard.py before the render), so a hand-built context is a
faithful, fully deterministic stand-in for what the real route builds.

Used once (before the pilot-card macro refactor) to capture the golden
fixture at tests/fixtures/dashboard_pilot_cards_golden.json, and again by
tests/test_dashboard_pilot_card_refactor.py (after the refactor) to prove the
rendered `.b-card` markup is byte-for-byte identical once whitespace is
normalized.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.auth import scopes as perms
from app.dashboard.prefs import DEFAULT_PREFS
from app.dashboard.summary import build_pilot_summaries
from app.db.models import Character
from app.routes import dashboard as dash_mod
from app.routes.characters import group_skill_data

ALL_SCOPES_STR = " ".join(perms.ALL_SCOPES)


def _char(character_id, name, **kw):
    defaults = dict(
        character_name=name,
        corporation_id=None,
        corporation_name=None,
        alliance_id=None,
        alliance_name=None,
        scopes=ALL_SCOPES_STR,
        declined_scopes="",
        account_group="Sample Corp",
        sort_order=character_id,
        access_token="x",
        refresh_token="x",
        token_expiry=datetime(2030, 1, 1, tzinfo=timezone.utc),
        is_main=False,
        user_id=1,
    )
    defaults.update(kw)
    return Character(character_id=character_id, **defaults)


# ── Fixture characters — one per card state ──────────────────────────────────
# ids are stable across golden capture and later re-renders.
CHAR_OK = _char(1001, "Pilot One", corporation_id=98000001, corporation_name="Sample Corp",
                alliance_id=99000001, alliance_name="Sample Alliance")
CHAR_WARNING = _char(1002, "Pilot Two", corporation_name="Sample Corp")  # no corp id -> no logo
CHAR_CRITICAL = _char(1003, "Pilot Three")
CHAR_PAUSED = _char(1004, "Pilot Four", corporation_id=98000002, corporation_name="Other Corp")
CHAR_EMPTY = _char(1005, "Pilot Five")
CHAR_NOSCOPE = _char(1006, "Pilot Six")
CHAR_REAUTH = _char(1007, "Pilot Seven", corporation_id=98000001, corporation_name="Sample Corp")
CHAR_SYNCING = _char(1008, "Pilot Eight")
CHAR_SYNCERR = _char(1009, "Pilot Nine")
CHAR_NEVER = _char(1010, "Pilot Ten")
CHAR_SKILLERR = _char(1011, "Pilot Eleven")

CHARACTERS = [
    CHAR_OK, CHAR_WARNING, CHAR_CRITICAL, CHAR_PAUSED, CHAR_EMPTY,
    CHAR_NOSCOPE, CHAR_REAUTH, CHAR_SYNCING, CHAR_SYNCERR, CHAR_NEVER,
    CHAR_SKILLERR,
]


def _sk(warning, **kw):
    base = dict(current_skill=None, current_level=None, current_finish_str=None,
                time_remaining_str=None, progress_pct=0, queue_length=0, warning=warning)
    base.update(kw)
    return base


LOCATIONS = {
    1001: {"is_online": True, "security": 0.9, "system_name": "Jita", "docked_at": "Sample Station",
           "ship_type_id": 587, "ship_type_name": "Rifter"},
    1002: {"is_online": True, "security": 0.4, "system_name": "Amamake", "docked_at": None,
           "ship_type_id": 621, "ship_type_name": "Caracal"},
    1003: {"is_online": False, "security": -0.5, "system_name": "Rancer", "docked_at": None,
           "ship_type_id": None, "ship_type_name": None},
    1004: {"is_online": True, "security": 0.9, "system_name": "Jita", "docked_at": "Sample Station",
           "ship_type_id": None, "ship_type_name": None},
    1005: {"is_online": False, "security": None, "system_name": None, "docked_at": None,
           "ship_type_id": None, "ship_type_name": None},
    1006: {"is_online": True, "security": 0.5, "system_name": "Perimeter", "docked_at": "Sample Outpost",
           "ship_type_id": None, "ship_type_name": None},
    1007: {"is_online": True, "security": 0.9, "system_name": "Jita", "docked_at": "Sample Station",
           "ship_type_id": None, "ship_type_name": None},
    1008: {"is_online": True, "security": 0.6, "system_name": "Dodixie", "docked_at": "Sample Station",
           "ship_type_id": None, "ship_type_name": None},
    1009: {"is_online": True, "security": 0.6, "system_name": "Dodixie", "docked_at": "Sample Station",
           "ship_type_id": None, "ship_type_name": None},
    1010: None,  # never synced — no location block at all
    1011: {"is_online": False, "security": 0.3, "system_name": "Tama", "docked_at": None,
           "ship_type_id": None, "ship_type_name": None},
}

WALLETS = {
    1001: 1_000_000.0, 1002: 2_500_000.0, 1003: 0.0, 1004: None, 1005: None,
    1006: 500_000.0, 1007: 3_000_000.0, 1008: 750_000.0, 1009: 750_000.0,
    1010: None, 1011: None,
}

CLONES = {
    1001: {"jump_clones_count": 2, "jump_cooldown_str": None},
    1002: {"jump_clones_count": 1, "jump_cooldown_str": "3h 12m"},
    1003: None, 1004: None, 1005: None,
    1006: {"jump_clones_count": 0, "jump_cooldown_str": None},
    1007: None, 1008: None, 1009: None, 1010: None, 1011: None,
}

SKILL_MAP = {
    1001: _sk("ok", current_skill="Gunnery", current_level=5, current_finish_str="3h 12m",
              time_remaining_str="1d 2h", progress_pct=45),
    1002: _sk("warning", current_skill="Spaceship Command", current_level=4,
              current_finish_str="12h 0m", time_remaining_str="13d 1h", progress_pct=80),
    1003: _sk("critical", current_skill="Engineering", current_level=3,
              current_finish_str="1h 0m", time_remaining_str="6d 23h", progress_pct=95),
    # A paused queue keeps its first entry as current_skill (the skill-queue
    # processor falls back to pending[0]); only the dates are missing.
    1004: _sk("paused", current_skill="Navigation", current_level=3, queue_length=3),
    1005: _sk("empty"),
    1006: _sk("no_scope"),
    1007: _sk("ok", current_skill="Drones", current_level=2, current_finish_str="2h 0m",
              time_remaining_str="4d 0h", progress_pct=10),
    1008: _sk("ok", current_skill="Navigation", current_level=1, current_finish_str="0h 5m",
              time_remaining_str="0h 5m", progress_pct=99),
    1009: _sk("ok", current_skill="Navigation", current_level=1, current_finish_str="0h 5m",
              time_remaining_str="0h 5m", progress_pct=99),
    1010: _sk("empty"),
    1011: _sk("error"),
}

NEEDS_REAUTH = {c.character_id: (c.character_id == 1007) for c in CHARACTERS}

SYNC_STATUSES = {
    1001: "idle", 1002: "idle", 1003: "idle", 1004: "idle", 1005: "idle",
    1006: "idle", 1007: "idle", 1008: "syncing", 1009: "error", 1010: "idle",
    1011: "idle",
}
STALENESS = {
    1001: "fresh", 1002: "warning", 1003: "critical", 1004: "fresh", 1005: "fresh",
    1006: "fresh", 1007: "fresh", 1008: "fresh", 1009: "critical", 1010: "never",
    1011: "fresh",
}
LAST_SYNCED_STRS = {
    1001: "5m ago", 1002: "2h 0m ago", 1003: "1d ago", 1004: "5m ago", 1005: "5m ago",
    1006: "5m ago", 1007: "5m ago", 1008: "5m ago", 1009: "1d ago", 1010: None,
    1011: "5m ago",
}


class _FakeState:
    csp_nonce = "test-nonce-0000"


class _FakeRequest:
    """Just enough of Starlette's Request for the content block."""
    state = _FakeState()


def build_context(sort: str) -> dict:
    characters = list(CHARACTERS)
    if sort == "name":
        characters = sorted(characters, key=lambda c: c.character_name.lower())
        char_groups = {}
    else:
        char_groups = {}
        for c in characters:
            char_groups.setdefault(c.account_group or "Ungrouped", []).append(c)

    skill_data = [dict(char=c, **SKILL_MAP[c.character_id]) for c in CHARACTERS]
    skill_groups = group_skill_data(skill_data)

    wallets = WALLETS
    total_wallet = sum(v for v in wallets.values() if v is not None)
    char_rows = []
    for c in CHARACTERS:
        w = wallets.get(c.character_id)
        loc = LOCATIONS.get(c.character_id)
        char_rows.append({
            "char": c, "wallet": w,
            "wallet_str": dash_mod._format_isk_py(w) if w else "—",
            "is_online": loc.get("is_online", False) if isinstance(loc, dict) else False,
        })
    char_rows.sort(key=lambda x: x["wallet"] or 0, reverse=True)

    dash_prefs = copy.deepcopy(DEFAULT_PREFS)
    pilot_summaries = build_pilot_summaries(
        CHARACTERS, WALLETS, LOCATIONS, CLONES,
        {c.character_id: dict(char=c, **SKILL_MAP[c.character_id]) for c in CHARACTERS},
        SYNC_STATUSES, STALENESS, LAST_SYNCED_STRS, NEEDS_REAUTH, {}, {}, char_groups,
    )

    return {
        "request": _FakeRequest(),
        "characters": characters,
        "cache_stats": {"active_entries": 3, "total_entries": 5, "expired_entries": 2},
        "skill_groups": skill_groups,
        "wallets": wallets,
        "locations": LOCATIONS,
        "clones": CLONES,
        "contracts": {},
        "pi": {},
        "server_status": {"online": None, "players": None},
        "zkill": {},
        "total_wallet": total_wallet,
        "sync_statuses": SYNC_STATUSES,
        "staleness": STALENESS,
        "last_synced_strs": LAST_SYNCED_STRS,
        "any_syncing": True,
        "sync_warnings": {},
        "needs_reauth": NEEDS_REAUTH,
        "new_permission_chars": [],
        "total_corporations": 2,
        "char_rows": char_rows,
        "skill_map": {c.character_id: dict(char=c, **SKILL_MAP[c.character_id]) for c in CHARACTERS},
        "sort": sort,
        "char_groups": char_groups,
        "killmails_enabled": False,
        "battles_enabled": False,
        "dash_prefs": dash_prefs,
        "dash_mode": "cards",
        "pilot_summaries": pilot_summaries,
    }


def render_content(sort: str) -> str:
    template = dash_mod.templates.env.get_template("dashboard.html")
    ctx = template.new_context(vars=build_context(sort))
    return "".join(template.blocks["content"](ctx))


def render_full(sort: str, **overrides) -> str:
    """Like render_content(), but lets a caller override/merge any context
    key — dash_mode, killmails_enabled/battles_enabled (the game-wide
    sections are all gated off by default in build_context()), or a patch
    onto dash_prefs via the special `prefs_patch` kwarg."""
    ctx = build_context(sort)
    prefs_patch = overrides.pop("prefs_patch", None)
    ctx.update(overrides)
    if prefs_patch:
        ctx["dash_prefs"] = {**ctx["dash_prefs"], **prefs_patch}
    template = dash_mod.templates.env.get_template("dashboard.html")
    tctx = template.new_context(vars=ctx)
    return "".join(template.blocks["content"](tctx))


_WS_RE = re.compile(r"\s+")


def normalize(html: str) -> str:
    return _WS_RE.sub(" ", html).strip()


def extract_card(page_html: str, character_id: int) -> str:
    """Pull out the single `.b-card` div for `character_id`, balanced on
    div-tag depth (the card nests several plain <div>s)."""
    marker = f'data-char-id="{character_id}"'
    marker_idx = page_html.index(marker)
    start = page_html.rfind("<div", 0, marker_idx)
    depth = 0
    for m in re.finditer(r"<div\b|</div\s*>", page_html[start:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            end = start + m.end()
            return normalize(page_html[start:end])
    raise AssertionError(f"unbalanced <div> for character {character_id}")


def all_cards(page_html: str) -> dict[int, str]:
    return {c.character_id: extract_card(page_html, c.character_id) for c in CHARACTERS}
