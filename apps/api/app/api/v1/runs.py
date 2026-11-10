"""Run creation, history, and durable event replay endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import (
    AnswersRequest,
    PlanUpdateRequest,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
)
from app.repos.events import EventRepository
from app.repos.runs import RunRepository
from app.services.orchestration import (
    InvalidAnswers,
    PlanService,
    QuestionService,
    WorkflowConflict,
    enqueue_advance,
)
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
    return RunResponse.model_validate(run)
