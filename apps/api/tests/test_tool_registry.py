"""Risk assignment and fake-tool registration checks."""

import pytest

from app.agents.demo_tools import demo_tool_registry
from app.agents.tools import ToolRegistry
from app.core.config import Settings
from app.gate.engine import RiskClass


async def _handler(payload: object, key: str) -> dict[str, object]:
    return {"payload": str(payload), "key": key}


def test_registry_rejects_missing_and_invalid_risk_class() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="must declare a risk class"):
        registry.register("missing-risk", _handler)
    with pytest.raises(ValueError):
        registry.register("invalid-risk", _handler, risk="unknown")


def test_registry_requires_risk_for_registration() -> None:
    registry = ToolRegistry()
    definition = registry.register("safe-read", _handler, risk=RiskClass.READ)
    assert definition.risk is RiskClass.READ
    assert registry.get("safe-read") is definition


def test_demo_tools_exist_only_with_fake_provider_outside_production() -> None:
    disabled = demo_tool_registry(
        Settings(_env_file=None, app_env="test", fake_provider_enabled=False)
    )
    enabled = demo_tool_registry(
        Settings(_env_file=None, app_env="test", fake_provider_enabled=True)
    )
    production_fake = Settings.model_construct(app_env="production", fake_provider_enabled=True)
    assert disabled.list() == ()
    assert {tool.risk for tool in enabled.list()} == set(RiskClass)
    assert len(enabled.list()) == len(RiskClass)
    assert demo_tool_registry(production_fake).list() == ()
