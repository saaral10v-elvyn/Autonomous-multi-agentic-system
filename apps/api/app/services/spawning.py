"""Dynamic sub-agent creation with bounded depth, concurrency and safe roles."""

from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.demo_tools import agent_tool_registry
from app.agents.registry import ROLE_TEMPLATES, RoleTemplate
from app.agents.runtime import AgentRuntime
from app.agents.schemas import AdHocRole
from app.agents.tools import ToolHandler
from app.events.bus import EventBus
from app.llm.client import LLMClient
from app.llm.types import ModelTier, Provider
from app.models.agent import Agent
from app.models.event import Event
from app.models.task import Task
from app.repos.agents import AgentRepository
from app.repos.runs import RunRepository

MAX_AGENT_DEPTH = 3
MAX_CONCURRENT_AGENTS = 4


def validate_spawn_limits(parent_depth: int, active_count: int) -> None:
    """Raise clear errors before creating a child beyond configured limits."""
    if parent_depth >= MAX_AGENT_DEPTH:
        raise ValueError("Maximum agent depth of 3 has been reached.")
    if active_count >= MAX_CONCURRENT_AGENTS:
        raise ValueError("Maximum concurrent agents per run (4) has been reached.")


class AgentSpawner:
    """Create and execute bounded child agents via the M4 tool pipeline."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        llm_client: LLMClient,
        provider: Provider,
        provider_key_id: UUID,
        event_bus: EventBus,
    ) -> None:
        self._sessions = sessions
        self._llm = llm_client
        self._provider = provider
        self._provider_key_id = provider_key_id
        self._bus = event_bus
        self._runtime = AgentRuntime(sessions, event_bus)

    def handler(self, user_id: UUID, run_id: UUID, parent_id: UUID) -> ToolHandler:
        """Return a registered handler scoped to one parent agent and run."""

        async def spawn(payload: Mapping[str, object], call_key: str) -> Mapping[str, object]:
            return await self.spawn(user_id, run_id, parent_id, payload, call_key)

        return spawn

    async def spawn(
        self,
        user_id: UUID,
        run_id: UUID,
        parent_id: UUID,
        payload: Mapping[str, object],
        call_key: str,
    ) -> Mapping[str, object]:
        """Validate role and bounds, persist a child, and return its TaskResult."""
        if payload.get("parent_id") != str(parent_id):
            raise ValueError("parent_id must match the calling agent.")
        role = payload.get("role")
        task_text = payload.get("task")
        if not isinstance(role, str) or not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("role and a non-empty task are required.")
        raw_refs = payload.get("context_refs", [])
        if not isinstance(raw_refs, list) or any(not isinstance(item, str) for item in raw_refs):
            raise ValueError("context_refs must be a list of strings.")
        if len(raw_refs) > 50:
            raise ValueError("At most 50 context references are allowed.")
        if role in ROLE_TEMPLATES:
            template = ROLE_TEMPLATES[role]
        else:
            raw_role = payload.get("ad_hoc_role")
            try:
                ad_hoc = AdHocRole.model_validate(raw_role)
            except (ValueError, TypeError):
                raise ValueError("Unknown roles require a valid safe ad_hoc_role schema.") from None
            if ad_hoc.name != role:
                raise ValueError("ad_hoc_role name must match role.")
            template = RoleTemplate(
                name=ad_hoc.name,
                system_prompt=ad_hoc.system_prompt,
                model_tier=ModelTier(ad_hoc.model_tier),
                tool_allowlist=tuple(ad_hoc.tool_allowlist) + ("spawn_agent", "finish"),
                output_schema="TaskResult",
                max_steps=ad_hoc.max_steps,
                max_cost_usd=ad_hoc.max_cost_usd,
            )
        child_config = {
            "name": template.name,
            "system_prompt": template.system_prompt,
            "model_tier": template.model_tier.value,
            "tool_allowlist": list(template.tool_allowlist),
            "output_schema": template.output_schema,
            "max_steps": template.max_steps,
            "max_cost_usd": template.max_cost_usd,
        }
        external_id = f"child-{call_key[:48]}"
        async with self._sessions() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            parent = await AgentRepository(session).get_for_id(user_id, parent_id)
            if run is None or parent is None or parent.run_id != run_id:
                raise LookupError("Parent agent not found.")
            if run.status != "EXECUTING":
                raise ValueError("Run is not accepting spawned agents.")
            depth = await self._depth(session, user_id, parent)
            active_count = await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(
                    Agent.run_id == run_id,
                    Agent.status.in_(("created", "running")),
                )
            )
            validate_spawn_limits(depth, int(active_count or 0))
            task = await session.scalar(
                select(Task).where(Task.run_id == run_id, Task.external_id == external_id)
            )
            parent_task_id = await session.scalar(
                select(Task.id).where(Task.run_id == run_id, Task.agent_id == parent_id)
            )
            task_id = task.id if task is not None else uuid4()
            agent = await AgentRepository(session).get_or_create_for_task(
                user_id,
                run_id,
                template.name,
                task_id,
                child_config,
                parent_id=parent_id,
            )
            if agent is None:
                raise LookupError("Run not found.")
            if task is None:
                task = Task(
                    id=task_id,
                    run_id=run_id,
                    agent_id=agent.id,
                    parent_task_id=parent_task_id,
                    external_id=external_id,
                    title=f"{template.name} child task",
                    description=task_text.strip(),
                    status="running",
                    depends_on=[],
                    input={"context_refs": raw_refs, "idempotency_key": call_key},
                )
                session.add(task)
            else:
                task.agent_id = agent.id
                task.status = "running"
            agent.status = "running"
            events = [
                await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.spawned",
                    payload={
                        "agent_id": str(agent.id),
                        "parent_id": str(parent_id),
                        "role": template.name,
                        "task_id": external_id,
                        "depth": depth + 1,
                    },
                ),
                await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="agent.status",
                    payload={"agent_id": str(agent.id), "status": "running"},
                ),
                await self._bus.persist(
                    session,
                    user_id,
                    run_id,
                    event_type="task.updated",
                    payload={
                        "task_id": str(task.id),
                        "agent_id": str(agent.id),
                        "parent_task_id": str(parent_task_id) if parent_task_id else None,
                        "status": "running",
                    },
                ),
            ]
            await session.commit()
            committed = [event for event in events if isinstance(event, Event)]
            for event in committed:
                await session.refresh(event)
        for event in committed:
            await self._bus.publish(event)
        registry = agent_tool_registry(spawn_handler=self.handler(user_id, run_id, agent.id))
        child_prompt = f"Task: {task_text.strip()}\nContext references: {raw_refs}"
        result = await self._runtime.run_task(
            user_id,
            run_id,
            agent.id,
            self._provider,
            self._provider_key_id,
            template,
            child_prompt,
            self._llm,
            registry,
        )
        async with self._sessions() as session:
            from app.repos.tasks import TaskRepository

            is_waiting = result.status == "waiting_approval"
            saved_status = "running" if is_waiting else result.status
            saved_task = await TaskRepository(session).finish(
                user_id,
                run_id,
                task.id,
                saved_status,
                result.model_dump(mode="json"),
            )
            saved_agent = await AgentRepository(session).get_for_id(user_id, agent.id)
            if saved_task is None or saved_agent is None:
                raise LookupError("Child agent result could not be saved.")
            saved_agent.status = (
                "running"
                if is_waiting
                else "completed"
                if result.status == "completed"
                else "interrupted"
                if result.status == "interrupted"
                else "failed"
            )
            task_updated_event = await self._bus.persist(
                session,
                user_id,
                run_id,
                event_type="task.updated",
                payload={"task_id": str(saved_task.id), "result": result.model_dump(mode="json")},
            )
            status_event = await self._bus.persist(
                session,
                user_id,
                run_id,
                event_type="agent.status",
                payload={"agent_id": str(saved_agent.id), "status": saved_agent.status},
            )
            await session.commit()
            for pending in (task_updated_event, status_event):
                if isinstance(pending, Event):
                    await session.refresh(pending)
        for pending in (task_updated_event, status_event):
            if isinstance(pending, Event):
                await self._bus.publish(pending)
        return {**result.model_dump(mode="json"), "child_task_id": str(task.id)}

    async def _depth(self, session: AsyncSession, user_id: UUID, parent: Agent) -> int:
        depth = 0
        current = parent
        while current.parent_id is not None:
            ancestor = await AgentRepository(session).get_for_id(user_id, current.parent_id)
            if ancestor is None:
                raise LookupError("Parent agent not found.")
            depth += 1
            current = ancestor
        return depth
