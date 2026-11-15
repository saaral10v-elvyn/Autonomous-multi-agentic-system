"""Postgres-backed approval, suspension, and idempotent tool tests."""

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from functools import partial
from queue import Empty, Queue
from threading import Thread
from typing import TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi.testclient import TestClient

import app.api.v1.websockets as websocket_module
from app.agents.demo_tools import demo_tool_registry
from app.agents.registry import RoleTemplate
from app.agents.runtime import AgentRuntime
from app.agents.tools import ToolRegistry
from app.core.config import Settings, get_settings
from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.gate.engine import AutonomyLevel, GateDecision, RiskClass, decide
from app.llm.client import LLMClient
from app.llm.fake import DevelopmentFakeKeyResolver, FakeProvider
from app.llm.types import LLMResponse, ModelTier, Provider, ToolCall
from app.models.event import Event
from app.models.run import Run
from app.repos.agents import AgentRepository
from app.repos.projects import ProjectRepository
from app.repos.runs import RunRepository
from app.services.approvals import ApprovalService
from app.services.orchestration import resume_approval_job
from app.services.tools import ToolExecutionPipeline, ToolExecutionResult
from tests.conftest import WEB_ORIGIN

T = TypeVar("T")


async def _bounded_call[T](function: Callable[[], Awaitable[T]]) -> T:
    """Bound portal waits so stalled DB or worker operations fail promptly."""
    return await asyncio.wait_for(function(), timeout=5)


def _portal_call[T](
    client: TestClient,
    function: Callable[..., Awaitable[T]],
    *args: object,
    **kwargs: object,
) -> T:
    """Call an async function through AnyIO without forwarding portal kwargs."""
    return client.portal.call(_bounded_call, partial(function, *args, **kwargs))


def _receive_json_bounded(receiver: Callable[[], object]) -> dict[str, object]:
    """Read one WebSocket frame with a deadline and a daemon reader thread."""
    received: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def receive() -> None:
        try:
            received.put((True, receiver()))
        except Exception as error:
            received.put((False, error))

    Thread(target=receive, daemon=True).start()
    try:
        succeeded, value = received.get(timeout=5)
    except Empty as error:
        raise AssertionError("Timed out waiting for a WebSocket event.") from error
    if not succeeded:
        if isinstance(value, BaseException):
            raise AssertionError("WebSocket receive failed.") from value
        raise AssertionError("WebSocket receive failed.")
    if not isinstance(value, dict):
        raise AssertionError("Expected a JSON object from the WebSocket.")
    return value


class SilentEventBus(EventBus):
    """Persist integration-test events without contacting Redis."""

    async def publish(self, event: Event) -> None:
        del event


async def _discard_publish(_: EventBus, event: Event) -> None:
    del event


def _silence_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(EventBus, "publish", _discard_publish)


def _signup(client: TestClient, email: str) -> UUID:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "m4-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201
    return UUID(client.get("/api/v1/me").json()["id"])


async def _create_executing_run(user_id: UUID, level: AutonomyLevel) -> tuple[UUID, UUID]:
    async with SessionFactory() as session:
        project = await ProjectRepository(session).create_for_user(user_id, name="M4 test")
        run = await RunRepository(session).create_for_user(
            user_id,
            project_id=project.id,
            idea="Exercise approval gates",
            autonomy_level=level.value,
            budget_usd=Decimal("10.00"),
        )
        run.status = "EXECUTING"
        agent = await AgentRepository(session).get_or_create_for_role(
            user_id, run.id, "coder", {"role": "coder"}
        )
        if agent is None:
            raise AssertionError("Test agent was not created.")
        session.add(
            Event(
                run_id=run.id,
                seq=1,
                type="run.state_changed",
                payload={"status": "EXECUTING"},
            )
        )
        await session.commit()
        return run.id, agent.id


async def _get_run_status(run_id: UUID) -> str:
    async with SessionFactory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise AssertionError("Test run was not found.")
        return run.status


async def _set_run_status(run_id: UUID, status: str) -> None:
    async with SessionFactory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise AssertionError("Test run was not found.")
        run.status = status
        await session.commit()


def _registry() -> ToolRegistry:
    return demo_tool_registry(Settings(_env_file=None, app_env="test", fake_provider_enabled=True))


@pytest.mark.parametrize(
    ("level", "risk", "tool_name"),
    [
        (level, risk, tool_name)
        for level in AutonomyLevel
        for risk, tool_name in (
            (RiskClass.READ, "demo_read"),
            (RiskClass.SANDBOX_WRITE, "demo_sandbox_write"),
            (RiskClass.REVERSIBLE_EXTERNAL, "demo_reversible_external"),
            (RiskClass.IRREVERSIBLE, "demo_irreversible"),
        )
    ],
)
def test_scripted_tool_call_follows_every_gate_matrix_pair(
    api_client: TestClient,
    test_emails: Callable[[], str],
    level: AutonomyLevel,
    risk: RiskClass,
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each risk and autonomy pair either executes or suspends with no queue call."""

    async def fail_if_enqueued(*_: object, **__: object) -> None:
        raise AssertionError("An approval-waiting run must not enqueue a worker job.")

    monkeypatch.setattr("arq.create_pool", fail_if_enqueued)
    user_id = _signup(api_client, test_emails())
    run_id, agent_id = _portal_call(api_client, _create_executing_run, user_id, level)
    tool_call_id = f"matrix-{level.value}-{risk.value}"
    tool_call = ToolCall(tool_call_id, tool_name, {"operation": "test"})
    fake_provider = FakeProvider(
        [
            LLMResponse(
                provider=Provider.OPENAI,
                model="fake-m4-model",
                content=None,
                tool_calls=[tool_call],
            )
        ]
    )
    llm = LLMClient(DevelopmentFakeKeyResolver(), providers={Provider.OPENAI: fake_provider})
    template = RoleTemplate(
        name="coder",
        system_prompt="Call the scripted tool.",
        model_tier=ModelTier.FAST,
        tool_allowlist=(tool_name,),
        output_schema="ToolCall",
        max_steps=1,
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()

    async def scripted_run() -> ToolExecutionResult:
        runtime = AgentRuntime(SessionFactory, SilentEventBus())
        response = await runtime.complete(
            user_id,
            run_id,
            Provider.OPENAI,
            uuid5(NAMESPACE_URL, f"forge-fake:{user_id}"),
            template,
            [{"role": "user", "content": "Exercise the gate."}],
            llm,
            tool_registry=_registry(),
        )
        assert response.tool_calls == [tool_call]
        assert fake_provider.tools[0] is not None
        assert fake_provider.tools[0][0]["function"]["name"] == tool_name
        return await runtime.execute_tool_call(
            user_id,
            run_id,
            agent_id,
            response.tool_calls[0],
            _registry(),
            reason="Exercise the configured risk gate.",
        )

    try:
        result = _portal_call(api_client, scripted_run)
    finally:
        get_settings.cache_clear()
    expected = decide(level, risk)
    if expected is GateDecision.REQUIRE_APPROVAL:
        assert result.status == "suspended"
        assert result.approval_id is not None
        assert _portal_call(api_client, _get_run_status, run_id) == "AWAITING_APPROVAL"
    else:
        assert result.status == "completed"
        assert _portal_call(api_client, _get_run_status, run_id) == "EXECUTING"


@pytest.mark.parametrize("decision", ["approve", "reject", "edit"])
def test_decision_resumes_once_and_rejection_has_structured_feedback(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """Approval decisions persist before resuming; edits execute the edited payload."""
    _silence_redis(monkeypatch)

    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("app.api.v1.approvals.enqueue_approval_resume", no_enqueue)
    user_id = _signup(api_client, test_emails())
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
    )
    handler_calls: list[tuple[dict[str, object], str]] = []

    async def handler(payload: object, key: str) -> dict[str, object]:
        assert isinstance(payload, dict)
        handler_calls.append((payload, key))
        return {"payload": payload}

    registry = ToolRegistry()
    registry.register("demo_sandbox_write", handler, risk=RiskClass.SANDBOX_WRITE)
    pipeline = ToolExecutionPipeline(SessionFactory, registry, SilentEventBus())
    request = _portal_call(
        api_client,
        pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "demo_sandbox_write",
        {"value": "original"},
        tool_call_id=f"decision-{decision}",
        reason="Write a generated file.",
        estimated_cost_usd=Decimal("0.01"),
        rollback_info={"action": "remove generated file"},
    )
    assert request.status == "suspended"
    assert request.approval_id is not None
    assert _portal_call(api_client, _get_run_status, run_id) == "AWAITING_APPROVAL"
    edited_payload = {"value": "user-edited"} if decision == "edit" else None
    response = api_client.post(
        f"/api/v1/approvals/{request.approval_id}/decide",
        json={"decision": decision, "edited_payload": edited_payload},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 200
    assert _portal_call(api_client, _get_run_status, run_id) == "EXECUTING"
    resumed = _portal_call(
        api_client, pipeline.resume_approval, user_id, run_id, request.approval_id
    )
    assert resumed.status == "completed"
    expected_payload = edited_payload or {"value": "original"}
    if decision == "reject":
        assert response.json()["feedback"]["kind"] == "approval_rejected"
        assert resumed.output is not None
        assert resumed.output["status"] == "rejected"
    else:
        assert resumed.output is not None
        assert resumed.output["payload"] == expected_payload
    assert len(handler_calls) == (0 if decision == "reject" else 1)
    if handler_calls:
        assert handler_calls[0][0] == expected_payload
    second_resume = _portal_call(
        api_client, pipeline.resume_approval, user_id, run_id, request.approval_id
    )
    assert second_resume.output == resumed.output


def test_rejection_feedback_is_sent_to_fake_agent_for_replanning(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected action becomes tool feedback in the agent's next FakeProvider turn."""
    _silence_redis(monkeypatch)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    try:
        user_id = _signup(api_client, test_emails())
        run_id, agent_id = _portal_call(
            api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
        )
        call = ToolCall("replan-tool-call", "demo_sandbox_write", {"value": "original"})
        fake_provider = FakeProvider(
            [
                LLMResponse(
                    provider=Provider.OPENAI,
                    model="fake-m4-model",
                    content=None,
                    tool_calls=[call],
                ),
                LLMResponse(
                    provider=Provider.OPENAI,
                    model="fake-m4-model",
                    content="I will revise the plan around that rejection.",
                ),
            ]
        )
        llm = LLMClient(DevelopmentFakeKeyResolver(), providers={Provider.OPENAI: fake_provider})
        template = RoleTemplate(
            name="coder",
            system_prompt="Plan and act safely.",
            model_tier=ModelTier.FAST,
            tool_allowlist=("demo_sandbox_write",),
            output_schema="ToolCall",
            max_steps=3,
        )
        runtime = AgentRuntime(SessionFactory, SilentEventBus())

        async def request_and_suspend() -> ToolExecutionResult:
            await runtime.complete(
                user_id,
                run_id,
                Provider.OPENAI,
                uuid5(NAMESPACE_URL, f"forge-fake:{user_id}"),
                template,
                [{"role": "system", "content": template.system_prompt}],
                llm,
                tool_registry=_registry(),
            )
            return await runtime.execute_tool_call(
                user_id,
                run_id,
                agent_id,
                call,
                _registry(),
                reason="Write the page requested by the plan.",
            )

        request = _portal_call(api_client, request_and_suspend)
        assert request.status == "suspended"
        assert request.approval_id is not None

        async def reject() -> None:
            async with SessionFactory() as session:
                await ApprovalService(session, SilentEventBus()).decide(
                    user_id, request.approval_id, "reject"
                )

        _portal_call(api_client, reject)
        resumed = _portal_call(
            api_client,
            resume_approval_job,
            user_id,
            run_id,
            request.approval_id,
            llm,
            SilentEventBus(),
        )
        assert resumed.output is not None
        assert resumed.output["status"] == "rejected"
        assert len(fake_provider.messages) == 2
        feedback = fake_provider.messages[1][-1]["content"]
        assert isinstance(feedback, str) and "approval_rejected" in feedback
        assert fake_provider.messages[1][-1]["role"] == "tool"
    finally:
        get_settings.cache_clear()


def test_cross_tenant_denial_wrong_state_and_double_decision(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval list and decisions remain tenant-scoped and pending-only."""
    _silence_redis(monkeypatch)

    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("app.api.v1.approvals.enqueue_approval_resume", no_enqueue)
    owner_email = test_emails()
    user_id = _signup(api_client, owner_email)
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
    )
    pipeline = ToolExecutionPipeline(SessionFactory, _registry(), SilentEventBus())
    request = _portal_call(
        api_client,
        pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "demo_sandbox_write",
        {"value": "write"},
        tool_call_id="tenant-test",
        reason="Need approval to write.",
    )
    assert request.approval_id is not None
    _signup(api_client, test_emails())
    assert api_client.get(f"/api/v1/runs/{run_id}/approvals").status_code == 404
    assert (
        api_client.post(
            f"/api/v1/approvals/{request.approval_id}/decide",
            json={"decision": "approve"},
            headers={"Origin": WEB_ORIGIN},
        ).status_code
        == 404
    )
    login = api_client.post(
        "/api/v1/auth/login",
        json={"email": owner_email, "password": "m4-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert login.status_code == 200
    _portal_call(api_client, _set_run_status, run_id, "EXECUTING")
    wrong_state = api_client.post(
        f"/api/v1/approvals/{request.approval_id}/decide",
        json={"decision": "approve"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert wrong_state.status_code == 409
    _portal_call(api_client, _set_run_status, run_id, "AWAITING_APPROVAL")
    first_decision = api_client.post(
        f"/api/v1/approvals/{request.approval_id}/decide",
        json={"decision": "approve"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert first_decision.status_code == 200
    duplicate_decision = api_client.post(
        f"/api/v1/approvals/{request.approval_id}/decide",
        json={"decision": "reject"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert duplicate_decision.status_code == 409


def test_websocket_approval_decide_resolves_and_resumes(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run WebSocket accepts approval decisions and queues the resume job."""
    _silence_redis(monkeypatch)

    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(websocket_module, "enqueue_approval_resume", no_enqueue)
    user_id = _signup(api_client, test_emails())
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
    )
    pipeline = ToolExecutionPipeline(SessionFactory, _registry(), SilentEventBus())
    request = _portal_call(
        api_client,
        pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "demo_sandbox_write",
        {"value": "socket"},
        tool_call_id="websocket-decision",
        reason="Test WebSocket approval handling.",
    )
    assert request.approval_id is not None
    existing_events = api_client.get(f"/api/v1/runs/{run_id}/events?after_seq=0").json()
    with api_client.websocket_connect(
        f"/ws/runs/{run_id}?after_seq=0", headers={"Origin": WEB_ORIGIN}
    ) as connection:
        for _ in existing_events:
            _receive_json_bounded(connection.receive_json)
        connection.send_json(
            {
                "type": "approval.decide",
                "approval_id": str(request.approval_id),
                "decision": "approve",
            }
        )
        accepted: dict[str, object] = {}
        for _ in range(5):
            message = _receive_json_bounded(connection.receive_json)
            if message.get("type") == "request.accepted":
                accepted = message
                break
        assert accepted.get("request_type") == "approval.decide"
    assert _portal_call(api_client, _get_run_status, run_id) == "EXECUTING"


def test_mid_run_autonomy_change_applies_at_next_gate(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing autonomy persists an event and the next tool reads the new level."""
    _silence_redis(monkeypatch)
    user_id = _signup(api_client, test_emails())
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
    )
    changed = api_client.patch(
        f"/api/v1/runs/{run_id}/autonomy",
        json={"autonomy_level": "milestones"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert changed.status_code == 200
    assert changed.json()["autonomy_level"] == "milestones"
    event_response = api_client.get(f"/api/v1/runs/{run_id}/events?after_seq=0")
    assert any(event["type"] == "run.autonomy_changed" for event in event_response.json())
    pipeline = ToolExecutionPipeline(SessionFactory, _registry(), SilentEventBus())
    result = _portal_call(
        api_client,
        pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "demo_sandbox_write",
        {"value": "next gate"},
        tool_call_id="autonomy-next-gate",
        reason="Check the updated level.",
    )
    assert result.status == "completed"


def test_worker_restart_reuses_idempotency_key_after_side_effect(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    """A new pipeline instance retries a side effect with the same idempotency key."""
    user_id = _signup(api_client, test_emails())
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.AUTOPILOT
    )
    side_effects: dict[str, dict[str, object]] = {}
    crashed_once: set[str] = set()

    async def flaky_handler(payload: object, key: str) -> dict[str, object]:
        if key in side_effects:
            return side_effects[key]
        side_effects[key] = {"payload": payload, "side_effect_count": 1}
        if key not in crashed_once:
            crashed_once.add(key)
            raise RuntimeError("simulated worker crash after side effect")
        return side_effects[key]

    first_registry = ToolRegistry()
    first_registry.register("idempotent", flaky_handler, risk=RiskClass.READ)
    first_pipeline = ToolExecutionPipeline(SessionFactory, first_registry, SilentEventBus())

    async def first_attempt() -> None:
        try:
            await first_pipeline.execute(
                user_id,
                run_id,
                agent_id,
                "idempotent",
                {"value": "once"},
                tool_call_id="restart-call",
                reason="Test restart safety.",
            )
        except RuntimeError:
            return
        raise AssertionError("The scripted first attempt should crash.")

    _portal_call(api_client, first_attempt)
    restarted_registry = ToolRegistry()
    restarted_registry.register("idempotent", flaky_handler, risk=RiskClass.READ)
    restarted_pipeline = ToolExecutionPipeline(SessionFactory, restarted_registry, SilentEventBus())
    result = _portal_call(
        api_client,
        restarted_pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "idempotent",
        {"value": "once"},
        tool_call_id="restart-call",
        reason="Test restart safety.",
    )
    assert result.status == "completed"
    assert result.output is not None
    assert next(iter(side_effects.values()))["side_effect_count"] == 1


def test_approval_payloads_and_events_do_not_expose_provider_key(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seeded provider credential is absent from approval payloads and events."""
    _silence_redis(monkeypatch)
    user_id = _signup(api_client, test_emails())
    secret = "m4-seeded-provider-secret-that-must-not-leak"
    key_response = api_client.post(
        "/api/v1/provider-keys",
        json={"provider": "openai", "label": "M4 leak test", "api_key": secret},
        headers={"Origin": WEB_ORIGIN},
    )
    assert key_response.status_code == 201
    run_id, agent_id = _portal_call(
        api_client, _create_executing_run, user_id, AutonomyLevel.STEP_BY_STEP
    )
    pipeline = ToolExecutionPipeline(SessionFactory, _registry(), SilentEventBus())
    request = _portal_call(
        api_client,
        pipeline.execute,
        user_id,
        run_id,
        agent_id,
        "demo_sandbox_write",
        {"operation": "generated write"},
        tool_call_id="secret-test",
        reason="Test safe payload serialization.",
    )
    assert request.approval_id is not None
    approvals = api_client.get(f"/api/v1/runs/{run_id}/approvals")
    events = api_client.get(f"/api/v1/runs/{run_id}/events?after_seq=0")
    assert approvals.status_code == 200
    assert events.status_code == 200
    assert secret not in approvals.text
    assert secret not in events.text
    event_types = {event["type"] for event in events.json()}
    assert {"agent.tool_call", "agent.tool_result"}.issubset(event_types)
