#!/usr/bin/env bash
# Planner-stats seeding for vigilant.db (T-038) — LIVE-SAFE wrapper.
#
# Runs scripts/seed_planner_stats.py inside the running app container:
# per-table sampled ANALYZE for small tables (short write txns that coexist
# with ingestion) + hand-seeded sqlite_stat1 rows for killmails /
# killmail_attackers. See that script's docstring for the rationale.
#
# DO NOT resurrect the old stop-the-app full-ANALYZE approach: on this
# host's spinning disk a real ANALYZE of the 192GB file is a multi-hour
# seek storm, and app startup does writes — the 2026-07-10 attempt kept
# the whole site down (startup died on "database is locked") until the
# ANALYZE container was aborted. ~25 min unplanned outage.
#
# Stats are read at connection/schema load — restart the app (or just let
# the next deploy do it) for the running process to pick them up.
#
# Usage:  /opt/vigilant/scripts/analyze-db.sh

set -euo pipefail
cd /opt/vigilant

echo "[analyze-db] seeding planner stats (live, no service stop)..."
docker exec -i vigilant-app-1 python3 - < scripts/seed_planner_stats.py
echo "[analyze-db] complete. Stats take effect on next app restart/deploy."
