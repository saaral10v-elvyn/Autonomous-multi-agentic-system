"""Single gated, durable pipeline for registered agent tools."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.tools import ToolHandler, ToolRegistry
from app.events.bus import EventBus
from app.gate.engine import GateDecision, decide
from app.models.agent import Agent
from app.models.event import Event
from app.models.run import Run
from app.models.tool_execution import ToolExecution
from app.orchestrator.state import IllegalRunTransition, stage_transition
from app.repos.agents import AgentRepository
from app.repos.approvals import ApprovalRepository
from app.repos.runs import RunRepository
from app.repos.tool_executions import ToolExecutionRepository


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Whether a call ran or was suspended for a user decision."""

    status: str
    output: dict[str, object] | None = None
    approval_id: UUID | None = None


class ToolExecutionPipeline:
    """Look up, gate, suspend or execute, then persist every tool call."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
        event_bus: EventBus | None = None,
    ) -> None:
        self._sessions = sessions
        self._registry = registry
        self._bus = event_bus or EventBus()

    async def execute(
        self,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        tool_name: str,
        payload: Mapping[str, object],
        *,
        tool_call_id: str,
        reason: str,
        estimated_cost_usd: Decimal | None = None,
        rollback_info: Mapping[str, object] | None = None,
    ) -> ToolExecutionResult:
        """Gate and execute one tool using a stable idempotency key."""
        definition = self._registry.get(tool_name)
        call_key = idempotency_key(run_id, agent_id, tool_call_id)
        request_payload = dict(payload)
        events: list[Event] = []
        suspended_approval_id: UUID | None = None
        new_execution = False
        async with self._sessions() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            if run is None:
                raise LookupError("Run not found.")
            if tool_name == "finish" and request_payload.get("status") == "completed":
                role = await session.scalar(
                    select(Agent.role).where(Agent.id == agent_id, Agent.run_id == run_id)
                )
                if role == "tester" and not await _tester_report_passed(
                    session, user_id, run_id, agent_id
                ):
                    raise IllegalRunTransition(
                        "Tester cannot complete successfully without a passing report "
                        "for its latest executed command."
                    )
            if definition.requires_reviewer_pass and not await _reviewer_passed(
                session, user_id, run_id
            ):
                request = await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="review.requested",
                    payload={"agent_id": str(agent_id), "required_for_tool": tool_name},
                )
                await session.commit()
                if request is not None:
                    await session.refresh(request)
                    await self._bus.publish(request)
                raise IllegalRunTransition(
                    "A passing Reviewer result is required before this tool."
                )
            if run.status == "AWAITING_APPROVAL":
                approval = await ApprovalRepository(session).get_for_idempotency_key(
                    user_id, run_id, call_key
                )
                if approval is None:
                    raise LookupError("Approval request not found.")
                return ToolExecutionResult("suspended", approval_id=approval.id)
            clarification_tool = (
                run.status == "CLARIFYING"
                and tool_name == "ask_user"
                and definition.risk.value == "read"
            )
            execution_states = {"EXECUTING", "VERIFYING", "DEPLOYING", "REPORTING"}
            if run.status not in execution_states and not clarification_tool:
                raise IllegalRunTransition("Tools can run only while the run is executing.")

            execution_repo = ToolExecutionRepository(session)
            execution = await execution_repo.get_for_key(user_id, run_id, call_key, lock=True)
            if execution is not None:
                _validate_replay(execution, tool_name, request_payload)
                if execution.status == "completed":
                    return ToolExecutionResult("completed", execution.output_payload)
            else:
                execution = await execution_repo.create_for_user(
                    user_id,
                    run_id,
                    agent_id,
                    idempotency_key=call_key,
                    tool_name=tool_name,
                    input_payload=request_payload,
                )
                if execution is None:
                    raise LookupError("Run not found.")
                new_execution = True

            if definition.risk.value == "irreversible":
                prior = await session.scalars(
                    select(ToolExecution)
                    .where(
                        ToolExecution.run_id == run_id,
                        ToolExecution.tool_name == tool_name,
                        ToolExecution.idempotency_key != call_key,
                    )
                    .order_by(ToolExecution.created_at, ToolExecution.id)
                    .with_for_update()
                )
                replay = irreversible_action_replay(prior.all(), tool_name, request_payload)
                if replay is not None:
                    return replay

            if new_execution:
                call_event = await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.tool_call",
                    payload={
                        "agent_id": str(agent_id),
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                    },
                )
                if call_event is None:
                    raise LookupError("Run not found.")
                events.append(call_event)

            decision = decide(run.autonomy_level, definition.risk)
            if decision is GateDecision.REQUIRE_APPROVAL:
                approvals = ApprovalRepository(session)
                approval = await approvals.get_for_idempotency_key(
                    user_id, run_id, call_key, lock=True
                )
                if approval is None:
                    approval = await approvals.create_for_user(
                        user_id,
                        run_id,
                        agent_id=agent_id,
                        action_type=tool_name,
                        risk=definition.risk.value,
                        payload=request_payload,
                        reason=reason,
                        estimated_cost_usd=estimated_cost_usd,
                        rollback_info=rollback_info,
                        idempotency_key=call_key,
                    )
                if approval is None:
                    raise LookupError("Run not found.")
                suspended_approval_id = approval.id
                if run.status in execution_states:
                    run.paused_from_status = run.status
                    event = await stage_transition(
                        session,
                        user_id,
                        run,
                        "AWAITING_APPROVAL",
                        event_bus=self._bus,
                        payload={"approval_id": str(approval.id)},
                    )
                    events.append(event)
                request_event = await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="approval.requested",
                    payload={
                        "approval_id": str(approval.id),
                        "action": tool_name,
                        "payload": request_payload,
                        "risk": definition.risk.value,
                        "agent_id": str(agent_id),
                        "reason": reason,
                        "estimated_cost_usd": (
                            str(estimated_cost_usd) if estimated_cost_usd is not None else None
                        ),
                        "rollback_info": dict(rollback_info) if rollback_info is not None else None,
                    },
                )
                if request_event is not None:
                    events.append(request_event)
                result_event = await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.tool_result",
                    payload={
                        "agent_id": str(agent_id),
                        "tool_name": tool_name,
                        "idempotency_key": call_key,
                        "output": {
                            "status": "waiting_approval",
                            "approval_id": str(approval.id),
                        },
                    },
                )
                if result_event is not None:
                    events.append(result_event)
                await session.commit()
                refreshed = await _refresh_events(session, events)
            else:
                await session.commit()
                approval = None
                refreshed = []
        await _publish_all(self._bus, refreshed)
        if decision is GateDecision.REQUIRE_APPROVAL:
            if suspended_approval_id is None:
                raise LookupError("Approval request not found.")
            return ToolExecutionResult("suspended", approval_id=suspended_approval_id)
        if execution is None:
            raise LookupError("Tool execution was not found.")
        return await self._run_handler(
            user_id,
            run_id,
            agent_id,
            execution,
            definition.handler,
            request_payload,
            call_key,
            irreversible=definition.risk.value == "irreversible",
        )

    async def resume_approval(
        self, user_id: UUID, run_id: UUID, approval_id: UUID
    ) -> ToolExecutionResult:
        """Resolve the persisted decision and continue the same idempotent call."""
        terminal_result: (
            tuple[UUID, UUID, str, str, dict[str, object], dict[str, object]] | None
        ) = None
        async with self._sessions() as session:
            approval = await ApprovalRepository(session).get_for_user(user_id, approval_id)
            run = await RunRepository(session).get_for_user(user_id, run_id)
            if approval is None or run is None or approval.run_id != run_id:
                raise LookupError("Approval not found.")
            if approval.status == "pending":
                raise IllegalRunTransition("Approval is still pending.")
            if approval.status not in {"approved", "rejected", "edited", "expired"}:
                raise IllegalRunTransition("Approval has no executable decision.")
            if run.status not in {"EXECUTING", "VERIFYING", "DEPLOYING", "REPORTING"}:
                raise IllegalRunTransition("Run is not ready to resume the approved action.")
            if approval.idempotency_key is None:
                raise LookupError("Approval is not linked to a tool call.")
            execution = await ToolExecutionRepository(session).get_for_key(
                user_id, run_id, approval.idempotency_key, lock=True
            )
            if execution is None:
                raise LookupError("Tool execution was not found.")
            if execution.status == "completed":
                return ToolExecutionResult("completed", execution.output_payload)
            if approval.status == "rejected":
                feedback: dict[str, object] = {
                    "kind": "approval_rejected",
                    "approval_id": str(approval.id),
                    "action": approval.action_type,
                    "instruction": "Do not execute this action; replan using the user's rejection.",
                }
                if approval.edited_payload is not None:
                    feedback["user_feedback"] = approval.edited_payload
                terminal_result = (
                    execution.id,
                    execution.agent_id,
                    execution.tool_name,
                    execution.idempotency_key,
                    dict(execution.input_payload),
                    {"status": "rejected", "feedback": feedback},
                )
            elif approval.status == "expired":
                feedback = {
                    "kind": "approval_expired",
                    "approval_id": str(approval.id),
                    "action": approval.action_type,
                    "instruction": "Do not execute this action; request fresh approval if needed.",
                }
                terminal_result = (
                    execution.id,
                    execution.agent_id,
                    execution.tool_name,
                    execution.idempotency_key,
                    dict(execution.input_payload),
                    {"status": "expired", "feedback": feedback},
                )
            else:
                definition = self._registry.get(execution.tool_name)
                actual_payload = (
                    approval.edited_payload
                    if approval.status == "edited" and approval.edited_payload is not None
                    else execution.input_payload
                )
        if terminal_result is not None:
            execution_id, agent_id, tool_name, call_key, input_payload, output = terminal_result
            return await self._persist_result(
                user_id,
                run_id,
                execution_id,
                agent_id,
                tool_name,
                call_key,
                input_payload,
                output,
            )
        return await self._run_handler(
            user_id,
            run_id,
            execution.agent_id,
            execution,
            definition.handler,
            actual_payload,
            execution.idempotency_key,
            irreversible=definition.risk.value == "irreversible",
        )

    async def _run_handler(
        self,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        execution: ToolExecution,
        handler: ToolHandler,
        payload: Mapping[str, object],
        call_key: str,
        *,
        irreversible: bool,
    ) -> ToolExecutionResult:
        """Invoke a risk-classified handler, then atomically persist its result."""
        if execution.status == "completed":
            return ToolExecutionResult("completed", execution.output_payload)
        if (
            irreversible
            and execution.output_payload
            and execution.output_payload.get("_started") is True
        ):
            return ToolExecutionResult(
                "uncertain", {"status": "outcome_unknown", "tool_name": execution.tool_name}
            )
        async with self._sessions() as session:
            current = await ToolExecutionRepository(session).get_for_key(
                user_id, run_id, call_key, lock=True
            )
            if current is None:
                raise LookupError("Tool execution was not found.")
            if (
                irreversible
                and current.output_payload
                and current.output_payload.get("_started") is True
            ):
                return ToolExecutionResult(
                    "uncertain", {"status": "outcome_unknown", "tool_name": execution.tool_name}
                )
            if irreversible:
                current.input_payload = {
                    "_request_payload": _request_payload(current.input_payload),
                    "_executed_payload": dict(payload),
                }
                current.output_payload = {"_started": True}
                await session.commit()
        output_value = await handler(payload, call_key)
        output = dict(output_value)
        return await self._persist_result(
            user_id,
            run_id,
            execution.id,
            agent_id,
            execution.tool_name,
            call_key,
            payload,
            output,
        )

    async def _persist_result(
        self,
        user_id: UUID,
        run_id: UUID,
        execution_id: UUID,
        agent_id: UUID,
        tool_name: str,
        call_key: str,
        input_payload: Mapping[str, object],
        output: Mapping[str, object],
    ) -> ToolExecutionResult:
        """Commit execution output, agent step, and result event together."""
        async with self._sessions() as session:
            execution, was_completed = await ToolExecutionRepository(session).complete_for_user(
                user_id, run_id, execution_id, output
            )
            if execution is None:
                raise LookupError("Tool execution was not found.")
            if was_completed:
                return ToolExecutionResult("completed", execution.output_payload)
            step = await AgentRepository(session).create_step(
                user_id,
                agent_id,
                kind="tool",
                payload={
                    "tool_name": tool_name,
                    "idempotency_key": call_key,
                    "input": dict(input_payload),
                    "output": dict(output),
                },
            )
            if step is None:
                raise LookupError("Agent not found.")
            event = await self._bus.persist(
                session,
                user_id,
                run_id,
                event_type="agent.tool_result",
                payload={
                    "agent_id": str(agent_id),
                    "tool_name": tool_name,
                    "idempotency_key": call_key,
                    "output": dict(output),
                },
            )
            if event is None:
                raise LookupError("Run not found.")
            await session.commit()
            await session.refresh(event)
        await self._bus.publish(event)
        return ToolExecutionResult("completed", dict(output))


def idempotency_key(run_id: UUID, agent_id: UUID, tool_call_id: str) -> str:
    """Build a stable bounded key from the persisted agent tool-call identity."""
    return sha256(f"{run_id}:{agent_id}:{tool_call_id}".encode()).hexdigest()


def _validate_replay(
    execution: ToolExecution, tool_name: str, payload: Mapping[str, object]
) -> None:
    """Reject accidental reuse of one call ID for a different action."""
    if execution.tool_name != tool_name or _request_payload(execution.input_payload) != dict(
        payload
    ):
        raise ValueError("Tool idempotency key was reused with different action data.")


def _request_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return the model's original call when an edited approval is recorded."""
    request = payload.get("_request_payload")
    return dict(request) if isinstance(request, dict) else dict(payload)


def _executed_action_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return the exact approved target for semantic irreversible-action replay."""
    executed = payload.get("_executed_payload")
    return dict(executed) if isinstance(executed, dict) else _request_payload(payload)


def irreversible_action_replay(
    prior_executions: Sequence[ToolExecution],
    tool_name: str,
    payload: Mapping[str, object],
) -> ToolExecutionResult | None:
    """Reuse a successful external action or fail closed when its outcome is uncertain."""
    for execution in prior_executions:
        if execution.status == "completed":
            output = execution.output_payload or {}
            if output.get("status") in {"rejected", "expired"}:
                continue
            if _executed_action_payload(execution.input_payload) != dict(payload):
                raise IllegalRunTransition(
                    "This irreversible action already has a different recorded target."
                )
            return ToolExecutionResult("completed", execution.output_payload)
        return ToolExecutionResult(
            "uncertain", {"status": "outcome_unknown", "tool_name": tool_name}
        )
    return None


async def _reviewer_passed(
    session: AsyncSession,
    user_id: UUID,
    run_id: UUID,
    expected_agent_id: UUID | None = None,
) -> bool:
    result = await session.scalars(
        select(Event)
        .join(Run, Run.id == Event.run_id)
        .where(
            Run.user_id == user_id,
            Event.run_id == run_id,
            Event.type.in_(("review.requested", "review.completed")),
        )
        .order_by(Event.seq.desc())
        .limit(1)
    )
    event = result.first()
    if event is None or event.type != "review.completed" or event.payload.get("passed") is not True:
        return False
    raw_agent_id = event.payload.get("agent_id")
    if not isinstance(raw_agent_id, str):
        return False
    try:
        agent_id = UUID(raw_agent_id)
    except ValueError:
        return False
    if expected_agent_id is not None and agent_id != expected_agent_id:
        return False
    reviewer = await session.scalar(
        select(Agent.id)
        .join(Run, Run.id == Agent.run_id)
        .where(
            Run.user_id == user_id,
            Agent.id == agent_id,
            Agent.run_id == run_id,
            Agent.role == "reviewer",
        )
    )
    return _is_passing_reviewer(event.payload, "reviewer" if reviewer is not None else None)


def _is_passing_reviewer(payload: Mapping[str, object], role: str | None) -> bool:
    """Accept a positive completion only when its author has the Reviewer role."""
    return role == "reviewer" and payload.get("passed") is True


async def _tester_report_passed(
    session: AsyncSession, user_id: UUID, run_id: UUID, agent_id: UUID
) -> bool:
    report = await session.scalar(
        select(ToolExecution)
        .join(Run, Run.id == ToolExecution.run_id)
        .where(
            Run.user_id == user_id,
            ToolExecution.run_id == run_id,
            ToolExecution.agent_id == agent_id,
            ToolExecution.tool_name == "tester_report",
            ToolExecution.status == "completed",
        )
        .order_by(ToolExecution.completed_at.desc(), ToolExecution.created_at.desc())
        .limit(1)
    )
    if report is None or not isinstance(report.output_payload, dict):
        return False
    latest_execution = await session.scalar(
        select(ToolExecution)
        .join(Run, Run.id == ToolExecution.run_id)
        .where(
            Run.user_id == user_id,
            ToolExecution.run_id == run_id,
            ToolExecution.agent_id == agent_id,
            ToolExecution.tool_name == "sandbox_exec",
            ToolExecution.status == "completed",
        )
        .order_by(ToolExecution.completed_at.desc(), ToolExecution.created_at.desc())
        .limit(1)
    )
    if latest_execution is None or not isinstance(latest_execution.output_payload, dict):
        return False
    output = report.output_payload
    passed = output.get("passed")
    skipped = output.get("skipped")
    if type(passed) is not int or type(skipped) is not int:
        return False
    return (
        output.get("source_execution_id") == str(latest_execution.id)
        and output.get("exit_code") == 0
        and output.get("failed") == 0
        and passed + skipped > 0
        and latest_execution.output_payload.get("exit_code") == 0
        and latest_execution.output_payload.get("timed_out") is not True
    )


async def _refresh_events(session: AsyncSession, events: list[Event]) -> list[Event]:
    for event in events:
        await session.refresh(event)
    return events


async def _publish_all(bus: EventBus, events: list[Event]) -> None:
    for event in events:
        await bus.publish(event)
