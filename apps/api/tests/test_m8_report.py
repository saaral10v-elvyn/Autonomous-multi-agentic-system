"""M8 report rendering contracts from persisted run results."""

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SessionFactory
from app.models.project import Project
from app.models.run import Run
from app.models.task import Task
from app.services.reports import create_final_report, render_report
from app.services.runs import RunService
from tests.conftest import WEB_ORIGIN


def test_report_covers_delivery_cost_approvals_and_test_outcomes() -> None:
    user_id = uuid4()
    project = Project(
        id=uuid4(),
        user_id=user_id,
        name="Notes app",
        repo_url="https://github.com/example/notes",
        live_url="https://notes.example.vercel.app",
    )
    run = Run(
        id=uuid4(),
        project_id=project.id,
        user_id=user_id,
        idea="A simple notes app",
        autonomy_level="milestones",
        status="COMPLETED",
        budget_usd=Decimal("20.00"),
        spent_usd=Decimal("1.25"),
    )
    task = Task(
        id=uuid4(),
        run_id=run.id,
        external_id="test",
        title="Run tests",
        description="Run the project tests.",
        status="completed",
        depends_on=[],
        input={},
        output={"summary": "12 tests passed."},
    )

    markdown = render_report(run, project, [task], [], plan={"product_brief": "Private notes"})

    assert "A simple notes app" in markdown
    assert "12 tests passed." in markdown
    assert "https://github.com/example/notes" in markdown
    assert "https://notes.example.vercel.app" in markdown
    assert "$1.25" in markdown
    assert "Private notes" in markdown


def test_project_iteration_and_report_are_owner_scoped(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    first_email, second_email = test_emails(), test_emails()
    headers = {"Origin": WEB_ORIGIN}
    first_signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": first_email, "password": "m8-test-password"},
        headers=headers,
    )
    assert first_signup.status_code == 201
    owner_id = UUID(first_signup.json()["id"])

    async def create_project_runs() -> tuple[UUID, UUID, UUID]:
        async with SessionFactory() as session:
            service = RunService(session)
            first = await service.create(
                owner_id,
                idea="A reading list",
                autonomy_level="milestones",
                budget_usd=Decimal("12.00"),
            )
            project = await session.get(Project, first.project_id)
            assert project is not None
            project.repo_url = "https://github.com/example/reading-list"
            session.add(
                Task(
                    run_id=first.id,
                    external_id="build-list",
                    title="Build reading list",
                    description="Build the list.",
                    status="completed",
                    depends_on=[],
                    input={},
                    output={"summary": "Reading list app built."},
                )
            )
            followup = await service.create(
                first.user_id,
                idea="Add keyboard shortcuts",
                autonomy_level="milestones",
                budget_usd=Decimal("12.00"),
                project_id=first.project_id,
            )
            session.add(
                Task(
                    run_id=followup.id,
                    external_id="followup-task",
                    title="Add keyboard shortcuts",
                    description="Implement shortcuts.",
                    status="failed",
                    depends_on=[],
                    input={},
                    output={"summary": "Needs retry."},
                )
            )
            await create_final_report(session, first.user_id, followup.id)
            await session.commit()
            retry_task_id = await session.scalar(select(Task.id).where(Task.run_id == followup.id))
            assert retry_task_id is not None
            return first.project_id, followup.id, retry_task_id

    project_id, followup_id, retry_task_id = api_client.portal.call(create_project_runs)
    project_detail = api_client.get(f"/api/v1/projects/{project_id}")
    assert project_detail.status_code == 200
    assert len(project_detail.json()["runs"]) == 2
    assert project_detail.json()["runs"][0]["project_id"] == str(project_id)
    report = api_client.get(f"/api/v1/runs/{followup_id}/report")
    assert report.status_code == 200
    assert "Add keyboard shortcuts" in report.json()["markdown"]

    async def followup_idea() -> str:
        async with SessionFactory() as session:
            run = await session.get(Run, followup_id)
            assert run is not None
            return run.idea

    context = api_client.portal.call(followup_idea)
    assert "Previous run idea: A reading list" in context
    assert "Reading list app built." in context
    assert "https://github.com/example/reading-list" in context

    second_signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": second_email, "password": "m8-test-password"},
        headers=headers,
    )
    assert second_signup.status_code == 201
    assert api_client.get(f"/api/v1/projects/{project_id}").status_code == 404
    assert api_client.get(f"/api/v1/runs/{followup_id}/report").status_code == 404
    assert (
        api_client.post(
            f"/api/v1/runs/{followup_id}/tasks/{retry_task_id}/retry", headers=headers
        ).status_code
        == 404
    )
    denied_iteration = api_client.post(
        "/api/v1/runs",
        json={
            "idea": "Attempt cross-tenant iteration",
            "autonomy_level": "milestones",
            "budget_usd": "1.00",
            "project_id": str(project_id),
        },
        headers=headers,
    )
    assert denied_iteration.status_code == 404
