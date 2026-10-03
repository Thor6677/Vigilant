"""T-071 needs-attention strip.

Two layers, matching the module split:

* `app.dashboard.attention.build_attention` — pure, no I/O, no wall clock of
  its own. Every rule, every boundary, sort order, fingerprints, age-out.
* `app.routes.dashboard_attention` — the route: 401s, the empty-body state,
  dismissal durations and lapsing, IDOR refusal, purge-on-removal.
"""
from __future__ import annotations

import base64
import json
import tempfile
from datetime import datetime, timedelta, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard.attention import (
    AGE_OUT_DAYS,
    PI_ABANDONED_DAYS,
    PI_EXPIRY_AMBER_SECONDS,
    SKILL_QUEUE_CRITICAL_DAYS,
    SKILL_QUEUE_WARNING_DAYS,
    STALE_CRITICAL_SECONDS,
    STALE_WARNING_SECONDS,
    SYNC_STALE_COLLAPSE_MIN,
    build_attention,
    skill_queue_state,
    staleness,
)

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def _pilot(cid=1, name="Pilot One", **kw):
    base = {
        "character_id": cid,
        "character_name": name,
        "account_group": None,
        "needs_reauth": False,
        "skillqueue": None,
        "pi": None,
        "industry_jobs": None,
        "industry_synced_at": None,
        "sync_status": "idle",
        "sync_error": None,
        "last_synced": NOW,
    }
    base.update(kw)
    return base


def _sq_entry(finish_in_days: float, start_in_days: float = -1, base: datetime = NOW):
    # `base` defaults to the pinned NOW; route tests, whose server reads the
    # real clock, pass the real current time instead.
    finish = base + timedelta(days=finish_in_days)
    start = base + timedelta(days=start_in_days)
    return {
        "skill_id": 1,
        "finished_level": 5,
        "start_date": start.isoformat(),
        "finish_date": finish.isoformat(),
    }


# ── no_scope / None: no item ─────────────────────────────────────────────────

def test_no_scope_produces_no_item_for_that_signal():
    p = _pilot(needs_reauth=False, skillqueue="no_scope", pi="no_scope", industry_jobs="no_scope")
    assert build_attention([p], NOW) == []


def test_none_field_produces_no_item():
    p = _pilot(skillqueue=None, pi=None, industry_jobs=None)
    assert build_attention([p], NOW) == []


# ── red: reauth ──────────────────────────────────────────────────────────────

def test_reauth_is_red_and_never_ages():
    p = _pilot(needs_reauth=True, last_synced=NOW - timedelta(days=400))
    items = build_attention([p], NOW)
    reauth = [i for i in items if i.key == "reauth:1"]
    assert len(reauth) == 1
    assert reauth[0].severity == "red"
    assert reauth[0].since is None
    assert reauth[0].action_url == "/account/permissions/1"
    assert reauth[0].action_method == "get"


def test_reauth_suppresses_the_grey_sync_item():
    p = _pilot(needs_reauth=True, sync_status="error", sync_error="token_revoked",
               last_synced=NOW - timedelta(days=10))
    items = build_attention([p], NOW)
    assert not any(i.key.startswith("sync_stale") for i in items)


# ── skill queue ──────────────────────────────────────────────────────────────

def test_solo_empty_queue_is_account_idle_red():
    """A pilot with no account_group is its own account (see the account-
    grouping tests below), so an empty queue still surfaces — just under the
    account_idle key/text, not a per-pilot 'empty queue' one."""
    p = _pilot(skillqueue=[])
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["account_idle:1"]
    assert items[0].severity == "red"
    assert items[0].since is None
    assert items[0].text == "no pilot on this account is training"


def test_all_finished_queue_is_paused_not_empty():
    """The exact shape advisor flagged: every cached entry already finished
    gives pending=[] internally, but the RAW queue is non-empty, so the card
    (and skill_queue_state) reports 'paused', not 'empty'. Both still roll
    up into the same account_idle item — paused and empty are no longer
    told apart at the item level, only at the pure-helper level."""
    stale_entry = _sq_entry(finish_in_days=-5, start_in_days=-10)
    warning, queue_end = skill_queue_state([stale_entry], NOW)
    assert warning == "paused"
    assert queue_end is None

    p = _pilot(skillqueue=[stale_entry])
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["account_idle:1"]
    assert items[0].severity == "red"


def test_paused_when_no_finish_date_at_all():
    entry = {"skill_id": 1, "finished_level": 1, "start_date": NOW.isoformat()}
    warning, _ = skill_queue_state([entry], NOW)
    assert warning == "paused"

    p = _pilot(skillqueue=[entry])
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["account_idle:1"]


@pytest.mark.parametrize("finish_in_days,expected", [
    (SKILL_QUEUE_CRITICAL_DAYS - 1 + 0.99, "critical"),   # ~7d23h -> still critical
    (SKILL_QUEUE_CRITICAL_DAYS + 1.5, "warning"),          # ~8d12h -> warning
    (SKILL_QUEUE_WARNING_DAYS - 1 + 0.99, "warning"),      # ~14d23h -> still warning
    (SKILL_QUEUE_WARNING_DAYS + 1.5, "ok"),                # ~15d12h -> ok, no item
])
def test_skill_queue_day_boundaries(finish_in_days, expected):
    entry = _sq_entry(finish_in_days=finish_in_days)
    warning, _ = skill_queue_state([entry], NOW)
    assert warning == expected

    p = _pilot(skillqueue=[entry])
    items = build_attention([p], NOW)
    if expected == "ok":
        assert items == []
    else:
        key = "queue_critical:1" if expected == "critical" else "queue_warning:1"
        assert [i.key for i in items] == [key]
        assert items[0].severity == ("amber" if expected == "critical" else "gold")


def test_skill_queue_critical_pins_against_the_real_skill_warning():
    """Cross-check against app.routes.characters.skill_warning using a single
    shared `now` for both sides, so there's no real-clock drift at the
    boundary — see module docstring on why attention.py doesn't import it."""
    from app.routes.characters import skill_warning

    for finish_in_days, expected in ((3, "critical"), (10, "warning"), (30, "ok")):
        queue_end = NOW + timedelta(days=finish_in_days)
        queue = [{"finish_date": queue_end.isoformat()}]
        assert skill_queue_state(queue, NOW)[0] == expected
        assert skill_warning(queue, queue_end, NOW) == expected


# ── account grouping (the queue-idle signal is per-account, not per-pilot) ──

def _training_entry():
    """A queue that reads as 'ok' — nowhere near critical/warning."""
    return [_sq_entry(finish_in_days=SKILL_QUEUE_WARNING_DAYS + 10)]


def _idle_entry():
    return []


def test_account_with_one_training_pilot_and_two_idle_gives_no_item():
    pilots = [
        _pilot(cid=1, name="Main", account_group="Acct A", skillqueue=_training_entry()),
        _pilot(cid=2, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=3, name="Alt Two", account_group="Acct A", skillqueue=_idle_entry()),
    ]
    items = build_attention(pilots, NOW)
    assert not any(i.key.startswith("account_idle") for i in items)


def test_account_all_idle_gives_exactly_one_red_item():
    pilots = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=2, name="Alt Two", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=3, name="Alt Three", account_group="Acct A", skillqueue=_idle_entry()),
    ]
    items = build_attention(pilots, NOW)
    idle = [i for i in items if i.key.startswith("account_idle")]
    assert len(idle) == 1
    assert idle[0].key == "account_idle:Acct A"
    assert idle[0].severity == "red"
    assert idle[0].character_name == "Acct A"


def test_account_idle_prefers_a_paused_pilot_as_the_action_target():
    paused_entry = [{"skill_id": 1, "finished_level": 1, "start_date": NOW.isoformat()}]
    pilots = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=2, name="Alt Two (paused)", account_group="Acct A", skillqueue=paused_entry),
    ]
    items = build_attention(pilots, NOW)
    idle = next(i for i in items if i.key.startswith("account_idle"))
    assert idle.character_id == 2
    assert idle.action_url == "/character/2/skills"


def test_ungrouped_pilots_each_count_as_their_own_account():
    pilots = [
        _pilot(cid=1, name="Solo One", account_group=None, skillqueue=_idle_entry()),
        _pilot(cid=2, name="Solo Two", account_group=None, skillqueue=_idle_entry()),
    ]
    items = build_attention(pilots, NOW)
    idle_keys = sorted(i.key for i in items if i.key.startswith("account_idle"))
    assert idle_keys == ["account_idle:1", "account_idle:2"]


def test_account_idle_ignores_no_scope_pilots():
    # A no_scope alt never counts toward "training", and can't save an
    # otherwise-idle account.
    pilots = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=2, name="Alt Two", account_group="Acct A", skillqueue="no_scope"),
    ]
    items = build_attention(pilots, NOW)
    idle = [i for i in items if i.key.startswith("account_idle")]
    assert len(idle) == 1
    assert idle[0].character_id == 1  # the only scoped pilot


def test_account_with_every_pilot_no_scope_gives_no_item():
    pilots = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue="no_scope"),
        _pilot(cid=2, name="Alt Two", account_group="Acct A", skillqueue=None),
    ]
    assert build_attention(pilots, NOW) == []


def test_account_idle_fingerprint_uses_pilot_ids_and_states():
    pilots_a = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=2, name="Alt Two", account_group="Acct A", skillqueue=_idle_entry()),
    ]
    pilots_b = [
        _pilot(cid=1, name="Alt One", account_group="Acct A", skillqueue=_idle_entry()),
        _pilot(cid=2, name="Alt Two", account_group="Acct A",
               skillqueue=[{"skill_id": 1, "finished_level": 1, "start_date": NOW.isoformat()}]),  # now paused
    ]
    fp_a = next(i for i in build_attention(pilots_a, NOW) if i.key.startswith("account_idle")).fingerprint
    fp_b = next(i for i in build_attention(pilots_b, NOW) if i.key.startswith("account_idle")).fingerprint
    assert fp_a != fp_b


# ── PI ───────────────────────────────────────────────────────────────────────

def _planet(pid, expiry_in_seconds, name="POS System"):
    return {
        "planet_id": pid,
        "system_name": name,
        "expiry_time": (NOW + timedelta(seconds=expiry_in_seconds)).isoformat(),
    }


def test_pi_expired_is_red():
    p = _pilot(pi=[_planet(1, -10)])
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["pi_expired:1"]
    assert items[0].severity == "red"
    assert items[0].since == NOW - timedelta(seconds=10)


def test_pi_within_24h_is_amber():
    p = _pilot(pi=[_planet(1, 3600 * 2)])  # 2h out — inside pi.py's own "critical" band too
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["pi_critical:1"]
    assert items[0].severity == "amber"


def test_pi_beyond_24h_is_no_item():
    p = _pilot(pi=[_planet(1, PI_EXPIRY_AMBER_SECONDS + 60)])
    assert build_attention([p], NOW) == []


def test_pi_planet_without_expiry_time_is_ignored():
    p = _pilot(pi=[{"planet_id": 1, "system_name": "X"}])
    assert build_attention([p], NOW) == []


def test_pi_expired_aggregates_multiple_planets_and_uses_the_oldest_as_since():
    p = _pilot(pi=[_planet(1, -10), _planet(2, -1000)])
    items = build_attention([p], NOW)
    item = next(i for i in items if i.key == "pi_expired:1")
    assert "2" in item.text  # count of expired extractors mentioned
    assert item.since == NOW - timedelta(seconds=1000)


def test_pi_expired_ages_out_to_grey_after_seven_days():
    just_inside = _pilot(cid=1, pi=[_planet(1, -(AGE_OUT_DAYS * 86400 - 3600))])
    just_outside = _pilot(cid=2, pi=[_planet(1, -(AGE_OUT_DAYS * 86400 + 3600))])

    fresh_items = build_attention([just_inside], NOW)
    aged_items = build_attention([just_outside], NOW)

    assert fresh_items[0].severity == "red"
    assert "ago" not in fresh_items[0].text
    assert aged_items[0].severity == "grey"
    assert "ago" in aged_items[0].text
    # key and the state-hash both survive the ageing transition unchanged —
    # a "until it changes" dismissal recorded while red must still match.
    assert fresh_items[0].key == aged_items[0].key.replace(":2", ":1")


def test_pi_expired_more_than_pi_abandoned_days_is_dropped_entirely():
    """Past PI_ABANDONED_DAYS, an expired extractor is an abandoned colony,
    not an attention item — no red, no grey, nothing at all (T-077)."""
    just_inside = _pilot(cid=1, pi=[_planet(1, -(PI_ABANDONED_DAYS * 86400 - 3600))])
    just_outside = _pilot(cid=2, pi=[_planet(1, -(PI_ABANDONED_DAYS * 86400 + 3600))])

    inside_items = build_attention([just_inside], NOW)
    outside_items = build_attention([just_outside], NOW)

    assert [i.key for i in inside_items] == ["pi_expired:1"]
    assert inside_items[0].severity == "grey"  # still past AGE_OUT_DAYS, just not abandoned
    assert outside_items == []


def test_pi_expired_abandoned_planet_is_filtered_out_of_a_mixed_set():
    """One abandoned extractor (>14d) alongside one merely-aged one (7-14d)
    on the same pilot: the abandoned one drops out of the count/since, the
    other still surfaces grey."""
    fresh_expired = _planet(1, -(AGE_OUT_DAYS * 86400 + 3600))         # aged, grey
    abandoned = _planet(2, -(PI_ABANDONED_DAYS * 86400 + 3600))        # abandoned, dropped
    p = _pilot(pi=[fresh_expired, abandoned])
    items = build_attention([p], NOW)
    item = next(i for i in items if i.key == "pi_expired:1")
    assert item.severity == "grey"
    # Singular phrasing ("extractor", not "extractors") -- only the one
    # non-abandoned extractor was counted, the abandoned one is invisible.
    assert "extractors" not in item.text
    assert item.since == NOW - timedelta(seconds=AGE_OUT_DAYS * 86400 + 3600)


def test_pi_thresholds_pin_against_recompute_expiry():
    from app.routes.pi import _recompute_expiry

    real_now = datetime.now(timezone.utc)
    for delta_seconds, expected in ((-5, "expired"), (1800, "critical"), (43200, "warning")):
        expiry = (real_now + timedelta(seconds=delta_seconds)).isoformat()
        got = _recompute_expiry({"expiry_time": expiry})["expiry_warning"]
        assert got == expected
        # attention.py's own single amber band covers pi.py's "critical" and
        # "warning" both — see the rules table in the module docstring.
        from app.dashboard.attention import pi_expiry_state
        mine, _ = pi_expiry_state(expiry, real_now)
        assert mine == expected


# ── industry jobs ────────────────────────────────────────────────────────────

def test_jobs_ready_is_gold():
    jobs = [{"activity_id": 1, "blueprint_type_id": 1, "product_type_id": 2, "runs": 1, "status": "ready"}]
    p = _pilot(industry_jobs=jobs, industry_synced_at=NOW)
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["jobs_ready:1"]
    assert items[0].severity == "gold"
    assert items[0].action_url == "/industry/jobs"


def test_jobs_active_is_no_item_given_the_cached_shape_lacks_end_date():
    """Documents the T-071 gap: the trimmed industry_json cache has neither
    job_id nor end_date, so an 'active' job past its end can't be told apart
    from one still running purely from cache — see the module docstring."""
    jobs = [{"activity_id": 1, "blueprint_type_id": 1, "product_type_id": 2, "runs": 1, "status": "active"}]
    p = _pilot(industry_jobs=jobs)
    assert build_attention([p], NOW) == []


def test_jobs_ready_ages_out_to_grey_using_industry_synced_at():
    jobs = [{"activity_id": 1, "blueprint_type_id": 1, "product_type_id": 2, "runs": 1, "status": "ready"}]
    fresh = _pilot(cid=1, industry_jobs=jobs, industry_synced_at=NOW - timedelta(days=1))
    aged = _pilot(cid=2, industry_jobs=jobs, industry_synced_at=NOW - timedelta(days=AGE_OUT_DAYS + 1))

    fresh_items = build_attention([fresh], NOW)
    aged_items = build_attention([aged], NOW)
    assert fresh_items[0].severity == "gold"
    assert aged_items[0].severity == "grey"
    assert "ago" in aged_items[0].text


# ── sync stale / erroring ────────────────────────────────────────────────────

def test_sync_error_is_grey():
    p = _pilot(sync_status="error", sync_error="esi_error: TimeoutError", last_synced=NOW)
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["sync_stale:1"]
    assert items[0].severity == "grey"
    assert items[0].action_method == "post"
    assert items[0].action_url == "/dashboard/sync/1"


def test_sync_never_synced_is_grey():
    p = _pilot(sync_status="idle", last_synced=None)
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["sync_stale:1"]
    assert "never synced" in items[0].text


def test_sync_fresh_is_no_item():
    p = _pilot(sync_status="idle", last_synced=NOW - timedelta(seconds=STALE_WARNING_SECONDS - 1))
    assert build_attention([p], NOW) == []


def test_sync_stale_beyond_critical_is_grey():
    p = _pilot(sync_status="idle", last_synced=NOW - timedelta(seconds=STALE_CRITICAL_SECONDS + 1))
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["sync_stale:1"]


def test_sync_suppressed_while_actively_syncing():
    p = _pilot(sync_status="syncing", last_synced=NOW - timedelta(days=5))
    assert build_attention([p], NOW) == []


# ── sync stale collapse (T-077) ──────────────────────────────────────────────

def _stale_pilot(cid, name):
    return _pilot(cid=cid, name=name, sync_status="idle",
                  last_synced=NOW - timedelta(seconds=STALE_CRITICAL_SECONDS + 1))


def test_below_collapse_threshold_stays_per_pilot():
    pilots = [_stale_pilot(i, f"Pilot {i}") for i in range(1, SYNC_STALE_COLLAPSE_MIN)]
    items = build_attention(pilots, NOW)
    keys = sorted(i.key for i in items)
    assert keys == [f"sync_stale:{i}" for i in range(1, SYNC_STALE_COLLAPSE_MIN)]


def test_at_collapse_threshold_becomes_one_grey_item():
    pilots = [_stale_pilot(i, f"Pilot {i}") for i in range(1, SYNC_STALE_COLLAPSE_MIN + 1)]
    items = build_attention(pilots, NOW)
    stale_items = [i for i in items if i.key.startswith("sync_stale")]
    assert len(stale_items) == 1
    item = stale_items[0]
    assert item.key == "sync_stale:many"
    assert item.severity == "grey"
    assert f"{SYNC_STALE_COLLAPSE_MIN} pilots have stale data" in item.text
    assert item.action_label is None
    assert item.action_url is None
    assert item.action_method is None


def test_collapsed_item_names_up_to_three_pilots_then_the_rest():
    pilots = [_stale_pilot(i, name) for i, name in
              enumerate(["Amy", "Bo", "Cy", "Deb", "Eli"], start=1)]
    items = build_attention(pilots, NOW)
    item = next(i for i in items if i.key == "sync_stale:many")
    assert item.text == "5 pilots have stale data — Amy, Bo, Cy and 2 more"


def test_collapsed_item_fingerprint_is_over_sorted_character_ids():
    pilots_a = [_stale_pilot(i, f"Pilot {i}") for i in (1, 2, 3)]
    pilots_b = [_stale_pilot(i, f"Pilot {i}") for i in (3, 2, 1)]  # same set, different order
    fp_a = next(i for i in build_attention(pilots_a, NOW) if i.key == "sync_stale:many").fingerprint
    fp_b = next(i for i in build_attention(pilots_b, NOW) if i.key == "sync_stale:many").fingerprint
    assert fp_a == fp_b

    pilots_c = [_stale_pilot(i, f"Pilot {i}") for i in (1, 2, 4)]  # different set
    fp_c = next(i for i in build_attention(pilots_c, NOW) if i.key == "sync_stale:many").fingerprint
    assert fp_c != fp_a


def test_collapse_is_suppressed_by_reauth_and_syncing_same_as_per_pilot():
    pilots = [
        _stale_pilot(1, "Amy"),
        _stale_pilot(2, "Bo"),
        _pilot(cid=3, name="Cy", needs_reauth=True,
               sync_status="idle", last_synced=NOW - timedelta(seconds=STALE_CRITICAL_SECONDS + 1)),
        _pilot(cid=4, name="Deb", sync_status="syncing", last_synced=NOW - timedelta(days=5)),
    ]
    items = build_attention(pilots, NOW)
    # Only Amy and Bo actually qualify as stale candidates (Cy is suppressed
    # by re-auth, Deb is mid-sync) -- below SYNC_STALE_COLLAPSE_MIN, so both
    # stay per-pilot rather than collapsing.
    stale_keys = sorted(i.key for i in items if i.key.startswith("sync_stale"))
    assert stale_keys == ["sync_stale:1", "sync_stale:2"]


def test_staleness_thresholds_pin_against_dashboard():
    from app.routes.dashboard import STALE_CRITICAL_SECONDS as D_CRIT
    from app.routes.dashboard import STALE_WARNING_SECONDS as D_WARN
    assert STALE_WARNING_SECONDS == D_WARN
    assert STALE_CRITICAL_SECONDS == D_CRIT
    assert staleness(NOW, NOW - timedelta(seconds=D_CRIT + 1)) == "critical"
    assert staleness(NOW, None) == "never"
    assert staleness(NOW, NOW) == "fresh"


# ── sort order ───────────────────────────────────────────────────────────────

def test_sort_order_is_severity_then_since_then_name():
    pilots = [
        _pilot(cid=1, name="Zed", industry_jobs=[
            {"activity_id": 1, "blueprint_type_id": 1, "product_type_id": 1, "runs": 1, "status": "ready"}
        ], industry_synced_at=NOW),  # gold
        _pilot(cid=2, name="Amy", needs_reauth=True),  # red, since=None
        _pilot(cid=3, name="Bo", skillqueue=[]),  # red, since=None
        _pilot(cid=4, name="Cy", pi=[_planet(1, -100)]),  # red, since=NOW-100s (known, sorts before None)
        _pilot(cid=5, name="Deb", sync_status="idle", last_synced=None),  # grey
    ]
    items = build_attention(pilots, NOW)
    keys = [i.key for i in items]
    # All reds first, PI-expired (known since) ahead of the two None-since reds,
    # which then break the tie alphabetically by name (Amy before Bo).
    assert keys[0] == "pi_expired:4"
    assert keys[1] == "reauth:2"
    assert keys[2] == "account_idle:3"
    assert keys[3] == "jobs_ready:1"   # gold
    assert keys[4] == "sync_stale:5"   # grey


# ── fingerprints ─────────────────────────────────────────────────────────────

def test_fingerprint_stable_across_identical_calls():
    p1 = _pilot(pi=[_planet(1, -10)])
    p2 = _pilot(pi=[_planet(1, -10)])
    a = build_attention([p1], NOW)[0]
    b = build_attention([p2], NOW)[0]
    assert a.fingerprint == b.fingerprint
    assert a.key == b.key


def test_fingerprint_changes_when_state_changes_but_key_does_not():
    base = build_attention([_pilot(pi=[_planet(1, -10)])], NOW)[0]
    changed = build_attention([_pilot(pi=[_planet(1, -10), _planet(2, -20)])], NOW)[0]
    assert base.key == changed.key
    assert base.fingerprint != changed.fingerprint


def test_fingerprint_survives_age_out_transition():
    """Age-out must change severity/text only, never the fingerprint (or a
    dismissal recorded before the transition would silently stop applying)."""
    fresh = build_attention([_pilot(pi=[_planet(1, -10)])], NOW)[0]
    aged = build_attention([_pilot(pi=[_planet(1, -10)])], NOW + timedelta(days=AGE_OUT_DAYS, hours=1))[0]
    assert fresh.key == aged.key
    assert fresh.fingerprint == aged.fingerprint
    assert fresh.severity != aged.severity


# ── route layer ──────────────────────────────────────────────────────────────

CSRF = "test-csrf-token"


def _cookie(secret_key: str, **data) -> str:
    signer = itsdangerous.TimestampSigner(secret_key)
    payload = base64.b64encode(json.dumps(data).encode())
    return signer.sign(payload).decode()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.fixture
def attn_client():
    import app.main as main
    from app.db.models import Base

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(_create())

    async def _override():
        async with SessionLocal() as s:
            yield s
    from app.db.models import get_db
    main.app.dependency_overrides[get_db] = _override

    client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    client.SessionLocal = SessionLocal
    client.engine = engine

    def login(user_id: int):
        cookie = _cookie(main.settings.secret_key, user_id=user_id, csrf_token=CSRF)
        client.cookies.set("vigilant_session", cookie)
        client.headers.update({"X-CSRF-Token": CSRF})

    def logged_out_with_csrf():
        cookie = _cookie(main.settings.secret_key, csrf_token=CSRF)
        client.cookies.set("vigilant_session", cookie)
        client.headers.update({"X-CSRF-Token": CSRF})

    client.login = login
    client.logged_out_with_csrf = logged_out_with_csrf

    yield client

    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


def _seed_user(client, user_id: int):
    from app.db.models import User

    async def go():
        async with client.SessionLocal() as db:
            db.add(User(id=user_id, role="user"))
            await db.commit()
    _run(go())


def _seed_character(client, *, cid: int, user_id: int, name="Pilot One",
                    scopes="", cache: dict | None = None):
    from app.db.models import Character, CharacterDashboardCache

    async def go():
        async with client.SessionLocal() as db:
            db.add(Character(
                character_id=cid, character_name=name, scopes=scopes,
                access_token="tok", refresh_token="tok",
                token_expiry=datetime.now(timezone.utc), user_id=user_id,
            ))
            if cache is not None:
                db.add(CharacterDashboardCache(character_id=cid, **cache))
            await db.commit()
    _run(go())


SQ_SCOPE = "esi-skills.read_skillqueue.v1"


def _synced_now(scopes: str) -> str:
    """field_synced_json with every field the scopes grant synced just now."""
    from app.dashboard.staleness import granted_fields
    stamp = datetime.now(timezone.utc).isoformat()
    return json.dumps({f: stamp for f in granted_fields(scopes)})


def _fresh(scopes: str = SQ_SCOPE, **extra):
    return {
        "last_synced": datetime.now(timezone.utc).replace(tzinfo=None),
        "field_synced_json": _synced_now(scopes),
        **extra,
    }


def test_get_requires_a_session(attn_client):
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 401
    assert r.text == ""


def test_dismiss_requires_a_session(attn_client):
    attn_client.logged_out_with_csrf()
    r = attn_client.post("/dashboard/attention/dismiss", data={"key": "reauth:1", "for": "24h"})
    assert r.status_code == 401


def test_get_is_empty_when_nothing_needs_attention(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(SQ_SCOPE, skillqueue_json=json.dumps([_sq_entry(30, base=datetime.now(timezone.utc))]),
                                 sync_status="idle"))
    attn_client.login(1)
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 200
    assert r.text == ""


def test_get_renders_an_item(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, name="Sample Pilot",
                    scopes=SQ_SCOPE, cache=_fresh(SQ_SCOPE, skillqueue_json=json.dumps([])))
    attn_client.login(1)
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 200
    assert 'id="dash-attention"' in r.text
    assert "Sample Pilot" in r.text
    assert "no pilot on this account is training" in r.text


def test_more_than_eight_items_render_the_rest_inside_a_details(attn_client):
    """T-077: the dev instance showed 31 items at once. Past
    MAX_VISIBLE_ATTENTION_ITEMS (8), the rest render inside a collapsed
    <details> rather than every row rendering open."""
    from app.routes.dashboard_attention import MAX_VISIBLE_ATTENTION_ITEMS

    _seed_user(attn_client, 1)
    n = MAX_VISIBLE_ATTENTION_ITEMS + 3
    for i in range(n):
        _seed_character(
            attn_client, cid=100 + i, user_id=1, name=f"Pilot {i:02d}", scopes=SQ_SCOPE,
            cache=_fresh(SQ_SCOPE, sync_warnings_json=json.dumps({"wallet": "token_revoked"})),
        )
    attn_client.login(1)
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 200
    # All n rows are present in the markup (native <details> just hides the
    # overflow ones until opened, doesn't remove them from the response).
    assert r.text.count('class="da-row"') == n
    assert "<details" in r.text
    assert f"Show all {n}" in r.text
    assert f"Needs attention · {n}" in r.text


def test_dismissing_an_overflow_item_re_renders_the_details_open(attn_client):
    """T-077: dismissing a row that came from inside the collapsed
    <details> must re-render it OPEN, so clearing several overflow items in
    a row doesn't mean re-opening it after every single dismiss."""
    from app.routes.dashboard_attention import MAX_VISIBLE_ATTENTION_ITEMS

    _seed_user(attn_client, 1)
    n = MAX_VISIBLE_ATTENTION_ITEMS + 3
    for i in range(n):
        _seed_character(
            attn_client, cid=100 + i, user_id=1, name=f"Pilot {i:02d}", scopes=SQ_SCOPE,
            cache=_fresh(SQ_SCOPE, sync_warnings_json=json.dumps({"wallet": "token_revoked"})),
        )
    attn_client.login(1)

    # Pilot 08 (cid 108) sorts into the overflow tail (alphabetically after
    # the first 8: Pilot 00..Pilot 07).
    r = attn_client.post("/dashboard/attention/dismiss",
                         data={"key": "reauth:108", "for": "24h", "expanded": "1"})
    assert r.status_code == 200
    assert "<details class=\"da-more\" open>" in r.text

    # Without the flag (e.g. a dismiss from one of the first 8), it stays closed.
    r2 = attn_client.post("/dashboard/attention/dismiss",
                          data={"key": "reauth:100", "for": "24h"})
    assert r2.status_code == 200
    assert "<details class=\"da-more\" open>" not in r2.text
    assert "<details class=\"da-more\">" in r2.text


def test_dismiss_unknown_key_is_refused(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)
    r = attn_client.post("/dashboard/attention/dismiss",
                         data={"key": "account_idle:999999", "for": "24h"})
    assert r.status_code == 404


def test_dismiss_another_users_key_is_refused(attn_client):
    _seed_user(attn_client, 1)
    _seed_user(attn_client, 2)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    _seed_character(attn_client, cid=200, user_id=2, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)
    r = attn_client.post("/dashboard/attention/dismiss",
                         data={"key": "account_idle:200", "for": "24h"})
    assert r.status_code == 404


def test_dismiss_invalid_duration_is_rejected(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)
    r = attn_client.post("/dashboard/attention/dismiss",
                         data={"key": "account_idle:100", "for": "next-tuesday"})
    assert r.status_code == 400


def test_dismiss_24h_hides_then_lapses(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)

    assert "no pilot on this account is training" in attn_client.get("/dashboard/attention").text

    r = attn_client.post("/dashboard/attention/dismiss",
                         data={"key": "account_idle:100", "for": "24h"})
    assert r.status_code == 200
    assert "no pilot on this account is training" not in r.text

    r2 = attn_client.get("/dashboard/attention")
    assert r2.status_code == 200
    assert r2.text == ""

    # Force the dismissal into the past — it should lapse and the item come back.
    from app.db.models import DashboardAttentionDismissal

    async def expire_it():
        async with attn_client.SessionLocal() as db:
            row = (await db.execute(select(DashboardAttentionDismissal))).scalar_one()
            row.dismissed_until = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            await db.commit()
    _run(expire_it())

    r3 = attn_client.get("/dashboard/attention")
    assert "no pilot on this account is training" in r3.text

    async def count_rows():
        async with attn_client.SessionLocal() as db:
            return (await db.execute(select(DashboardAttentionDismissal))).scalars().all()
    # The lapsed row was purged, not just ignored.
    assert _run(count_rows()) == []


def test_dismiss_7d_hides_for_a_week(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)
    attn_client.post("/dashboard/attention/dismiss", data={"key": "account_idle:100", "for": "7d"})

    from app.db.models import DashboardAttentionDismissal

    async def get_row():
        async with attn_client.SessionLocal() as db:
            return (await db.execute(select(DashboardAttentionDismissal))).scalar_one()
    row = _run(get_row())
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert row.dismissed_until > naive_now + timedelta(days=6, hours=23)
    assert row.dismissed_until < naive_now + timedelta(days=7, hours=1)

    assert attn_client.get("/dashboard/attention").text == ""


def test_dismiss_until_change_lapses_when_fingerprint_changes(attn_client):
    _seed_user(attn_client, 1)
    queue_end = datetime.now(timezone.utc) + timedelta(days=3)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([{
                        "skill_id": 1, "finished_level": 1,
                        "start_date": (queue_end - timedelta(days=1)).isoformat(),
                        "finish_date": queue_end.isoformat(),
                    }])))
    attn_client.login(1)

    r = attn_client.get("/dashboard/attention")
    assert "skill queue ends in" in r.text

    dismiss = attn_client.post("/dashboard/attention/dismiss",
                              data={"key": "queue_critical:100", "for": "change"})
    assert dismiss.status_code == 200
    assert attn_client.get("/dashboard/attention").text == ""

    # Change the underlying state (requeue further out) — same signal, new
    # fingerprint. The "until it changes" dismissal must lapse.
    new_end = datetime.now(timezone.utc) + timedelta(days=3, hours=2)

    async def requeue():
        from app.db.models import CharacterDashboardCache
        async with attn_client.SessionLocal() as db:
            cache = (await db.execute(
                select(CharacterDashboardCache).where(CharacterDashboardCache.character_id == 100)
            )).scalar_one()
            cache.skillqueue_json = json.dumps([{
                "skill_id": 1, "finished_level": 1,
                "start_date": (new_end - timedelta(days=1)).isoformat(),
                "finish_date": new_end.isoformat(),
            }])
            await db.commit()
    _run(requeue())

    r2 = attn_client.get("/dashboard/attention")
    assert "skill queue ends in" in r2.text

    from app.db.models import DashboardAttentionDismissal

    async def count_rows():
        async with attn_client.SessionLocal() as db:
            return (await db.execute(select(DashboardAttentionDismissal))).scalars().all()
    assert _run(count_rows()) == []


def test_purge_on_character_removal_deletes_the_dismissal(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=SQ_SCOPE,
                    cache=_fresh(skillqueue_json=json.dumps([])))
    attn_client.login(1)
    attn_client.post("/dashboard/attention/dismiss", data={"key": "account_idle:100", "for": "7d"})

    from app.auth.purge import purge_character_user_rows
    from app.db.models import DashboardAttentionDismissal

    async def check_and_purge():
        async with attn_client.SessionLocal() as db:
            before = (await db.execute(select(DashboardAttentionDismissal))).scalars().all()
            assert len(before) == 1
            deleted = await purge_character_user_rows(db, 100)
            await db.commit()
            after = (await db.execute(select(DashboardAttentionDismissal))).scalars().all()
            return deleted, after
    deleted, after = _run(check_and_purge())
    assert deleted == 1
    assert after == []


# ── v1.7.1: ISS-069 no-permissions / per-field staleness, ISS-062 banners ────

def test_route_no_permissions_pilot_gets_the_permissions_item(attn_client):
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, name="Test Alt", scopes="",
                    cache={"last_synced": datetime.now(timezone.utc).replace(tzinfo=None)})
    attn_client.login(1)
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 200
    assert "shares no permissions" in r.text
    assert "/account/permissions/100" in r.text
    assert "stale" not in r.text and "never synced" not in r.text


def test_route_hourly_only_pilot_is_not_stale_at_45_minutes(attn_client):
    now = datetime.now(timezone.utc)
    scope = "esi-clones.read_clones.v1"
    fs = json.dumps({"clones": (now - timedelta(minutes=45)).isoformat(),
                     "zkill": (now - timedelta(minutes=45)).isoformat()})
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes=scope,
                    cache={"last_synced": (now - timedelta(minutes=45)).replace(tzinfo=None),
                           "field_synced_json": fs})
    attn_client.login(1)
    r = attn_client.get("/dashboard/attention")
    assert r.status_code == 200 and r.text == ""


def test_route_structure_banners_drop_old_alerts(attn_client):
    from app.routes import dashboard as dash_mod
    dash_mod._structure_banner_cache.clear()
    now = datetime.now(timezone.utc)

    def notif(nid, ntype, age):
        return {"notification_id": nid, "type": ntype, "text": "",
                "timestamp": (now - age).isoformat().replace("+00:00", "Z")}

    notifs = {"notifications": [
        notif(1, "TowerAlertMsg", timedelta(days=18)),      # too old
        notif(2, "StructureUnderAttack", timedelta(hours=1)),  # fresh
        notif(3, "StructureFuelAlert", timedelta(days=3)),  # kept (7 d)
        notif(4, "StructureFuelAlert", timedelta(days=9)),  # too old
    ]}
    _seed_user(attn_client, 1)
    _seed_character(attn_client, cid=100, user_id=1, scopes="",
                    cache={"notifications_json": json.dumps(notifs)})
    attn_client.login(1)
    r = attn_client.get("/alerts/structure-banners")
    assert r.status_code == 200
    labels = dash_mod._STRUCTURE_ALERT_LABELS
    assert labels["StructureUnderAttack"] in r.text
    assert labels["StructureFuelAlert"] in r.text
    assert labels["TowerAlertMsg"] not in r.text
    assert r.text.count(labels["StructureFuelAlert"]) == 1
    dash_mod._structure_banner_cache.clear()
