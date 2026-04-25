from sqlalchemy import Column, Integer, String, DateTime, Date, Boolean, Text, Float, ForeignKey, UniqueConstraint, event
from app.db.encryption import EncryptedText
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime, timezone
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    if "sqlite" in str(engine.url):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


class Base(DeclarativeBase):
    pass


class User(Base):
    """Represents a player account. Identified by their main EVE character."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_admin = Column(Boolean, default=False)
    role = Column(String(16), default="user")  # user, manager, admin

    characters = relationship("Character", back_populates="user")


class Character(Base):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    character_id = Column(Integer, unique=True, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    corporation_id = Column(Integer, nullable=True)
    corporation_name = Column(String, nullable=True)
    alliance_id = Column(Integer, nullable=True)
    alliance_name = Column(String, nullable=True)
    access_token = Column(EncryptedText, nullable=False)
    refresh_token = Column(EncryptedText, nullable=False)
    token_expiry = Column(DateTime, nullable=False)
    scopes = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sort_order = Column(Integer, default=0)
    security_status = Column(Float, nullable=True)
    birthday = Column(DateTime, nullable=True)  # Character creation date from ESI
    account_group = Column(String, default="Ungrouped")
    # Account ownership — nullable to support migration of pre-existing rows
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    is_main = Column(Boolean, default=False)

    user = relationship("User", back_populates="characters")

    @property
    def has_corp_roles(self) -> bool:
        return "esi-corporations.read_corporation_membership.v1" in self.scopes


class CharacterDashboardCache(Base):
    """Persistent cache of processed dashboard data per character."""
    __tablename__ = "character_dashboard_cache"

    character_id = Column(Integer, ForeignKey("characters.character_id"), primary_key=True)
    wallet = Column(Float, nullable=True)
    location_json = Column(Text, nullable=True)
    industry_json = Column(Text, nullable=True)
    clones_json = Column(Text, nullable=True)
    orders_json = Column(Text, nullable=True)
    mail_json = Column(Text, nullable=True)
    notifications_json = Column(Text, nullable=True)
    contracts_json = Column(Text, nullable=True)
    pi_json = Column(Text, nullable=True)
    skillqueue_json = Column(Text, nullable=True)
    zkill_json = Column(Text, nullable=True)
    last_synced = Column(DateTime, nullable=True)      # naive UTC
    sync_status = Column(String(16), nullable=False, default="idle")  # idle | syncing | error
    sync_error = Column(Text, nullable=True)
    sync_warnings_json = Column(Text, nullable=True)  # JSON: {"wallet": "token_refresh_failed", ...}
    field_synced_json = Column(Text, nullable=True)   # JSON: {"wallet": "2024-01-01T00:00:00", ...}


class WalletSnapshot(Base):
    """Periodic snapshots of a character's wallet balance for historical charting."""
    __tablename__ = "wallet_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    character_id = Column(Integer, ForeignKey("characters.character_id"), nullable=False, index=True)
    balance = Column(Float, nullable=False)
    recorded_at = Column(DateTime, nullable=False)  # naive UTC


class KillmailDailyAggregate(Base):
    """Daily total kill counts and ISK destroyed. Multi-source by design:

    - source='zkb-totals' rows come from zKillboard's
      /api/history/totals.json — kill_count only, no ISK, but covers
      2007-12-05 → today (~6700 days). One-shot fetched at startup.
    - source='vigilant' rows are rolled up from our killmails table by a
      daily background task BEFORE the 30-day discovery-scope GC fires.
      Includes both kill_count and total_isk_destroyed.

    Chart queries prefer 'vigilant' rows on overlapping dates (we have ISK
    there); 'zkb-totals' carries the long-tail history."""
    __tablename__ = "killmail_daily_aggregates"
    __table_args__ = (
        UniqueConstraint("source", "date", name="uq_kda_source_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)
    kill_count = Column(Integer, nullable=False, default=0)
    total_isk_destroyed = Column(Float, nullable=True)
    rolled_up_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class PlayerCountSnapshot(Base):
    """TQ player count over time. One table for live ESI samples + historical
    backfill from third-party archives. The (source, recorded_at) unique
    constraint makes idempotent re-runs safe and lets us cross-validate
    overlapping sources without duplication."""
    __tablename__ = "player_count_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "recorded_at", name="uq_pcs_source_time"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at = Column(DateTime, nullable=False, index=True)  # naive UTC
    player_count = Column(Integer, nullable=False)
    source = Column(String(32), nullable=False, index=True)
        # 'esi' | 'eve-offline-net' | 'eve-offline-com'
    granularity = Column(String(16), nullable=False, default="60s")
        # '60s' | 'hourly' | 'daily'
    # ESI-only fields (nullable for non-esi rows)
    server_version = Column(String(32), nullable=True)
    server_start_time = Column(DateTime, nullable=True)
    vip_mode = Column(Boolean, nullable=True)


class PlayerCountDailyAggregate(Base):
    """Per-(source, date) rollup of PlayerCountSnapshot. Derived cache —
    raw snapshots remain the source of truth and can rebuild this table
    from scratch. Long-window activity charts read from here so they don't
    have to GROUP BY on a 10M-row table per request."""
    __tablename__ = "player_count_daily_aggregates"
    __table_args__ = (
        UniqueConstraint("source", "date", name="uq_pcda_source_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    source = Column(String(32), nullable=False, index=True)
    avg_pc = Column(Float, nullable=False)
    peak_pc = Column(Integer, nullable=False)
    sample_count = Column(Integer, nullable=False, default=0)
    rolled_up_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class ESIRateLimitEvent(Base):
    __tablename__ = "esi_rate_limit_events"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    event_type  = Column(String(32), nullable=False)  # "429","420","group_warning"
    group_name  = Column(String(128), nullable=True)
    path        = Column(String(512), nullable=False)
    remaining   = Column(Integer, nullable=True)
    limit_str   = Column(String(64), nullable=True)   # e.g. "150/15m"
    retry_after = Column(Integer, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    # Soft-archive: admin-dismissed events persist here with archived_at set
    # so they drop off the main list but remain auditable. A daily GC deletes
    # archived rows older than 30 days.
    archived_at = Column(DateTime, nullable=True)


class CharacterAssetCache(Base):
    """Pre-resolved asset list per character, refreshed by background sync."""
    __tablename__ = "character_asset_cache"

    character_id = Column(Integer, ForeignKey("characters.character_id"), primary_key=True)
    assets_json  = Column(Text, nullable=True)   # JSON list of resolved asset dicts
    last_fetched = Column(DateTime, nullable=True)  # naive UTC


class MiningLedgerEntry(Base):
    """Persistent mining ledger — survives beyond ESI's 30-day window."""
    __tablename__ = "mining_ledger_entries"
    __table_args__ = (
        UniqueConstraint("character_id", "date", "type_id", "solar_system_id",
                         name="uq_mining_entry"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    character_id = Column(Integer, ForeignKey("characters.character_id"), nullable=False, index=True)
    date = Column(String(10), nullable=False)          # "2026-03-15"
    type_id = Column(Integer, nullable=False)
    solar_system_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)


class CorpInventoryThreshold(Base):
    """User-defined monitoring thresholds for items in corp hangars."""
    __tablename__ = "corp_inventory_thresholds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    corp_id = Column(Integer, nullable=False, index=True)
    location_id = Column(Integer, nullable=False)
    location_name = Column(String, nullable=True)
    location_flag = Column(String, nullable=False, default="")
    type_id = Column(Integer, nullable=False)
    type_name = Column(String, nullable=True)
    threshold_low = Column(Integer, nullable=False, default=0)
    threshold_critical = Column(Integer, nullable=False, default=0)
    current_quantity = Column(Integer, nullable=True)
    last_checked = Column(DateTime, nullable=True)
    alert_state = Column(String(16), nullable=False, default="ok")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CorpContractThreshold(Base):
    """User-defined monitoring thresholds for outstanding corp contracts."""
    __tablename__ = "corp_contract_thresholds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    corp_id = Column(Integer, nullable=False, index=True)
    match_type = Column(String(16), nullable=False)       # "item" or "title"
    match_value = Column(String(256), nullable=False)      # type_id (as string) or title keyword
    match_label = Column(String(256), nullable=True)       # display name
    type_id = Column(Integer, nullable=True)               # only for item match
    threshold_low = Column(Integer, nullable=False, default=0)
    threshold_critical = Column(Integer, nullable=False, default=0)
    current_count = Column(Integer, nullable=True)
    last_checked = Column(DateTime, nullable=True)
    alert_state = Column(String(16), nullable=False, default="ok")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DScanResult(Base):
    """Stored d-scan parse result with shareable public URL."""
    __tablename__ = "dscan_results"

    id = Column(String(12), primary_key=True)
    paste_data = Column(Text, nullable=False)
    parsed_json = Column(Text, nullable=False)
    summary_json = Column(Text, nullable=True)
    label = Column(String(128), nullable=True)
    user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)


class SovereigntyChangeEvent(Base):
    """Records a sovereignty holder change for a solar system."""
    __tablename__ = "sovereignty_changes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    system_id = Column(Integer, nullable=False, index=True)
    old_alliance_id = Column(Integer, nullable=True)
    new_alliance_id = Column(Integer, nullable=True)
    old_faction_id = Column(Integer, nullable=True)
    new_faction_id = Column(Integer, nullable=True)
    changed_at = Column(DateTime, nullable=False, index=True)


class StructureTimer(Base):
    """Shared structure reinforcement timer board."""
    __tablename__ = "structure_timers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    structure_name = Column(String, nullable=False)
    structure_type = Column(String(16), nullable=False)   # citadel/ec/refinery/sov/poco/skyhook/other
    system_name = Column(String, nullable=False)
    region_name = Column(String, nullable=True)
    owner_name = Column(String, nullable=False)
    disposition = Column(String(16), nullable=False)      # hostile/friendly
    timer_phase = Column(String(8), nullable=False)       # shield/armor/hull
    timer_expires = Column(DateTime, nullable=False)      # naive UTC
    priority = Column(String(8), nullable=False, default="normal")  # low/normal/critical
    notes = Column(Text, nullable=True)
    source = Column(String(8), nullable=False, default="manual")    # manual/esi
    esi_structure_id = Column(Integer, nullable=True, index=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    archived_at = Column(DateTime, nullable=True)
    acl_group_id = Column(Integer, ForeignKey("timer_acl_groups.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TimerACLGroup(Base):
    """Named access control group for timer visibility."""
    __tablename__ = "timer_acl_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    entries = relationship("TimerACLEntry", back_populates="group", cascade="all, delete-orphan")


class TimerACLEntry(Base):
    """Entry in an ACL group — a character, corporation, or alliance."""
    __tablename__ = "timer_acl_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("timer_acl_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    entry_type = Column(String(16), nullable=False)  # character/corporation/alliance
    eve_id = Column(Integer, nullable=False)
    name = Column(String, nullable=True)

    group = relationship("TimerACLGroup", back_populates="entries")


class RegistrationAllowlist(Base):
    """Allowlist for who can register. Entries can be character, corp, or alliance IDs."""
    __tablename__ = "registration_allowlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_type = Column(String(16), nullable=False)   # character, corporation, alliance
    eve_id = Column(Integer, nullable=False)
    name = Column(String, nullable=True)
    added_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class StructureNameCache(Base):
    """Cache of player-owned structure names resolved via corp structures API."""
    __tablename__ = "structure_name_cache"

    structure_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    solar_system_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SkillPlan(Base):
    """User-created skill training plans.

    visibility controls who can see / edit the plan:
      - "personal"     — owner only (legacy default)
      - "corporation"  — members of owner_corp_id; editors need an EVE corp role
      - "alliance"     — members of owner_alliance_id; editors need a Director/CEO
                          role in any corp of the alliance
      - "custom"       — access governed by SkillPlanACL entries (view/edit/admin)
    share_token remains for the public read-only URL flow; works alongside scopes.
    """
    __tablename__ = "skill_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    share_token = Column(String(16), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Sharing scope — see class docstring for the semantics
    visibility = Column(String(16), nullable=False, default="personal",
                        server_default="personal")
    owner_corp_id = Column(Integer, nullable=True, index=True)
    owner_alliance_id = Column(Integer, nullable=True, index=True)
    last_edited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    last_edited_at = Column(DateTime, nullable=True)

    entries = relationship("SkillPlanEntry", back_populates="plan", cascade="all, delete-orphan",
                           order_by="SkillPlanEntry.sort_order")
    acl_entries = relationship("SkillPlanACL", cascade="all, delete-orphan",
                               backref="plan")


class SkillPlanEntry(Base):
    """Individual skill + target level within a skill plan."""
    __tablename__ = "skill_plan_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("skill_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    skill_type_id = Column(Integer, nullable=False)
    target_level = Column(Integer, nullable=False)  # 1-5
    sort_order = Column(Integer, nullable=False, default=0)

    plan = relationship("SkillPlan", back_populates="entries")


class SkillPlanACL(Base):
    """Per-subject access control entries for custom-scope SkillPlans.

    subject_type is one of: "character" | "corporation" | "alliance".
    permission levels:
      - "view"  — can see the plan on the list and detail pages
      - "edit"  — can add/remove/reorder skills, rename, duplicate
      - "admin" — edit + manage the ACL itself + delete the plan
    """
    __tablename__ = "skill_plan_acl"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("skill_plans.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    subject_type = Column(String(16), nullable=False)
    subject_id = Column(Integer, nullable=False)
    subject_name = Column(String, nullable=False)
    permission = Column(String(8), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("plan_id", "subject_type", "subject_id",
                         name="uq_skill_plan_acl_subject"),
    )


class CharacterCorpRoles(Base):
    """Cached EVE corp roles from GET /characters/{id}/roles/.

    Populated by the background sync scheduler (TTL ~1h). Used by the skill
    plan permission helpers to gate corp/alliance edit access without firing
    an ESI call on every request.
    """
    __tablename__ = "character_corp_roles"

    character_id = Column(Integer, primary_key=True)
    roles_json = Column(Text, nullable=False)  # JSON array of role strings
    fetched_at = Column(DateTime, nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class AdminAuditLog(Base):
    """Admin audit trail for logins, errors, and admin actions."""
    __tablename__ = "admin_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    event_type = Column(String(64), nullable=False, index=True)
    detail = Column(Text, nullable=True)
    character_id = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class HostedImage(Base):
    """User-uploaded image converted to JPEG and served via short URL."""
    __tablename__ = "hosted_images"

    id = Column(String(12), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    label = Column(String(128), nullable=True)
    original_filename = Column(String(256), nullable=True)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    view_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)  # NULL = never expires


class UserAvoidEntry(Base):
    """A system, constellation, or region the user has marked to avoid in
    gate route planning. Per-user, applies to all of that user's characters."""
    __tablename__ = "user_avoid_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "entity_id", name="uq_user_avoid_entry"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # 'system' | 'constellation' | 'region'
    entity_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class SavedGateRoute(Base):
    """User-saved gate route with origin, destination, intermediate waypoints,
    routing preference, and per-route avoid list. Optionally shareable via token."""
    __tablename__ = "saved_gate_routes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    origin_system_id = Column(Integer, nullable=False)
    dest_system_id = Column(Integer, nullable=False)
    waypoints_json = Column(Text, nullable=False, default="[]")     # JSON list[int]
    preference = Column(String(16), nullable=False, default="shortest")
    avoid_json = Column(Text, nullable=False, default="[]")         # JSON list[int]
    share_token = Column(String(16), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserFittingFolder(Base):
    """Nested folder for organizing user fittings. Root = parent_id NULL."""
    __tablename__ = "user_fitting_folders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("user_fitting_folders.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserFitting(Base):
    """User-created ship fittings (local to Vigilant, not ESI)."""
    __tablename__ = "user_fittings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    folder_id = Column(Integer, ForeignKey("user_fitting_folders.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    ship_type_id = Column(Integer, nullable=False)
    items_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserMapBookmark(Base):
    """User-pinned system/constellation/region on the star map."""
    __tablename__ = "user_map_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "entity_id", name="uq_user_map_bookmark"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)          # 'system' | 'constellation' | 'region'
    entity_id = Column(Integer, nullable=False)
    label = Column(String(64), nullable=True)           # optional user-chosen label
    color = Column(String(8), nullable=True)            # hex like "#c8a951"
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserSystemWatch(Base):
    """Per-user system watchlist. Kills in these systems fan out to
    KillAlertEvent via the killmail.stream consumer. Manual entries only;
    the Intel watch page offers a one-click bulk-add for asset-bearing
    systems."""
    __tablename__ = "user_system_watches"
    __table_args__ = (
        UniqueConstraint("user_id", "system_id", name="uq_user_system_watch"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    system_id = Column(Integer, nullable=False, index=True)
    label = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UserHunterWatch(Base):
    """Per-user enemy watchlist. A kill where a watched entity appears as
    attacker fans out to KillAlertEvent."""
    __tablename__ = "user_hunter_watches"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "entity_id", name="uq_user_hunter_watch"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # 'character' | 'corporation' | 'alliance'
    entity_id = Column(Integer, nullable=False, index=True)
    label = Column(String(128), nullable=True)  # free-text display name cached at add time
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class KillAlertEvent(Base):
    """Emitted when a streamed kill matches a user's system or hunter watch.
    Unique per (user_id, killmail_id, kind) so the killmail.stream 24h
    replay after restart doesn't double-fire. Feeds both the /notifications
    poll queue and a history page."""
    __tablename__ = "kill_alert_events"
    __table_args__ = (
        UniqueConstraint("user_id", "killmail_id", "kind", name="uq_kill_alert_event"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # 'system_watch' | 'hunter_watch'
    killmail_id = Column(Integer, nullable=False, index=True)
    system_id = Column(Integer, nullable=False, index=True)
    matched_entity_id = Column(Integer, nullable=True)  # for hunter_watch: the entity that matched
    matched_label = Column(String(128), nullable=True)  # snapshot of watch.label at fire time
    triggered_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True)
    dismissed_at = Column(DateTime, nullable=True)


class SystemActivitySnapshot(Base):
    """Hourly kill/jump snapshot per system. Drives the 48h sparkline in the
    map's system info panel and the 'most violent (3h)' trending list."""
    __tablename__ = "system_activity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    system_id = Column(Integer, nullable=False, index=True)
    captured_at = Column(DateTime, nullable=False, index=True)
    ship_kills = Column(Integer, nullable=False, default=0)
    pod_kills = Column(Integer, nullable=False, default=0)
    npc_kills = Column(Integer, nullable=False, default=0)
    jumps = Column(Integer, nullable=False, default=0)


class AllianceNameCache(Base):
    """Persistent cache of ESI alliance ID → name (and ticker when available).

    Alliance names effectively never change — they shift only when alliances
    dissolve/reform, so we refresh entries on a 30-day TTL rather than per
    request. Populated by the bulk POST /universe/names/ endpoint which
    resolves up to 1,000 IDs per call.
    """
    __tablename__ = "alliance_name_cache"

    alliance_id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    ticker = Column(String(8), nullable=True)
    cached_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)


class Killmail(Base):
    __tablename__ = "killmails"

    killmail_id = Column(Integer, primary_key=True)
    killmail_hash = Column(String(64), nullable=False)
    killmail_time = Column(DateTime, nullable=False, index=True)
    solar_system_id = Column(Integer, nullable=False, index=True)
    victim_character_id = Column(Integer, nullable=True, index=True)
    victim_corporation_id = Column(Integer, nullable=True, index=True)
    victim_alliance_id = Column(Integer, nullable=True, index=True)
    victim_ship_type_id = Column(Integer, nullable=False, index=True)
    total_value = Column(Float, nullable=True)
    is_npc = Column(Boolean, nullable=False, default=False)
    attacker_count = Column(Integer, nullable=False, default=1)
    final_blow_character_id = Column(Integer, nullable=True)
    involves_our_char = Column(Boolean, nullable=False, default=False, index=True)
    fetched_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class KillmailAttacker(Base):
    __tablename__ = "killmail_attackers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    killmail_id = Column(Integer, ForeignKey("killmails.killmail_id"), nullable=False, index=True)
    character_id = Column(Integer, nullable=True, index=True)
    corporation_id = Column(Integer, nullable=True, index=True)
    alliance_id = Column(Integer, nullable=True, index=True)
    ship_type_id = Column(Integer, nullable=True, index=True)
    weapon_type_id = Column(Integer, nullable=True, index=True)
    final_blow = Column(Boolean, nullable=False, default=False)


class CharacterKillIngest(Base):
    __tablename__ = "character_kill_ingest"

    character_id = Column(Integer, primary_key=True)
    last_backfill_page = Column(Integer, nullable=False, default=0)
    backfill_complete = Column(Boolean, nullable=False, default=False)
    last_seen_killmail_id = Column(Integer, nullable=True)
    last_synced = Column(DateTime, nullable=True)


class DetectedBattle(Base):
    __tablename__ = "detected_battles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    system_id = Column(Integer, nullable=False, index=True)
    system_name = Column(String, nullable=True)
    security = Column(Float, nullable=True)
    group_label = Column(String, nullable=False, index=True)
    band = Column(String, nullable=False, index=True)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, nullable=False, default=0)
    kill_count = Column(Integer, nullable=False, default=0)
    pilots_involved = Column(Integer, nullable=False, default=0)
    total_isk = Column(Float, nullable=False, default=0.0)
    top_attacker_corp_id = Column(Integer, nullable=True)
    top_attacker_corp_name = Column(String, nullable=True)
    top_attacker_corp_kills = Column(Integer, nullable=False, default=0)
    top_victim_corp_id = Column(Integer, nullable=True)
    top_victim_corp_name = Column(String, nullable=True)
    top_victim_corp_kills = Column(Integer, nullable=False, default=0)
    top_ships_json = Column(Text, nullable=False, default="[]")
    killmail_ids_json = Column(Text, nullable=False, default="[]")
    detected_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("system_id", "start_time", name="uix_battle_system_start"),
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
