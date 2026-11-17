"""Tenant-scoped project repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.run import Run


class ProjectRepository:
    """Access projects only when their owner ID is supplied."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_user(self, user_id: UUID, *, name: str) -> Project:
        project = Project(user_id=user_id, name=name)
        self._session.add(project)
        await self._session.flush()
        return project

    async def list_for_user(self, user_id: UUID) -> list[Project]:
        result = await self._session.execute(
            select(Project).where(Project.user_id == user_id).order_by(Project.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_for_user(self, user_id: UUID, project_id: UUID) -> Project | None:
        return await self._session.scalar(
            select(Project).where(Project.user_id == user_id, Project.id == project_id)
        )

    async def create_run_for_user(
        self, user_id: UUID, project_id: UUID, *, idea: str, autonomy_level: str, budget_usd: object
    ) -> Run | None:
        project = await self.get_for_user(user_id, project_id)
        if project is None:
            return None
        run = Run(
            user_id=user_id,
            project_id=project_id,
            idea=idea,
            autonomy_level=autonomy_level,
            status="CREATED",
            budget_usd=budget_usd,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def runs_for_user(self, user_id: UUID, project_id: UUID) -> list[Run] | None:
        if await self.get_for_user(user_id, project_id) is None:
            return None
        result = await self._session.scalars(
            select(Run)
            .where(Run.user_id == user_id, Run.project_id == project_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
        )
        return list(result.all())

    async def update_delivery_links(
        self, user_id: UUID, project_id: UUID, *, repo_url: str | None, live_url: str | None
    ) -> Project | None:
        project = await self.get_for_user(user_id, project_id)
        if project is None:
            return None
        if repo_url is not None:
            project.repo_url = repo_url
        if live_url is not None:
            project.live_url = live_url
        await self._session.flush()
        return project
