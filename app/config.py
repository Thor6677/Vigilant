from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    eve_client_id: str
    eve_client_secret: str
    eve_callback_url: str = "http://localhost:8000/auth/callback"
    secret_key: str
    database_url: str = "sqlite+aiosqlite:///./vigilant.db"
    uploads_dir: str = "./uploads"
    debug: bool = False

    eve_sso_auth_url: str = "https://login.eveonline.com/v2/oauth/authorize"
    eve_sso_token_url: str = "https://login.eveonline.com/v2/oauth/token"
    eve_sso_verify_url: str = "https://login.eveonline.com/oauth/verify"
    eve_esi_base: str = "https://esi.evetech.net/latest"

    killmails_enabled: bool = False
    killmail_dashboard_enabled: bool = False
    killmail_battles_enabled: bool = False
    killmail_stream_enabled: bool = False  # killmail.stream live consumer for big-battle banner

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
