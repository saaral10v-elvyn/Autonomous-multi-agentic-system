"""Tenant-scoped project repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project


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
