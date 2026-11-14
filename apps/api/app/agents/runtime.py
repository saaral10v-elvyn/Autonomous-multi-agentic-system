"""Durable agent loop with persisted responses for restart recovery."""

import json
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import RoleTemplate
from app.agents.schemas import TaskResult
from app.agents.tools import ToolRegistry
from app.core.config import get_settings
from app.events.bus import EventBus
from app.llm.client import LLMClient
from app.llm.types import LLMResponse, LLMRunContext, Provider, ToolCall
from app.models.event import Event
from app.models.message import Message
from app.orchestrator.state import IllegalRunTransition
from app.repos.agents import AgentRepository
from app.repos.runs import RunRepository
from app.repos.tasks import TaskRepository
from app.services.tools import ToolExecutionPipeline, ToolExecutionResult

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
        *,
        tool_registry: ToolRegistry | None = None,
        force_new: bool = False,
        agent_id: UUID | None = None,
    ) -> LLMResponse:
        """Return the saved step if present, otherwise call, persist, then return it."""
        async with self._sessions() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            if run is None:
                raise LookupError("Run not found.")
            allowed_states = {"EXECUTING"}
            if template.name == "clarifier":
                allowed_states.add("CLARIFYING")
            if template.name == "planner":
                allowed_states.add("PLANNING")
            if run.status not in allowed_states:
                raise IllegalRunTransition("The run is not ready for an agent step.")
            repository = AgentRepository(session)
            existing_agent = (
                await repository.get_for_id(user_id, agent_id)
                if agent_id is not None
                else await repository.get_for_role(user_id, run_id, template.name)
            )
            agent = (
                await repository.get_for_id(user_id, agent_id)
                if agent_id is not None
                else await repository.get_or_create_for_role(
                    user_id, run_id, template.name, _template_config(template)
                )
            )
            if agent is None:
                raise LookupError("Run not found.")
            if agent.run_id != run_id:
                raise LookupError("Agent not found.")
            is_new_agent = existing_agent is None
            status_changed = agent.status != "running"
            steps = await AgentRepository(session).list_steps_for_agent(user_id, agent.id)
            last_llm_step = next((step for step in reversed(steps) if step.kind == "llm"), None)
            if last_llm_step is not None and not force_new:
                return _response_from_payload(last_llm_step.payload)
            agent_cap = Decimal(str(template.max_cost_usd))
            if agent.spent_usd >= agent_cap:
                raise BudgetExceededError("Agent budget exhausted before the next LLM call.")
            if run.spent_usd >= run.budget_usd:
                raise BudgetExceededError
            previous_run_spend = run.spent_usd
            previous_agent_spend = agent.spent_usd
            if tool_registry is not None:
                tools = tool_registry.schemas(template.tool_allowlist) or None
            elif template.tool_allowlist == ("ask_user",):
                tools = [ASK_USER_TOOL]
            elif template.tool_allowlist:
                raise ValueError("An executable tool registry is required for this role.")
            else:
                tools = None
            context = LLMRunContext(
                user_id=user_id,
                provider=provider,
                provider_key_id=provider_key_id,
                run_id=run_id,
                agent_id=agent.id,
            )
            result = await session.execute(
                select(Message)
                .where(
                    Message.run_id == run_id,
                    Message.sender == "user",
                    (Message.agent_id.is_(None) | (Message.agent_id == agent.id)),
                )
                .order_by(Message.created_at, Message.id)
            )
            known_user_messages = {
                str(message.get("content")) for message in messages if message.get("role") == "user"
            }
            for user_message in result.scalars().all():
                if user_message.content not in known_user_messages:
                    messages.append({"role": "user", "content": user_message.content})
                    known_user_messages.add(user_message.content)
            response = await llm_client.complete(
                messages,
                tools,
                template.model_tier,
                context,
            )
            payload = _payload_from_response(response, messages)
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
            warning_events: list[Event] = []
            if is_new_agent:
                spawned = await self._event_bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.spawned",
                    payload={
                        "agent_id": str(agent.id),
                        "parent_id": str(agent.parent_id) if agent.parent_id else None,
                        "role": agent.role,
                    },
                )
                if spawned is not None:
                    warning_events.append(spawned)
            if is_new_agent or status_changed:
                status_event = await self._event_bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.status",
                    payload={"agent_id": str(agent.id), "status": "running"},
                )
                if status_event is not None:
                    warning_events.append(status_event)
            if response.content is not None:
                message_event = await self._event_bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.message",
                    payload={"agent_id": str(agent.id), "content": response.content},
                )
                if message_event is not None:
                    warning_events.append(message_event)
            if (
                run.budget_usd > 0
                and previous_run_spend / run.budget_usd < Decimal("0.8")
                and run.spent_usd / run.budget_usd >= Decimal("0.8")
            ):
                warning = await self._event_bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="budget.warning",
                    payload={"scope": "run", "spent_usd": str(run.spent_usd)},
                )
                if warning is not None:
                    warning_events.append(warning)
            if previous_agent_spend / agent_cap < Decimal(
                "0.8"
            ) and agent.spent_usd / agent_cap >= Decimal("0.8"):
                warning = await self._event_bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="budget.warning",
                    payload={
                        "scope": "agent",
                        "agent_id": str(agent.id),
                        "spent_usd": str(agent.spent_usd),
                    },
                )
                if warning is not None:
                    warning_events.append(warning)
            event = await self._event_bus.persist(
                session,
                user_id,
                run_id,
                event_type="agent.step_completed",
                payload={"agent_id": str(agent.id), "role": template.name, "idx": step.idx},
            )
            if event is None:
                raise LookupError("Run not found.")
            cost_event = await self._event_bus.persist(
                session,
                user_id,
                run_id,
                event_type="cost.updated",
                payload={
                    "agent_id": str(agent.id),
                    "agent_spent_usd": str(agent.spent_usd),
                    "run_spent_usd": str(run.spent_usd),
                },
            )
            warning_events.append(event)
            if cost_event is not None:
                warning_events.append(cost_event)
            await session.commit()
            for pending_event in warning_events:
                await session.refresh(pending_event)
        for pending_event in warning_events:
            await self._event_bus.publish(pending_event)
        if get_settings().fake_provider_enabled:
            await _pause_after_persisted_step()
        return response

    async def execute_tool_call(
        self,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        tool_call: ToolCall,
        registry: ToolRegistry,
        *,
        reason: str,
    ) -> ToolExecutionResult:
        """Route every model tool call through the risk and persistence pipeline."""
        return await ToolExecutionPipeline(self._sessions, registry, self._event_bus).execute(
            user_id,
            run_id,
            agent_id,
            tool_call.name,
            tool_call.arguments,
            tool_call_id=tool_call.id,
            reason=reason,
        )

    async def run_task(
        self,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        provider: Provider,
        provider_key_id: UUID,
        template: RoleTemplate,
        task_prompt: str,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
    ) -> TaskResult:
        """Run a bounded, replayable agent loop for one scheduled task."""
        messages = await self._saved_input_messages(user_id, agent_id)
        if not messages:
            messages = [
                {"role": "system", "content": template.system_prompt},
                {
                    "role": "user",
                    "content": f"Agent ID: {agent_id}\nTask: {task_prompt}",
                },
            ]
        response: LLMResponse | None = None
        for step_index in range(template.max_steps):
            response = await self.complete(
                user_id,
                run_id,
                provider,
                provider_key_id,
                template,
                messages,
                llm_client,
                tool_registry=tool_registry,
                force_new=step_index > 0,
                agent_id=agent_id,
            )
            if await self._is_interrupted(user_id, agent_id):
                return TaskResult(status="interrupted", summary="The agent was interrupted.")
            if not response.tool_calls:
                summary = response.content or "The agent completed without a text summary."
                try:
                    return TaskResult.model_validate_json(summary)
                except ValueError:
                    return TaskResult(status="completed", summary=summary[:5000])
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                result = await self.execute_tool_call(
                    user_id,
                    run_id,
                    agent_id,
                    call,
                    tool_registry,
                    reason=f"Complete the assigned {template.name} task.",
                )
                if result.status == "suspended":
                    return TaskResult(
                        status="waiting_approval",
                        summary="Waiting for approval before continuing this task.",
                    )
                if result.output is not None and result.output.get("child_task_id"):
                    child_id = result.output.get("child_task_id")
                    try:
                        child_uuid = UUID(str(child_id))
                    except ValueError:
                        raise ValueError("Child task reference is invalid.") from None
                    async with self._sessions() as session:
                        child = await TaskRepository(session).get_for_user(
                            user_id, run_id, child_uuid
                        )
                    if child is None:
                        raise LookupError("Child task not found.")
                    if child.status == "completed" and child.output is not None:
                        result = ToolExecutionResult("completed", child.output)
                    elif child.status in {"failed", "interrupted"}:
                        return TaskResult(
                            status="failed" if child.status == "failed" else "interrupted",
                            summary=f"Child task {child.status}: {child.title}",
                        )
                    else:
                        return TaskResult(
                            status="waiting_approval",
                            summary="A child agent is waiting to finish its task.",
                        )
                if result.output is not None and result.output.get("status") in {
                    "interrupted",
                    "waiting_approval",
                }:
                    return TaskResult(
                        status="waiting_approval",
                        summary="A child agent is waiting for approval.",
                    )
                if call.name == "finish" and result.output is not None:
                    return TaskResult.model_validate(result.output)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result.output or {}),
                    }
                )
        return TaskResult(
            status="failed",
            summary=f"Agent reached the configured limit of {template.max_steps} steps.",
        )

    async def _saved_input_messages(self, user_id: UUID, agent_id: UUID) -> list[dict[str, object]]:
        """Restore the exact persisted context when resuming a worker attempt."""
        async with self._sessions() as session:
            steps = await AgentRepository(session).list_steps_for_agent(user_id, agent_id)
            latest = next((step for step in reversed(steps) if step.kind == "llm"), None)
            raw = latest.payload.get("input_messages") if latest is not None else None
            if not isinstance(raw, list):
                return []
            return [dict(message) for message in raw if isinstance(message, dict)]

    async def _is_interrupted(self, user_id: UUID, agent_id: UUID) -> bool:
        """Observe an interrupt committed while an LLM call was in flight."""
        async with self._sessions() as session:
            agent = await AgentRepository(session).get_for_id(user_id, agent_id)
            return agent is None or agent.status == "interrupted"


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
        "max_cost_usd": template.max_cost_usd,
    }


def _payload_from_response(
    response: LLMResponse, input_messages: list[dict[str, object]]
) -> dict[str, object]:
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
        "input_messages": input_messages,
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
