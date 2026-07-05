"""Tests for Phase 5 Task 2 — entity combat stats from the local killmail
archive (`app/intel/entity_stats.py`).

Two concerns:

1. Stat math on a seeded fixture set — kills/losses/solo/danger, the
   COUNT(DISTINCT killmail_id) dedup (two same-corp attackers on one killmail
   count as ONE kill), self-victim exclusion (awox is a loss, not a kill), and
   window exclusion (a kill outside the window doesn't count). Exercised across
   all three kinds (character / corporation / alliance) since the
   kind→column mapping is the new surface.

2. Window enforcement (the BUG-4 lesson) — every query BUILDER must emit a
   `killmail_time >=` lower bound. Asserted on the compiled SQL of each builder
   for every kind.

Sync-style harness (no pytest-asyncio): one manually-managed event loop + a
temp file sqlite DB, per tests/test_kill_streaks.py. entity_stats imports
AsyncSessionLocal by name, so we monkeypatch the name on the module.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Killmail, KillmailAttacker
import app.intel.entity_stats as entity_stats


NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _km(kid, days_ago, victim_char=None, victim_corp=None, victim_ally=None,
        attacker_count=1, system_id=30000142, ship=587):
    return Killmail(
        killmail_id=kid,
        killmail_hash=f"h{kid}",
        killmail_time=NOW - timedelta(days=days_ago),
        solar_system_id=system_id,
        victim_ship_type_id=ship,
        victim_character_id=victim_char,
        victim_corporation_id=victim_corp,
        victim_alliance_id=victim_ally,
        attacker_count=attacker_count,
    )


def _att(kid, char=None, corp=None, ally=None, ship=None):
    return KillmailAttacker(
        killmail_id=kid, character_id=char, corporation_id=corp,
        alliance_id=ally, ship_type_id=ship,
    )


# Fixture entities
CHAR, OTHER_CHAR = 100, 199
CORP, CORP2, OTHER_CORP = 200, 201, 299
ALLY, OTHER_ALLY = 300, 399


def _seed(db):
    # ── character CHAR ──────────────────────────────────────────────────
    # km1: solo kill (attacker_count=1), ship 587, sys 30000142, in-window
    db.add(_km(1, 1, victim_char=OTHER_CHAR, attacker_count=1, ship=587, system_id=30000142))
    db.add(_att(1, char=CHAR, ship=587))
    # km2: gang kill (attacker_count=3), ship 588, sys 30000142, in-window
    db.add(_km(2, 2, victim_char=OTHER_CHAR, attacker_count=3, ship=588, system_id=30000142))
    db.add(_att(2, char=CHAR, ship=588))
    # km3: loss, sys 30000143, in-window
    db.add(_km(3, 3, victim_char=CHAR, system_id=30000143))
    # km4: kill OUTSIDE the 90d window (120d ago) — must be excluded
    db.add(_km(4, 120, victim_char=OTHER_CHAR, attacker_count=1, ship=587))
    db.add(_att(4, char=CHAR, ship=587))

    # ── corporation CORP — dedup: two members on one killmail ───────────
    db.add(_km(5, 1, victim_corp=OTHER_CORP, attacker_count=2))
    db.add(_att(5, char=1001, corp=CORP))
    db.add(_att(5, char=1002, corp=CORP))  # same corp, same km → 1 kill

    # ── corporation CORP2 — awox: self-victim excluded from kills ───────
    db.add(_km(6, 1, victim_corp=CORP2, attacker_count=1))
    db.add(_att(6, char=2001, corp=CORP2))

    # ── alliance ALLY — one kill (solo) + one loss ──────────────────────
    db.add(_km(7, 1, victim_ally=OTHER_ALLY, attacker_count=1))
    db.add(_att(7, char=3001, ally=ALLY))
    db.add(_km(8, 2, victim_ally=ALLY, attacker_count=1))


def _run(coro_factory):
    """coro_factory(db_session_local) -> awaitable computed after seeding."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    orig = entity_stats.AsyncSessionLocal
    entity_stats.AsyncSessionLocal = SessionLocal

    async def _scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            _seed(db)
            await db.commit()
        return await coro_factory()

    try:
        return loop.run_until_complete(_scenario())
    finally:
        loop.run_until_complete(engine.dispose())
        entity_stats.AsyncSessionLocal = orig
        loop.close()
        os.unlink(tmp.name)


# ── Math correctness ────────────────────────────────────────────────────────

def test_character_summary_math():
    res = _run(lambda: entity_stats.entity_summary("character", CHAR, days=90))
    assert res["kills"] == 2, res       # km1 + km2 (km4 out of window)
    assert res["losses"] == 1, res      # km3
    assert res["solo"] == 1, res        # km1 only (attacker_count==1)
    assert abs(res["solo_ratio"] - 0.5) < 1e-9, res
    assert abs(res["danger"] - (2 / 3)) < 1e-9, res


def test_corp_dedup_counts_killmail_once():
    """Two same-corp attackers on one killmail must count as a single kill."""
    res = _run(lambda: entity_stats.entity_summary("corporation", CORP, days=90))
    assert res["kills"] == 1, res
    assert res["losses"] == 0, res
    assert res["solo"] == 0, res        # attacker_count == 2
    assert abs(res["danger"] - 1.0) < 1e-9, res


def test_awox_self_victim_excluded_from_kills():
    """CORP2 is both victim and attacker on km6 → a loss, never a kill."""
    res = _run(lambda: entity_stats.entity_summary("corporation", CORP2, days=90))
    assert res["kills"] == 0, res
    assert res["losses"] == 1, res
    assert abs(res["danger"] - 0.0) < 1e-9, res


def test_alliance_summary_math():
    res = _run(lambda: entity_stats.entity_summary("alliance", ALLY, days=90))
    assert res["kills"] == 1, res
    assert res["losses"] == 1, res
    assert res["solo"] == 1, res
    assert abs(res["danger"] - 0.5) < 1e-9, res


def test_empty_entity_no_div_by_zero():
    res = _run(lambda: entity_stats.entity_summary("character", 424242, days=90))
    assert res == {"kills": 0, "losses": 0, "solo": 0, "solo_ratio": 0.0, "danger": 0.0}, res


def test_window_excludes_old_kill():
    """km4 (120d ago) counts at 90d? no — at a wide-open... we only allow
    7/30/90, so km4 never counts. But a 7d window drops km2/km3 too."""
    res = _run(lambda: entity_stats.entity_summary("character", CHAR, days=7))
    assert res["kills"] == 2 and res["losses"] == 1, res  # all in last 3 days
    # tighten conceptually: everything seeded for CHAR in-window is <=3d old.


def test_top_ships():
    rows = _run(lambda: entity_stats.entity_top_ships("character", CHAR, days=90, limit=5))
    got = {r["ship_type_id"]: r["count"] for r in rows}
    assert got == {587: 1, 588: 1}, got  # km4's 587 excluded by window


def test_top_systems_combines_kills_and_losses():
    rows = _run(lambda: entity_stats.entity_top_systems("character", CHAR, days=90, limit=5))
    got = {r["system_id"]: r["count"] for r in rows}
    assert got == {30000142: 2, 30000143: 1}, got  # km1+km2 vs km3


def test_heatmap_total_activity():
    cells = _run(lambda: entity_stats.entity_heatmap("character", CHAR, days=90))
    total = sum(c["count"] for c in cells)
    assert total == 3, cells  # km1 + km2 + km3, km4 excluded
    for c in cells:
        assert 0 <= c["dow"] <= 6 and 0 <= c["hour"] <= 23, c


# ── Window enforcement (BUG-4): every builder carries killmail_time >= ───────

def test_every_builder_has_time_bound():
    cutoff = datetime(2020, 1, 1)
    for kind in entity_stats.VALID_KINDS:
        builders = [
            entity_stats.kills_solo_query(kind, 1, cutoff),
            entity_stats.losses_query(kind, 1, cutoff),
            entity_stats.heatmap_kills_query(kind, 1, cutoff),
            entity_stats.heatmap_losses_query(kind, 1, cutoff),
            entity_stats.top_ships_query(kind, 1, cutoff, 5),
            entity_stats.top_systems_kills_query(kind, 1, cutoff),
            entity_stats.top_systems_losses_query(kind, 1, cutoff),
        ]
        for q in builders:
            sql = str(q)
            assert "killmail_time >=" in sql, (kind, sql)


def test_invalid_kind_rejected():
    import pytest
    with pytest.raises(ValueError):
        entity_stats.losses_query("corp", 1, datetime(2020, 1, 1))
