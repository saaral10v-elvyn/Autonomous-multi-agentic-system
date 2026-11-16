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
    max_cost_usd: float = 0.5


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
        max_cost_usd=0.25,
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
        max_cost_usd=1.0,
    ),
}

for _role, _purpose, _tier, _allowlist in (
    (
        "researcher",
        "Gather and summarize relevant information.",
        ModelTier.FAST,
        ("demo_read", "spawn_agent", "finish"),
    ),
    (
        "architect",
        "Describe a concrete module and file design.",
        ModelTier.BALANCED,
        ("read_file", "write_file", "list_dir", "finish"),
    ),
    (
        "coder",
        "Implement the assigned task safely.",
        ModelTier.BALANCED,
        (
            "sandbox_exec",
            "read_file",
            "write_file",
            "list_dir",
            "git_init",
            "git_status",
            "git_diff",
            "git_add",
            "git_commit",
            "git_branch",
            "git_checkout",
            "request_review",
            "spawn_agent",
            "finish",
        ),
    ),
    (
        "tester",
        "Run tests via sandbox_exec and report structured results.",
        ModelTier.FAST,
        ("sandbox_exec", "read_file", "tester_report", "finish"),
    ),
    (
        "reviewer",
        "Review the sandbox diff and report using review_complete.",
        ModelTier.STRONG,
        ("read_file", "git_diff", "review_complete", "finish"),
    ),
    ("deployer", "Prepare deployment readiness.", ModelTier.BALANCED, ("finish",)),
    ("reporter", "Summarize completed work.", ModelTier.FAST, ("finish",)),
):
    ROLE_TEMPLATES[_role] = RoleTemplate(
        name=_role,
        system_prompt=f"{_purpose} Use only registered tools and return structured JSON.",
        model_tier=_tier,
        tool_allowlist=_allowlist,
        output_schema="TaskResult",
        max_steps=5,
        max_cost_usd=0.5,
    )
