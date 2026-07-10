"""Live-safe planner-stats seeding for vigilant.db (T-038).

Runs INSIDE the app container against the live DB — no service stop:

  docker exec -i vigilant-app-1 python3 - < scripts/seed_planner_stats.py

Phase A — per-table sampled ANALYZE for every table EXCEPT the two giants
(killmails, killmail_attackers). Each table is its own short write
transaction with a generous busy_timeout, so killmail ingestion coexists.

Phase B — hand-seeded sqlite_stat1 rows for the giants. A real ANALYZE of
a 192GB file on this host's spinning disk is a multi-hour random-seek
storm (the 2026-07-10 attempt ran 25+ min with the app stopped and had to
be aborted — and app startup does writes, so the whole site was down).
The planner only needs order-of-magnitude selectivities, so we write them
directly: row counts measured via max(rowid) (O(1)), per-column averages
from EVE-domain knowledge, documented inline. Seeding is one sub-second
INSERT transaction.

Long-lived app connections read sqlite_stat1 at schema load — the next
app restart (any deploy) picks the stats up.

sqlite_stat1.stat format: "<rows> <avg rows per distinct col1>
[<avg per (col1,col2)> ...]" — one value per indexed column prefix.
"""

import sqlite3
import time

DB = "/data/vigilant.db"
GIANTS = ("killmails", "killmail_attackers")

conn = sqlite3.connect(DB, timeout=30)
conn.execute("PRAGMA busy_timeout=30000")
conn.execute("PRAGMA analysis_limit=4000")

# ── Phase A: real sampled ANALYZE, one table per txn ──────────────────────
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
    " AND name NOT LIKE 'sqlite_%' ORDER BY name"
)]
for t in tables:
    if t in GIANTS:
        continue
    t0 = time.time()
    try:
        conn.execute(f'ANALYZE "{t}"')
        conn.commit()
        print(f"  analyzed {t} in {time.time()-t0:.1f}s")
    except sqlite3.OperationalError as e:
        conn.rollback()
        print(f"  SKIPPED {t}: {e}")

# ── Phase B: hand-seeded stats for the giants ─────────────────────────────
n_km = conn.execute("SELECT max(rowid) FROM killmails").fetchone()[0] or 1
n_at = conn.execute("SELECT max(rowid) FROM killmail_attackers").fetchone()[0] or 1
print(f"  killmails ~{n_km:,} rows, killmail_attackers ~{n_at:,} rows")


def stat(n, *avgs):
    return " ".join(str(int(x)) for x in (n, *avgs))


# Selectivity assumptions (order of magnitude, EVE domain):
#   ~8.5k killable systems, ~200k victim corps, ~2M victim chars,
#   ~5k alliances, ~4k ship types, ~10k weapon types, ~7 attackers/kill,
#   killmail_time near-unique, (col, time/kid) pairs near-unique.
KM = n_km
rows = [
    ("killmails", None, str(KM)),
    ("killmails", "ix_killmails_killmail_time",        stat(KM, 2)),
    ("killmails", "ix_killmails_solar_system_id",      stat(KM, max(2, KM // 8_500))),
    ("killmails", "ix_killmails_victim_corporation_id", stat(KM, max(2, KM // 200_000))),
    ("killmails", "ix_killmails_victim_character_id",  stat(KM, max(2, KM // 2_000_000))),
    ("killmails", "ix_killmails_victim_alliance_id",   stat(KM, max(2, KM // 5_000))),
    ("killmails", "ix_killmails_victim_ship_type_id",  stat(KM, max(2, KM // 4_000))),
    ("killmails", "ix_killmails_involves_our_char",    stat(KM, KM // 2)),
    ("killmails", "ix_killmail_system_time",           stat(KM, max(2, KM // 8_500), 2)),
    ("killmails", "ix_km_victim_corp_time",            stat(KM, max(2, KM // 200_000), 2)),
    ("killmails", "ix_km_victim_alli_time",            stat(KM, max(2, KM // 5_000), 2)),
    ("killmails", "ix_km_victim_ship_time",            stat(KM, max(2, KM // 4_000), 2)),
    ("killmails", "ix_killmails_total_value_kid",      stat(KM, 3, 1)),
    ("killmails", "ix_killmails_attacker_count_kid",   stat(KM, max(2, KM // 2_000), 1)),
]
AT = n_at
rows += [
    ("killmail_attackers", None, str(AT)),
    ("killmail_attackers", "ix_killmail_attackers_killmail_id",    stat(AT, 7)),
    ("killmail_attackers", "ix_killmail_attackers_character_id",   stat(AT, max(2, AT // 3_000_000))),
    ("killmail_attackers", "ix_killmail_attackers_corporation_id", stat(AT, max(2, AT // 300_000))),
    ("killmail_attackers", "ix_killmail_attackers_alliance_id",    stat(AT, max(2, AT // 5_000))),
    ("killmail_attackers", "ix_killmail_attackers_ship_type_id",   stat(AT, max(2, AT // 4_000))),
    ("killmail_attackers", "ix_killmail_attackers_weapon_type_id", stat(AT, max(2, AT // 10_000))),
    ("killmail_attackers", "ix_kma_corp_time",                     stat(AT, max(2, AT // 300_000), 2)),
    ("killmail_attackers", "ix_kma_alli_time",                     stat(AT, max(2, AT // 5_000), 2)),
]

# sqlite_stat1 only springs into existence via a successful ANALYZE. Phase A
# normally provides that; if every table somehow skipped, bootstrap with the
# tiny users table so Phase B's writes have a target.
has_stat1 = conn.execute(
    "SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0]
if not has_stat1:
    conn.execute('ANALYZE "users"')
    conn.commit()

# Only seed stat1 rows for indexes that actually exist (schema drift guard).
existing = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index'")}
t0 = time.time()
for tbl, idx, st in rows:
    if idx is not None and idx not in existing:
        print(f"  skip missing index {idx}")
        continue
    conn.execute(
        "DELETE FROM sqlite_stat1 WHERE tbl=? AND idx IS ?", (tbl, idx))
    conn.execute(
        "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)", (tbl, idx, st))
conn.commit()
print(f"  giant-table stats seeded in {time.time()-t0:.2f}s")

total = conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0]
print(f"done — sqlite_stat1 rows: {total}")
conn.close()
