"""Run creation and tenant-scoped queries."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.bus import EventBus
from app.models.run import Run
from app.repos.projects import ProjectRepository
from app.repos.runs import RunRepository


def _project_name(idea: str) -> str:
    """Derive a readable project label from the initial idea."""
    normalized = " ".join(idea.split())
    if not normalized:
        return "New project"
    return normalized[:197].rstrip() + ("..." if len(normalized) > 197 else "")


class RunService:
    """Create projects and runs with their initial event in one transaction."""

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._runs = RunRepository(session)
        self._event_bus = event_bus or EventBus()

    async def create(
        self,
        user_id: UUID,
        *,
        idea: str,
        autonomy_level: str,
        budget_usd: Decimal,
    ) -> Run:
        project = await self._projects.create_for_user(user_id, name=_project_name(idea))
        run = await self._runs.create_for_user(
            user_id,
            project_id=project.id,
            idea=idea.strip(),
            autonomy_level=autonomy_level,
            budget_usd=budget_usd,
        )
        event = await self._event_bus.persist(
            self._session,
            user_id,
            run.id,
            event_type="run.state_changed",
            payload={"status": "CREATED", "project_id": str(project.id)},
        )
        if event is None:
            raise RuntimeError("New run could not be scoped for event creation.")
        await self._session.commit()
        await self._session.refresh(run)
        await self._session.refresh(event)
        await self._event_bus.publish(event)
        return run

    async def list(self, user_id: UUID) -> list[Run]:
        return await self._runs.list_for_user(user_id)

    async def get(self, user_id: UUID, run_id: UUID) -> Run | None:
        return await self._runs.get_for_user(user_id, run_id)
