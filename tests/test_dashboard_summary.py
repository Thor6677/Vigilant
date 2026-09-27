"""app/dashboard/summary.py: build_pilot_summaries() is a pure function over
values app/routes/dashboard.py already computes. These tests exercise every
training.warning state, every sync/flag combination, and the frozen key
shape, using tests/_dashboard_fixture.py's fixture characters so the same
data doubles as the refactor golden's source of truth.
"""
from tests._dashboard_fixture import (
    CHARACTERS, CLONES, LAST_SYNCED_STRS, LOCATIONS, NEEDS_REAUTH,
    SKILL_MAP, STALENESS, SYNC_STATUSES, WALLETS,
)
from app.dashboard.summary import build_pilot_summaries


def _summaries(char_groups=None):
    return build_pilot_summaries(
        CHARACTERS, WALLETS, LOCATIONS, CLONES, SKILL_MAP, SYNC_STATUSES,
        STALENESS, LAST_SYNCED_STRS, NEEDS_REAUTH, {}, {}, char_groups or {},
    )


def test_every_character_is_present_with_the_frozen_keys():
    summaries = _summaries()
    assert set(summaries) == {c.character_id for c in CHARACTERS}
    frozen_top = {
        "character_id", "name", "corporation_name", "alliance_name", "account",
        "is_online", "security", "system_name", "docked_at", "ship_type_id",
        "ship_name", "wallet", "training", "clones", "sync", "needs_reauth",
        "flags",
    }
    for s in summaries.values():
        assert frozen_top <= set(s)
        assert {"skill", "level", "finish_str", "queue_left_str", "progress_pct", "warning"} <= set(s["training"])
        assert {"count", "cooldown_str"} <= set(s["clones"])
        assert {"status", "last_str", "stale"} <= set(s["sync"])


def test_ok_state_has_no_training_flag():
    s = _summaries()[1001]
    assert s["training"]["warning"] == "ok"
    assert s["training"]["skill"] == "Gunnery"
    assert not any(f["label"] in ("EMPTY", "PAUSED", "<7D", "<14D") for f in s["flags"])


def test_warning_state_flags_lt14d_gold():
    s = _summaries()[1002]
    assert s["training"]["warning"] == "warning"
    assert {"label": "<14D", "sev": "gold"} in s["flags"]
    # 1002 is staleness=warning, sync idle — the card shows a retry button
    # for this combination too (stale in critical/warning/never), so the
    # summary must flag it the same way.
    assert {"label": "SYNC STALE", "sev": "amber"} in s["flags"]


def test_critical_state_flags_lt7d_amber():
    s = _summaries()[1003]
    assert s["training"]["warning"] == "critical"
    assert {"label": "<7D", "sev": "amber"} in s["flags"]
    # 1003 is also staleness=critical, sync idle -> SYNC STALE amber
    assert {"label": "SYNC STALE", "sev": "amber"} in s["flags"]


def test_paused_state_flags_grey():
    s = _summaries()[1004]
    assert s["training"]["warning"] == "paused"
    assert {"label": "PAUSED", "sev": "grey"} in s["flags"]


def test_empty_state_flags_red():
    s = _summaries()[1005]
    assert s["training"]["warning"] == "empty"
    assert {"label": "EMPTY", "sev": "red"} in s["flags"]


def test_no_scope_state_has_no_training_flag_and_passes_through():
    s = _summaries()[1006]
    assert s["training"]["warning"] == "no_scope"
    assert not any(f["label"] in ("EMPTY", "PAUSED", "<7D", "<14D") for f in s["flags"])


def test_needs_reauth_flags_renew_red():
    s = _summaries()[1007]
    assert s["needs_reauth"] is True
    assert {"label": "RENEW", "sev": "red"} in s["flags"]


def test_syncing_status_has_no_sync_flag():
    s = _summaries()[1008]
    assert s["sync"]["status"] == "syncing"
    assert not any("SYNC" in f["label"] or f["label"] == "NEVER SYNCED" for f in s["flags"])


def test_sync_error_flags_red():
    s = _summaries()[1009]
    assert s["sync"]["status"] == "error"
    assert {"label": "SYNC ERROR", "sev": "red"} in s["flags"]


def test_never_synced_flags_grey():
    s = _summaries()[1010]
    assert s["sync"]["stale"] == "never"
    assert s["sync"]["last_str"] is None
    assert {"label": "NEVER SYNCED", "sev": "grey"} in s["flags"]
    # No location was ever cached for this character either.
    assert s["is_online"] is False
    assert s["system_name"] is None


def test_generic_skill_error_state_has_no_training_flag():
    s = _summaries()[1011]
    assert s["training"]["warning"] == "error"
    assert not any(f["label"] in ("EMPTY", "PAUSED", "<7D", "<14D") for f in s["flags"])


def test_account_comes_from_char_groups_membership():
    char_groups = {"Alpha": [c for c in CHARACTERS if c.character_id in (1001, 1002)]}
    summaries = build_pilot_summaries(
        CHARACTERS, WALLETS, LOCATIONS, CLONES, SKILL_MAP, SYNC_STATUSES,
        STALENESS, LAST_SYNCED_STRS, NEEDS_REAUTH, {}, {}, char_groups,
    )
    assert summaries[1001]["account"] == "Alpha"
    # Not in any group in the membership map -> falls back to the
    # character's own account_group.
    assert summaries[1003]["account"] == "Sample Corp"


def test_clone_count_is_none_when_never_synced_not_zero():
    s = _summaries()[1003]  # CLONES[1003] is None
    assert s["clones"]["count"] is None
    assert s["clones"]["cooldown_str"] is None


def test_clone_cooldown_is_surfaced():
    s = _summaries()[1002]
    assert s["clones"]["count"] == 1
    assert s["clones"]["cooldown_str"] == "3h 12m"


def test_is_pure_and_does_not_mutate_inputs():
    before = dict(WALLETS)
    _summaries()
    assert WALLETS == before


def test_syncing_suppresses_every_sync_flag_even_when_stale_is_critical():
    """The card's retry button never shows while a sync is in flight,
    whatever the staleness says (`char_sync != 'syncing' and (...)`) — the
    summary's sync flags must agree, not just for the 'fresh' case any of
    the standard fixtures happen to cover."""
    from tests._dashboard_fixture import _char
    char = _char(9001, "Pilot Syncing Critical")
    summaries = build_pilot_summaries(
        [char], {9001: 0.0}, {9001: None}, {9001: None},
        {9001: {"warning": "ok"}},
        {9001: "syncing"}, {9001: "critical"}, {9001: "5m ago"},
        {9001: False}, {}, {}, {},
    )
    s = summaries[9001]
    assert s["sync"]["status"] == "syncing"
    assert s["sync"]["stale"] == "critical"
    assert not any("SYNC" in f["label"] or f["label"] == "NEVER SYNCED" for f in s["flags"])
