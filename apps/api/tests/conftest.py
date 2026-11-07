"""Shared live-Postgres integration fixtures and safe test credentials."""

import asyncio
import base64
from collections.abc import Callable, Generator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.v1.deps import get_llm_client
from app.core.config import get_settings
from app.core.db import SessionFactory, engine
from app.llm.client import LLMClient
from app.llm.fake import FakeProvider
from app.llm.key_resolver import RepositoryProviderKeyResolver
from app.llm.types import Provider
from app.main import app
from app.models.provider_key import ProviderKey
from app.models.user import User

TEST_MASTER_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
WEB_ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def configure_test_master_key(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("MASTER_KEY", TEST_MASTER_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def test_emails() -> Generator[Callable[[], str], None, None]:
    emails: list[str] = []

    def allocate() -> str:
        email = f"m1-test-{uuid4().hex}@example.test"
        emails.append(email)
        return email

    yield allocate

    async def cleanup() -> None:
        async with SessionFactory() as session:
            user_ids = select(User.id).where(User.email.in_(emails))
            await session.execute(delete(ProviderKey).where(ProviderKey.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.email.in_(emails)))
            await session.commit()
        await engine.dispose()

    if emails:
        asyncio.run(cleanup())


@pytest.fixture
def api_client(
    test_emails: Callable[[], str], fake_provider: FakeProvider
) -> Generator[TestClient, None, None]:
    del test_emails

    def make_llm_client() -> LLMClient:
        adapters = {provider: fake_provider for provider in Provider}
        return LLMClient(RepositoryProviderKeyResolver(SessionFactory), providers=adapters)

    app.dependency_overrides[get_llm_client] = make_llm_client
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
