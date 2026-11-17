"""Tenant-scoped final report generation and retrieval."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.artifact import Artifact
from app.models.project import Project
from app.models.run import Run
from app.models.task import Task
from app.repos.artifacts import ArtifactRepository


def render_report(
    run: Run,
    project: Project,
    tasks: list[Task],
    approvals: list[Approval],
    *,
    plan: dict[str, object] | None = None,
    agents: list[Agent] | None = None,
) -> str:
    """Render a complete Markdown report from persisted run records."""
    lines = [
        f"# Run report: {project.name}",
        "",
        "## Idea and requirements",
        run.idea,
        "",
        "## Decisions",
        f"- Autonomy level: `{run.autonomy_level}`",
    ]
    reporter_result = next(
        (
            task.output.get("summary")
            for task in tasks
            if task.input.get("system_role") == "reporter"
            and task.output is not None
            and isinstance(task.output.get("summary"), str)
        ),
        None,
    )
    if isinstance(reporter_result, str):
        lines.extend(["", "## Reporter summary", reporter_result])
    if isinstance(plan, dict):
        brief = plan.get("product_brief")
        stack = plan.get("tech_stack")
        if isinstance(brief, str):
            lines.extend(["", "## Product brief", brief])
        if isinstance(stack, list):
            lines.extend(["", "## Technology", ", ".join(str(item) for item in stack)])
    lines.extend(["", "## Work completed"])
    if tasks:
        lines.extend(
            f"- **{task.title}** — {task.status}: "
            f"{str((task.output or {}).get('summary', task.description))}"
            for task in tasks
        )
    else:
        lines.append("- No task records were created.")
    lines.extend(["", "## Test results"])
    test_results = [task.output for task in tasks if task.output and "test" in task.title.lower()]
    lines.extend(f"- {result.get('summary', 'No summary recorded.')}" for result in test_results)
    if not test_results:
        lines.append("- No dedicated test task was recorded.")
    lines.extend(["", "## Repository and deployment"])
    lines.append(f"- Repository: {project.repo_url or 'Not available'}")
    lines.append(f"- Live URL: {project.live_url or 'Not available'}")
    lines.extend(["", "## Approval history"])
    if approvals:
        lines.extend(
            f"- `{approval.action_type}` ({approval.risk}): {approval.status}; "
            f"decided by {approval.decided_by or 'not recorded'}"
            for approval in approvals
        )
    else:
        lines.append("- No approval requests were recorded.")
    lines.extend(
        [
            "",
            "## Cost",
            f"- Recorded run spend: ${run.spent_usd}",
            f"- Budget cap: ${run.budget_usd}",
        ]
    )
    if agents:
        lines.extend(["", "### By agent and model"])
        lines.extend(
            f"- {agent.role} ({agent.model or 'model not recorded'}): ${agent.spent_usd}"
            for agent in agents
        )
    lines.extend(
        [
            "",
            "## Known issues and next steps",
            "Review any failed or blocked tasks above, then start a follow-up run to iterate.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


async def create_final_report(
    session: AsyncSession, user_id: UUID, run_id: UUID
) -> Artifact | None:
    run = await session.scalar(select(Run).where(Run.id == run_id, Run.user_id == user_id))
    if run is None:
        return None
    project = await session.scalar(
        select(Project).where(Project.id == run.project_id, Project.user_id == user_id)
    )
    if project is None:
        return None
    tasks_result = await session.scalars(
        select(Task).where(Task.run_id == run_id).order_by(Task.created_at, Task.id)
    )
    approvals_result = await session.scalars(
        select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
    )
    agents_result = await session.scalars(
        select(Agent)
        .join(Run, Run.id == Agent.run_id)
        .where(Run.user_id == user_id, Run.id == run_id)
        .order_by(Agent.created_at, Agent.id)
    )
    from app.repos.plans import PlanRepository

    saved_plan = await PlanRepository(session).latest_for_run(user_id, run_id)
    plan = saved_plan.meta.get("plan") if saved_plan is not None else None
    markdown = render_report(
        run,
        project,
        list(tasks_result.all()),
        list(approvals_result.all()),
        plan=plan if isinstance(plan, dict) else None,
        agents=list(agents_result.all()),
    )
    return await ArtifactRepository(session).save_report(user_id, run_id, markdown)


async def get_final_report(session: AsyncSession, user_id: UUID, run_id: UUID) -> Artifact | None:
    return await ArtifactRepository(session).report_for_run(user_id, run_id)
