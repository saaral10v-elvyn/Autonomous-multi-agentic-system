"""User messages and run/agent controls for an active orchestration."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.bus import EventBus
from app.models.agent import Agent
from app.models.message import Message
from app.models.run import Run
from app.models.task import Task
from app.orchestrator.state import IllegalRunTransition, resume_run, transition_run
from app.repos.agents import AgentRepository
from app.repos.runs import RunRepository


class RunControlService:
    """Persist controls as events and enforce owner and current-state checks."""

    def __init__(self, session: AsyncSession, event_bus: EventBus | None = None) -> None:
        self._session = session
        self._bus = event_bus or EventBus()

    async def message(
        self, user_id: UUID, run_id: UUID, content: str, agent_id: UUID | None = None
    ) -> None:
        run = await RunRepository(self._session).get_for_user(user_id, run_id, lock=True)
        if run is None:
            raise LookupError("Run not found.")
        if run.status not in {"EXECUTING", "PAUSED", "AWAITING_APPROVAL"}:
            raise IllegalRunTransition("This run is not accepting agent messages.")
        if agent_id is not None:
            agent = await AgentRepository(self._session).get_for_id(user_id, agent_id)
            if agent is None or agent.run_id != run_id:
                raise LookupError("Agent not found.")
        self._session.add(Message(run_id=run_id, agent_id=agent_id, sender="user", content=content))
        event = await self._bus.persist(
            self._session,
            user_id,
            run_id,
            event_type="user.message",
            payload={"agent_id": str(agent_id) if agent_id else None, "content": content},
        )
        if event is None:
            raise LookupError("Run not found.")
        if agent_id is not None:
            await AgentRepository(self._session).create_step(
                user_id,
                agent_id,
                kind="user_msg",
                payload={"content": content},
            )
        await self._session.commit()
        await self._session.refresh(event)
        await self._bus.publish(event)

    async def pause(self, user_id: UUID, run_id: UUID) -> Run:
        return await transition_run(self._session, user_id, run_id, "PAUSED", event_bus=self._bus)

    async def resume(self, user_id: UUID, run_id: UUID) -> Run:
        run = await resume_run(self._session, user_id, run_id, event_bus=self._bus)
        return run

    async def cancel(self, user_id: UUID, run_id: UUID) -> Run:
        return await transition_run(
            self._session, user_id, run_id, "CANCELLED", event_bus=self._bus
        )

    async def interrupt(self, user_id: UUID, agent_id: UUID) -> Agent:
        agent = await AgentRepository(self._session).get_for_id(user_id, agent_id)
        if agent is None:
            raise LookupError("Agent not found.")
        if agent.status != "running":
            raise IllegalRunTransition("Only a running agent can be interrupted.")
        run = await self._session.scalar(
            select(Run).where(Run.id == agent.run_id, Run.user_id == user_id).with_for_update()
        )
        if run is None:
            raise LookupError("Agent not found.")
        agent.status = "interrupted"
        task = await self._session.scalar(
            select(Task).where(Task.run_id == agent.run_id, Task.agent_id == agent.id)
        )
        if task is not None and task.status == "running":
            task.status = "interrupted"
            task.output = {
                "status": "interrupted",
                "summary": "The agent was interrupted by the user.",
                "artifacts": [],
                "follow_ups": [],
            }
        event = await self._bus.persist(
            self._session,
            user_id,
            run.id,
            event_type="agent.status",
            payload={"agent_id": str(agent.id), "status": "interrupted"},
        )
        if event is None:
            raise LookupError("Run not found.")
        task_event = None
        if task is not None:
            task_event = await self._bus.persist(
                self._session,
                user_id,
                run.id,
                event_type="task.updated",
                payload={"task_id": str(task.id), "status": task.status},
            )
        await self._session.commit()
        await self._session.refresh(event)
        await self._bus.publish(event)
        if task_event is not None:
            await self._session.refresh(task_event)
            await self._bus.publish(task_event)
        return agent
