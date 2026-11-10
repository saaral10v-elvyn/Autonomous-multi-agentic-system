"""Real-Postgres M3 orchestration tests using the deterministic FakeProvider."""

from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi.testclient import TestClient

from app.agents.registry import ROLE_TEMPLATES
from app.agents.runtime import AgentRuntime
from app.agents.schemas import PlanDocument
from app.api.v1.deps import get_llm_client
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.llm.client import LLMClient
from app.llm.key_resolver import RepositoryProviderKeyResolver
from app.llm.types import Provider
from app.models.event import Event
from app.models.run import Run
from app.orchestrator.state import transition_run
from app.repos.events import EventRepository
from app.repos.questions import QuestionRepository
from app.services.orchestration import PlanService, QuestionService, advance_run_job
from app.services.runs import RunService
from tests.conftest import WEB_ORIGIN


def make_fake_client() -> LLMClient:
    return get_llm_client()


def make_plan() -> dict[str, object]:
    return {
        "product_brief": "A private journal for small teams.",
        "tech_stack": ["Next.js", "TypeScript"],
        "milestones": [
            {"id": "foundation", "title": "Foundation", "description": "Set up the app."}
        ],
        "tasks": [
            {
                "id": "landing",
                "title": "Build the landing page",
                "description": "Create a responsive first page.",
                "depends_on": [],
            }
        ],
    }


def create_user_and_run(
    client: TestClient, email: str, *, autonomy: str = "step_by_step", budget: str = "25.00"
) -> tuple[UUID, UUID]:
    signup = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "m3-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    user_id = UUID(signup.json()["id"])

    async def create() -> Run:
        async with SessionFactory() as session:
            return await RunService(session).create(
                user_id,
                idea="A useful personal journal",
                autonomy_level=autonomy,
                budget_usd=budget,
            )

    run = client.portal.call(create)
    return user_id, run.id


def get_events(client: TestClient, user_id: UUID, run_id: UUID) -> list[Event]:
    async def fetch() -> list[Event]:
        async with SessionFactory() as session:
            return await EventRepository(session).list_after(user_id, run_id, 0, limit=500)

    return client.portal.call(fetch)


def advance(client: TestClient, user_id: UUID, run_id: UUID, llm: LLMClient) -> str:
    async def run_job() -> str:
        return await advance_run_job(user_id, run_id, llm)

    return client.portal.call(run_job)


def test_fake_provider_flow_emits_exact_events_through_plan_approval(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails())
    llm = make_fake_client()

    assert advance(api_client, user_id, run_id, llm) == "AWAITING_ANSWERS"

    async def submit() -> None:
        async with SessionFactory() as session:
            await QuestionService(session).submit(
                user_id, run_id, {"primary_users": "decide_for_me"}
            )

    api_client.portal.call(submit)
    assert advance(api_client, user_id, run_id, llm) == "AWAITING_PLAN_APPROVAL"

    async def approve() -> str:
        async with SessionFactory() as session:
            run = await PlanService(session).approve(user_id, run_id)
            return run.status

    assert api_client.portal.call(approve) == "EXECUTING"
    event_types = [event.type for event in get_events(api_client, user_id, run_id)]
    assert event_types == [
        "run.state_changed",
        "run.state_changed",
        "agent.step_completed",
        "question.asked",
        "run.state_changed",
        "question.answered",
        "run.state_changed",
        "agent.step_completed",
        "plan.created",
        "run.state_changed",
        "plan.approved",
        "run.state_changed",
    ]
    assert event_types[-1] == "run.state_changed"


def test_autopilot_auto_approves_plan_and_records_event(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails(), autonomy="autopilot")
    llm = make_fake_client()
    assert advance(api_client, user_id, run_id, llm) == "AWAITING_ANSWERS"

    async def submit() -> None:
        async with SessionFactory() as session:
            await QuestionService(session).submit(
                user_id, run_id, {"primary_users": "decide_for_me"}
            )

    api_client.portal.call(submit)
    assert advance(api_client, user_id, run_id, llm) == "EXECUTING"
    events = get_events(api_client, user_id, run_id)
    assert [event.type for event in events][-3:] == [
        "plan.created",
        "plan.auto_approved",
        "run.state_changed",
    ]
    assert events[-1].payload["plan_auto_approved"] is True


def test_missing_provider_key_fails_run_with_clear_event(
    api_client: TestClient, test_emails: Callable[[], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "false")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails())
    client = LLMClient(RepositoryProviderKeyResolver(SessionFactory))
    assert advance(api_client, user_id, run_id, client) == "FAILED"
    events = get_events(api_client, user_id, run_id)
    failed = next(event for event in events if event.type == "run.failed")
    assert "No provider key is configured" in failed.payload["message"]


def test_awaiting_input_run_does_not_enqueue_another_worker_job(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails())
    llm = make_fake_client()

    async def check() -> tuple[str, int, int]:
        from redis.asyncio import Redis

        from app.core.config import get_settings as current_settings

        redis = Redis.from_url(current_settings().redis_url)
        before = await redis.zcard("arq:queue")
        state = await advance_run_job(user_id, run_id, llm)
        after = await redis.zcard("arq:queue")
        await redis.close()
        return state, before, after

    state, before, after = api_client.portal.call(check)
    assert state == "AWAITING_ANSWERS"
    assert before == after


def test_worker_restart_reuses_persisted_planner_step_without_duplicate_events(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails())
    llm = make_fake_client()
    assert advance(api_client, user_id, run_id, llm) == "AWAITING_ANSWERS"

    async def prepare_planning() -> None:
        async with SessionFactory() as session:
            await QuestionService(session).submit(
                user_id, run_id, {"primary_users": "decide_for_me"}
            )

    api_client.portal.call(prepare_planning)
    key_id = uuid5(NAMESPACE_URL, f"forge-fake:{user_id}")
    runtime = AgentRuntime(SessionFactory, EventBus())

    async def persist_planner_response() -> None:
        await runtime.complete(
            user_id,
            run_id,
            Provider.OPENAI,
            key_id,
            ROLE_TEMPLATES["planner"],
            [{"role": "user", "content": "resume this plan"}],
            llm,
        )

    api_client.portal.call(persist_planner_response)
    count_before = sum(
        event.type == "agent.step_completed" and event.payload.get("role") == "planner"
        for event in get_events(api_client, user_id, run_id)
    )

    assert advance(api_client, user_id, run_id, llm) == "AWAITING_PLAN_APPROVAL"
    events = get_events(api_client, user_id, run_id)
    count_after = sum(
        event.type == "agent.step_completed" and event.payload.get("role") == "planner"
        for event in events
    )
    assert count_before == count_after == 1
    assert sum(event.type == "plan.created" for event in events) == 1
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


def test_run_owned_answers_plan_and_approval_routes_reject_other_user(
    api_client: TestClient,
    test_emails: Callable[[], str],
) -> None:
    owner_id, run_id = create_user_and_run(api_client, test_emails())
    del owner_id
    other_signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "m3-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert other_signup.status_code == 201
    headers = {"Origin": WEB_ORIGIN}
    valid_plan = make_plan()
    assert (
        api_client.post(
            f"/api/v1/runs/{run_id}/answers",
            json={"answers": {"primary_users": "decide_for_me"}},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        api_client.put(
            f"/api/v1/runs/{run_id}/plan", json={"plan": valid_plan}, headers=headers
        ).status_code
        == 404
    )
    assert (
        api_client.post(f"/api/v1/runs/{run_id}/plan/approve", headers=headers).status_code == 404
    )


def test_answers_rejected_in_wrong_state_and_invalid_answers_rejected(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    user_id, run_id = create_user_and_run(api_client, test_emails())
    headers = {"Origin": WEB_ORIGIN}
    wrong_state = api_client.post(
        f"/api/v1/runs/{run_id}/answers",
        json={"answers": {"primary_users": "decide_for_me"}},
        headers=headers,
    )
    assert wrong_state.status_code == 409
    advance(api_client, user_id, run_id, make_fake_client())
    invalid = api_client.post(
        f"/api/v1/runs/{run_id}/answers",
        json={"answers": {"unknown": "value"}},
        headers=headers,
    )
    assert invalid.status_code == 422


def test_pause_state_remembers_and_restores_prior_state(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    user_id, run_id = create_user_and_run(api_client, test_emails())

    async def pause_resume() -> tuple[str, str]:
        async with SessionFactory() as session:
            await transition_run(session, user_id, run_id, "CLARIFYING")
        async with SessionFactory() as session:
            paused = await transition_run(session, user_id, run_id, "PAUSED")
            previous = paused.paused_from_status or ""
        async with SessionFactory() as session:
            from app.orchestrator.state import resume_run

            resumed = await resume_run(session, user_id, run_id)
            return previous, resumed.status

    previous, resumed = api_client.portal.call(pause_resume)
    assert (previous, resumed) == ("CLARIFYING", "CLARIFYING")


def test_cycle_detection_rejects_cyclic_plans() -> None:
    plan = make_plan()
    plan["tasks"] = [
        {"id": "a", "title": "A", "description": "A task", "depends_on": ["b"]},
        {"id": "b", "title": "B", "description": "A task", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="cycle"):
        PlanDocument.model_validate(plan)


async def seed_open_questions(
    user_id: UUID, run_id: UUID, question_items: list[dict[str, object]]
) -> None:
    async with SessionFactory() as session:
        run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
        assert run is not None
        run.status = "AWAITING_ANSWERS"
        question = await QuestionRepository(session).create_for_run(
            user_id,
            run_id,
            question_schema={"questions": question_items},
        )
        assert question is not None
        await session.commit()


def question_item(
    question_id: str, question_type: str, options: list[dict[str, str]] | None = None
) -> dict[str, object]:
    return {
        "id": question_id,
        "prompt": f"Answer {question_id}",
        "type": question_type,
        "options": options or [],
        "allow_decide_for_me": False,
    }


@pytest.mark.parametrize(
    ("item", "answer"),
    [
        (
            question_item("choice", "single_choice", [{"id": "one", "label": "One"}]),
            "one",
        ),
        (
            question_item(
                "choices",
                "multi_choice",
                [{"id": "one", "label": "One"}, {"id": "two", "label": "Two"}],
            ),
            ["one", "two"],
        ),
        (question_item("details", "free_text"), "A regular text response."),
    ],
)
def test_rest_answers_accept_each_question_type(
    api_client: TestClient,
    test_emails: Callable[[], str],
    item: dict[str, object],
    answer: object,
) -> None:
    user_id, run_id = create_user_and_run(api_client, test_emails())
    api_client.portal.call(seed_open_questions, user_id, run_id, [item])

    response = api_client.post(
        f"/api/v1/runs/{run_id}/answers",
        json={"answers": {item["id"]: answer}},
        headers={"Origin": WEB_ORIGIN},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PLANNING"
    events = get_events(api_client, user_id, run_id)
    assert any(event.type == "question.answered" for event in events)


@pytest.mark.parametrize(
    ("item", "answers", "message"),
    [
        (
            question_item("choice", "single_choice", [{"id": "one", "label": "One"}]),
            {"choice": "outside-list"},
            "invalid choice",
        ),
        (
            question_item(
                "choices",
                "multi_choice",
                [{"id": "one", "label": "One"}, {"id": "two", "label": "Two"}],
            ),
            {"choices": ["one", "outside-list"]},
            "invalid choices",
        ),
        (question_item("details", "free_text"), "", "requires text"),
        (question_item("required", "free_text"), {}, "exactly the open question IDs"),
    ],
)
def test_rest_answers_reject_invalid_choices_and_missing_required_answer(
    api_client: TestClient,
    test_emails: Callable[[], str],
    item: dict[str, object],
    answers: dict[str, object],
    message: str,
) -> None:
    user_id, run_id = create_user_and_run(api_client, test_emails())
    api_client.portal.call(seed_open_questions, user_id, run_id, [item])
    before = get_events(api_client, user_id, run_id)

    response = api_client.post(
        f"/api/v1/runs/{run_id}/answers",
        json={"answers": answers},
        headers={"Origin": WEB_ORIGIN},
    )

    assert response.status_code == 422
    assert message in response.json()["detail"]
    status_response = api_client.get(f"/api/v1/runs/{run_id}")
    assert status_response.json()["status"] == "AWAITING_ANSWERS"
    after = get_events(api_client, user_id, run_id)
    assert [(event.seq, event.type) for event in after] == [
        (event.seq, event.type) for event in before
    ]


def invalid_plan_cases() -> list[tuple[dict[str, object], str]]:
    missing_field: dict[str, object] = {
        "product_brief": "A journal.",
        "milestones": [{"id": "m1", "title": "Start", "description": "Begin."}],
        "tasks": [{"id": "t1", "title": "Page", "description": "Build it."}],
    }
    cyclic = make_plan()
    cyclic["tasks"] = [
        {"id": "a", "title": "A", "description": "A task", "depends_on": ["b"]},
        {"id": "b", "title": "B", "description": "B task", "depends_on": ["a"]},
    ]
    return [(missing_field, "tech_stack"), (cyclic, "cycle")]


async def set_plan_approval_state(user_id: UUID, run_id: UUID) -> None:
    async with SessionFactory() as session:
        run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
        assert run is not None
        run.status = "AWAITING_PLAN_APPROVAL"
        await session.commit()


def event_signature(events: list[Event]) -> list[tuple[int, str, dict[str, object]]]:
    return [(event.seq, event.type, event.payload) for event in events]


@pytest.mark.parametrize("transport", ["rest", "websocket"])
@pytest.mark.parametrize(("plan", "message"), invalid_plan_cases())
def test_plan_edits_reject_invalid_and_cyclic_plans_without_side_effects(
    api_client: TestClient,
    test_emails: Callable[[], str],
    transport: str,
    plan: dict[str, object],
    message: str,
) -> None:
    user_id, run_id = create_user_and_run(api_client, test_emails())
    api_client.portal.call(set_plan_approval_state, user_id, run_id)
    before = get_events(api_client, user_id, run_id)

    if transport == "rest":
        response = api_client.put(
            f"/api/v1/runs/{run_id}/plan",
            json={"plan": plan},
            headers={"Origin": WEB_ORIGIN},
        )
        assert response.status_code == 422
        assert message in response.text.lower()
    else:
        with api_client.websocket_connect(
            f"/ws/runs/{run_id}?after_seq=0", headers={"Origin": WEB_ORIGIN}
        ) as connection:
            for _ in before:
                connection.receive_json()
            connection.send_json({"type": "plan.edit", "plan": plan})
            rejection = connection.receive_json()
        assert rejection["type"] == "request.rejected"
        assert rejection["request_type"] == "plan.edit"
        assert message in rejection["message"].lower()

    assert api_client.get(f"/api/v1/runs/{run_id}").json()["status"] == ("AWAITING_PLAN_APPROVAL")
    assert event_signature(get_events(api_client, user_id, run_id)) == event_signature(before)
