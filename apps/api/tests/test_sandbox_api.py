"""Cross-tenant contracts for the sandbox file endpoints."""

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.repos.runs import RunRepository
from app.sandbox.base import SandboxSpec
from app.sandbox.fake_provider import FakeSandboxProvider
from app.services.orchestration import _mark_completed
from tests.conftest import WEB_ORIGIN


def test_sandbox_file_endpoints_hide_other_tenant_runs(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    first = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-api-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert first.status_code == 201
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Private sandbox", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    second = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-api-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert second.status_code == 201
    assert api_client.get(f"/api/v1/runs/{run_id}/files").status_code == 404
    assert (
        api_client.get(
            f"/api/v1/runs/{run_id}/files/content?path=/workspace/secret.txt"
        ).status_code
        == 404
    )
    assert api_client.get(f"/api/v1/runs/{run_id}/diff").status_code == 404


def test_run_file_tree_classifies_empty_small_large_files_and_directories(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed_up = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-api-test-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signed_up.status_code == 201
    run_response = api_client.post(
        "/api/v1/runs",
        json={"idea": "Inspect sandbox files", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert run_response.status_code == 201
    run_id = UUID(run_response.json()["id"])
    provider = FakeSandboxProvider()

    async def prepare_files() -> None:
        handle = await provider.create(str(run_id), "agent", SandboxSpec())
        await provider.write_file(handle, "empty.txt", "", 2_000_000)
        await provider.write_file(handle, "small.txt", "ok", 2_000_000)
        await provider.write_file(handle, "large.txt", "x" * 100_000, 2_000_000)
        await provider.write_file(handle, "nested/data.txt", "nested", 2_000_000)
        provider.make_dir(handle, "empty-directory")

    api_client.portal.call(prepare_files)
    monkeypatch.setattr("app.api.v1.runs.get_sandbox_provider", lambda: provider)

    response = api_client.get(f"/api/v1/runs/{run_id}/files")
    assert response.status_code == 200
    entries = {
        entry["path"].removeprefix("/workspace/"): entry["type"]
        for entry in response.json()["files"]
    }
    assert entries == {
        "empty.txt": "file",
        "small.txt": "file",
        "large.txt": "file",
        "nested": "directory",
        "nested/data.txt": "file",
        "empty-directory": "directory",
    }

    async def fake_diff(_provider: object, _handle: object, _limit: int) -> str:
        return "diff --git b/new.txt"

    monkeypatch.setattr("app.api.v1.runs.sandbox_diff", fake_diff)
    assert api_client.get(f"/api/v1/runs/{run_id}/diff").json() == {"diff": "diff --git b/new.txt"}
    unsafe = api_client.get(f"/api/v1/runs/{run_id}/files/content?path=..%2F..%2Fetc%2Fpasswd")
    assert unsafe.status_code == 404


def test_cancel_destroys_the_run_sandbox(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    async def no_publish(self: EventBus, event: object) -> None:
        del self, event

    monkeypatch.setattr("app.api.v1.runs.enqueue_advance", no_enqueue)
    monkeypatch.setattr(EventBus, "publish", no_publish)
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-cleanup-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Cancel cleanup", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])
    provider = FakeSandboxProvider()
    api_client.portal.call(provider.create, str(run_id), "agent", SandboxSpec())
    monkeypatch.setattr("app.sandbox.lifecycle.get_sandbox_provider", lambda: provider)

    response = api_client.post(f"/api/v1/runs/{run_id}/cancel", headers={"Origin": WEB_ORIGIN})
    assert response.status_code == 200
    assert api_client.portal.call(provider.list_active) == []


def test_completion_destroys_the_run_sandbox(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    async def no_publish(self: EventBus, event: object) -> None:
        del self, event

    monkeypatch.setattr("app.api.v1.runs.enqueue_advance", no_enqueue)
    monkeypatch.setattr(EventBus, "publish", no_publish)
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-cleanup-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    user_id = UUID(api_client.get("/api/v1/me").json()["id"])
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Completion cleanup", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])
    provider = FakeSandboxProvider()
    api_client.portal.call(provider.create, str(run_id), "agent", SandboxSpec())
    monkeypatch.setattr("app.sandbox.lifecycle.get_sandbox_provider", lambda: provider)

    async def mark_executing() -> None:
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            assert run is not None
            run.status = "EXECUTING"
            await session.commit()

    api_client.portal.call(mark_executing)
    api_client.portal.call(_mark_completed, user_id, run_id)
    assert api_client.portal.call(provider.list_active) == []
