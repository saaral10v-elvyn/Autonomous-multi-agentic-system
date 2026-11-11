"""Table-driven legal and illegal run state-machine transitions."""

import pytest

from app.orchestrator.state import IllegalRunTransition, validate_transition

LEGAL = {
    "CREATED": {"CLARIFYING", "PAUSED", "FAILED", "CANCELLED"},
    "CLARIFYING": {"AWAITING_ANSWERS", "PAUSED", "FAILED", "CANCELLED"},
    "AWAITING_ANSWERS": {"PLANNING", "PAUSED", "FAILED", "CANCELLED"},
    "PLANNING": {"AWAITING_PLAN_APPROVAL", "EXECUTING", "PAUSED", "FAILED", "CANCELLED"},
    "AWAITING_PLAN_APPROVAL": {"EXECUTING", "PAUSED", "FAILED", "CANCELLED"},
    "EXECUTING": {"AWAITING_APPROVAL", "PAUSED", "FAILED", "CANCELLED"},
    "AWAITING_APPROVAL": {"EXECUTING", "PAUSED", "FAILED", "CANCELLED"},
    "PAUSED": {
        "CLARIFYING",
        "AWAITING_ANSWERS",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "EXECUTING",
        "AWAITING_APPROVAL",
        "FAILED",
        "CANCELLED",
    },
    "FAILED": set(),
    "CANCELLED": set(),
}


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in LEGAL.items() for target in targets],
)
def test_every_legal_transition_passes(current: str, target: str) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize("current", LEGAL)
@pytest.mark.parametrize("target", LEGAL)
def test_every_other_transition_raises(current: str, target: str) -> None:
    if target in LEGAL[current]:
        return
    with pytest.raises(IllegalRunTransition):
        validate_transition(current, target)
