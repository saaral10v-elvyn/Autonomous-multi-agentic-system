"""Provider-agnostic LLM gateway with call-time key resolution and retry."""

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from typing import Protocol

from app.core.config import get_settings
from app.core.crypto import EncryptedSecret, decrypt_secret
from app.llm.config import MODEL_TIERS
from app.llm.errors import LLMRequestError, ProviderError, ProviderKeyUnavailable
from app.llm.providers.litellm import LiteLLMProvider
from app.llm.types import (
    LLMResponse,
    LLMRunContext,
    ModelTier,
    Provider,
    ProviderAdapter,
    ProviderKeyResolver,
)


class SleepFunction(Protocol):
    def __call__(self, seconds: float) -> Awaitable[None]: ...


class LLMClient:
    """Resolve BYOK credentials at call time, then invoke an injected provider adapter."""

    def __init__(
        self,
        key_resolver: ProviderKeyResolver,
        *,
        providers: Mapping[Provider, ProviderAdapter] | None = None,
        max_retries: int | None = None,
        retry_base_seconds: float | None = None,
        sleep: SleepFunction | None = None,
    ) -> None:
        self._key_resolver = key_resolver
        self._providers = dict(providers or {})
        self._default_provider = LiteLLMProvider()
        settings = get_settings()
        self._max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._retry_base_seconds = (
            retry_base_seconds
            if retry_base_seconds is not None
            else settings.llm_retry_base_seconds
        )
        self._sleep = sleep or asyncio.sleep

    async def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
        model_tier: ModelTier,
        run_ctx: LLMRunContext,
    ) -> LLMResponse:
        """Complete a chat request and return normalized response and usage accounting."""
        credential = await self._key_resolver.get_for_user(run_ctx.user_id, run_ctx.provider_key_id)
        if credential is None or credential.provider != run_ctx.provider:
            raise ProviderKeyUnavailable("Provider key unavailable.")
        encrypted = EncryptedSecret(credential.ciphertext, credential.nonce)
        api_key = decrypt_secret(
            encrypted,
            user_id=run_ctx.user_id,
            record_id=credential.id,
            master_key=get_settings().master_key,
        )
        model = MODEL_TIERS[run_ctx.provider][model_tier]
        adapter = self._providers.get(run_ctx.provider, self._default_provider)
        delay = self._retry_base_seconds
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    return await adapter.complete(
                        model=model,
                        messages=messages,
                        tools=tools,
                        api_key=api_key,
                    )
                except ProviderError as error:
                    if not error.retryable or attempt == self._max_retries:
                        raise LLMRequestError("The provider request failed.") from None
                    await self._sleep(delay)
                    delay *= 2
                except Exception:
                    raise LLMRequestError("The provider request failed.") from None
        finally:
            api_key = ""
        raise LLMRequestError("The provider request failed.")
