#!/usr/bin/env bash
# Sampled ANALYZE for vigilant.db during a brief maintenance stop (T-038).
#
# Why this exists: the killmails DB (~192GB, 60M+ rows) had no sqlite_stat1,
# so the query planner guessed badly as tables grew (root cause of the
# 2026-07-03 killfeed 504). A live ANALYZE is one long write transaction —
# it held the write lock 19+ minutes and killed ingestion — so it must run
# with the app STOPPED. Sampled ANALYZE (PRAGMA analysis_limit) reads only
# ~4k rows per index, so the whole run is typically well under a minute:
# downtime comparable to a normal deploy.
#
# Ongoing freshness is handled separately by the app's daily
# `PRAGMA optimize` scheduler tick (bounded, incremental, needs the stats
# seeded by this script to be willing to touch large tables).
#
# Usage:  /opt/vigilant/scripts/analyze-db.sh
# Safe to re-run any time; also a candidate for the weekly auto-update
# window (Sun 08:00 UTC, thunderborn-ops).

set -euo pipefail

cd /opt/vigilant

MAINT=/opt/thunderborn-ops/scripts/maintenance.sh
[ -x "$MAINT" ] && "$MAINT" on "vigilant analyze-db" || true

echo "[analyze-db] stopping app container..."
docker compose stop app

cleanup() {
    echo "[analyze-db] starting app container..."
    docker compose start app
    [ -x "$MAINT" ] && "$MAINT" off || true
}
trap cleanup EXIT

echo "[analyze-db] running sampled ANALYZE (analysis_limit=4000)..."
# -i is required: python3 reads the script from stdin (a plain `docker run`
# leaves stdin unattached and python3 exits silently having done nothing).
docker run --rm -i -v vigilant_app_data:/data --entrypoint python3 vigilant-app - <<'PYEOF'
import sqlite3, time

t0 = time.time()
conn = sqlite3.connect("/data/vigilant.db", timeout=60)
conn.execute("PRAGMA analysis_limit=4000")
conn.execute("ANALYZE")
conn.commit()
rows = conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0]
# Flush the WAL so the stats are in the main DB file before restart.
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.close()
print(f"[analyze-db] done in {time.time()-t0:.1f}s — sqlite_stat1 rows: {rows}")
PYEOF

echo "[analyze-db] complete."
