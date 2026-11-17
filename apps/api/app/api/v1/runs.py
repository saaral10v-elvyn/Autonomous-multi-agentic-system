"""Run creation, history, and durable event replay endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import (
    AgentResponse,
    AnswersRequest,
    ApprovalResponse,
    AutonomyUpdateRequest,
    PlanUpdateRequest,
    ReportResponse,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
    TaskResponse,
    UserMessageRequest,
)
from app.core.config import get_settings
from app.events.bus import EventBus
from app.orchestrator.state import IllegalRunTransition, stage_transition
from app.repos.agents import AgentRepository
from app.repos.events import EventRepository
from app.repos.runs import RunRepository
from app.repos.tasks import TaskRepository
from app.sandbox.base import SandboxHandle, SandboxProvider
from app.sandbox.factory import get_sandbox_provider
from app.sandbox.tools import sandbox_diff
from app.services.approvals import ApprovalConflict, ApprovalService
from app.services.orchestration import (
    InvalidAnswers,
    PlanService,
    QuestionService,
    WorkflowConflict,
    enqueue_advance,
)
from app.services.reports import get_final_report
from app.services.run_controls import RunControlService
from app.services.runs import RunService

router = APIRouter(prefix="/runs")


async def _owned_sandbox(
    user_id: UUID, run_id: UUID, session: DbSession
) -> tuple[SandboxProvider, SandboxHandle]:
    if await RunRepository(session).get_for_user(user_id, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    provider = get_sandbox_provider()
    handle = next(
        (item for item in await provider.list_active() if item.run_id == str(run_id)), None
    )
    if handle is None:
        raise HTTPException(status_code=404, detail="Sandbox not found.")
    return provider, handle


@router.get("/{run_id}/files")
async def run_files(run_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, object]:
    provider, handle = await _owned_sandbox(user.id, run_id, session)
    entries: list[dict[str, object]] = []

    async def walk(path: str, depth: int = 0) -> None:
        if depth > 12:
            return
        for name in await provider.list_dir(handle, path):
            child = f"{path.rstrip('/')}/{name}"
            try:
                is_dir = await provider.is_dir(handle, child)
            except (ValueError, LookupError, RuntimeError):
                continue
            entries.append({"path": child, "name": name, "type": "directory" if is_dir else "file"})
            if is_dir:
                await walk(child, depth + 1)

    await walk("/workspace")
    return {"files": entries}


@router.get("/{run_id}/files/content")
async def run_file_content(
    run_id: UUID, path: str, user: CurrentUser, session: DbSession
) -> dict[str, str]:
    provider, handle = await _owned_sandbox(user.id, run_id, session)
    try:
        return {
            "path": path,
            "content": await provider.read_file(
                handle, path, get_settings().sandbox_max_output_bytes
            ),
        }
    except (ValueError, LookupError, RuntimeError):
        raise HTTPException(status_code=404, detail="File not found.") from None


@router.get("/{run_id}/diff")
async def run_diff(run_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, str]:
    provider, handle = await _owned_sandbox(user.id, run_id, session)
    return {"diff": await sandbox_diff(provider, handle, get_settings().sandbox_max_output_bytes)}


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunCreateRequest, user: CurrentUser, session: DbSession
) -> RunResponse:
    try:
        run = await RunService(session).create(
            user.id,
            idea=payload.idea,
            autonomy_level=payload.autonomy_level.value,
            budget_usd=payload.budget_usd,
            project_id=payload.project_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    await enqueue_advance(user.id, run.id)
    return RunResponse.model_validate(run)


@router.get("/{run_id}/report", response_model=ReportResponse)
async def get_run_report(run_id: UUID, user: CurrentUser, session: DbSession) -> ReportResponse:
    report = await get_final_report(session, user.id, run_id)
    if report is None:
        if await RunRepository(session).get_for_user(user.id, run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        raise HTTPException(status_code=404, detail="Final report is not available yet.")
    return ReportResponse(
        id=report.id,
        run_id=run_id,
        markdown=str(report.meta.get("markdown", "")),
        created_at=report.created_at,
    )


@router.post("/{run_id}/tasks/{task_id}/retry", response_model=RunResponse)
async def retry_task(
    run_id: UUID, task_id: UUID, user: CurrentUser, session: DbSession
) -> RunResponse:
    run = await RunRepository(session).get_for_user(user.id, run_id, lock=True)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if run.status != "FAILED":
        raise HTTPException(status_code=409, detail="Run cannot retry a task in its current state.")
    tasks = await TaskRepository(session).list_for_run(user.id, run_id)
    task = next((item for item in tasks or [] if item.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.status not in {"failed", "blocked", "interrupted"}:
        raise HTTPException(
            status_code=409, detail="Only failed, blocked, or interrupted tasks can be retried."
        )
    reset = await TaskRepository(session).retry_from(user.id, run_id, task_id)
    if reset is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    bus = EventBus()
    state_event = None
    if run.status == "FAILED":
        run.completed_at = None
        reset_ids = {item.id for item in reset}
        reset_roles = {
            str(item.input.get("system_role"))
            for item in tasks or []
            if item.id in reset_ids and item.input.get("system_role")
        }
        reset_plan_task = any(
            item.id in reset_ids and not item.input.get("system_role") for item in tasks or []
        )
        target = (
            "EXECUTING"
            if reset_plan_task
            else next(
                (
                    state
                    for role, state in (
                        ("tester", "VERIFYING"),
                        ("reviewer", "VERIFYING"),
                        ("deployer", "DEPLOYING"),
                        ("reporter", "REPORTING"),
                    )
                    if role in reset_roles
                ),
                "EXECUTING",
            )
        )
        state_event = await stage_transition(
            session,
            user.id,
            run,
            target,
            event_bus=bus,
            payload={"retry_from_task_id": str(task_id)},
        )
    event = await bus.persist(
        session,
        user.id,
        run_id,
        event_type="task.retry_requested",
        payload={"task_id": str(task_id), "reset_task_ids": [str(item.id) for item in reset]},
    )
    await session.commit()
    if state_event is not None:
        await session.refresh(state_event)
        await bus.publish(state_event)
    if event is not None:
        await session.refresh(event)
        await bus.publish(event)
    await session.refresh(run)
    await enqueue_advance(user.id, run_id)
    return RunResponse.model_validate(run)


@router.get("", response_model=list[RunResponse])
async def list_runs(user: CurrentUser, session: DbSession) -> list[RunResponse]:
    runs = await RunService(session).list(user.id)
    return [RunResponse.model_validate(run) for run in runs]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, user: CurrentUser, session: DbSession) -> RunResponse:
    run = await RunRepository(session).get_for_user(user.id, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return RunResponse.model_validate(run)


@router.get("/{run_id}/events", response_model=list[RunEventResponse])
async def get_run_events(
    run_id: UUID,
    user: CurrentUser,
    session: DbSession,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> list[RunEventResponse]:
    events = await EventRepository(session).list_after(user.id, run_id, after_seq, limit=500)
    if await EventRepository(session).high_watermark(user.id, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return [RunEventResponse.model_validate(event) for event in events]


@router.get("/{run_id}/agents", response_model=list[AgentResponse])
async def list_run_agents(
    run_id: UUID, user: CurrentUser, session: DbSession
) -> list[AgentResponse]:
    agents = await AgentRepository(session).list_for_run(user.id, run_id)
    if agents is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return [AgentResponse.model_validate(agent) for agent in agents]


@router.get("/{run_id}/tasks", response_model=list[TaskResponse])
async def list_run_tasks(run_id: UUID, user: CurrentUser, session: DbSession) -> list[TaskResponse]:
    tasks = await TaskRepository(session).list_for_run(user.id, run_id)
    if tasks is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return [TaskResponse.model_validate(task) for task in tasks]


@router.post("/{run_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_run_message(
    run_id: UUID,
    payload: UserMessageRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, str]:
    try:
        await RunControlService(session).message(
            user.id, run_id, payload.content.strip(), payload.agent_id
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Run or agent not found.") from None
    except IllegalRunTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return {"status": "accepted"}


@router.post("/{run_id}/pause", response_model=RunResponse)
async def pause_run(run_id: UUID, user: CurrentUser, session: DbSession) -> RunResponse:
    try:
        run = await RunControlService(session).pause(user.id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except IllegalRunTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return RunResponse.model_validate(run)


@router.post("/{run_id}/resume", response_model=RunResponse)
async def resume_run_endpoint(run_id: UUID, user: CurrentUser, session: DbSession) -> RunResponse:
    try:
        run = await RunControlService(session).resume(user.id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except IllegalRunTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    if run.status != "AWAITING_APPROVAL":
        await enqueue_advance(user.id, run_id)
    return RunResponse.model_validate(run)


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, user: CurrentUser, session: DbSession) -> RunResponse:
    try:
        run = await RunControlService(session).cancel(user.id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except IllegalRunTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return RunResponse.model_validate(run)


@router.get("/{run_id}/approvals", response_model=list[ApprovalResponse])
async def list_run_approvals(
    run_id: UUID, user: CurrentUser, session: DbSession
) -> list[ApprovalResponse]:
    try:
        approvals = await ApprovalService(session).list_for_run(user.id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    return [ApprovalResponse.model_validate(approval) for approval in approvals]


@router.patch("/{run_id}/autonomy", response_model=RunResponse)
async def update_run_autonomy(
    run_id: UUID,
    payload: AutonomyUpdateRequest,
    user: CurrentUser,
    session: DbSession,
) -> RunResponse:
    try:
        run = await ApprovalService(session).update_autonomy(
            user.id, run_id, payload.autonomy_level.value
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except ApprovalConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return RunResponse.model_validate(run)


@router.post("/{run_id}/answers", response_model=RunResponse)
async def submit_answers(
    run_id: UUID, payload: AnswersRequest, user: CurrentUser, session: DbSession
) -> RunResponse:
    try:
        run = await QuestionService(session).submit(user.id, run_id, payload.answers)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except InvalidAnswers as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except WorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    await enqueue_advance(user.id, run_id)
    return RunResponse.model_validate(run)


@router.put("/{run_id}/plan")
async def update_plan(
    run_id: UUID, payload: PlanUpdateRequest, user: CurrentUser, session: DbSession
) -> dict[str, object]:
    try:
        return await PlanService(session).update(user.id, run_id, payload.plan)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except WorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValidationError:
        raise HTTPException(status_code=422, detail="Plan is invalid.") from None


@router.post("/{run_id}/plan/approve", response_model=RunResponse)
async def approve_plan(run_id: UUID, user: CurrentUser, session: DbSession) -> RunResponse:
    try:
        run = await PlanService(session).approve(user.id, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found.") from None
    except WorkflowConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    await enqueue_advance(user.id, run_id)
    return RunResponse.model_validate(run)
