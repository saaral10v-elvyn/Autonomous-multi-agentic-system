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

    async def save_tester_result_for_user(
        self, user_id: UUID, run_id: UUID, result: dict[str, object]
    ) -> tuple[Artifact | None, bool]:
        artifact = await self._session.scalar(
            select(Artifact)
            .join(Run, Run.id == Artifact.run_id)
            .where(
                Run.user_id == user_id,
                Run.id == run_id,
                Artifact.kind == "test_result",
                Artifact.name == "Tester results",
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if artifact is not None:
            if artifact.meta.get("result") == result:
                return artifact, False
            artifact.meta = {"result": result}
            await self._session.flush()
            return artifact, True
        if (
            await self._session.scalar(
                select(Run.id).where(Run.id == run_id, Run.user_id == user_id)
            )
            is None
        ):
            return None, False
        artifact = Artifact(
            run_id=run_id,
            kind="test_result",
            name="Tester results",
            uri=f"sandbox://{run_id}/test-results.json",
            meta={"result": result},
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact, True

    async def report_for_run(self, user_id: UUID, run_id: UUID) -> Artifact | None:
        return await self._session.scalar(
            select(Artifact)
            .join(Run, Run.id == Artifact.run_id)
            .where(
                Run.user_id == user_id,
                Run.id == run_id,
                Artifact.kind == "report",
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )

    async def save_report(self, user_id: UUID, run_id: UUID, markdown: str) -> Artifact | None:
        run_exists = await self._session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == user_id)
        )
        if run_exists is None:
            return None
        artifact = await self.report_for_run(user_id, run_id)
        if artifact is None:
            artifact = Artifact(
                run_id=run_id,
                kind="report",
                name="Final report",
                uri=f"run://{run_id}/report.md",
                meta={"markdown": markdown},
            )
            self._session.add(artifact)
        else:
            artifact.meta = {"markdown": markdown}
        await self._session.flush()
        return artifact
