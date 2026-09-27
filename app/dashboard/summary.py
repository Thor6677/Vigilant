"""Pure per-pilot summary builder for the T-070 dashboard views.

`build_pilot_summaries()` takes the same values app/routes/dashboard.py's
`dashboard()` handler already computed for the Cards view (no I/O, no
re-fetching, no re-deriving a threshold a different way — Cards and every
other view must always agree on a pilot's state) and reshapes them into one
dict per character, keyed by character_id.

Later dashboard streams (Detailed mode, the Table view, deltas/sparklines,
tag chips, the can-fly badge) build on top of this dict, so its shape below
is FROZEN as of T-070. Do not rename, remove, or change the meaning of a key
below without updating every consumer:

    character_id       int
    name               str  — char.character_name
    corporation_name    str | None
    alliance_name       str | None
    account             str — the account-group name this pilot sits under in
                         custom grouping (falls back to "Ungrouped")
    is_online           bool
    security             float | None — raw security status of the current system
    system_name          str | None
    docked_at            str | None — station/structure name, or None if in space
    ship_type_id         int | None
    ship_name            str | None
    wallet               float | None
    training             dict:
        skill            str | None   — current_skill
        level            int | None   — current_level
        finish_str       str | None   — current_finish_str
        queue_left_str   str | None   — time_remaining_str
        progress_pct     int          — 0 when there is nothing training
        warning          str          — exactly the value the card uses today:
                         one of ok | warning | critical | empty | paused |
                         no_scope | error (skill-queue fetch failed but the
                         character DOES have the scope — distinct from
                         no_scope, which means the scope itself is missing)
    clones               dict:
        count            int | None   — jump_clones_count; None means clone
                         data has never synced for this character
        cooldown_str     str | None
    sync                 dict:
        status           str  — sync_statuses value: idle | syncing | error
        last_str          str | None — last_synced_strs value
        stale            str  — _staleness()'s value: fresh | warning |
                         critical | never
    needs_reauth         bool
    flags                list[dict] — [{"label": str, "sev": str}, ...],
                         sev in red | amber | gold | grey. Rules (in the
                         order a card would show the equivalent state):
                           training.warning == "empty"    -> EMPTY,  red
                           training.warning == "paused"   -> PAUSED, grey
                           training.warning == "critical" -> <7D,    amber
                           training.warning == "warning"  -> <14D,   gold
                           needs_reauth                   -> RENEW,  red
                         Sync flags mirror EXACTLY the condition the card
                         uses to show its retry (↺) button — sync.status
                         != "syncing" and (sync.stale in ("critical",
                         "warning", "never") or sync.status == "error"):
                           sync.status == "syncing"        -> no sync flag,
                                                              whatever stale is
                           sync.status == "error"          -> SYNC ERROR, red
                           elif sync.stale in ("critical",
                                "warning")                 -> SYNC STALE, amber
                           elif sync.stale == "never"      -> NEVER SYNCED, grey
                         (sync flags are mutually exclusive with each other;
                         training flags are mutually exclusive with each
                         other; a pilot can carry one of each kind plus RENEW)

A few more keys ride along but are NOT frozen — they pass through untouched
(or default to a documented empty shape) purely as a convenience for a later
stream that wants them; nothing in T-070 reads any of these back:

    contracts, pi         — the route's own `contracts`/`pi` dicts (T-070):
                             None, "no_scope", or a dict.
    wallet_delta           — T-076: an app.dashboard.walletdelta entry
                             ({"direction": "up"|"down"|"flat", "amount"}),
                             or None if there's no 7-day-old snapshot to
                             diff against. Threaded through so Compact rows,
                             Cards and Table can all show the same arrow
                             without recomputing it.
    tags                   — T-076: an app.tags.load_character_tags() entry
                             ({"tags": [...], "note": str|None}), or the
                             empty shape {"tags": [], "note": None} when the
                             caller passes nothing.
"""
from __future__ import annotations

FLAG_SEV_BY_TRAINING_WARNING = {
    "empty": ("EMPTY", "red"),
    "paused": ("PAUSED", "grey"),
    "critical": ("<7D", "amber"),
    "warning": ("<14D", "gold"),
}


def build_pilot_summaries(
    characters,
    wallets: dict,
    locations: dict,
    clones: dict,
    skill_map: dict,
    sync_statuses: dict,
    staleness: dict,
    last_synced_strs: dict,
    needs_reauth: dict,
    contracts: dict,
    pi: dict,
    char_groups: dict,
    wallet_deltas: dict | None = None,
    tags_by_char: dict | None = None,
) -> dict[int, dict]:
    """Build one summary dict per character. Pure — no I/O, no `datetime.now()`;
    every value here is read straight from what the caller already computed."""
    account_of: dict[int, str] = {}
    for group_name, chars in (char_groups or {}).items():
        for c in chars:
            account_of[c.character_id] = group_name

    out: dict[int, dict] = {}
    for char in characters:
        cid = char.character_id
        loc = locations.get(cid) if locations else None
        sk = skill_map.get(cid, {}) if skill_map else {}
        cd = clones.get(cid) if clones else None
        sync_status = sync_statuses.get(cid, "idle") if sync_statuses else "idle"
        stale = staleness.get(cid, "never") if staleness else "never"
        last_str = last_synced_strs.get(cid) if last_synced_strs else None
        reauth = bool(needs_reauth.get(cid)) if needs_reauth else False
        training_warning = sk.get("warning", "error")

        flags: list[dict] = []
        train_flag = FLAG_SEV_BY_TRAINING_WARNING.get(training_warning)
        if train_flag:
            flags.append({"label": train_flag[0], "sev": train_flag[1]})
        if reauth:
            flags.append({"label": "RENEW", "sev": "red"})
        # Mirrors the card's retry-button condition exactly: nothing fires
        # while a sync is in flight, whatever the staleness says.
        if sync_status != "syncing":
            if sync_status == "error":
                flags.append({"label": "SYNC ERROR", "sev": "red"})
            elif stale in ("critical", "warning"):
                flags.append({"label": "SYNC STALE", "sev": "amber"})
            elif stale == "never":
                flags.append({"label": "NEVER SYNCED", "sev": "grey"})

        out[cid] = {
            "character_id": cid,
            "name": char.character_name,
            "corporation_name": char.corporation_name,
            "alliance_name": char.alliance_name,
            "account": account_of.get(cid, char.account_group or "Ungrouped"),
            "is_online": bool(loc.get("is_online")) if isinstance(loc, dict) else False,
            "security": loc.get("security") if isinstance(loc, dict) else None,
            "system_name": loc.get("system_name") if isinstance(loc, dict) else None,
            "docked_at": loc.get("docked_at") if isinstance(loc, dict) else None,
            "ship_type_id": loc.get("ship_type_id") if isinstance(loc, dict) else None,
            "ship_name": loc.get("ship_type_name") if isinstance(loc, dict) else None,
            "wallet": wallets.get(cid) if wallets else None,
            "training": {
                "skill": sk.get("current_skill"),
                "level": sk.get("current_level"),
                "finish_str": sk.get("current_finish_str"),
                "queue_left_str": sk.get("time_remaining_str"),
                "progress_pct": sk.get("progress_pct", 0) or 0,
                "warning": training_warning,
            },
            "clones": {
                "count": cd.get("jump_clones_count") if isinstance(cd, dict) else None,
                "cooldown_str": cd.get("jump_cooldown_str") if isinstance(cd, dict) else None,
            },
            "sync": {
                "status": sync_status,
                "last_str": last_str,
                "stale": stale,
            },
            "needs_reauth": reauth,
            "flags": flags,
            # Provisional — not frozen, see module docstring.
            "contracts": contracts.get(cid) if contracts else None,
            "pi": pi.get(cid) if pi else None,
            "wallet_delta": wallet_deltas.get(cid) if wallet_deltas else None,
            "tags": (tags_by_char.get(cid) if tags_by_char else None) or {"tags": [], "note": None},
        }
    return out
