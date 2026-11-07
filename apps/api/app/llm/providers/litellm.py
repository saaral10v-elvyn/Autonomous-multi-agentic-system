"""LiteLLM adapter; provider SDKs are accessed only inside app/llm/."""

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.llm.errors import ProviderError
from app.llm.types import LLMResponse, Provider, ToolCall


class LiteLLMProvider:
    """Normalize LiteLLM chat completions and accounting into gateway types."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
        api_key: str,
    ) -> LLMResponse:
        try:
            from litellm import acompletion, completion_cost

            response: Any = await acompletion(
                model=model,
                messages=[dict(message) for message in messages],
                tools=[dict(tool) for tool in tools] if tools is not None else None,
                api_key=api_key,
            )
            choice = response.choices[0]
            message = choice.message
            usage = response.usage
            raw_cost = completion_cost(completion_response=response)
            normalized_calls = self._normalize_tool_calls(message.tool_calls or [])
            provider_text = model.split("/", maxsplit=1)[0]
            provider = (
                Provider.ANTHROPIC
                if provider_text == "anthropic"
                else Provider("openai" if provider_text == "openai" else "gemini")
            )
            return LLMResponse(
                provider=provider,
                model=model,
                content=message.content,
                tool_calls=normalized_calls,
                tokens_in=int(usage.prompt_tokens or 0),
                tokens_out=int(usage.completion_tokens or 0),
                cost_usd=Decimal(str(raw_cost or 0)),
            )
        except ProviderError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None)
            error_name = type(error).__name__
            retryable = (
                isinstance(status, int) and (status in {408, 409, 425, 429} or status >= 500)
            ) or error_name in {
                "APIConnectionError",
                "Timeout",
                "TimeoutError",
                "RateLimitError",
                "ServiceUnavailableError",
            }
            raise ProviderError(retryable=retryable) from None

    @staticmethod
    def _normalize_tool_calls(raw_calls: Sequence[Any]) -> list[ToolCall]:
        normalized: list[ToolCall] = []
        for call in raw_calls:
            try:
                arguments_raw = call.function.arguments
                arguments = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                )
                if not isinstance(arguments, dict):
                    raise ValueError
                normalized.append(
                    ToolCall(
                        id=str(call.id),
                        name=str(call.function.name),
                        arguments=arguments,
                    )
                )
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                raise ProviderError("Provider returned an invalid tool call.") from None
        return normalized
