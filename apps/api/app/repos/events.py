"""Tenant-scoped event queries and append sequencing."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.run import Run


class EventRepository:
    """Access event rows only after scoping the owning run by user ID."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_for_user(
        self,
        user_id: UUID,
        run_id: UUID,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Event | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        highest = await self._session.scalar(
            select(func.coalesce(func.max(Event.seq), 0)).where(Event.run_id == run_id)
        )
        event = Event(
            run_id=run_id,
            seq=int(highest or 0) + 1,
            type=event_type,
            payload=dict(payload),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def high_watermark(self, user_id: UUID, run_id: UUID) -> int | None:
        owner = await self._session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == user_id)
        )
        if owner is None:
            return None
        highest = await self._session.scalar(
            select(func.coalesce(func.max(Event.seq), 0)).where(Event.run_id == run_id)
        )
        return int(highest or 0)

    async def list_after(
        self,
        user_id: UUID,
        run_id: UUID,
        after_seq: int,
        *,
        through_seq: int | None = None,
        limit: int = 500,
    ) -> list[Event]:
        statement = (
            select(Event)
            .join(Run, Run.id == Event.run_id)
            .where(Run.user_id == user_id, Event.run_id == run_id, Event.seq > after_seq)
            .order_by(Event.seq)
            .limit(limit)
        )
        if through_seq is not None:
            statement = statement.where(Event.seq <= through_seq)
        result = await self._session.execute(statement)
        return list(result.scalars().all())
