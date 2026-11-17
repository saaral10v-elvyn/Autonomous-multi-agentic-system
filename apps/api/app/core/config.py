"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.sandbox.egress import normalize_allowlist


class Settings(BaseSettings):
    """Runtime settings loaded from environment and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file="../../.env", env_file_encoding="utf-8", extra="ignore"
    )
    database_url: str = "postgresql+asyncpg://forge:forge@localhost:5432/forge"
    redis_url: str = "redis://localhost:6379/0"
    web_origin: str = "http://localhost:3000"
    app_env: Literal["development", "test", "production"] = "development"
    fake_provider_enabled: bool = False
    fake_provider_step_delay_seconds: float = 0
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    master_key: SecretStr | None = None
    cookie_secure: bool = False
    session_max_age_seconds: int = 43_200
    llm_max_retries: int = 3
    llm_retry_base_seconds: float = 0.25
    approval_ttl_seconds: int = 86_400
    sandbox_provider: Literal["docker", "fake"] = "docker"
    sandbox_image: str = "forge-sandbox:latest"
    sandbox_cpu: float = 1.0
    sandbox_memory_mb: int = 1024
    sandbox_pids_limit: int = 256
    sandbox_disk_mb: int = 2048
    sandbox_ttl_seconds: int = 3600
    sandbox_network_mode: Literal["none", "allowlisted"] = "none"
    sandbox_allowlist_hosts: tuple[str, ...] = (
        "registry.npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "api.github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    )
    sandbox_max_output_bytes: int = 65536
    api_rate_limit_requests: int = Field(default=120, gt=0, le=100_000)
    api_rate_limit_window_seconds: int = Field(default=60, gt=0, le=86_400)
    max_concurrent_runs_per_user: int = Field(default=2, gt=0, le=100)
    platform_paid_enabled: bool = False
    otel_service_name: str = "forge-api"
    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def prevent_fake_provider_in_production(self) -> "Settings":
        """Keep the deterministic demo provider unavailable in production settings."""
        if self.app_env == "production" and self.fake_provider_enabled:
            raise ValueError("FAKE_PROVIDER_ENABLED cannot be enabled in production.")
        if self.app_env == "production" and self.sandbox_provider == "fake":
            raise ValueError("SANDBOX_PROVIDER=fake cannot be used in production.")
        if self.sandbox_network_mode == "allowlisted" and self.sandbox_provider != "docker":
            raise ValueError("SANDBOX_NETWORK_MODE=allowlisted requires SANDBOX_PROVIDER=docker.")
        if self.sandbox_allowlist_hosts:
            normalize_allowlist(self.sandbox_allowlist_hosts)
        elif self.sandbox_network_mode == "allowlisted":
            raise ValueError("SANDBOX_ALLOWLIST_HOSTS must include at least one hostname.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
