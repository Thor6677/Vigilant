"""v1.7.1 Dashboard freshness and attention: ISS-069 (per-pilot staleness and
the "no permissions" state), ISS-064 (job_id/end_date in the industry trim),
ISS-062 (age limits on structure alert banners). Pure and template-render
tests, no database."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.auth import scopes as perms
from app.dashboard import attention as attn
from app.dashboard import staleness as st
from app.dashboard.attention import build_attention
from app.dashboard.detail import industry_detail
from app.dashboard.summary import build_pilot_summaries
from app.dashboard.table import build_table_row
from app.networth.snapshot import value_industry_jobs
from app.routes import dashboard as dash_mod
from tests._dashboard_fixture import CHARACTERS, build_context, render_full

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
MIN = 60


def _synced(ages: dict[str, float], now: datetime = NOW) -> str:
    return json.dumps({f: (now - timedelta(seconds=a)).isoformat() for f, a in ages.items()})


def _all_granted(scopes: str, age: float) -> str:
    return _synced({f: age for f in st.granted_fields(scopes)})


HOURLY_SCOPES = perms.CLONES            # granted: clones (1h) + zkill (1h)
FAST_SCOPES = perms.WALLET              # granted: wallet (2m), transactions (1h), zkill (1h)


# ── ISS-069: the shared helper ───────────────────────────────────────────────

def test_hourly_only_pilot_is_fresh_at_45_minutes():
    fs = _all_granted(HOURLY_SCOPES, 45 * MIN)
    assert st.staleness(NOW, HOURLY_SCOPES, fs) == "fresh"


def test_hourly_only_pilot_boundaries():
    window = 3600
    assert st.staleness(NOW, HOURLY_SCOPES, _all_granted(HOURLY_SCOPES, window + 900)) == "fresh"
    assert st.staleness(NOW, HOURLY_SCOPES, _all_granted(HOURLY_SCOPES, window + 901)) == "warning"
    assert st.staleness(NOW, HOURLY_SCOPES, _all_granted(HOURLY_SCOPES, window + 1800)) == "warning"
    assert st.staleness(NOW, HOURLY_SCOPES, _all_granted(HOURLY_SCOPES, window + 1801)) == "critical"


def test_one_overdue_granted_hourly_field_makes_the_pilot_critical():
    scopes = perms.WALLET + " " + perms.CLONES
    ages = {f: 30 for f in st.granted_fields(scopes)}
    ages["clones"] = 3600 + 31 * MIN
    assert st.staleness(NOW, scopes, _synced(ages)) == "critical"


def test_fast_field_pilot_behaves_as_before():
    # wallet has a 120 s window: 120 s + 15 min grace is still fresh.
    ages = {f: 10 for f in st.granted_fields(FAST_SCOPES)}
    ages["wallet"] = 120 + 900
    assert st.staleness(NOW, FAST_SCOPES, _synced(ages)) == "fresh"
    ages["wallet"] = 120 + 901
    assert st.staleness(NOW, FAST_SCOPES, _synced(ages)) == "warning"
    ages["wallet"] = 120 + 1801
    assert st.staleness(NOW, FAST_SCOPES, _synced(ages)) == "critical"


def test_ungranted_field_does_not_count():
    # No wallet scope: an ancient "wallet" entry must be ignored.
    ages = {f: 10 for f in st.granted_fields(HOURLY_SCOPES)}
    ages["wallet"] = 10 * 86400
    assert st.staleness(NOW, HOURLY_SCOPES, _synced(ages)) == "fresh"


def test_never_synced_granted_field_is_never():
    ages = {f: 10 for f in st.granted_fields(FAST_SCOPES)}
    del ages["transactions"]
    assert st.staleness(NOW, FAST_SCOPES, _synced(ages)) == "never"
    assert st.staleness(NOW, FAST_SCOPES, None) == "never"
    assert st.staleness(NOW, FAST_SCOPES, "not json") == "never"


def test_has_permissions():
    assert not st.has_permissions("")
    assert not st.has_permissions(None)
    assert not st.has_permissions("   ")
    assert st.has_permissions(perms.WALLET)


def test_route_and_attention_give_identical_answers():
    """The two former copies now share one implementation."""
    for scopes, age in [(HOURLY_SCOPES, 45 * MIN), (HOURLY_SCOPES, 3600 + 1801),
                        (FAST_SCOPES, 5), (FAST_SCOPES, 3600 + 901), ("", 5)]:
        char = SimpleNamespace(scopes=scopes)
        fs = _all_granted(scopes, age)
        cache = SimpleNamespace(field_synced_json=fs)
        route_answer = dash_mod._staleness(char, cache, NOW)
        attn_answer = attn._pilot_staleness(
            {"staleness": st.staleness(NOW, scopes, fs)}, NOW)
        assert route_answer == attn_answer == st.staleness(NOW, scopes, fs)
    assert dash_mod._staleness(SimpleNamespace(scopes=FAST_SCOPES), None, NOW) == "never"


def test_reexports_and_constants_are_shared():
    assert dash_mod.FIELD_CACHE_SECONDS is st.FIELD_CACHE_SECONDS
    assert dash_mod.FIELD_SCOPES is st.FIELD_SCOPES
    assert dash_mod.STALE_WARNING_SECONDS == attn.STALE_WARNING_SECONDS == st.STALE_WARNING_SECONDS
    assert dash_mod.STALE_CRITICAL_SECONDS == attn.STALE_CRITICAL_SECONDS == st.STALE_CRITICAL_SECONDS


def test_scheduler_any_field_stale_is_unchanged():
    char = SimpleNamespace(scopes=HOURLY_SCOPES)
    # _any_field_stale reads the real clock, so anchor the fixtures to it.
    now = datetime.now(timezone.utc)

    def cache(minutes):
        return SimpleNamespace(field_synced_json=_all_granted_at(HOURLY_SCOPES, minutes, now))

    assert dash_mod._any_field_stale(char, cache(45)) is False
    assert dash_mod._any_field_stale(char, cache(61)) is True
    assert dash_mod._any_field_stale(char, None) is True


def _all_granted_at(scopes, minutes, now):
    return json.dumps({f: (now - timedelta(minutes=minutes)).isoformat()
                       for f in st.granted_fields(scopes)})


# ── ISS-069: attention items ─────────────────────────────────────────────────

def _pilot(cid=1, name="Pilot A", **kw):
    base = {
        "character_id": cid, "character_name": name, "account_group": None,
        "needs_reauth": False, "skillqueue": None, "pi": None,
        "industry_jobs": None, "industry_synced_at": None,
        "sync_status": "idle", "sync_error": None, "last_synced": NOW,
    }
    base.update(kw)
    return base


def test_attention_uses_the_precomputed_staleness():
    # last_synced is 5 h old, which the age-only rule calls critical, but the
    # route judged the pilot fresh against its own hourly cadence.
    p = _pilot(last_synced=NOW - timedelta(hours=5), staleness="fresh")
    assert build_attention([p], NOW) == []
    p = _pilot(last_synced=NOW, staleness="critical")
    assert [i.key for i in build_attention([p], NOW)] == ["sync_stale:1"]


def test_no_perms_pilot_gets_a_grey_permissions_item_and_no_stale_item():
    p = _pilot(cid=7, name="Test Alt", no_perms=True, staleness="never",
               last_synced=None, sync_status="error", sync_error="boom")
    items = build_attention([p], NOW)
    assert [i.key for i in items] == ["no_perms:7"]
    it = items[0]
    assert it.severity == "grey"
    assert it.text == "shares no permissions"
    assert (it.action_label, it.action_url, it.action_method) == (
        "Permissions", "/account/permissions/7", "get")
    assert it.character_id == 7


def test_no_perms_items_are_not_folded_into_the_stale_collapse():
    pilots = [_pilot(cid=i, name=f"Pilot {i}", no_perms=True) for i in range(1, 6)]
    pilots += [_pilot(cid=10 + i, name=f"Stale {i}", staleness="critical") for i in range(3)]
    keys = sorted(i.key for i in build_attention(pilots, NOW))
    assert keys == sorted([f"no_perms:{i}" for i in range(1, 6)] + ["sync_stale:many"])


def test_no_perms_fingerprint_is_stable_for_dismissals():
    a = build_attention([_pilot(no_perms=True)], NOW)[0]
    b = build_attention([_pilot(no_perms=True)], NOW + timedelta(days=30))[0]
    assert a.fingerprint == b.fingerprint


# ── ISS-069: "no permissions" in every Dashboard mode ────────────────────────

NP_ID = CHARACTERS[0].character_id


def _np_summaries(chars=CHARACTERS):
    ctx = build_context("custom")
    no_perms = {NP_ID: True}
    summaries = build_pilot_summaries(
        chars, ctx["wallets"], ctx["locations"], ctx["clones"], ctx["skill_map"],
        ctx["sync_statuses"], ctx["staleness"], ctx["last_synced_strs"],
        ctx["needs_reauth"], {}, {}, ctx["char_groups"], no_perms=no_perms,
    )
    return summaries, no_perms


def test_summary_flags_no_permissions_instead_of_stale_flags():
    summaries, _ = _np_summaries()
    s = summaries[NP_ID]
    assert s["sync"]["no_perms"] is True
    assert {"label": "NO PERMISSIONS", "sev": "grey"} in s["flags"]
    assert not any(f["label"] in ("SYNC STALE", "NEVER SYNCED", "SYNC ERROR") for f in s["flags"])
    # Default (no map) is unchanged for everyone else.
    assert summaries[CHARACTERS[1].character_id]["sync"]["no_perms"] is False


def test_cards_and_detailed_show_no_permissions_link_and_no_sync_button():
    summaries, no_perms = _np_summaries()
    for mode in ("cards", "detailed"):
        html = render_full("name", dash_mode=mode, pilot_summaries=summaries, no_perms=no_perms)
        assert f'href="/account/permissions/{NP_ID}"' in html
        assert ">no permissions</a>" in html
        assert f'action="/dashboard/sync/{NP_ID}"' not in html
        # Another pilot in a stale state still gets its button (control).
        assert f'action="/dashboard/sync/{CHARACTERS[1].character_id}"' in html


def test_compact_shows_the_no_permissions_flag():
    summaries, no_perms = _np_summaries()
    html = render_full("name", dash_mode="compact", pilot_summaries=summaries, no_perms=no_perms)
    assert "NO PERMISSIONS" in html


def test_table_last_sync_cell_links_to_permissions():
    from app.dashboard import prefs as prefs_mod
    summaries, _ = _np_summaries()
    row = build_table_row(summaries[NP_ID], None, None, None, None, no_perms=True)
    assert row["cells"]["last_sync"]["text"] == "no permissions"
    assert row["cells"]["last_sync"]["href"] == f"/account/permissions/{NP_ID}"
    html = render_full(
        "custom", dash_mode="table", TABLE_COLUMNS=prefs_mod.TABLE_COLUMNS, table_rows=[row],
        prefs_patch={"table_columns": list(prefs_mod.TABLE_COLUMNS)},
    )
    assert f'<a href="/account/permissions/{NP_ID}"' in html
    assert "no permissions</a>" in html


def test_pilots_with_permissions_render_exactly_as_before():
    html = render_full("name", dash_mode="cards")
    assert "no permissions" not in html
    assert "NO PERMISSIONS" not in html


# ── ISS-064: job_id / end_date ───────────────────────────────────────────────

def _job(job_id=None, status="active", end_in=None, **kw):
    j = {"activity_id": 1, "blueprint_type_id": 10, "product_type_id": 20,
         "runs": 2, "status": status}
    if job_id is not None:
        j["job_id"] = job_id
    if end_in is not None:
        j["end_date"] = (NOW + timedelta(seconds=end_in)).isoformat().replace("+00:00", "Z")
    j.update(kw)
    return j


def _jobs_item(jobs, synced=NOW - timedelta(minutes=10)):
    items = build_attention([_pilot(industry_jobs=jobs, industry_synced_at=synced)], NOW)
    return next((i for i in items if i.key == "jobs_ready:1"), None)


def test_fetch_industry_jobs_data_keeps_job_id_and_end_date(monkeypatch):
    raw = [
        {"job_id": 501, "activity_id": 1, "blueprint_type_id": 10, "product_type_id": 20,
         "runs": 3, "status": "active", "end_date": "2026-09-27T13:00:00Z", "cost": 5.0},
        {"job_id": 502, "status": "delivered", "runs": 1},
    ]

    async def fake_jobs(client, cid, include_completed=False):
        return raw

    async def fake_client(char):
        return object(), None

    async def fake_persist(db, cid, jobs):
        return 0

    import app.esi.industry as esi_industry
    monkeypatch.setattr(esi_industry, "get_character_jobs", fake_jobs)
    monkeypatch.setattr(dash_mod, "_client_for", fake_client)
    monkeypatch.setattr(dash_mod, "_persist_completed_jobs", fake_persist)
    char = SimpleNamespace(character_id=42, scopes="esi-industry.read_character_jobs.v1")
    out = asyncio.run(dash_mod.fetch_industry_jobs_data([char], None))
    trimmed, warn = out[42]
    assert warn is None
    assert trimmed == [{
        "activity_id": 1, "blueprint_type_id": 10, "product_type_id": 20, "runs": 3,
        "status": "active", "job_id": 501, "end_date": "2026-09-27T13:00:00Z",
    }]


def test_jobs_ready_fingerprint_uses_sorted_job_ids_and_earliest_end_date():
    a = _jobs_item([_job(2, "ready", end_in=-3600), _job(1, "ready", end_in=-7200)])
    b = _jobs_item([_job(1, "ready", end_in=-7200), _job(2, "ready", end_in=-3600)])
    assert a.fingerprint == b.fingerprint
    assert a.since == NOW - timedelta(hours=2)
    c = _jobs_item([_job(1, "ready", end_in=-7200), _job(3, "ready", end_in=-60)])
    assert c.fingerprint != a.fingerprint


def test_fingerprint_ignores_run_count_changes_but_not_job_identity():
    a = _jobs_item([_job(1, "ready", end_in=-60, runs=2)])
    b = _jobs_item([_job(1, "ready", end_in=-60, runs=9)])
    assert a.fingerprint == b.fingerprint


def test_active_job_past_its_end_date_counts_as_ready():
    it = _jobs_item([_job(5, "active", end_in=-90)])
    assert it is not None
    assert it.severity == "gold"
    assert it.since == NOW - timedelta(seconds=90)
    assert _jobs_item([_job(5, "active", end_in=+90)]) is None
    # Exactly at end_date counts as finished.
    assert _jobs_item([_job(5, "active", end_in=0)]) is not None


def test_paused_job_past_end_date_is_not_ready():
    assert _jobs_item([_job(5, "paused", end_in=-90)]) is None


def test_ready_job_ages_to_grey_from_end_date_not_sync_time():
    it = _jobs_item([_job(9, "ready", end_in=-9 * 86400)], synced=NOW)
    assert it.severity == "grey"
    assert "9d ago" in it.text


def test_old_cache_rows_without_job_id_fall_back_to_status_only():
    synced = NOW - timedelta(minutes=20)
    legacy = [_job(None, "ready"), _job(None, "active")]
    it = _jobs_item(legacy, synced=synced)
    assert it is not None
    assert it.since == synced
    # The legacy fingerprint is the pre-change tuple hash...
    expected = attn._fp("ready", "1:10:20:2")
    assert it.fingerprint == expected
    # ...and an active job without end_date is never treated as ready.
    assert _jobs_item([_job(None, "active")], synced=synced) is None


def test_new_fingerprint_differs_from_the_legacy_one():
    """Release note: each dismissed "jobs ready" item comes back once."""
    legacy = _jobs_item([_job(None, "ready")])
    new = _jobs_item([_job(77, "ready", end_in=-60)])
    assert legacy.fingerprint != new.fingerprint


def test_mixed_rows_fall_back_to_legacy():
    it = _jobs_item([_job(1, "ready", end_in=-60), _job(None, "ready")])
    assert it.fingerprint == attn._fp("ready", "1:10:20:2", "1:10:20:2")


def test_industry_detail_counts_finished_active_jobs_as_ready():
    jobs = [_job(1, "active", end_in=-5), _job(2, "active", end_in=500), _job(3, "ready")]
    assert industry_detail(jobs) == {"active": 2, "ready": 1}
    assert industry_detail(jobs, NOW) == {"active": 1, "ready": 2}
    # Legacy rows (no end_date) behave as before with or without `now`.
    legacy = [_job(None, "active"), _job(None, "ready")]
    assert industry_detail(legacy, NOW) == {"active": 1, "ready": 1}


def test_networth_valuation_ignores_the_new_keys():
    jobs = [_job(1, "active", end_in=100), _job(None, "active")]
    assert value_industry_jobs(jobs, {20: 10.0}, {10: 1}) == 40.0


# ── ISS-062: structure banner age limits ─────────────────────────────────────

def _notif(ntype, age: timedelta, text="", ts_now=NOW):
    return {"type": ntype, "notification_id": 1, "text": text,
            "timestamp": (ts_now - age).isoformat().replace("+00:00", "Z")}


LIVE = dash_mod._structure_banner_live


@pytest.mark.parametrize("ntype", sorted(dash_mod._BANNER_ATTACK_TYPES))
def test_attack_banners_drop_after_48h(ntype):
    assert LIVE(ntype, _notif(ntype, timedelta(hours=47)), NOW)
    assert LIVE(ntype, _notif(ntype, timedelta(hours=48)), NOW)
    assert not LIVE(ntype, _notif(ntype, timedelta(hours=48, minutes=1)), NOW)
    assert not LIVE(ntype, _notif(ntype, timedelta(days=18)), NOW)


@pytest.mark.parametrize("ntype", sorted(dash_mod._BANNER_FUEL_TYPES))
def test_fuel_banners_drop_after_7_days(ntype):
    assert LIVE(ntype, _notif(ntype, timedelta(days=6, hours=23)), NOW)
    assert not LIVE(ntype, _notif(ntype, timedelta(days=7, minutes=1)), NOW)


def _ticks(td: timedelta) -> int:
    return int(td.total_seconds() * 10_000_000)


@pytest.mark.parametrize("ntype", sorted(dash_mod._BANNER_REINFORCE_TYPES))
def test_reinforce_banner_lives_until_its_timer_ends(ntype):
    text = f"solarsystemID: 1\ntimeLeft: {_ticks(timedelta(days=3))}\n"
    # 2 days after the notification the timer (3 days) is still ahead: kept,
    # although a plain 48 h cutoff would already have dropped it.
    assert LIVE(ntype, _notif(ntype, timedelta(days=2, hours=1), text), NOW)
    assert LIVE(ntype, _notif(ntype, timedelta(days=3, hours=1), text), NOW)  # inside the grace
    assert not LIVE(ntype, _notif(ntype, timedelta(days=3, hours=3), text), NOW)


def test_reinforce_banner_without_a_parseable_timer_uses_48h():
    ntype = "StructureLostShields"
    for text in ("", "timeLeft: soon\n", "timeLeft: 0\n", "timeLeft: -5\n", "solarsystemID: 1\n"):
        assert LIVE(ntype, _notif(ntype, timedelta(hours=47), text), NOW)
        assert not LIVE(ntype, _notif(ntype, timedelta(hours=49), text), NOW)


def test_reinforce_timer_as_absolute_filetime():
    ntype = "StructureLostArmor"
    end = NOW + timedelta(hours=6)
    ticks = int((end - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
    n = _notif(ntype, timedelta(days=5), f"timeLeft: {ticks}\n")
    assert LIVE(ntype, n, NOW)
    assert not LIVE(ntype, n, NOW + timedelta(hours=9))


def test_unparseable_timestamp_is_dropped():
    assert not LIVE("StructureUnderAttack", {"type": "StructureUnderAttack", "timestamp": "??"}, NOW)
    assert not LIVE("StructureUnderAttack", {"type": "StructureUnderAttack"}, NOW)


def test_banner_cutoffs_are_named_constants():
    assert dash_mod._BANNER_ATTACK_MAX_AGE == timedelta(hours=48)
    assert dash_mod._BANNER_FUEL_MAX_AGE == timedelta(days=7)
    assert dash_mod._BANNER_REINFORCE_FALLBACK_AGE == timedelta(hours=48)
