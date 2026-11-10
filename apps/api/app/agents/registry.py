"""Role template registry used by the bounded M3 agent loop."""

from dataclasses import dataclass

from app.llm.types import ModelTier


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """Configuration for one agent role."""

    name: str
    system_prompt: str
    model_tier: ModelTier
    tool_allowlist: tuple[str, ...]
    output_schema: str
    max_steps: int


ROLE_TEMPLATES = {
    "clarifier": RoleTemplate(
        name="clarifier",
        system_prompt=(
            "Ask only the most important clarifying questions for the user's idea. "
            "Return one batched question set with the ask_user tool. Do not include secrets."
        ),
        model_tier=ModelTier.FAST,
        tool_allowlist=("ask_user",),
        output_schema="QuestionSet",
        max_steps=3,
    ),
    "planner": RoleTemplate(
        name="planner",
        system_prompt=(
            "Create a concise product brief, tech stack, milestones, and a dependency graph. "
            "Return only JSON matching the supplied schema."
        ),
        model_tier=ModelTier.BALANCED,
        tool_allowlist=(),
        output_schema="PlanDocument",
        max_steps=3,
    ),
}
