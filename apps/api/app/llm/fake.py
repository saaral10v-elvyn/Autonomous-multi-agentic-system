"""Scripted provider adapter for tests; it never makes network calls."""

import json
from collections import deque
from collections.abc import Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.llm.errors import ProviderError
from app.llm.types import (
    EncryptedProviderCredential,
    LLMResponse,
    Provider,
    ProviderAdapter,
    ProviderKeyResolver,
    ToolCall,
)


class FakeProvider(ProviderAdapter):
    """Return scripted completions or errors and capture calls for assertions."""

    def __init__(self, responses: Sequence[LLMResponse | Exception] = ()) -> None:
        self.responses: deque[LLMResponse | Exception] = deque(responses)
        self.models: list[str] = []
        self.messages: list[Sequence[Mapping[str, object]]] = []
        self.tools: list[Sequence[Mapping[str, object]] | None] = []
        self.received_api_keys: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
        api_key: str,
    ) -> LLMResponse:
        self.models.append(model)
        self.messages.append(messages)
        self.tools.append(tools)
        self.received_api_keys.append(api_key)
        if not self.responses:
            raise ProviderError("No fake response was scripted.")
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class DevelopmentFakeKeyResolver(ProviderKeyResolver):
    """Return an ephemeral encrypted test credential for the development fake only."""

    async def get_for_user(
        self, user_id: UUID, provider_key_id: UUID
    ) -> EncryptedProviderCredential | None:
        settings = get_settings()
        if settings.app_env == "production" or not settings.fake_provider_enabled:
            return None
        if provider_key_id != uuid5(NAMESPACE_URL, f"forge-fake:{user_id}"):
            return None
        encrypted = encrypt_secret(
            "forge-test-only-fake-key",
            user_id=user_id,
            record_id=provider_key_id,
            master_key=settings.master_key,
        )
        return EncryptedProviderCredential(
            id=provider_key_id,
            user_id=user_id,
            provider=Provider.OPENAI,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
        )


class DemoWorkflowFakeProvider(FakeProvider):
    """Deterministic Clarifier and Planner responses for local M3 smoke tests."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
        api_key: str,
    ) -> LLMResponse:
        self.models.append(model)
        self.messages.append(messages)
        self.tools.append(tools)
        self.received_api_keys.append(api_key)
        provider = Provider.OPENAI
        if tools:
            return LLMResponse(
                provider=provider,
                model=model,
                content=None,
                tool_calls=[
                    ToolCall(
                        id="fake-ask-user",
                        name="ask_user",
                        arguments={
                            "questions": [
                                {
                                    "id": "primary_users",
                                    "prompt": "Who is the primary audience?",
                                    "type": "free_text",
                                    "options": [],
                                    "allow_decide_for_me": True,
                                }
                            ]
                        },
                    )
                ],
                tokens_in=36,
                tokens_out=18,
            )
        plan = {
            "product_brief": "A small web app tailored to the clarified audience.",
            "tech_stack": ["Next.js", "TypeScript"],
            "milestones": [
                {"id": "foundation", "title": "Foundation", "description": "Set up the app."}
            ],
            "tasks": [
                {
                    "id": "landing",
                    "title": "Build the landing page",
                    "description": "Create the first responsive page.",
                    "depends_on": [],
                }
            ],
        }
        return LLMResponse(
            provider=provider,
            model=model,
            content=json.dumps(plan),
            tokens_in=52,
            tokens_out=44,
        )
