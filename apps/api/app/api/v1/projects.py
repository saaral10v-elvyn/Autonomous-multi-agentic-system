"""Tenant-scoped project history and iteration endpoints."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import ProjectResponse, RunResponse
from app.repos.projects import ProjectRepository

router = APIRouter(prefix="/projects")


@router.get("", response_model=list[ProjectResponse])
async def list_projects(user: CurrentUser, session: DbSession) -> list[ProjectResponse]:
    projects = await ProjectRepository(session).list_for_user(user.id)
    return [ProjectResponse.model_validate(project) for project in projects]


@router.get("/{project_id}")
async def get_project(project_id: UUID, user: CurrentUser, session: DbSession) -> dict[str, object]:
    repository = ProjectRepository(session)
    project = await repository.get_for_user(user.id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    runs = await repository.runs_for_user(user.id, project_id)
    return {
        "project": ProjectResponse.model_validate(project),
        "runs": [RunResponse.model_validate(run) for run in runs or []],
    }
