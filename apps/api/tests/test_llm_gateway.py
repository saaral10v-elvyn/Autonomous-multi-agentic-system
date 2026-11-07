"""Gateway retry, accounting, and tool-call tests using only FakeProvider."""

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.crypto import encrypt_secret
from app.llm.client import LLMClient
from app.llm.errors import LLMRequestError, ProviderError
from app.llm.fake import FakeProvider
from app.llm.types import (
    EncryptedProviderCredential,
    LLMResponse,
    LLMRunContext,
    ModelTier,
    Provider,
    ToolCall,
)
from tests.conftest import TEST_MASTER_KEY


class StaticKeyResolver:
    def __init__(self, credential: EncryptedProviderCredential) -> None:
        self.credential = credential

    async def get_for_user(
        self, user_id: object, provider_key_id: object
    ) -> EncryptedProviderCredential | None:
        if user_id == self.credential.user_id and provider_key_id == self.credential.id:
            return self.credential
        return None


def make_context() -> tuple[LLMRunContext, StaticKeyResolver, str]:
    user_id = uuid4()
    key_id = uuid4()
    secret = "sk-test-gateway-only"
    encrypted = encrypt_secret(
        secret, user_id=user_id, record_id=key_id, master_key=TEST_MASTER_KEY
    )
    credential = EncryptedProviderCredential(
        id=key_id,
        user_id=user_id,
        provider=Provider.OPENAI,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
    )
    return (
        LLMRunContext(user_id=user_id, provider=Provider.OPENAI, provider_key_id=key_id),
        StaticKeyResolver(credential),
        secret,
    )


@pytest.mark.asyncio
async def test_retry_uses_exponential_backoff_and_returns_cost_accounting() -> None:
    context, resolver, secret = make_context()
    expected = LLMResponse(
        provider=Provider.OPENAI,
        model="openai/gpt-6-luna",
        content="done",
        tokens_in=12,
        tokens_out=7,
        cost_usd=Decimal("0.0042"),
    )
    provider = FakeProvider([ProviderError("temporary error", retryable=True), expected])
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    client = LLMClient(
        resolver,
        providers={Provider.OPENAI: provider},
        max_retries=2,
        retry_base_seconds=0.01,
        sleep=fake_sleep,
    )
    result = await client.complete(
        [{"role": "user", "content": "hello"}], None, ModelTier.FAST, context
    )

    assert result == expected
    assert provider.received_api_keys == [secret, secret]
    assert delays == [0.01]
    assert result.tokens_in == 12
    assert result.tokens_out == 7
    assert result.cost_usd == Decimal("0.0042")


@pytest.mark.asyncio
async def test_fake_provider_tool_call_round_trip() -> None:
    context, resolver, _ = make_context()
    tool_call = ToolCall(id="call_1", name="lookup", arguments={"query": "forge"})
    expected = LLMResponse(
        provider=Provider.OPENAI,
        model="openai/gpt-6-luna",
        content=None,
        tool_calls=[tool_call],
        tokens_in=4,
        tokens_out=6,
        cost_usd=Decimal("0.0009"),
    )
    provider = FakeProvider([expected])
    client = LLMClient(resolver, providers={Provider.OPENAI: provider})
    tools: Sequence[dict[str, object]] = [{"type": "function", "function": {"name": "lookup"}}]

    result = await client.complete(
        [{"role": "user", "content": "search"}], tools, ModelTier.FAST, context
    )

    assert result.tool_calls == [tool_call]
    assert provider.tools == [tools]


@pytest.mark.asyncio
async def test_provider_errors_are_sanitized() -> None:
    context, resolver, secret = make_context()
    provider = FakeProvider([RuntimeError(f"upstream rejected {secret}")])
    client = LLMClient(resolver, providers={Provider.OPENAI: provider}, max_retries=0)

    with pytest.raises(LLMRequestError) as error:
        await client.complete([{"role": "user", "content": "test"}], None, ModelTier.FAST, context)

    assert secret not in str(error.value)
