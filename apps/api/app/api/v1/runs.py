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
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
    TaskResponse,
    UserMessageRequest,
)
from app.orchestrator.state import IllegalRunTransition
from app.repos.agents import AgentRepository
from app.repos.events import EventRepository
from app.repos.runs import RunRepository
from app.repos.tasks import TaskRepository
from app.services.approvals import ApprovalConflict, ApprovalService
from app.services.orchestration import (
    InvalidAnswers,
    PlanService,
    QuestionService,
    WorkflowConflict,
    enqueue_advance,
)
from app.services.run_controls import RunControlService
from app.services.runs import RunService

router = APIRouter(prefix="/runs")


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunCreateRequest, user: CurrentUser, session: DbSession
) -> RunResponse:
    run = await RunService(session).create(
        user.id,
        idea=payload.idea,
        autonomy_level=payload.autonomy_level.value,
        budget_usd=payload.budget_usd,
    )
    await enqueue_advance(user.id, run.id)
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
