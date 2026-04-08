from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Float, ForeignKey, UniqueConstraint
from app.db.encryption import EncryptedText
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime, timezone
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
    """User-created skill training plans."""
    __tablename__ = "skill_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    share_token = Column(String(16), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    entries = relationship("SkillPlanEntry", back_populates="plan", cascade="all, delete-orphan",
                           order_by="SkillPlanEntry.sort_order")


class SkillPlanEntry(Base):
    """Individual skill + target level within a skill plan."""
    __tablename__ = "skill_plan_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("skill_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    skill_type_id = Column(Integer, nullable=False)
    target_level = Column(Integer, nullable=False)  # 1-5
    sort_order = Column(Integer, nullable=False, default=0)

    plan = relationship("SkillPlan", back_populates="entries")


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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
