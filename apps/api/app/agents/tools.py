"""Typed risk-class registry for every executable agent tool."""

import builtins
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from app.gate.engine import RiskClass

ToolHandler = Callable[[Mapping[str, object], str], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A registered action and its mandatory code-enforced risk class."""

    name: str
    risk: RiskClass
    handler: ToolHandler
    description: str
    parameters: dict[str, object] = field(
        default_factory=lambda: {"type": "object", "additionalProperties": True}
    )


class ToolRegistry:
    """Registry that refuses tools without a valid risk classification."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        risk: RiskClass | str | None = None,
        description: str = "",
        parameters: Mapping[str, object] | None = None,
    ) -> ToolDefinition:
        """Register a tool only when its risk is explicitly supplied."""
        if risk is None:
            raise ValueError("Every registered tool must declare a risk class.")
        if not name.strip():
            raise ValueError("Tool name cannot be empty.")
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        definition = ToolDefinition(
            name,
            RiskClass(risk),
            handler,
            description,
            dict(parameters)
            if parameters is not None
            else {"type": "object", "additionalProperties": True},
        )
        self._tools[name] = definition
        return definition

    def get(self, name: str) -> ToolDefinition:
        """Return a registered tool or raise a useful lookup error."""
        try:
            return self._tools[name]
        except KeyError:
            raise LookupError(f"Tool '{name}' is not registered.") from None

    def list(self) -> tuple[ToolDefinition, ...]:
        """Return registered tools in registration order."""
        return tuple(self._tools.values())

    def schemas(self, names: tuple[str, ...]) -> builtins.list[dict[str, object]]:
        """Build provider-neutral function schemas for an agent's allowlist."""
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters,
                },
            }
            for definition in (self.get(name) for name in names)
        ]
