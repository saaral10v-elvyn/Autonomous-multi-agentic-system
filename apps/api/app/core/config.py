"""Environment-backed application settings."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file="../../.env", env_file_encoding="utf-8", extra="ignore"
    )
    database_url: str = "postgresql+asyncpg://forge:forge@localhost:5432/forge"
    redis_url: str = "redis://localhost:6379/0"
    web_origin: str = "http://localhost:3000"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    master_key: SecretStr | None = None
    cookie_secure: bool = False
    session_max_age_seconds: int = 43_200
    llm_max_retries: int = 3
    llm_retry_base_seconds: float = 0.25


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
