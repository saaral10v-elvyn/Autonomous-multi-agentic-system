"""Run orchestration, human-input flows, and durable approval resumption."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.demo_tools import agent_tool_registry
from app.agents.registry import ROLE_TEMPLATES, RoleTemplate
from app.agents.runtime import AgentRuntime, BudgetExceededError
from app.agents.schemas import PlanDocument, QuestionSet
from app.core.config import get_settings
from app.core.db import SessionFactory, engine
from app.events.bus import EventBus
from app.gate.engine import GateDecision, RiskClass, WorkflowGate, decide
from app.integrations.registry import run_tool_handlers
from app.llm.client import LLMClient
from app.llm.errors import LLMRequestError, ProviderKeyUnavailable
from app.llm.types import ModelTier, Provider
from app.models.agent import Agent
from app.models.event import Event
from app.models.message import Message
from app.models.project import Project
from app.models.provider_key import ProviderKey
from app.models.question import Question
from app.models.run import Run
from app.models.tool_execution import ToolExecution
from app.orchestrator.state import IllegalRunTransition, stage_transition
from app.repos.agents import AgentRepository
from app.repos.approvals import ApprovalRepository
from app.repos.plans import PlanRepository
from app.repos.projects import ProjectRepository
from app.repos.provider_keys import ProviderKeyRepository
from app.repos.questions import QuestionRepository
from app.repos.runs import RunRepository
from app.repos.tasks import TaskRepository
from app.repos.tool_executions import ToolExecutionRepository
from app.sandbox.lifecycle import destroy_run_sandbox
from app.services.reports import create_final_report
from app.services.spawning import AgentSpawner
from app.services.tools import (
    ToolExecutionPipeline,
    ToolExecutionResult,
    _reviewer_passed,
    _tester_report_passed,
)


class WorkflowConflict(ValueError):
    """The run is not currently accepting the requested operation."""


class InvalidAnswers(ValueError):
    """The submitted responses do not match the open question schema."""


async def enqueue_advance(user_id: UUID, run_id: UUID) -> None:
    """Queue one durable run-advance job using owner and run identifiers."""
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.core.config import get_settings

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job("advance_run", str(user_id), str(run_id))
    finally:
        await pool.close()


def _provider_context(user_id: UUID, keys: list[ProviderKey]) -> tuple[Provider, UUID] | None:
    if get_settings().fake_provider_enabled:
        return Provider.OPENAI, uuid5(NAMESPACE_URL, f"forge-fake:{user_id}")
    if not keys:
        return None
    key = keys[0]
    return Provider(str(key.provider)), key.id


async def _publish_all(bus: EventBus, events: list[Event]) -> None:
    for event in events:
        await bus.publish(event)


async def _mark_failed(user_id: UUID, run_id: UUID, message: str) -> None:
    bus = EventBus()
    async with SessionFactory() as session:
        run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
        if run is None or run.status in {"FAILED", "CANCELLED", "COMPLETED"}:
            return
        event = await stage_transition(
            session,
            user_id,
            run,
            "FAILED",
            event_bus=bus,
            payload={"message": message},
        )
        failure_event = await bus.persist(
            session,
            user_id,
            run_id,
            event_type="run.failed",
            payload={"message": message},
        )
        await session.commit()
        await session.refresh(event)
        events = [event]
        if isinstance(failure_event, Event):
            await session.refresh(failure_event)
            events.append(failure_event)
    await _publish_all(bus, events)


async def _mark_completed(user_id: UUID, run_id: UUID) -> None:
    bus = EventBus()
    async with SessionFactory() as session:
        run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
        if run is None or run.status != "REPORTING":
            return
        run.completed_at = datetime.now(UTC)
        report = await create_final_report(session, user_id, run_id)
        if report is None:
            await session.rollback()
            await _mark_failed(user_id, run_id, "The final report could not be saved.")
            return
        report_event = (
            await bus.persist(
                session,
                user_id,
                run_id,
                event_type="artifact.created",
                payload={"artifact_id": str(report.id), "kind": "report", "name": report.name},
            )
            if report is not None
            else None
        )
        state = await stage_transition(session, user_id, run, "COMPLETED", event_bus=bus)
        completed = await bus.persist(
            session, user_id, run_id, event_type="run.completed", payload={"status": "COMPLETED"}
        )
        await session.commit()
        await session.refresh(state)
        events = [state]
        if report_event is not None:
            await session.refresh(report_event)
            events.insert(0, report_event)
        if completed is not None:
            await session.refresh(completed)
            events.append(completed)
    await _publish_all(bus, events)
    await destroy_run_sandbox(run_id)


def _workflow_task_description(role: str) -> str:
    """Keep post-build agent tasks constrained to their M6/M7 responsibilities."""
    instructions = {
        "tester": (
            "Run the project test suite in the sandbox. Use tester_report with the actual "
            "command, exit code, and test counts, then finish with a concise result."
        ),
        "reviewer": (
            "Review the completed project diff for correctness and security. Use "
            "review_complete with a pass or fail and concrete findings, then finish."
        ),
        "deployer": (
            "Deploy the tested application using the connected GitHub and Vercel integrations. "
            "List integrations first, use only returned integration IDs, push or deploy only "
            "through the approval gate, poll for success, and run http_check on the live HTTPS "
            "URL. Report repository and live URLs. Never expose credentials."
        ),
        "reporter": (
            "Summarize the idea, implementation, test results, approvals, cost, known issues, "
            "repository URL, and live URL. Finish with a structured result."
        ),
    }
    return instructions[role]


class RunOrchestrator:
    """Advance a run through Clarifier and Planner until human input is needed."""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client
        self._bus = EventBus()
        self._runtime = AgentRuntime(SessionFactory, self._bus)

    async def advance_until_input(self, user_id: UUID, run_id: UUID) -> str:
        """Advance at most through the current phase, stopping at a user gate."""
        lock_key = int.from_bytes(sha256(run_id.bytes).digest()[:8], "big") & ((1 << 63) - 1)
        async with engine.connect() as connection:
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key}
                )
            )
            if not acquired:
                async with SessionFactory() as session:
                    run = await RunRepository(session).get_for_user(user_id, run_id)
                if run is None:
                    raise LookupError("Run not found.")
                return run.status
            try:
                return await self._advance_until_input_locked(user_id, run_id)
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
                )

    async def _advance_until_input_locked(self, user_id: UUID, run_id: UUID) -> str:
        """Run one workflow while holding the per-run PostgreSQL advisory lock."""
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id)
            keys = await ProviderKeyRepository(session).list_for_user(user_id)
        if run is None:
            raise LookupError("Run not found.")
        if run.status in {"EXECUTING", "VERIFYING", "DEPLOYING", "REPORTING"}:
            return await self._advance_tasks(user_id, run_id)
        if run.status not in {"CREATED", "CLARIFYING", "PLANNING"}:
            return run.status
        provider_info = _provider_context(user_id, keys)
        if provider_info is None:
            await _mark_failed(
                user_id,
                run_id,
                "No provider key is configured. Add a provider key to continue this run.",
            )
            return "FAILED"
        provider, provider_key_id = provider_info

        if run.status == "CREATED":
            async with SessionFactory() as session:
                current = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
                if current is None:
                    raise LookupError("Run not found.")
                event = await stage_transition(
                    session, user_id, current, "CLARIFYING", event_bus=self._bus
                )
                await session.commit()
                await session.refresh(event)
            await self._bus.publish(event)
            run.status = "CLARIFYING"

        if run.status == "CLARIFYING":
            await self._clarify(user_id, run_id, run.idea, provider, provider_key_id)
            async with SessionFactory() as session:
                latest = await RunRepository(session).get_for_user(user_id, run_id)
                return latest.status if latest is not None else "FAILED"

        if run.status == "PLANNING":
            await self._plan(user_id, run_id, run.idea, provider, provider_key_id)
            async with SessionFactory() as session:
                latest = await RunRepository(session).get_for_user(user_id, run_id)
                next_status = latest.status if latest is not None else "FAILED"
            if next_status == "EXECUTING":
                await enqueue_advance(user_id, run_id)
            return next_status
        return run.status

    async def _advance_tasks(self, user_id: UUID, run_id: UUID) -> str:
        """Run dependency-ready tasks in bounded batches, resuming running rows."""
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id)
            keys = await ProviderKeyRepository(session).list_for_user(user_id)
        if run is None:
            raise LookupError("Run not found.")
        provider_info = _provider_context(user_id, keys)
        if provider_info is None:
            return "EXECUTING"
        provider, provider_key_id = provider_info
        template = ROLE_TEMPLATES["coder"]
        while True:
            async with SessionFactory() as session:
                current = await RunRepository(session).get_for_user(user_id, run_id)
                if current is None or current.status not in {
                    "EXECUTING",
                    "VERIFYING",
                    "DEPLOYING",
                    "REPORTING",
                }:
                    return current.status if current is not None else "FAILED"
                batch = await TaskRepository(session).claim_ready(user_id, run_id, 4)
                if batch is None:
                    raise LookupError("Run not found.")
                tasks, blocked_tasks, newly_claimed = batch
                assignments: list[tuple[UUID, UUID, str, int, RoleTemplate]] = []
                pending_events: list[Event | None] = []
                for blocked_task in blocked_tasks:
                    blocked_event = await self._bus.persist(
                        session,
                        user_id,
                        run_id,
                        event_type="task.updated",
                        payload={
                            "task_id": str(blocked_task.id),
                            "status": "blocked",
                            "result": blocked_task.output or {},
                        },
                    )
                    pending_events.append(blocked_event)
                for task in tasks:
                    task_role = str(task.input.get("system_role", "coder"))
                    task_template = ROLE_TEMPLATES.get(task_role, template)
                    attempts = task.input.get("attempts", 0)
                    previous_attempts = attempts if isinstance(attempts, int) else 0
                    if task.id in newly_claimed:
                        attempt_count = previous_attempts + 1
                        task.input = {**task.input, "attempts": attempt_count}
                    else:
                        attempt_count = max(1, previous_attempts)
                    config = {
                        "name": task_template.name,
                        "system_prompt": task_template.system_prompt,
                        "model_tier": task_template.model_tier.value,
                        "tool_allowlist": list(task_template.tool_allowlist),
                        "output_schema": task_template.output_schema,
                        "max_steps": task_template.max_steps,
                        "max_cost_usd": task_template.max_cost_usd,
                    }
                    agent_repo = AgentRepository(session)
                    agent = (
                        await agent_repo.get_for_id(user_id, task.agent_id)
                        if task.agent_id is not None and attempt_count == 1
                        else None
                    )
                    is_new_agent = agent is None
                    if agent is None:
                        agent_task_id = (
                            task.id
                            if attempt_count == 1
                            else uuid5(
                                NAMESPACE_URL,
                                f"forge-task:{task.id}:attempt:{attempt_count}",
                            )
                        )
                        agent = await agent_repo.get_or_create_for_task(
                            user_id, run_id, task_role, agent_task_id, config
                        )
                    if agent is None:
                        raise LookupError("Run not found.")
                    status_changed = agent.status != "running"
                    task.agent_id = agent.id
                    agent.status = "running"
                    if is_new_agent:
                        pending_events.append(
                            await self._bus.persist(
                                session,
                                user_id,
                                run_id,
                                event_type="agent.spawned",
                                payload={
                                    "agent_id": str(agent.id),
                                    "parent_id": None,
                                    "role": agent.role,
                                    "task_id": str(task.id),
                                },
                            )
                        )
                    if is_new_agent or status_changed:
                        pending_events.append(
                            await self._bus.persist(
                                session,
                                user_id,
                                run_id,
                                event_type="agent.status",
                                payload={"agent_id": str(agent.id), "status": "running"},
                            )
                        )
                    if task.id in newly_claimed:
                        task_event = await self._bus.persist(
                            session,
                            user_id,
                            run_id,
                            event_type="task.updated",
                            payload={
                                "task_id": str(task.id),
                                "agent_id": str(agent.id),
                                "status": "running",
                            },
                        )
                        pending_events.append(task_event)
                    assignments.append(
                        (
                            task.id,
                            agent.id,
                            f"{task.title}: {task.description}",
                            attempt_count,
                            task_template,
                        )
                    )
                await session.commit()
                committed_events = await _refresh_events(session, pending_events)
            await _publish_all(self._bus, committed_events)
            if not assignments:
                async with SessionFactory() as session:
                    final_tasks = await TaskRepository(session).list_for_run(user_id, run_id)
                if final_tasks is not None and all(
                    task.status == "completed" for task in final_tasks
                ):
                    completed_by_role = {
                        str(task.input.get("system_role"))
                        for task in final_tasks
                        if task.input.get("system_role")
                    }
                    next_role = next(
                        (
                            role
                            for role in ("tester", "reviewer", "deployer", "reporter")
                            if role not in completed_by_role
                        ),
                        None,
                    )
                    if next_role is None:
                        await _mark_completed(user_id, run_id)
                        return "COMPLETED"
                    phase_by_role = {
                        "tester": "VERIFYING",
                        "reviewer": "VERIFYING",
                        "deployer": "DEPLOYING",
                        "reporter": "REPORTING",
                    }
                    workflow_tasks = {
                        str(task.input.get("system_role")): task
                        for task in final_tasks
                        if task.input.get("system_role")
                    }
                    if next_role == "tester":
                        dependencies = [
                            task.id for task in final_tasks if not task.input.get("system_role")
                        ]
                    else:
                        prerequisite = {
                            "reviewer": "tester",
                            "deployer": "reviewer",
                            "reporter": "deployer",
                        }[next_role]
                        prior_task = workflow_tasks.get(prerequisite)
                        dependencies = [prior_task.id] if prior_task is not None else []
                    async with SessionFactory() as session:
                        current_run = await RunRepository(session).get_for_user(
                            user_id, run_id, lock=True
                        )
                        if current_run is None:
                            raise LookupError("Run not found.")
                        target_phase = phase_by_role[next_role]
                        state_event = None
                        if current_run.status != target_phase:
                            state_event = await stage_transition(
                                session,
                                user_id,
                                current_run,
                                target_phase,
                                event_bus=self._bus,
                                payload={"role": next_role},
                            )
                        role_task, created = await TaskRepository(session).ensure_workflow_task(
                            user_id,
                            run_id,
                            next_role,
                            _workflow_task_description(next_role),
                            dependencies,
                        )
                        if role_task is None:
                            raise LookupError("Run not found.")
                        phase_event = (
                            await self._bus.persist(
                                session,
                                user_id,
                                run_id,
                                event_type="run.phase_changed",
                                payload={"phase": target_phase, "role": next_role},
                            )
                            if created
                            else None
                        )
                        await session.commit()
                        committed = await _refresh_events(session, [state_event, phase_event])
                    await _publish_all(self._bus, committed)
                    continue
                remaining = [task for task in final_tasks or [] if task.status != "completed"]
                if remaining and all(
                    task.status in {"failed", "blocked", "interrupted"} for task in remaining
                ):
                    await _mark_failed(
                        user_id, run_id, "All remaining tasks failed or were blocked."
                    )
                    return "FAILED"
                return "EXECUTING"
            outcomes = await asyncio.gather(
                *(
                    self._execute_task(
                        user_id,
                        run_id,
                        task_id,
                        agent_id,
                        prompt,
                        attempt,
                        provider,
                        provider_key_id,
                        task_template,
                    )
                    for task_id, agent_id, prompt, attempt, task_template in assignments
                )
            )
            if any(outcome == "paused" for outcome in outcomes):
                return "PAUSED"
            if any(outcome == "suspended" for outcome in outcomes):
                return "AWAITING_APPROVAL"

    async def _execute_task(
        self,
        user_id: UUID,
        run_id: UUID,
        task_id: UUID,
        agent_id: UUID,
        prompt: str,
        attempt: int,
        provider: Provider,
        provider_key_id: UUID,
        template: RoleTemplate,
    ) -> str:
        """Execute and durably finish one scheduled task attempt."""
        from app.agents.schemas import TaskResult

        spawner = AgentSpawner(SessionFactory, self._llm, provider, provider_key_id, self._bus)
        registry = agent_tool_registry(
            spawn_handler=spawner.handler(user_id, run_id, agent_id),
            extra_handlers=await run_tool_handlers(user_id, run_id, agent_id, self._bus),
        )
        try:
            result = await self._runtime.run_task(
                user_id,
                run_id,
                agent_id,
                provider,
                provider_key_id,
                template,
                prompt,
                self._llm,
                registry,
            )
        except IllegalRunTransition:
            async with SessionFactory() as session:
                paused_run = await RunRepository(session).get_for_user(user_id, run_id)
            if paused_run is not None and paused_run.status == "PAUSED":
                return "paused"
            result = TaskResult(status="failed", summary="The task could not continue.")
        except BudgetExceededError as error:
            result = TaskResult(status="failed", summary=str(error))
        except (LookupError, ValueError, LLMRequestError, ProviderKeyUnavailable) as error:
            result = TaskResult(status="failed", summary=str(error))
        if result.status == "completed" and template.name in {
            "tester",
            "reviewer",
            "deployer",
        }:
            async with SessionFactory() as session:
                valid = False
                if template.name == "tester":
                    valid = await _tester_report_passed(session, user_id, run_id, agent_id)
                elif template.name == "reviewer":
                    valid = await _reviewer_passed(session, user_id, run_id, agent_id)
                else:
                    await _persist_delivery_links(session, user_id, run_id, agent_id, self._bus)
                    valid = await _deployer_evidence(session, user_id, run_id, agent_id)
                if not valid:
                    result = TaskResult(
                        status="failed",
                        summary=(
                            f"{template.name.title()} cannot complete without its required "
                            "verified tool evidence."
                        ),
                    )
        status_value: Literal[
            "completed",
            "failed",
            "interrupted",
            "waiting_approval",
            "running",
            "pending",
        ] = result.status
        if status_value == "waiting_approval":
            status_value = "running"
        if status_value == "failed" and attempt < 2:
            status_value = "pending"
        async with SessionFactory() as session:
            task = await TaskRepository(session).finish(
                user_id,
                run_id,
                task_id,
                status_value,
                result.model_dump(mode="json"),
            )
            agent = await AgentRepository(session).get_for_id(user_id, agent_id)
            if task is None or agent is None:
                raise LookupError("Task or agent not found.")
            delivery_event = (
                await _persist_delivery_links(session, user_id, run_id, agent_id, self._bus)
                if status_value == "completed" and task.external_id == "__m8_deployer__"
                else None
            )
            if status_value in {"completed", "failed", "interrupted"}:
                agent.status = (
                    "completed"
                    if status_value == "completed"
                    else "interrupted"
                    if status_value == "interrupted"
                    else "failed"
                )
            task_event = await self._bus.persist(
                session,
                user_id,
                run_id,
                event_type="task.updated",
                payload={
                    "task_id": str(task.id),
                    "agent_id": str(agent.id),
                    "status": status_value,
                    "result": result.model_dump(mode="json"),
                },
            )
            status_event = await self._bus.persist(
                session,
                user_id,
                run_id,
                event_type="agent.status",
                payload={"agent_id": str(agent.id), "status": agent.status},
            )
            await session.commit()
            events = await _refresh_events(session, [delivery_event, task_event, status_event])
        await _publish_all(self._bus, events)
        return "suspended" if result.status == "waiting_approval" else status_value

    async def _clarify(
        self, user_id: UUID, run_id: UUID, idea: str, provider: Provider, key_id: UUID
    ) -> None:
        template = ROLE_TEMPLATES["clarifier"]
        tool_registry = agent_tool_registry()
        try:
            response = await self._runtime.complete(
                user_id,
                run_id,
                provider,
                key_id,
                template,
                [
                    {"role": "system", "content": template.system_prompt},
                    {"role": "user", "content": idea},
                ],
                self._llm,
                tool_registry=tool_registry,
            )
            if len(response.tool_calls) != 1 or response.tool_calls[0].name != "ask_user":
                raise ValueError("Clarifier did not return the ask_user tool.")
            tool_call = response.tool_calls[0]
            questions = QuestionSet.model_validate(tool_call.arguments)
            async with SessionFactory() as session:
                agent = await AgentRepository(session).get_for_role(user_id, run_id, template.name)
            if agent is None:
                raise LookupError("Clarifier agent was not found.")
            tool_result = await self._runtime.execute_tool_call(
                user_id,
                run_id,
                agent.id,
                tool_call,
                tool_registry,
                reason="Present the clarifying questions required to prepare a plan.",
            )
            if tool_result.status != "completed" or tool_result.output is None:
                return
            questions = QuestionSet.model_validate(tool_result.output)
        except BudgetExceededError:
            await _mark_failed(user_id, run_id, "Run budget exhausted before the next LLM call.")
            return
        except (ProviderKeyUnavailable, LLMRequestError, ValidationError, ValueError):
            await _mark_failed(user_id, run_id, "The clarifying questions could not be prepared.")
            return
        bus = self._bus
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            if run is None or run.status != "CLARIFYING":
                return
            question = await QuestionRepository(session).create_for_run(
                user_id,
                run_id,
                question_schema=questions.model_dump(mode="json"),
            )
            if question is None:
                raise LookupError("Run not found.")
            completed_agent = await _complete_agent(session, user_id, run_id, "clarifier")
            status_event = (
                await bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.status",
                    payload={
                        "agent_id": str(completed_agent.id),
                        "status": "completed",
                    },
                )
                if completed_agent is not None
                else None
            )
            question_event = await bus.persist(
                session,
                user_id,
                run_id,
                event_type="question.asked",
                payload={
                    "question_id": str(question.id),
                    "questions": questions.model_dump(mode="json"),
                },
            )
            state_event = await stage_transition(
                session, user_id, run, "AWAITING_ANSWERS", event_bus=bus
            )
            await session.commit()
            events = await _refresh_events(session, [status_event, question_event, state_event])
        await _publish_all(bus, events)

    async def _plan(
        self, user_id: UUID, run_id: UUID, idea: str, provider: Provider, key_id: UUID
    ) -> None:
        async with SessionFactory() as session:
            question = await QuestionRepository(session).latest_open_for_run(user_id, run_id)
            # Answered questions are intentionally retained for Planner context.
            if question is None:
                question = await _latest_answered_question(session, user_id, run_id)
        template = ROLE_TEMPLATES["planner"]
        user_context = {
            "idea": idea,
            "answers": question.answers if question is not None else {},
            "output_schema": PlanDocument.model_json_schema(),
        }
        try:
            response = await self._runtime.complete(
                user_id,
                run_id,
                provider,
                key_id,
                template,
                [
                    {"role": "system", "content": template.system_prompt},
                    {"role": "user", "content": json.dumps(user_context)},
                ],
                self._llm,
            )
            if response.content is None:
                raise ValueError("Planner response is empty.")
            plan = PlanDocument.model_validate_json(response.content)
        except BudgetExceededError:
            await _mark_failed(user_id, run_id, "Run budget exhausted before the next LLM call.")
            return
        except (ProviderKeyUnavailable, LLMRequestError, ValidationError, ValueError):
            await _mark_failed(user_id, run_id, "The project plan could not be prepared.")
            return
        bus = self._bus
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            if run is None or run.status != "PLANNING":
                return
            saved = await PlanRepository(session).save_for_run(
                user_id, run_id, plan.model_dump(mode="json")
            )
            if saved is None:
                raise LookupError("Run not found.")
            completed_agent = await _complete_agent(session, user_id, run_id, "planner")
            completed_status_event = (
                await bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.status",
                    payload={
                        "agent_id": str(completed_agent.id),
                        "status": "completed",
                    },
                )
                if completed_agent is not None
                else None
            )
            plan_event = await bus.persist(
                session,
                user_id,
                run_id,
                event_type="plan.created",
                payload={"artifact_id": str(saved.id), "plan": plan.model_dump(mode="json")},
            )
            pending = [completed_status_event, plan_event]
            plan_decision = decide(
                run.autonomy_level,
                RiskClass.READ,
                {"gate": WorkflowGate.PLAN_APPROVAL.value},
            )
            if plan_decision is GateDecision.AUTO:
                task_rows = await TaskRepository(session).create_from_plan(user_id, run_id, plan)
                if task_rows is None:
                    raise LookupError("Run not found.")
                tasks_event = await bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="task.updated",
                    payload={"count": len(task_rows), "status": "pending"},
                )
                if tasks_event is not None:
                    pending.append(tasks_event)
                auto_event = await bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="plan.auto_approved",
                    payload={"artifact_id": str(saved.id)},
                )
                pending.append(auto_event)
                state_event = await stage_transition(
                    session,
                    user_id,
                    run,
                    "EXECUTING",
                    event_bus=bus,
                    payload={"plan_auto_approved": True},
                )
            else:
                state_event = await stage_transition(
                    session, user_id, run, "AWAITING_PLAN_APPROVAL", event_bus=bus
                )
            pending.append(state_event)
            await session.commit()
            events = await _refresh_events(session, pending)
        await _publish_all(bus, events)


async def _persist_delivery_links(
    session: AsyncSession, user_id: UUID, run_id: UUID, agent_id: UUID, bus: EventBus
) -> Event | None:
    """Copy only validated public delivery URLs from safe adapter outputs to the project."""
    run = await RunRepository(session).get_for_user(user_id, run_id)
    if run is None:
        return None
    executions = await session.scalars(
        select(ToolExecution)
        .join(Run, Run.id == ToolExecution.run_id)
        .where(
            Run.user_id == user_id,
            ToolExecution.run_id == run_id,
            ToolExecution.agent_id == agent_id,
            ToolExecution.status == "completed",
        )
        .order_by(ToolExecution.completed_at, ToolExecution.created_at, ToolExecution.id)
    )
    repo_url: str | None = None
    live_url: str | None = None
    for execution in executions.all():
        output = execution.output_payload or {}
        raw_url = output.get("html_url") if execution.tool_name.startswith("github_") else None
        if execution.tool_name == "vercel_deployment_url":
            raw_url = output.get("url")
        if not isinstance(raw_url, str):
            continue
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        if execution.tool_name.startswith("github_") and parsed.hostname.lower() == "github.com":
            repo_url = raw_url
        if execution.tool_name == "vercel_deployment_url":
            live_url = raw_url
    if repo_url is None and live_url is None:
        return None
    project = await ProjectRepository(session).update_delivery_links(
        user_id, run.project_id, repo_url=repo_url, live_url=live_url
    )
    if project is None:
        return None
    return await bus.persist(
        session,
        user_id,
        run_id,
        event_type="project.delivery_updated",
        payload={"repo_url": repo_url, "live_url": live_url},
    )


async def _deployer_evidence(
    session: AsyncSession, user_id: UUID, run_id: UUID, agent_id: UUID
) -> bool:
    """Require a ready deployment, its public URL, and a passing HTTP smoke check."""
    executions = await session.scalars(
        select(ToolExecution)
        .join(Run, Run.id == ToolExecution.run_id)
        .where(
            Run.user_id == user_id,
            ToolExecution.run_id == run_id,
            ToolExecution.agent_id == agent_id,
            ToolExecution.status == "completed",
        )
        .order_by(ToolExecution.completed_at, ToolExecution.created_at, ToolExecution.id)
    )
    records = list(executions.all())
    outputs = {execution.tool_name: execution.output_payload or {} for execution in records}
    status = outputs.get("vercel_deployment_status", {})
    url = outputs.get("vercel_deployment_url", {})
    check = outputs.get("http_check", {})
    url_execution = next(
        (item for item in reversed(records) if item.tool_name == "vercel_deployment_url"), None
    )
    check_execution = next(
        (item for item in reversed(records) if item.tool_name == "http_check"), None
    )
    run = await session.scalar(select(Run).where(Run.id == run_id, Run.user_id == user_id))
    project = (
        await session.scalar(
            select(Project).where(Project.id == run.project_id, Project.user_id == user_id)
        )
        if run is not None
        else None
    )
    raw_url = url.get("url")
    parsed = urlsplit(raw_url) if isinstance(raw_url, str) else None
    return (
        "vercel_deploy" in outputs
        and status.get("readyState") == "READY"
        and parsed is not None
        and parsed.scheme == "https"
        and parsed.hostname is not None
        and check_execution is not None
        and check_execution.input_payload.get("url") == raw_url
        and check.get("ok") is True
        and check.get("status_code") == 200
        and project is not None
        and bool(project.repo_url)
        and project.live_url == raw_url
        and url_execution is not None
    )


async def _complete_agent(
    session: AsyncSession, user_id: UUID, run_id: UUID, role: str
) -> Agent | None:
    agent = await session.scalar(
        select(Agent)
        .join(Run, Run.id == Agent.run_id)
        .where(Run.user_id == user_id, Run.id == run_id, Agent.role == role)
        .with_for_update()
    )
    if agent is not None:
        agent.status = "completed"
    return agent


async def _latest_answered_question(
    session: AsyncSession, user_id: UUID, run_id: UUID
) -> Question | None:
    result = await session.execute(
        select(Question)
        .join(Run, Run.id == Question.run_id)
        .where(Run.user_id == user_id, Run.id == run_id, Question.status == "answered")
        .order_by(Question.created_at.desc(), Question.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _refresh_events(session: AsyncSession, items: Sequence[Event | None]) -> list[Event]:
    events: list[Event] = []
    for item in items:
        if isinstance(item, Event):
            await session.refresh(item)
            events.append(item)
    return events


def _validate_answers(question: Question, answers: Mapping[str, object]) -> dict[str, object]:
    schema = QuestionSet.model_validate(question.question_schema)
    expected = {item.id for item in schema.questions}
    if set(answers) != expected:
        raise InvalidAnswers("Answers must include exactly the open question IDs.")
    normalized: dict[str, object] = {}
    for item in schema.questions:
        answer = answers[item.id]
        if answer == "decide_for_me" and item.allow_decide_for_me:
            normalized[item.id] = answer
            continue
        if item.type == "free_text":
            if not isinstance(answer, str) or not answer.strip():
                raise InvalidAnswers(f"Question '{item.id}' requires text or decide_for_me.")
            normalized[item.id] = answer.strip()
            continue
        choices = {option.id for option in item.options}
        if item.type == "single_choice":
            if not isinstance(answer, str) or answer not in choices:
                raise InvalidAnswers(f"Question '{item.id}' has an invalid choice.")
            normalized[item.id] = answer
            continue
        if (
            not isinstance(answer, list)
            or not answer
            or any(not isinstance(choice, str) or choice not in choices for choice in answer)
            or len(set(answer)) != len(answer)
        ):
            raise InvalidAnswers(f"Question '{item.id}' has invalid choices.")
        normalized[item.id] = answer
    return normalized


class QuestionService:
    """Shared REST and WebSocket question answer flow."""

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._bus = event_bus or EventBus()

    async def submit(self, user_id: UUID, run_id: UUID, answers: Mapping[str, object]) -> Run:
        run = await RunRepository(self._session).get_for_user(user_id, run_id, lock=True)
        if run is None:
            raise LookupError("Run not found.")
        if run.status != "AWAITING_ANSWERS":
            raise WorkflowConflict("This run is not waiting for answers.")
        question = await QuestionRepository(self._session).latest_open_for_run(user_id, run_id)
        if question is None:
            raise WorkflowConflict("No open questions were found for this run.")
        normalized = _validate_answers(question, answers)
        question.answers = normalized
        question.status = "answered"
        self._session.add(Message(run_id=run_id, sender="user", content=json.dumps(normalized)))
        answered_event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="question.answered",
            payload={"question_id": str(question.id), "answers": normalized},
        )
        state_event = await stage_transition(
            self._session, user_id, run, "PLANNING", event_bus=self._bus
        )
        await self._session.commit()
        events = await _refresh_events(self._session, [answered_event, state_event])
        await _publish_all(self._bus, events)
        return run


class PlanService:
    """Shared REST and WebSocket plan update/approval operations."""

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._bus = event_bus or EventBus()

    async def update(self, user_id: UUID, run_id: UUID, plan: PlanDocument) -> dict[str, object]:
        run = await RunRepository(self._session).get_for_user(user_id, run_id, lock=True)
        if run is None:
            raise LookupError("Run not found.")
        if run.status != "AWAITING_PLAN_APPROVAL":
            raise WorkflowConflict("This run is not waiting for plan approval.")
        value = plan.model_dump(mode="json")
        artifact = await PlanRepository(self._session).save_for_run(user_id, run_id, value)
        if artifact is None:
            raise LookupError("Run not found.")
        event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="plan.updated",
            payload={"artifact_id": str(artifact.id), "plan": value},
        )
        await self._session.commit()
        events = await _refresh_events(self._session, [event])
        await _publish_all(self._bus, events)
        return value

    async def approve(self, user_id: UUID, run_id: UUID) -> Run:
        run = await RunRepository(self._session).get_for_user(user_id, run_id, lock=True)
        if run is None:
            raise LookupError("Run not found.")
        if run.status != "AWAITING_PLAN_APPROVAL":
            raise WorkflowConflict("This run is not waiting for plan approval.")
        plan = await PlanRepository(self._session).latest_for_run(user_id, run_id)
        if plan is None:
            raise WorkflowConflict("A plan must exist before it can be approved.")
        raw_plan = plan.meta.get("plan")
        if not isinstance(raw_plan, dict):
            raise WorkflowConflict("The saved plan is invalid.")
        try:
            plan_document = PlanDocument.model_validate(raw_plan)
        except ValidationError as error:
            raise WorkflowConflict("The saved plan is invalid.") from error
        task_rows = await TaskRepository(self._session).create_from_plan(
            user_id, run_id, plan_document
        )
        if task_rows is None:
            raise LookupError("Run not found.")
        tasks_event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="task.updated",
            payload={"count": len(task_rows), "status": "pending"},
        )
        approved_event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="plan.approved",
            payload={"artifact_id": str(plan.id)},
        )
        state_event = await stage_transition(
            self._session, user_id, run, "EXECUTING", event_bus=self._bus
        )
        await self._session.commit()
        events = await _refresh_events(self._session, [tasks_event, approved_event, state_event])
        await _publish_all(self._bus, events)
        return run


async def advance_run_job(user_id: UUID, run_id: UUID, llm_client: LLMClient) -> str:
    """Testable worker body; the Arq wrapper handles string serialization."""
    try:
        return await RunOrchestrator(llm_client).advance_until_input(user_id, run_id)
    except (IllegalRunTransition, LookupError):
        raise


async def resume_approval_job(
    user_id: UUID,
    run_id: UUID,
    approval_id: UUID,
    llm_client: LLMClient,
    event_bus: EventBus | None = None,
) -> ToolExecutionResult:
    """Resume a resolved tool and ask the requesting agent to replan on rejection."""
    bus = event_bus or EventBus()
    async with SessionFactory() as session:
        approval = await ApprovalRepository(session).get_for_user(user_id, approval_id)
        if approval is None or approval.run_id != run_id or approval.agent_id is None:
            raise LookupError("Approval not found.")
        extra = await run_tool_handlers(user_id, run_id, approval.agent_id, bus)
    result = await ToolExecutionPipeline(
        SessionFactory, agent_tool_registry(extra_handlers=extra), bus
    ).resume_approval(user_id, run_id, approval_id)
    if result.output is not None and result.output.get("status") == "rejected":
        await _replan_after_rejection(user_id, run_id, approval_id, result, llm_client, bus)
    if result.status == "completed":
        await enqueue_advance(user_id, run_id)
    return result


async def _replan_after_rejection(
    user_id: UUID,
    run_id: UUID,
    approval_id: UUID,
    tool_result: ToolExecutionResult,
    llm_client: LLMClient,
    event_bus: EventBus,
) -> None:
    """Persist one bounded follow-up LLM step with structured rejection feedback."""
    if tool_result.output is None:
        return
    async with SessionFactory() as session:
        approval = await ApprovalRepository(session).get_for_user(user_id, approval_id)
        run = await RunRepository(session).get_for_user(user_id, run_id)
        if approval is None or run is None or approval.agent_id is None:
            return
        agent = await AgentRepository(session).get_for_id(user_id, approval.agent_id)
        if agent is None:
            return
        steps = await AgentRepository(session).list_steps_for_agent(user_id, agent.id)
        if not steps or steps[-1].kind == "llm":
            return
        llm_step = next((step for step in reversed(steps) if step.kind == "llm"), None)
        if llm_step is None:
            return
        execution = (
            await ToolExecutionRepository(session).get_for_key(
                user_id, run_id, approval.idempotency_key
            )
            if approval.idempotency_key is not None
            else None
        )
        if execution is None:
            return
        raw_messages = llm_step.payload.get("input_messages")
        raw_calls = llm_step.payload.get("tool_calls")
        if not isinstance(raw_messages, list) or not isinstance(raw_calls, list):
            return
        messages = [dict(message) for message in raw_messages if isinstance(message, dict)]
        call_id = next(
            (
                str(call.get("id"))
                for call in raw_calls
                if isinstance(call, dict)
                and call.get("name") == execution.tool_name
                and call.get("arguments") == execution.input_payload
            ),
            None,
        )
        if call_id is None:
            return
        config = agent.config
        raw_allowlist = config.get("tool_allowlist", [])
        tool_allowlist = (
            tuple(item for item in raw_allowlist if isinstance(item, str))
            if isinstance(raw_allowlist, list)
            else ()
        )
        raw_max_steps = config.get("max_steps", 1)
        raw_max_cost = config.get("max_cost_usd", 0.5)
        if isinstance(raw_max_cost, (str, int, float, Decimal)):
            try:
                max_cost = float(raw_max_cost)
            except (ValueError, OverflowError):
                max_cost = 0.5
        else:
            max_cost = 0.5
        template = RoleTemplate(
            name=agent.role,
            system_prompt=str(config.get("system_prompt", "")),
            model_tier=ModelTier(str(config.get("model_tier", ModelTier.BALANCED.value))),
            tool_allowlist=tool_allowlist,
            output_schema=str(config.get("output_schema", "")),
            max_steps=raw_max_steps if isinstance(raw_max_steps, int) else 1,
            max_cost_usd=max_cost,
        )
        keys = await ProviderKeyRepository(session).list_for_user(user_id)
    provider_context = _provider_context(user_id, keys)
    if provider_context is None:
        return
    provider, provider_key_id = provider_context
    feedback_message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": execution.tool_name,
                    "arguments": json.dumps(execution.input_payload),
                },
            }
        ],
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(tool_result.output),
    }
    messages.extend([feedback_message, tool_message])
    await AgentRuntime(SessionFactory, event_bus).complete(
        user_id,
        run_id,
        provider,
        provider_key_id,
        template,
        messages,
        llm_client,
        tool_registry=agent_tool_registry(
            extra_handlers=await run_tool_handlers(user_id, run_id, execution.agent_id, event_bus)
        ),
        force_new=True,
    )
