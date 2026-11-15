"""User-scoped idempotency records for tool calls."""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.run import Run
from app.models.tool_execution import ToolExecution


class ToolExecutionRepository:
    """Durable tool-call records keyed within an owned run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_key(
        self, user_id: UUID, run_id: UUID, idempotency_key: str, *, lock: bool = False
    ) -> ToolExecution | None:
        statement = (
            select(ToolExecution)
            .join(Run, Run.id == ToolExecution.run_id)
            .where(
                Run.user_id == user_id,
                Run.id == run_id,
                ToolExecution.idempotency_key == idempotency_key,
            )
        )
        if lock:
            statement = statement.with_for_update(of=ToolExecution)
        return await self._session.scalar(statement)

    async def create_for_user(
        self,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        *,
        idempotency_key: str,
        tool_name: str,
        input_payload: Mapping[str, object],
    ) -> ToolExecution | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        agent = await self._session.scalar(
            select(Agent).where(Agent.id == agent_id, Agent.run_id == run_id).with_for_update()
        )
        if agent is None:
            return None
        existing = await self.get_for_key(user_id, run_id, idempotency_key, lock=True)
        if existing is not None:
            return existing
        record = ToolExecution(
            run_id=run_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            input_payload=dict(input_payload),
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def complete_for_user(
        self,
        user_id: UUID,
        run_id: UUID,
        execution_id: UUID,
        output_payload: Mapping[str, object],
    ) -> tuple[ToolExecution | None, bool]:
        """Complete an execution only after proving run ownership."""
        record = await self._session.scalar(
            select(ToolExecution)
            .join(Run, Run.id == ToolExecution.run_id)
            .where(
                Run.user_id == user_id,
                Run.id == run_id,
                ToolExecution.id == execution_id,
            )
            .with_for_update(of=ToolExecution)
        )
        if record is None:
            return None, False
        if record.status == "completed":
            return record, True
        record.output_payload = dict(output_payload)
        record.status = "completed"
        record.completed_at = datetime.now(UTC)
        await self._session.flush()
        return record, False
