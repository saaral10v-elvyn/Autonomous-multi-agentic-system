"""Unit contracts for M5 role, result, and spawn-bound validation."""

import pytest
from pydantic import ValidationError

from app.agents.registry import ROLE_TEMPLATES
from app.agents.schemas import AdHocRole, TaskResult
from app.services.spawning import validate_spawn_limits


def test_m5_role_templates_use_registered_safe_tools() -> None:
    expected = {"researcher", "architect", "coder", "tester", "reviewer", "deployer", "reporter"}
    assert expected.issubset(ROLE_TEMPLATES)
    for role in expected:
        template = ROLE_TEMPLATES[role]
        assert template.output_schema == "TaskResult"
        assert template.max_steps > 0
        assert template.max_cost_usd > 0
        assert set(template.tool_allowlist).issubset(
            {"demo_read", "demo_sandbox_write", "spawn_agent", "finish"}
        )


def test_ad_hoc_role_rejects_tools_outside_safe_subset() -> None:
    with pytest.raises(ValidationError):
        AdHocRole.model_validate(
            {
                "name": "custom_review",
                "system_prompt": "Review a proposed change.",
                "tool_allowlist": ["demo_irreversible"],
            }
        )


def test_spawn_depth_and_concurrency_limits_return_clear_errors() -> None:
    validate_spawn_limits(2, 3)
    with pytest.raises(ValueError, match="depth of 3"):
        validate_spawn_limits(3, 1)
    with pytest.raises(ValueError, match=r"concurrent agents per run \(4\)"):
        validate_spawn_limits(1, 4)


def test_task_result_is_structured_and_rejects_unknown_fields() -> None:
    result = TaskResult.model_validate(
        {
            "status": "completed",
            "summary": "The requested task is complete.",
            "artifacts": [],
            "follow_ups": [],
        }
    )
    assert result.status == "completed"
    with pytest.raises(ValidationError):
        TaskResult.model_validate(
            {"status": "completed", "summary": "Done.", "secret": "must not be returned"}
        )
