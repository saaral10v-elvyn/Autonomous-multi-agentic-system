"""Combine run sandbox tools and owner-scoped integration handlers."""

from collections.abc import Mapping
from uuid import UUID

from app.agents.tools import ToolHandler
from app.events.bus import EventBus
from app.integrations.tools import integration_handlers
from app.sandbox.registry import sandbox_handlers


async def run_tool_handlers(
    user_id: UUID, run_id: UUID, agent_id: UUID, bus: EventBus
) -> Mapping[str, ToolHandler]:
    handlers = dict(await sandbox_handlers(user_id, run_id, agent_id, bus))
    handlers.update(integration_handlers(user_id))
    return handlers
