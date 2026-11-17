"""Run creation and tenant-scoped queries."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.events.bus import EventBus
from app.models.run import Run
from app.repos.projects import ProjectRepository
from app.repos.runs import RunRepository
from app.repos.tasks import TaskRepository


class ConcurrentRunLimitExceeded(Exception):
    """Raised when creating another active run would exceed a user's configured cap."""


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
        project_id: UUID | None = None,
    ) -> Run:
        # Serialize creation for this user so simultaneous requests cannot exceed the cap.
        await self._session.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(user_id)))))
        active_count = await self._session.scalar(
            select(func.count(Run.id)).where(
                Run.user_id == user_id,
                Run.status.notin_(("COMPLETED", "FAILED", "CANCELLED")),
            )
        )
        if (active_count or 0) >= get_settings().max_concurrent_runs_per_user:
            raise ConcurrentRunLimitExceeded("Concurrent run limit reached.")
        final_idea = idea.strip()
        run: Run | None
        if project_id is None:
            project = await self._projects.create_for_user(user_id, name=_project_name(final_idea))
            run = await self._runs.create_for_user(
                user_id,
                project_id=project.id,
                idea=final_idea,
                autonomy_level=autonomy_level,
                budget_usd=budget_usd,
            )
        else:
            existing_project = await self._projects.get_for_user(user_id, project_id)
            if existing_project is None:
                raise LookupError("Project not found.")
            previous_runs = await self._projects.runs_for_user(user_id, project_id)
            context_lines: list[str] = []
            if previous_runs:
                previous = previous_runs[0]
                previous_idea = previous.idea.split("\n\nPrior project context:")[0][:1200]
                context_lines.append(f"Previous run idea: {previous_idea}")
                task_rows = await TaskRepository(self._session).list_for_run(user_id, previous.id)
                for task in (task_rows or [])[:8]:
                    summary = task.output.get("summary") if task.output else None
                    if isinstance(summary, str):
                        context_lines.append(f"{task.title}: {summary[:500]}")
                if existing_project.repo_url:
                    context_lines.append(f"Existing repository: {existing_project.repo_url}")
            if context_lines:
                final_idea = (
                    final_idea[:10_000] + "\n\nPrior project context:\n" + "\n".join(context_lines)
                )[:20_000]
            run = await self._projects.create_run_for_user(
                user_id,
                project_id,
                idea=final_idea,
                autonomy_level=autonomy_level,
                budget_usd=budget_usd,
            )
            if run is None:
                raise LookupError("Project not found.")
        event = await self._event_bus.persist(
            self._session,
            user_id,
            run.id,
            event_type="run.state_changed",
            payload={"status": "CREATED", "project_id": str(run.project_id)},
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
