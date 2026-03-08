from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Float, ForeignKey
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime, timezone
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Character(Base):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    character_id = Column(Integer, unique=True, nullable=False, index=True)
    character_name = Column(String, nullable=False)
    corporation_id = Column(Integer, nullable=True)
    corporation_name = Column(String, nullable=True)
    alliance_id = Column(Integer, nullable=True)
    alliance_name = Column(String, nullable=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    token_expiry = Column(DateTime, nullable=False)
    scopes = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sort_order = Column(Integer, default=0)
    account_group = Column(String, default="Ungrouped")

    chat_sessions = relationship("ChatSession", back_populates="character", cascade="all, delete-orphan")

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


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True)
    character_id = Column(Integer, ForeignKey("characters.character_id"), nullable=False)
    title = Column(String, nullable=False, default="New Chat")
    messages = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    character = relationship("Character", back_populates="chat_sessions")


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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
