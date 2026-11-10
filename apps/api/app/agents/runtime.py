"""Single-agent gateway loop with persisted responses for restart recovery."""

from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import RoleTemplate
from app.core.config import get_settings
from app.events.bus import EventBus
from app.llm.client import LLMClient
from app.llm.types import LLMResponse, LLMRunContext, Provider, ToolCall
from app.repos.agents import AgentRepository
from app.repos.runs import RunRepository

ASK_USER_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "Present a batch of structured clarification questions to the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {"type": "array", "minItems": 1, "items": {"type": "object"}}
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    },
}


class BudgetExceededError(RuntimeError):
    """Raised when another LLM call would exceed the configured run budget."""


class AgentRuntime:
    """Call the gateway once per durable step and reuse persisted results on resume."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._event_bus = event_bus or EventBus()

    async def complete(
        self,
        user_id: UUID,
        run_id: UUID,
        provider: Provider,
        provider_key_id: UUID,
        template: RoleTemplate,
        messages: list[dict[str, object]],
        llm_client: LLMClient,
    ) -> LLMResponse:
        """Return the saved step if present, otherwise call, persist, then return it."""
        async with self._sessions() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            if run is None:
                raise LookupError("Run not found.")
            agent = await AgentRepository(session).get_or_create_for_role(
                user_id, run_id, template.name, _template_config(template)
            )
            if agent is None:
                raise LookupError("Run not found.")
            steps = await AgentRepository(session).list_steps_for_agent(user_id, agent.id)
            if steps:
                return _response_from_payload(steps[-1].payload)
            if run.spent_usd >= run.budget_usd:
                raise BudgetExceededError
            tools = [ASK_USER_TOOL] if "ask_user" in template.tool_allowlist else None
            context = LLMRunContext(
                user_id=user_id,
                provider=provider,
                provider_key_id=provider_key_id,
                run_id=run_id,
                agent_id=agent.id,
            )
            response = await llm_client.complete(
                messages,
                tools,
                template.model_tier,
                context,
            )
            payload = _payload_from_response(response)
            step = await AgentRepository(session).create_step(
                user_id,
                agent.id,
                kind="llm",
                payload=payload,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
            )
            if step is None:
                raise LookupError("Agent not found.")
            agent.status = "running"
            agent.model = response.model
            agent.spent_usd += response.cost_usd
            run.spent_usd += response.cost_usd
            event = await self._event_bus.persist(
                session,
                user_id,
                run_id,
                event_type="agent.step_completed",
                payload={"agent_id": str(agent.id), "role": template.name, "idx": step.idx},
            )
            if event is None:
                raise LookupError("Run not found.")
            await session.commit()
            await session.refresh(event)
        await self._event_bus.publish(event)
        if get_settings().fake_provider_enabled:
            await _pause_after_persisted_step()
        return response


async def _pause_after_persisted_step() -> None:
    """Optionally pause the development fake after a durable step for restart tests."""
    seconds = get_settings().fake_provider_step_delay_seconds
    if seconds > 0:
        import asyncio

        await asyncio.sleep(seconds)


def _template_config(template: RoleTemplate) -> dict[str, object]:
    return {
        "name": template.name,
        "system_prompt": template.system_prompt,
        "model_tier": template.model_tier.value,
        "tool_allowlist": list(template.tool_allowlist),
        "output_schema": template.output_schema,
        "max_steps": template.max_steps,
    }


def _payload_from_response(response: LLMResponse) -> dict[str, object]:
    return {
        "provider": response.provider.value,
        "model": response.model,
        "content": response.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in response.tool_calls
        ],
        "tokens_in": response.tokens_in,
        "tokens_out": response.tokens_out,
        "cost_usd": str(response.cost_usd),
    }


def _response_from_payload(payload: dict[str, object]) -> LLMResponse:
    calls: list[ToolCall] = []
    raw_calls = payload.get("tool_calls", [])
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if isinstance(raw, dict):
                args = raw.get("arguments", {})
                calls.append(
                    ToolCall(
                        id=str(raw.get("id", "saved")),
                        name=str(raw.get("name", "")),
                        arguments=cast(dict[str, object], args if isinstance(args, dict) else {}),
                    )
                )
    content = payload.get("content")
    tokens_in = payload.get("tokens_in", 0)
    tokens_out = payload.get("tokens_out", 0)
    return LLMResponse(
        provider=Provider(str(payload["provider"])),
        model=str(payload["model"]),
        content=content if isinstance(content, str) else None,
        tool_calls=calls,
        tokens_in=tokens_in if isinstance(tokens_in, int) else 0,
        tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
        cost_usd=Decimal(str(payload.get("cost_usd", "0"))),
    )
