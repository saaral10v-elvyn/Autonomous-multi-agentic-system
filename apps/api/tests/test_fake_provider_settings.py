"""Production settings must make the development-only fake unreachable."""

import asyncio
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.llm.fake import DevelopmentFakeKeyResolver


def test_fake_provider_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_PROVIDER_ENABLED", raising=False)
    assert Settings(_env_file=None).fake_provider_enabled is False


def test_fake_provider_setting_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="cannot be enabled in production"):
        Settings(app_env="production", fake_provider_enabled=True)


def test_fake_key_resolver_returns_no_credential_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "false")
    get_settings.cache_clear()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    provider_key_id = uuid5(NAMESPACE_URL, f"forge-fake:{user_id}")
    try:
        credential = asyncio.run(
            DevelopmentFakeKeyResolver().get_for_user(user_id, provider_key_id)
        )
    finally:
        get_settings.cache_clear()
    assert credential is None
