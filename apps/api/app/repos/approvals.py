"""User-scoped approval persistence."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.run import Run


class ApprovalRepository:
    """Approval reads and writes are scoped through the owning run."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_user(
        self,
        user_id: UUID,
        run_id: UUID,
        *,
        agent_id: UUID | None,
        action_type: str,
        risk: str,
        payload: Mapping[str, object],
        reason: str,
        estimated_cost_usd: Decimal | None = None,
        rollback_info: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> Approval | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        if agent_id is not None:
            agent = await self._session.scalar(
                select(Agent).where(Agent.id == agent_id, Agent.run_id == run_id).with_for_update()
            )
            if agent is None:
                return None
        approval = Approval(
            run_id=run_id,
            agent_id=agent_id,
            action_type=action_type,
            risk=risk,
            payload=dict(payload),
            reason=reason,
            estimated_cost_usd=estimated_cost_usd,
            rollback_info=dict(rollback_info) if rollback_info is not None else None,
            idempotency_key=idempotency_key,
        )
        self._session.add(approval)
        await self._session.flush()
        return approval

    async def list_for_run(self, user_id: UUID, run_id: UUID) -> list[Approval]:
        result = await self._session.execute(
            select(Approval)
            .join(Run, Run.id == Approval.run_id)
            .where(Run.user_id == user_id, Run.id == run_id)
            .order_by(Approval.created_at, Approval.id)
        )
        return list(result.scalars().all())

    async def get_for_user(
        self, user_id: UUID, approval_id: UUID, *, lock: bool = False
    ) -> Approval | None:
        statement = (
            select(Approval)
            .join(Run, Run.id == Approval.run_id)
            .where(Run.user_id == user_id, Approval.id == approval_id)
        )
        if lock:
            statement = statement.with_for_update(of=Approval).execution_options(
                populate_existing=True
            )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def list_expired_pending(
        self, cutoff: datetime, limit: int = 100
    ) -> list[tuple[Approval, UUID]]:
        """Find pending approvals older than the configured TTL."""
        result = await self._session.execute(
            select(Approval, Run.user_id)
            .join(Run, Run.id == Approval.run_id)
            .where(Approval.status == "pending", Approval.created_at <= cutoff)
            .order_by(Approval.created_at, Approval.id)
            .limit(limit)
        )
        return [(approval, user_id) for approval, user_id in result.all()]

    async def get_for_idempotency_key(
        self, user_id: UUID, run_id: UUID, idempotency_key: str, *, lock: bool = False
    ) -> Approval | None:
        statement = (
            select(Approval)
            .join(Run, Run.id == Approval.run_id)
            .where(
                Run.user_id == user_id,
                Run.id == run_id,
                Approval.idempotency_key == idempotency_key,
            )
        )
        if lock:
            statement = statement.with_for_update(of=Approval)
        return await self._session.scalar(statement)
