"""Game constants for the T-073 skill-farm page.

Every number here is meant to be a fixed EVE Online game rule (not something a
player's own choices change) -- anything that DOES vary per player (their
Accounting skill level, how much SP they want to keep banked) is instead a
per-user setting (``SkillFarmSettings`` / ``SkillFarmPilot.base_sp`` in
app/db/models.py), never hardcoded here.

Verification: every constant below has been checked against a live or
official source (see each one's own comment) -- CCP's own support articles,
a live ESI lookup, or both.
"""
from __future__ import annotations

# ── Skill extraction ──────────────────────────────────────────────────────
# CCP support: "Skill Extractors and Skill Injectors"
# https://support.eveonline.com/hc/en-us/articles/207605005-Skill-Extractors-and-Skill-Injectors
# Confirms: a Skill Extractor can only be used on a character with at least
# 5,500,000 total SP; using one always removes exactly 500,000 SP; SP can
# never drop below 5,000,000; and UNALLOCATED SP doesn't count toward
# either check and can't itself be extracted -- exactly why this module's
# math (app/skillfarm/math.py) always works in ALLOCATED SP
# (total_sp - unallocated_sp), never raw total_sp. The two numbers below are
# not independent -- 5,500,000 - 500,000 = 5,000,000, i.e. an extraction can
# never take a pilot below the floor, only exactly to it or above.
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
# CCP's standard Omega price: 500 PLEX per 30 days. Longer subscription terms
# are discounted (lower PLEX/day), which is exactly why plex_per_month is the
# owner's own editable setting rather than a fixed multiple of this constant.
OMEGA_PLEX_PER_MONTH = 500

# ── NPC sales tax ───────────────────────────────────────────────────────────
# Base rate before any skill reduction: 7.5%, as of patch 22.02
# (12 March 2025). The Accounting skill reduces it 11% per level, down to
# 3.37% at Accounting V. Sources:
#   https://support.eveonline.com/hc/en-us/articles/203218962-Broker-Fee-and-Sales-Tax
#   https://wiki.eveuniversity.org/Tax
# This is ONLY ever used to pre-fill SkillFarmSettings.sales_tax_pct for a
# brand-new user -- every pilot's actual effective rate depends on their own
# Accounting level (and any corp/station modifiers) and is the owner's own
# editable setting from then on.
DEFAULT_SALES_TAX_PCT = 7.5

# ── SDE type ids ────────────────────────────────────────────────────────────
# Confirmed against the live public ESI /universe/types/{id} endpoint:
# 40519 -> "Skill Extractor", 40520 -> "Large Skill Injector",
# 44992 -> "PLEX". app/sde/lookup.py also resolves each id's display name at
# render time, which doubles as an ongoing live cross-check against the
# local SDE tables: a wrong id here would show the wrong item name on the
# page.
SKILL_EXTRACTOR_TYPE_ID = 40519
# Also corroborated independently: app/routes/skill_plans.py already uses
# this same id (`_LSI_TYPE_ID`) for its unrelated injection-yield calculator.
LARGE_SKILL_INJECTOR_TYPE_ID = 40520
PLEX_TYPE_ID = 44992

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
