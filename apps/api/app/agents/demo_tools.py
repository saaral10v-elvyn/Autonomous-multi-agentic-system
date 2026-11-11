"""Fake-provider-only tools for exercising each risk class."""

from collections.abc import Mapping

from app.agents.tools import ToolHandler, ToolRegistry
from app.core.config import Settings, get_settings
from app.gate.engine import RiskClass

_DEMO_SIDE_EFFECTS: dict[str, dict[str, object]] = {}


def demo_tool_registry(settings: Settings | None = None) -> ToolRegistry:
    """Build demo tools only when fake-provider mode is enabled outside production."""
    active_settings = settings or get_settings()
    registry = ToolRegistry()
    if not active_settings.fake_provider_enabled or active_settings.app_env == "production":
        return registry

    def handler_for(tool_name: str) -> ToolHandler:
        async def handle(
            payload: Mapping[str, object], idempotency_key: str
        ) -> Mapping[str, object]:
            if idempotency_key not in _DEMO_SIDE_EFFECTS:
                _DEMO_SIDE_EFFECTS[idempotency_key] = {
                    "tool": tool_name,
                    "payload": dict(payload),
                    "side_effect_count": 1,
                }
            return _DEMO_SIDE_EFFECTS[idempotency_key]

        return handle

    for name, risk in (
        ("demo_read", RiskClass.READ),
        ("demo_sandbox_write", RiskClass.SANDBOX_WRITE),
        ("demo_reversible_external", RiskClass.REVERSIBLE_EXTERNAL),
        ("demo_irreversible", RiskClass.IRREVERSIBLE),
    ):
        registry.register(
            name,
            handler_for(name),
            risk=risk,
            description=f"Test-only action with {risk.value} risk.",
        )
    return registry


def agent_tool_registry(settings: Settings | None = None) -> ToolRegistry:
    """Build the baseline workflow tool plus optional fake-provider demo tools."""
    active_settings = settings or get_settings()
    registry = ToolRegistry()

    async def ask_user(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
        return dict(payload)

    registry.register(
        "ask_user",
        ask_user,
        risk=RiskClass.READ,
        description="Present structured clarification questions to the user.",
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "object"},
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    )
    for tool in demo_tool_registry(active_settings).list():
        registry.register(
            tool.name,
            tool.handler,
            risk=tool.risk,
            description=tool.description,
            parameters=tool.parameters,
        )
    return registry
