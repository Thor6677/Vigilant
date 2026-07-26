"""Dev-seed behaviour for every table. Pure data — NO imports.

Kept import-free on purpose: scripts/seed-dev-db.py runs inside a bare container
with only the Python standard library available, so it cannot depend on
SQLAlchemy or on app.config being loadable. The pytest guard in
tests/test_dev_seed_classification.py loads this file by path and cross-checks it
against Base.metadata, so the two can never drift.

SLICE    — bounded to a recent window (see SLICE_DAYS). These three tables are
           essentially the entire 180 GiB of production data.
SKIP     — the table IS created, but no rows are copied. It never means "omit
           the table": a missing table makes the dev app throw `no such table`
           on the first query, which is far worse than an empty one.
COPY_ALL — copied whole. Everything else, including the SDE tables and BOTH
           killmail aggregate tables: those aggregates are small and are exactly
           what the activity/history pages read for day-and-longer windows, so
           dropping them would break the very pages the 30-day slice exists to
           support.

Watch the seeder's per-table row counts on the first run. The non-killmail data
was ~8 MiB as of the March 2026 backup, but market_history,
player_count_snapshots and system_activity_snapshots all grow continuously. If
any COPY_ALL table turns out to be over ~1 GiB, move it to its own time-filtered
slice rather than letting the dev DB bloat.
"""

SLICE_DAYS = 30

# Parent of the slice. Filtered directly on killmail_time.
SLICE_PARENT = "killmails"

# Children of the slice. Filtered by killmail_id against the surviving parents.
# Both have a killmail_id index (verified on production 2026-07-26), so this is
# an index SEARCH, not a scan of the largest tables in the database.
SLICE_CHILDREN = ("killmail_attackers", "killmail_items")

SLICE = (SLICE_PARENT,) + SLICE_CHILDREN

# Created empty. esi_cache is a pure response cache keyed to production's own
# ESI calls: it regenerates itself on demand, it is one of the tables that grows
# continuously, and copying it would have a dev instance serving stale
# production-cached ESI responses. Registered by app.db.cache rather than
# app.db.models, which is why it is easy to miss when enumerating models by hand
# — the coverage test is what caught it.
SKIP = (
    'esi_cache',
)

# Generated from Base.metadata on 2026-07-26 (85 tables, minus the 3 in SLICE).
COPY_ALL = (
    'admin_audit_log',
    'alliance_name_cache',
    'character_asset_cache',
    'character_corp_roles',
    'character_dashboard_cache',
    'character_kill_ingest',
    'characters',
    'corp_contract_thresholds',
    'corp_inventory_thresholds',
    'detected_battles',
    'dscan_results',
    'esi_rate_limit_events',
    'everef_import_days',
    'hosted_images',
    'industry_job_history',
    'kill_alert_events',
    # Both aggregate tables are small AND are what the activity/history pages
    # read for day-and-longer windows. Dropping them would break exactly the
    # pages the 30-day killmail slice exists to support.
    'killmail_daily_aggregates',
    'killmail_zone_daily_aggregates',
    'market_history',
    'market_history_meta',
    'mining_ledger_entries',
    'net_worth_snapshots',
    'player_count_daily_aggregates',
    'player_count_snapshots',
    'registration_allowlist',
    'saved_gate_routes',
    'sde_blueprint_info',
    'sde_blueprint_invention',
    'sde_blueprint_invention_materials',
    'sde_blueprint_invention_skills',
    'sde_blueprint_materials',
    'sde_certificate_skills',
    'sde_certificates',
    'sde_compressible',
    'sde_constellations',
    'sde_dogma_attributes',
    'sde_effects',
    'sde_groups',
    'sde_jumps',
    'sde_market_groups',
    'sde_meta',
    'sde_modifiers',
    'sde_module_slots',
    'sde_moons',
    'sde_npc_corps',
    'sde_planet_schematic_materials',
    'sde_planet_schematics',
    'sde_planets',
    'sde_regions',
    'sde_ship_masteries',
    'sde_skill_info',
    'sde_stars',
    'sde_stations',
    'sde_systems',
    'sde_type_bonuses',
    'sde_type_dogma_attrs',
    'sde_type_effects',
    'sde_type_materials',
    'sde_type_skill_reqs',
    'sde_types',
    'sde_wormhole_classes',
    'sde_wormhole_types',
    'skill_plan_acl',
    'skill_plan_entries',
    'skill_plans',
    'sovereignty_changes',
    'stockpile_targets',
    'structure_age_calibration',
    'structure_name_cache',
    'structure_timers',
    'system_activity_snapshots',
    'timer_acl_entries',
    'timer_acl_groups',
    # Copied whole (it is one row). Harmless in dev: the checker never notifies
    # from a "dev" build, so a copied notified_tag is inert there.
    'update_status',
    'user_avoid_entries',
    'user_fitting_folders',
    'user_fittings',
    'user_hunter_watches',
    'user_map_bookmarks',
    'user_system_watches',
    'users',
    'wallet_snapshots',
    'wallet_transactions',
)
