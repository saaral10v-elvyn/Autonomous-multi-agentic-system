"""Bind tool handlers to tenant, run and agent identifiers."""

from collections.abc import Mapping
from uuid import UUID

from app.agents.tools import ToolHandler
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.sandbox.factory import get_sandbox_provider
from app.sandbox.tools import SandboxToolHandlers


async def sandbox_handlers(
    user_id: UUID, run_id: UUID, agent_id: UUID, bus: EventBus
) -> Mapping[str, ToolHandler]:
    return await SandboxToolHandlers(
        get_sandbox_provider(),
        SessionFactory,
        bus,
        user_id,
        run_id,
        agent_id,
        get_settings().sandbox_max_output_bytes,
    ).handlers()
