"""User-scoped task graph persistence."""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import PlanDocument
from app.models.run import Run
from app.models.task import Task


class TaskRepository:
    """Task operations always prove ownership through the parent run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_run(self, user_id: UUID, run_id: UUID) -> list[Task] | None:
        owned = await self._session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == user_id)
        )
        if owned is None:
            return None
        result = await self._session.scalars(
            select(Task).where(Task.run_id == run_id).order_by(Task.created_at, Task.id)
        )
        return list(result.all())

    async def create_from_plan(
        self, user_id: UUID, run_id: UUID, plan: PlanDocument
    ) -> list[Task] | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        existing = await self._session.scalars(select(Task).where(Task.run_id == run_id))
        by_external_id = {task.external_id: task for task in existing.all()}
        for item in plan.tasks:
            task = by_external_id.get(item.id)
            if task is None:
                task = Task(
                    id=uuid4(),
                    run_id=run_id,
                    external_id=item.id,
                    title=item.title,
                    description=item.description,
                    depends_on=[],
                    input={},
                )
                self._session.add(task)
                by_external_id[item.id] = task
            else:
                task.title = item.title
                task.description = item.description
                if task.status == "pending":
                    task.input = {}
        for item in plan.tasks:
            task = by_external_id[item.id]
            task.depends_on = [by_external_id[key].id for key in item.depends_on]
        await self._session.flush()
        return list(by_external_id.values())

    async def claim_ready(
        self, user_id: UUID, run_id: UUID, limit: int
    ) -> tuple[list[Task], list[Task], set[UUID]] | None:
        """Atomically claim a bounded batch whose dependencies have completed."""
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        result = await self._session.scalars(
            select(Task).where(Task.run_id == run_id).order_by(Task.created_at, Task.id)
        )
        tasks = list(result.all())
        statuses = {str(task.id): task.status for task in tasks}
        claimed: list[Task] = []
        blocked: list[Task] = []
        newly_claimed: set[UUID] = set()
        for task in tasks:
            if task.status not in {"pending", "running"}:
                continue
            failed = [
                str(dependency)
                for dependency in task.depends_on
                if statuses.get(str(dependency)) in {"failed", "blocked", "interrupted"}
            ]
            if failed:
                task.status = "blocked"
                task.output = {
                    "status": "blocked",
                    "reason": "A task dependency failed.",
                    "failed_dependencies": failed,
                }
                statuses[str(task.id)] = "blocked"
                blocked.append(task)
                continue
            if task.status == "running":
                claimed.append(task)
                if len(claimed) >= limit:
                    break
                continue
            if all(statuses.get(str(dependency)) == "completed" for dependency in task.depends_on):
                task.status = "running"
                claimed.append(task)
                newly_claimed.add(task.id)
                if len(claimed) >= limit:
                    break
        await self._session.flush()
        return claimed, blocked, newly_claimed

    async def finish(
        self,
        user_id: UUID,
        run_id: UUID,
        task_id: UUID,
        status: str,
        output: dict[str, object],
    ) -> Task | None:
        """Update one task only through its owning run."""
        task = await self._session.scalar(
            select(Task)
            .join(Run, Run.id == Task.run_id)
            .where(Run.user_id == user_id, Run.id == run_id, Task.id == task_id)
            .with_for_update()
        )
        if task is None:
            return None
        task.status = status
        task.output = output
        await self._session.flush()
        return task

    async def get_for_user(self, user_id: UUID, run_id: UUID, task_id: UUID) -> Task | None:
        """Read a task only through its user-owned run."""
        return await self._session.scalar(
            select(Task)
            .join(Run, Run.id == Task.run_id)
            .where(Run.user_id == user_id, Run.id == run_id, Task.id == task_id)
        )
