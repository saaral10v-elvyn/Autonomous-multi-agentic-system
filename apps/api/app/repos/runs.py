"""Tenant-scoped run repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import Run


class RunRepository:
    """All run reads and writes require the owning user ID."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_user(
        self,
        user_id: UUID,
        *,
        project_id: UUID,
        idea: str,
        autonomy_level: str,
        budget_usd: object,
    ) -> Run:
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

    async def get_for_user(self, user_id: UUID, run_id: UUID, *, lock: bool = False) -> Run | None:
        statement = select(Run).where(Run.user_id == user_id, Run.id == run_id)
        if lock:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: UUID) -> list[Run]:
        result = await self._session.execute(
            select(Run).where(Run.user_id == user_id).order_by(Run.created_at.desc())
        )
        return list(result.scalars().all())
