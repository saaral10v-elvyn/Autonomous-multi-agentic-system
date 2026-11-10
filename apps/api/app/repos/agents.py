"""Tenant-scoped role instance and durable-step repository."""

from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_step import AgentStep
from app.models.run import Run


class AgentRepository:
    """Agent and step reads/writes require the owning user ID."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_for_role(
        self, user_id: UUID, run_id: UUID, role: str, config: Mapping[str, object]
    ) -> Agent | None:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            return None
        result = await self._session.execute(
            select(Agent).where(Agent.run_id == run_id, Agent.role == role).with_for_update()
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            agent = Agent(run_id=run_id, role=role, config=dict(config), status="running")
            self._session.add(agent)
            await self._session.flush()
        return agent

    async def list_steps_for_agent(self, user_id: UUID, agent_id: UUID) -> list[AgentStep]:
        result = await self._session.execute(
            select(AgentStep)
            .join(Agent, Agent.id == AgentStep.agent_id)
            .join(Run, Run.id == Agent.run_id)
            .where(Run.user_id == user_id, Agent.id == agent_id)
            .order_by(AgentStep.idx)
        )
        return list(result.scalars().all())

    async def create_step(
        self,
        user_id: UUID,
        agent_id: UUID,
        *,
        kind: str,
        payload: dict[str, object],
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: Decimal = Decimal("0"),
    ) -> AgentStep | None:
        agent = await self._session.scalar(
            select(Agent)
            .join(Run, Run.id == Agent.run_id)
            .where(Run.user_id == user_id, Agent.id == agent_id)
            .with_for_update()
        )
        if agent is None:
            return None
        highest = await self._session.scalar(
            select(func.coalesce(func.max(AgentStep.idx), 0)).where(AgentStep.agent_id == agent_id)
        )
        step = AgentStep(
            agent_id=agent_id,
            idx=int(highest or 0) + 1,
            kind=kind,
            payload=payload,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        self._session.add(step)
        await self._session.flush()
        return step
