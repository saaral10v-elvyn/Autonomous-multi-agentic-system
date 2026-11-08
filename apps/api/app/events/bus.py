"""Transactional event append and post-commit Redis publication."""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory
from app.events.redis import get_redis
from app.models.event import Event
from app.repos.events import EventRepository

logger = logging.getLogger(__name__)


def channel_for_run(run_id: UUID) -> str:
    """Return the Redis channel used to signal a run's durable event stream."""
    return f"forge:run:{run_id}:events"


class EventBus:
    """Append events transactionally, then publish a wake-up notification."""

    async def persist(
        self,
        session: AsyncSession,
        user_id: UUID,
        run_id: UUID,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> Event | None:
        """Add a sequenced event to the caller's transaction without committing."""
        return await EventRepository(session).append_for_user(
            user_id, run_id, event_type=event_type, payload=payload
        )

    async def append(
        self,
        user_id: UUID,
        run_id: UUID,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> Event | None:
        """Persist an event, commit, then publish its sequence as a Redis hint."""
        async with SessionFactory() as session:
            event = await self.persist(
                session, user_id, run_id, event_type=event_type, payload=payload
            )
            if event is None:
                return None
            await session.commit()
            await session.refresh(event)
        await self.publish(event)
        return event

    async def publish(self, event: Event) -> None:
        """Publish after commit; Postgres remains the source of truth."""
        try:
            await get_redis().publish(channel_for_run(event.run_id), str(event.seq))
        except Exception:
            logger.warning(
                "run_event.redis_publish_failed",
                extra={"run_id": str(event.run_id), "seq": event.seq},
            )
