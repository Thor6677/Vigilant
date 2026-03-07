from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    eve_client_id: str
    eve_client_secret: str
    eve_callback_url: str = "https://capsuleerai.app/auth/callback"
    anthropic_api_key: str
    secret_key: str
    database_url: str = "sqlite+aiosqlite:///./capsuleerai.db"
    debug: bool = False

    eve_sso_auth_url: str = "https://login.eveonline.com/v2/oauth/authorize"
    eve_sso_token_url: str = "https://login.eveonline.com/v2/oauth/token"
    eve_sso_verify_url: str = "https://login.eveonline.com/oauth/verify"
    eve_esi_base: str = "https://esi.evetech.net/latest"

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
