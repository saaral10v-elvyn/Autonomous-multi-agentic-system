"""Plan persistence through the tenant-scoped run artifact table."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.run import Run


class PlanRepository:
    """Read and update a plan only when its run belongs to the user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_run(self, user_id: UUID, run_id: UUID) -> Artifact | None:
        result = await self._session.execute(
            select(Artifact)
            .join(Run, Run.id == Artifact.run_id)
            .where(Run.user_id == user_id, Run.id == run_id, Artifact.kind == "plan")
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def save_for_run(
        self, user_id: UUID, run_id: UUID, plan: dict[str, object]
    ) -> Artifact | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        artifact = await self.latest_for_run(user_id, run_id)
        if artifact is None:
            artifact = Artifact(
                run_id=run_id, kind="plan", name="Project plan", uri="db://plans/current", meta={}
            )
            self._session.add(artifact)
        artifact.meta = {"plan": plan}
        await self._session.flush()
        return artifact
