"""Tenant-scoped clarification-question repository."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.question import Question
from app.models.run import Run


class QuestionRepository:
    """All question access requires the owner of the containing run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_open_for_run(self, user_id: UUID, run_id: UUID) -> Question | None:
        result = await self._session.execute(
            select(Question)
            .join(Run, Run.id == Question.run_id)
            .where(Run.user_id == user_id, Run.id == run_id, Question.status == "open")
            .order_by(Question.created_at.desc(), Question.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_for_run(
        self, user_id: UUID, run_id: UUID, *, question_schema: dict[str, object]
    ) -> Question | None:
        owner = await self._session.scalar(
            select(Run.id).where(Run.id == run_id, Run.user_id == user_id)
        )
        if owner is None:
            return None
        question = Question(run_id=run_id, question_schema=question_schema, status="open")
        self._session.add(question)
        await self._session.flush()
        return question
