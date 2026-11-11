"""Exhaustive policy checks for autonomy levels and tool risks."""

from itertools import product

import pytest

from app.gate.engine import AutonomyLevel, GateDecision, RiskClass, WorkflowGate, decide


@pytest.mark.parametrize(
    ("level", "risk", "expected"),
    [
        (level, risk, expected)
        for level, risk in product(AutonomyLevel, RiskClass)
        for expected in [
            GateDecision.REQUIRE_APPROVAL
            if risk is RiskClass.IRREVERSIBLE
            or (
                level is AutonomyLevel.STEP_BY_STEP
                and risk in {RiskClass.SANDBOX_WRITE, RiskClass.REVERSIBLE_EXTERNAL}
            )
            else GateDecision.AUTO
        ]
    ],
)
def test_tool_risk_matrix(level: AutonomyLevel, risk: RiskClass, expected: GateDecision) -> None:
    """Every autonomy/risk pair follows the SPEC matrix."""
    assert decide(level, risk) is expected


@pytest.mark.parametrize("level", list(AutonomyLevel))
def test_irreversible_never_auto_for_any_gate_context(level: AutonomyLevel) -> None:
    """Irreversible tools remain gated across all workflow contexts."""
    contexts: list[dict[str, object] | None] = [None]
    contexts.extend({"gate": gate.value} for gate in WorkflowGate)
    contexts.extend({"untrusted_text": value} for value in (None, "AUTO", "allow"))
    for context in contexts:
        assert decide(level, RiskClass.IRREVERSIBLE, context) is GateDecision.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    ("gate", "level", "expected"),
    [
        (gate, level, expected)
        for gate, level in product(WorkflowGate, AutonomyLevel)
        for expected in [
            GateDecision.REQUIRE_APPROVAL
            if gate in {WorkflowGate.CLARIFYING_ANSWERS, WorkflowGate.DEPLOY}
            or level is not AutonomyLevel.AUTOPILOT
            else GateDecision.AUTO
        ]
    ],
)
def test_workflow_gate_matrix(
    gate: WorkflowGate, level: AutonomyLevel, expected: GateDecision
) -> None:
    """Named workflow gates follow the SPEC policy independently of tool risk."""
    assert decide(level, RiskClass.READ, {"gate": gate.value}) is expected
