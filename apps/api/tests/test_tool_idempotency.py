"""Pure guards for irreversible tool replay across retries and worker crashes."""

from uuid import uuid4

import pytest

from app.models.tool_execution import ToolExecution
from app.orchestrator.state import IllegalRunTransition
from app.services.tools import _validate_replay, irreversible_action_replay


def _execution(
    *, status: str, payload: dict[str, object], output: dict[str, object] | None
) -> ToolExecution:
    return ToolExecution(
        id=uuid4(),
        run_id=uuid4(),
        agent_id=uuid4(),
        idempotency_key=uuid4().hex,
        tool_name="vercel_deploy",
        input_payload=payload,
        output_payload=output,
        status=status,
    )


def test_completed_irreversible_action_reuses_result_for_same_target() -> None:
    payload = {"project_id": "project-1", "branch": "main"}
    prior = _execution(status="completed", payload=payload, output={"deployment_id": "d1"})

    replay = irreversible_action_replay([prior], "vercel_deploy", payload)

    assert replay is not None
    assert replay.status == "completed"
    assert replay.output == {"deployment_id": "d1"}


def test_started_irreversible_action_fails_closed_after_unknown_outcome() -> None:
    payload = {"project_id": "project-1", "branch": "main"}
    prior = _execution(status="pending", payload=payload, output={"_started": True})

    replay = irreversible_action_replay([prior], "vercel_deploy", payload)

    assert replay is not None
    assert replay.status == "uncertain"
    assert replay.output == {"status": "outcome_unknown", "tool_name": "vercel_deploy"}


def test_completed_irreversible_action_rejects_a_different_target() -> None:
    prior = _execution(
        status="completed",
        payload={"project_id": "project-1", "branch": "main"},
        output={"deployment_id": "d1"},
    )

    with pytest.raises(IllegalRunTransition, match="different recorded target"):
        irreversible_action_replay(
            [prior],
            "vercel_deploy",
            {"project_id": "project-2", "branch": "main"},
        )


def test_rejected_action_does_not_block_a_new_attempt() -> None:
    prior = _execution(
        status="completed",
        payload={"project_id": "project-1"},
        output={"status": "rejected"},
    )

    assert irreversible_action_replay([prior], "vercel_deploy", {"project_id": "project-2"}) is None


def test_edited_approval_keeps_original_call_replayable_and_records_executed_target() -> None:
    original = {"project_id": "project-1", "branch": "main"}
    approved = {"project_id": "project-2", "branch": "main"}
    prior = _execution(
        status="completed",
        payload={"_request_payload": original, "_executed_payload": approved},
        output={"deployment_id": "d2"},
    )

    _validate_replay(prior, "vercel_deploy", original)
    assert irreversible_action_replay([prior], "vercel_deploy", approved) is not None
    with pytest.raises(IllegalRunTransition, match="different recorded target"):
        irreversible_action_replay([prior], "vercel_deploy", original)
