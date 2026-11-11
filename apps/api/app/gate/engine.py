"""Pure autonomy and risk policy for human approval gates."""

from collections.abc import Mapping
from enum import StrEnum


class AutonomyLevel(StrEnum):
    """Supported run autonomy levels."""

    STEP_BY_STEP = "step_by_step"
    MILESTONES = "milestones"
    AUTOPILOT = "autopilot"


class RiskClass(StrEnum):
    """Risk assigned to a registered tool."""

    READ = "read"
    SANDBOX_WRITE = "sandbox_write"
    REVERSIBLE_EXTERNAL = "reversible_external"
    IRREVERSIBLE = "irreversible"


class GateDecision(StrEnum):
    """Gate outcome consumed by the tool execution pipeline."""

    AUTO = "AUTO"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class WorkflowGate(StrEnum):
    """Non-tool milestones that still require a human gate."""

    CLARIFYING_ANSWERS = "clarifying_answers"
    PLAN_APPROVAL = "plan_approval"
    MILESTONE_COMPLETION = "milestone_completion"
    DEPLOY = "deploy"


_ALWAYS_APPROVED_GATES = {
    WorkflowGate.CLARIFYING_ANSWERS,
    WorkflowGate.DEPLOY,
}


def decide(
    level: AutonomyLevel | str,
    risk: RiskClass | str,
    context: Mapping[str, object] | None = None,
) -> GateDecision:
    """Return whether an action can proceed automatically under SPEC section 4."""
    autonomy = AutonomyLevel(level)
    action_risk = RiskClass(risk)
    gate_value = context.get("gate") if context is not None else None
    gate = WorkflowGate(gate_value) if isinstance(gate_value, str) else None

    if gate in _ALWAYS_APPROVED_GATES or action_risk is RiskClass.IRREVERSIBLE:
        return GateDecision.REQUIRE_APPROVAL
    if gate is WorkflowGate.PLAN_APPROVAL:
        return (
            GateDecision.AUTO
            if autonomy is AutonomyLevel.AUTOPILOT
            else GateDecision.REQUIRE_APPROVAL
        )
    if gate is WorkflowGate.MILESTONE_COMPLETION:
        return (
            GateDecision.AUTO
            if autonomy is AutonomyLevel.AUTOPILOT
            else GateDecision.REQUIRE_APPROVAL
        )

    if action_risk is RiskClass.READ:
        return GateDecision.AUTO
    if action_risk in {RiskClass.SANDBOX_WRITE, RiskClass.REVERSIBLE_EXTERNAL}:
        return (
            GateDecision.REQUIRE_APPROVAL
            if autonomy is AutonomyLevel.STEP_BY_STEP
            else GateDecision.AUTO
        )
    raise AssertionError("Risk matrix must handle every RiskClass member.")
