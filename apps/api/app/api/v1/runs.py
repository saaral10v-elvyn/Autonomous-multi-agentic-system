"""Run creation, history, and durable event replay endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import RunCreateRequest, RunEventResponse, RunResponse
from app.repos.events import EventRepository
from app.repos.runs import RunRepository
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
