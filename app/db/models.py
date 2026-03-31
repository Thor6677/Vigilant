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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
