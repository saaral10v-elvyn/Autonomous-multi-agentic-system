"""Durable M5 human controls and targeted agent context tests."""

from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.registry import ROLE_TEMPLATES
from app.agents.runtime import AgentRuntime
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.llm.client import LLMClient
from app.llm.fake import DevelopmentFakeKeyResolver, FakeProvider
from app.llm.types import LLMResponse, Provider
from app.models.agent import Agent
from app.models.run import Run
from app.models.task import Task
from app.services.run_controls import RunControlService
from app.services.runs import RunService
from tests.conftest import WEB_ORIGIN


def create_owner_run(client: TestClient, email: str) -> tuple[UUID, UUID]:
    response = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "m5-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201
    user_id = UUID(response.json()["id"])

    async def create() -> Run:
        async with SessionFactory() as session:
            return await RunService(session).create(
                user_id,
                idea="A task for the agent runtime",
                autonomy_level="step_by_step",
                budget_usd="10.00",
            )

    return user_id, client.portal.call(create).id


def test_targeted_user_message_is_injected_into_next_agent_context(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_owner_run(api_client, test_emails())

    async def prepare_agent() -> UUID:
        async with SessionFactory() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.status = "EXECUTING"
            agent = Agent(
                run_id=run_id,
                role="planner",
                config={"system_prompt": "Plan safely."},
                status="running",
            )
            session.add(agent)
            await session.commit()
            return agent.id

    agent_id = api_client.portal.call(prepare_agent)
    message = "Keep the plan to two small tasks."

    async def inject_and_complete(fake: FakeProvider) -> None:
        async with SessionFactory() as session:
            await RunControlService(session).message(user_id, run_id, message, agent_id=agent_id)
        key_id = uuid5(NAMESPACE_URL, f"forge-fake:{user_id}")
        client = LLMClient(
            DevelopmentFakeKeyResolver(),
            providers={Provider.OPENAI: fake},
            max_retries=0,
        )
        await AgentRuntime(SessionFactory).complete(
            user_id,
            run_id,
            Provider.OPENAI,
            key_id,
            ROLE_TEMPLATES["planner"],
            [{"role": "system", "content": "Plan safely."}],
            client,
            agent_id=agent_id,
        )

    fake = FakeProvider(
        [LLMResponse(provider=Provider.OPENAI, model="fake", content="Plan ready.")]
    )
    api_client.portal.call(inject_and_complete, fake)
    assert any(
        item.get("content") == message for item in fake.messages[0] if isinstance(item, dict)
    )


def test_pause_resume_interrupt_cancel_persist_expected_states(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    user_id, run_id = create_owner_run(api_client, test_emails())

    async def exercise_controls() -> tuple[str, str, str, str]:
        async with SessionFactory() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.status = "EXECUTING"
            agent = Agent(
                run_id=run_id,
                role="coder",
                config={},
                status="running",
            )
            session.add(agent)
            await session.flush()
            task = Task(
                run_id=run_id,
                agent_id=agent.id,
                external_id="control-task",
                title="Control task",
                description="Pause and interrupt this task.",
                status="running",
                depends_on=[],
                input={},
            )
            session.add(task)
            await session.commit()
            agent_id = agent.id

        async with SessionFactory() as session:
            controls = RunControlService(session)
            paused = await controls.pause(user_id, run_id)
            paused_from = paused.paused_from_status or ""
        async with SessionFactory() as session:
            resumed = await RunControlService(session).resume(user_id, run_id)
            resumed_status = resumed.status
        async with SessionFactory() as session:
            interrupted = await RunControlService(session).interrupt(user_id, agent_id)
            task_row = await session.scalar(select(Task).where(Task.agent_id == agent_id))
            task_status = task_row.status if task_row is not None else "missing"
            interrupted_status = interrupted.status
        async with SessionFactory() as session:
            cancelled = await RunControlService(session).cancel(user_id, run_id)
        return paused_from, resumed_status, f"{interrupted_status}:{task_status}", cancelled.status

    assert api_client.portal.call(exercise_controls) == (
        "EXECUTING",
        "EXECUTING",
        "interrupted:interrupted",
        "CANCELLED",
    )
