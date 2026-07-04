"""Tests for BUG-3: streaks() bounded to the most-recent STREAKS_MAX_ROWS
involvements.

The old streaks() selected EVERY (killmail_time, victim_character_id) row for a
character (victim OR attacker), ordered ascending, then walked them in Python —
an unbounded scan of the ~60M-row killmails table on every character-detail page
load. The fix fetches the newest STREAKS_MAX_ROWS rows (ORDER BY time DESC LIMIT)
and reverses them so the streak walk still sees ascending chronological order.

Two tests:
1. Correctness — a hand-computed win/loss sequence yields the expected
   current_win / longest_win / days_since_loss.
2. Bounding — with STREAKS_MAX_ROWS monkeypatched to 3, a history whose full
   answer differs from its last-3 window returns the windowed answer, proving
   both that the LIMIT applies and that the reverse keeps chronological order.

Sync-style (no pytest-asyncio): a single manually-managed event loop and a temp
*file* sqlite DB, per tests/test_sync_field_sessions.py. kill_queries imports
AsyncSessionLocal by name from app.db.models, so we monkeypatch the name on the
kill_queries module.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Killmail, KillmailAttacker
import app.intel.kill_queries as kill_queries


CHAR_ID = 90000042
OTHER_ID = 90000999


def _km(kid: int, when: datetime, victim_character_id: int | None) -> Killmail:
    """Minimal Killmail row — only NOT-NULL columns without a default are set."""
    return Killmail(
        killmail_id=kid,
        killmail_hash=f"hash{kid}",
        killmail_time=when,
        solar_system_id=30000142,
        victim_ship_type_id=587,
        victim_character_id=victim_character_id,
    )


def _run_streaks(seed_events, max_rows=None):
    """seed_events: list of ("W"|"L", datetime). W = char is an attacker on a kill
    of OTHER; L = char is the victim. Returns the streaks() result dict."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    kill_queries.AsyncSessionLocal = SessionLocal  # monkeypatch (restored below)
    if max_rows is not None:
        orig_max = kill_queries.STREAKS_MAX_ROWS
        kill_queries.STREAKS_MAX_ROWS = max_rows

    async def _scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            for i, (outcome, when) in enumerate(seed_events):
                kid = 1000 + i
                if outcome == "L":
                    db.add(_km(kid, when, victim_character_id=CHAR_ID))
                else:  # win — victim is someone else, char is an attacker
                    db.add(_km(kid, when, victim_character_id=OTHER_ID))
                    db.add(KillmailAttacker(killmail_id=kid, character_id=CHAR_ID))
            await db.commit()
        return await kill_queries.streaks(CHAR_ID)

    try:
        return loop.run_until_complete(_scenario())
    finally:
        loop.run_until_complete(engine.dispose())
        kill_queries.AsyncSessionLocal = _ORIG_SESSION_LOCAL
        if max_rows is not None:
            kill_queries.STREAKS_MAX_ROWS = orig_max
        loop.close()
        os.unlink(tmp.name)


_ORIG_SESSION_LOCAL = kill_queries.AsyncSessionLocal


def test_streaks_correctness():
    """Chronological sequence W W L W W W L W W W.
      - two wins then a loss  → longest so far 2, streak reset
      - three wins then a loss → longest 3, streak reset (this is the LAST loss)
      - three trailing wins    → current streak 3
    Expect current_win=3, longest_win=3, and days_since_loss keyed off the last
    loss, which we place exactly 20 days before now."""
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    # Strictly increasing times, 5-day spacing; oldest first.
    seq = ["W", "W", "L", "W", "W", "W", "L", "W", "W", "W"]
    events = [(o, base - timedelta(days=(len(seq) - i) * 5)) for i, o in enumerate(seq)]
    # Last loss is index 6 → offset (10-6)*5 = 20 days ago.

    result = _run_streaks(events)

    assert result["current_win"] == 3, result
    assert result["longest_win"] == 3, result
    assert result["days_since_loss"] == 20, result


def test_streaks_bounded_to_max_rows():
    """Full history W*7, L, W, W (chronological). Unbounded, longest_win would be
    7. With STREAKS_MAX_ROWS=3 only the last three rows (L, W, W) are scanned:
      - correct ascending walk → current_win=2, longest_win=2
      - if the reverse were wrong (descending W, W, L) → current_win would be 0
    So asserting current_win=2 proves chronological order survives the reverse,
    and longest_win=2 (not 7) proves the LIMIT actually bounds the scan."""
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    seq = ["W", "W", "W", "W", "W", "W", "W", "L", "W", "W"]
    events = [(o, base - timedelta(days=(len(seq) - i) * 5)) for i, o in enumerate(seq)]

    # Sanity: unbounded gives the *different* answer (longest 7), so the bound
    # is what makes the assertions below pass.
    full = _run_streaks(events)
    assert full["longest_win"] == 7, full
    assert full["current_win"] == 2, full

    windowed = _run_streaks(events, max_rows=3)
    assert windowed["current_win"] == 2, windowed
    assert windowed["longest_win"] == 2, windowed
    assert windowed["days_since_loss"] is not None, windowed
