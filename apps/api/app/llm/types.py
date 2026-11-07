"""Typed gateway contracts shared by adapters and callers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class Provider(StrEnum):
    """Supported BYOK providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class ModelTier(StrEnum):
    """Model quality/cost tier."""

    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Normalized tool call returned by an LLM."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Normalized completion plus accounting data."""

    provider: Provider
    model: str
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class LLMRunContext:
    """Caller identity and selected encrypted credential reference."""

    user_id: UUID
    provider: Provider
    provider_key_id: UUID
    run_id: UUID | None = None
    agent_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EncryptedProviderCredential:
    """Encrypted provider key data passed to the gateway for call-time decryption."""

    id: UUID
    user_id: UUID
    provider: Provider
    ciphertext: bytes
    nonce: bytes


class ProviderKeyResolver(Protocol):
    """Resolve one encrypted credential using both owner and record IDs."""

    async def get_for_user(
        self, user_id: UUID, provider_key_id: UUID
    ) -> EncryptedProviderCredential | None: ...


class ProviderAdapter(Protocol):
    """Provider-specific transport hidden behind the LLM gateway."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
        api_key: str,
    ) -> LLMResponse: ...
