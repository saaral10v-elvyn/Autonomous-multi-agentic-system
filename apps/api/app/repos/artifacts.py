"""Tenant-scoped run artifact repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.run import Run


class ArtifactRepository:
    """All artifact access is scoped through a run owned by the supplied user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_run(self, user_id: UUID, run_id: UUID) -> list[Artifact]:
        result = await self._session.execute(
            select(Artifact)
            .join(Run, Run.id == Artifact.run_id)
            .where(Run.user_id == user_id, Run.id == run_id)
            .order_by(Artifact.created_at, Artifact.id)
        )
        return list(result.scalars().all())
