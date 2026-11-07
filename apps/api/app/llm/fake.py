"""Scripted provider adapter for tests; it never makes network calls."""

from collections import deque
from collections.abc import Mapping, Sequence

from app.llm.errors import ProviderError
from app.llm.types import LLMResponse, ProviderAdapter


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
