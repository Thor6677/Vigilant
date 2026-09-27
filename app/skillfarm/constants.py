"""Game constants for the T-073 skill-farm page.

Every number here is meant to be a fixed EVE Online game rule (not something a
player's own choices change) -- anything that DOES vary per player (their
Accounting skill level, how much SP they want to keep banked) is instead a
per-user setting (``SkillFarmSettings`` / ``SkillFarmPilot.base_sp`` in
app/db/models.py), never hardcoded here.

Verification note: the PLEX region id below was confirmed against the live
public ESI market order book (see PLEX_REGION_ID's own comment). Everything
else marked "not re-verified against a live source when written" is recorded
from long-standing, previously well-known EVE Online values rather than a
freshly-checked CCP/EVE University page or the local SDE tables -- confirm
each one for real (browser + an `sde_types` query for the type ids) before
relying on it.
"""
from __future__ import annotations

# ── Skill extraction ──────────────────────────────────────────────────────
# Long-standing EVE Online rule since Skill Trading's 2016 introduction: a
# Skill Extractor can only be used on a character with at least this much
# total SP, and using one always removes exactly this many SP from the
# character it's used on. The two numbers are not independent --
# 5,500,000 - 500,000 = 5,000,000, i.e. an extraction can never take a pilot
# below the floor, only exactly to it or above.
# NOT re-verified against a live source when written.
SKILL_EXTRACTOR_MIN_SP = 5_500_000
SKILL_EXTRACTOR_SP_PER_USE = 500_000

# The floor a pilot's ALLOCATED SP never drops below via extraction. Kept as
# its own name (rather than inlining MIN_SP - PER_USE at every call site)
# because app/db/models.py's SkillFarmPilot.base_sp default must match it,
# and the two need to visibly be the same number, not a coincidence of
# arithmetic that could drift apart under a later edit.
SKILL_FLOOR_SP = SKILL_EXTRACTOR_MIN_SP - SKILL_EXTRACTOR_SP_PER_USE  # 5,000,000

# One Large Skill Injector always holds the SP a single extraction produces.
#
# NOTE: this is the EXTRACTION side (how much SP a farm pilot gives up per
# injector produced). It is deliberately NOT the same number as the
# INJECTION side -- how much SP a *target* character gains from consuming
# one, which varies with the target's own total SP under CCP's
# diminishing-returns brackets (500k/400k/300k/150k per injector; see
# app/routes/skill_plans.py's `_LSI_BRACKETS`, which models that separate
# mechanic for "how many injectors to fill a plan gap"). The farm page only
# cares about extraction and market sale, so it always uses the flat figure.
LARGE_SKILL_INJECTOR_SP = 500_000

# ── Omega subscription ─────────────────────────────────────────────────────
# CCP's standard Omega price in PLEX; unchanged since PLEX-priced Omega
# subscriptions were introduced. NOT re-verified against a live source when
# written.
OMEGA_PLEX_PER_MONTH = 500

# ── NPC sales tax ───────────────────────────────────────────────────────────
# Base rate before any skill reduction. The Accounting skill reduces sales
# tax per level (down to roughly half the base rate at level V), so this is
# ONLY ever used to pre-fill SkillFarmSettings.sales_tax_pct for a brand-new
# user -- every pilot's actual effective rate depends on their own Accounting
# level (and any corp/station modifiers) and is the owner's own editable
# setting from then on. NOT re-verified against a live source when written --
# confirm against the current EVE University "Sales Tax" page before release;
# CCP has revised broker-fee and tax mechanics more than once.
DEFAULT_SALES_TAX_PCT = 8.0

# ── SDE type ids ────────────────────────────────────────────────────────────
# app/sde/lookup.py resolves each id's display name at render time, which
# doubles as the live cross-check against the local SDE tables the brief
# asks for: a wrong id here would show the wrong item name on the page.
SKILL_EXTRACTOR_TYPE_ID = 40519  # NOT re-verified against a live source when written
# Corroborated: app/routes/skill_plans.py already uses this same id
# (`_LSI_TYPE_ID`) for its unrelated injection-yield calculator.
LARGE_SKILL_INJECTOR_TYPE_ID = 40520
PLEX_TYPE_ID = 44992  # confirmed live below

# PLEX does not trade at any single station -- every PLEX order in the game
# routes through one shared, region-wide order book rather than a normal
# regional market tied to a trade hub. Confirmed against the live public ESI
# market order book:
#   GET /markets/19000001/orders/?type_id=44992&order_type=sell
# returned real sell orders (128 of them) spanning several different station
# location_ids in the same response, which is exactly the shape a pooled,
# not-station-scoped market produces -- an empty or wrong region id would
# have returned zero orders.
PLEX_REGION_ID = 19000001

# CCP's own "average month" for SP/month math: 365.0 * 24 / 12 = 730 hours.
HOURS_PER_MONTH = 730
