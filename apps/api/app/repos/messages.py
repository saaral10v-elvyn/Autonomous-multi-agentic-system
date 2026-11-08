"""Tenant-scoped run message repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.run import Run


class MessageRepository:
    """All message access is scoped through a run owned by the supplied user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_run(self, user_id: UUID, run_id: UUID) -> list[Message]:
        result = await self._session.execute(
            select(Message)
            .join(Run, Run.id == Message.run_id)
            .where(Run.user_id == user_id, Run.id == run_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(result.scalars().all())
