"""M7 integration adapter contracts and secret/tenant boundaries."""

import asyncio
from collections.abc import Callable, Mapping
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.demo_tools import agent_tool_registry
from app.agents.tools import ToolRegistry
from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.db import SessionFactory
from app.gate.engine import AutonomyLevel, GateDecision, RiskClass, decide
from app.integrations.github import GitHubAdapter
from app.integrations.placeholders import DeferredHostingAdapter
from app.integrations.templates import load_template
from app.integrations.tools import integration_handlers, register_deployer_tools
from app.integrations.vercel import VercelAdapter
from app.models.integration import Integration
from tests.conftest import TEST_MASTER_KEY, WEB_ORIGIN


def test_token_connect_is_encrypted_write_only_and_owner_scoped(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "integration-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    first_id = UUID(api_client.get("/api/v1/me").json()["id"])
    token = "github-token-never-return-this-1234"
    response = api_client.post(
        "/api/v1/integrations/github/connect",
        json={"account_label": "Work account", "token": token},
        headers={"Origin": WEB_ORIGIN},
    )
    assert response.status_code == 201
    assert token not in response.text
    integration_id = UUID(response.json()["id"])

    async def verify() -> None:
        async with SessionFactory() as session:
            row = await session.scalar(select(Integration).where(Integration.id == integration_id))
            assert row is not None
            assert row.scopes == []
            assert token.encode() not in row.token_ciphertext
            assert (
                decrypt_secret(
                    EncryptedSecret(row.token_ciphertext, row.token_nonce),
                    user_id=first_id,
                    record_id=row.id,
                    master_key=TEST_MASTER_KEY,
                )
                == token
            )

    api_client.portal.call(verify)

    async def fake_execute(
        self: GitHubAdapter, action: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        del self, action, payload
        return {"ok": True}

    monkeypatch.setattr(GitHubAdapter, "execute", fake_execute)

    async def verify_model_boundary() -> None:
        handlers = integration_handlers(first_id)
        listing = await handlers["integration_list"]({}, "list-call")
        result = await handlers["github_create_repo"](
            {"integration_id": str(integration_id), "name": "app"}, "create-call"
        )
        assert token not in str(listing)
        assert token not in str(result)

    api_client.portal.call(verify_model_boundary)
    second = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "integration-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert second.status_code == 201
    assert api_client.get("/api/v1/integrations").json() == []
    assert (
        api_client.delete(
            f"/api/v1/integrations/{integration_id}", headers={"Origin": WEB_ORIGIN}
        ).status_code
        == 404
    )


def test_integrations_list_and_remove_are_owner_scoped(
    api_client: TestClient, test_emails: Callable[[], str]
) -> None:
    api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "integration-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    response = api_client.post(
        "/api/v1/integrations/vercel/connect",
        json={"account_label": "Personal", "token": "vercel-token-secret-123"},
        headers={"Origin": WEB_ORIGIN},
    )
    integration_id = response.json()["id"]
    assert response.status_code == 201
    listed = api_client.get("/api/v1/integrations")
    assert listed.status_code == 200 and len(listed.json()) == 1
    deleted = api_client.delete(
        f"/api/v1/integrations/{integration_id}", headers={"Origin": WEB_ORIGIN}
    )
    assert deleted.status_code == 204
    assert api_client.get("/api/v1/integrations").json() == []


def test_adapter_contracts_use_injected_fixture_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations import github, vercel

    calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def fixture(
        url: str, token: str, method: str, payload: dict[str, object] | None = None
    ) -> object:
        calls.append((url, method, payload))
        assert token == "secret-token"
        if url.endswith("/user/repos"):
            return {"full_name": "user/app", "html_url": "https://github.com/user/app"}
        if url.endswith("/v10/projects"):
            return {"id": "project-id", "name": "app"}
        if url.endswith("/v13/deployments"):
            return {"id": "deployment-id", "url": "app.vercel.app", "readyState": "QUEUED"}
        if url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "base-sha"}}
        if url.endswith("/git/refs"):
            return {"ref": "refs/heads/forge/app", "object": {"sha": "feature-sha"}}
        if "/contents/" in url:
            return {"commit": {"sha": "commit-sha", "html_url": "https://github.com/c"}}
        if url.endswith("/pulls"):
            return {"number": 2, "html_url": "https://github.com/pr", "state": "open"}
        if url.endswith("/git/refs/heads/main"):
            return {"ref": "refs/heads/main"}
        if url.endswith("/repos/user/app"):
            return {"id": 42, "full_name": "user/app", "default_branch": "main"}
        return {"id": "deployment-id", "url": "app.vercel.app", "readyState": "READY"}

    monkeypatch.setattr(github, "request_json", fixture)
    monkeypatch.setattr(vercel, "request_json", fixture)

    async def run() -> None:
        repo = await GitHubAdapter("secret-token").execute("create_repo", {"name": "app"})
        assert repo["full_name"] == "user/app"
        github = GitHubAdapter("secret-token")
        info = await github.execute("repo_info", {"owner": "user", "repo": "app"})
        assert info["default_branch"] == "main"
        head = await github.execute(
            "branch_head", {"owner": "user", "repo": "app", "branch": "main"}
        )
        assert head["sha"] == "base-sha"
        branch = await github.execute(
            "create_branch",
            {"owner": "user", "repo": "app", "branch": "forge/app", "sha": "base-sha"},
        )
        assert branch["branch"] == "forge/app"
        commit = await github.execute(
            "commit_file",
            {
                "owner": "user",
                "repo": "app",
                "branch": "forge/app",
                "path": "src/a.ts",
                "message": "add app",
                "content": "Y29udGVudA==",
            },
        )
        assert commit["sha"] == "commit-sha"
        pr = await github.execute(
            "create_pr",
            {"owner": "user", "repo": "app", "title": "App", "head": "forge/app", "base": "main"},
        )
        assert pr["number"] == 2
        pushed = await github.execute(
            "push_default", {"owner": "user", "repo": "app", "branch": "main", "sha": "commit-sha"}
        )
        assert pushed["updated"] is True
        project = await VercelAdapter("secret-token").execute(
            "create_project", {"name": "app", "repository": "user/app"}
        )
        assert project["id"] == "project-id"
        deployed = await VercelAdapter("secret-token").execute(
            "deploy", {"name": "app", "project_id": "project-id", "repo_id": "repo-id"}
        )
        assert deployed["url"] == "app.vercel.app"
        ready = await VercelAdapter("secret-token").execute(
            "deployment_status", {"deployment_id": "deployment-id"}
        )
        assert ready["readyState"] == "READY"
        live_url = await VercelAdapter("secret-token").execute(
            "deployment_url", {"deployment_id": "deployment-id"}
        )
        assert live_url["url"] == "https://app.vercel.app"

    asyncio.run(run())
    assert len(calls) == 12


def test_github_adapter_rejects_unsafe_paths_and_non_feature_commits() -> None:
    async def run() -> None:
        github = GitHubAdapter("fixture-token")
        with pytest.raises(ValueError, match="forge/"):
            await github.execute(
                "commit_file",
                {
                    "owner": "user",
                    "repo": "app",
                    "branch": "main",
                    "path": "x",
                    "message": "m",
                    "content": "eA==",
                },
            )
        with pytest.raises(ValueError, match="inside the repository"):
            await github.execute(
                "commit_file",
                {
                    "owner": "user",
                    "repo": "app",
                    "branch": "forge/fix",
                    "path": "../secret",
                    "message": "m",
                    "content": "eA==",
                },
            )

    asyncio.run(run())


def test_http_smoke_check_rejects_non_https_without_network_access() -> None:
    async def run() -> None:
        checker = integration_handlers(UUID(int=1))["http_check"]
        with pytest.raises(ValueError, match="public HTTPS"):
            await checker({"url": "http://127.0.0.1/"}, "check-call")

    asyncio.run(run())


def test_deployer_tools_classify_external_risks_and_gate_deploy() -> None:
    async def handler(payload: object, key: str) -> dict[str, object]:
        del payload, key
        return {"ok": True}

    registry = ToolRegistry()
    register_deployer_tools(
        registry,
        {
            name: handler
            for name in (
                "integration_list",
                "github_create_repo",
                "github_repo_info",
                "github_branch_head",
                "github_create_branch",
                "github_commit_file",
                "github_create_pr",
                "github_push_default",
                "vercel_create_project",
                "vercel_deploy",
                "vercel_deployment_status",
                "vercel_deployment_url",
                "http_check",
            )
        },
    )
    assert registry.get("github_create_repo").risk is RiskClass.IRREVERSIBLE
    assert registry.get("github_create_branch").risk is RiskClass.REVERSIBLE_EXTERNAL
    assert registry.get("github_create_pr").risk is RiskClass.REVERSIBLE_EXTERNAL
    assert registry.get("github_push_default").risk is RiskClass.IRREVERSIBLE
    assert registry.get("vercel_deploy").risk is RiskClass.IRREVERSIBLE
    assert registry.get("vercel_deploy").requires_reviewer_pass
    handler = registry.get("github_create_repo").handler
    integrated_registry = agent_tool_registry(extra_handlers={"github_create_repo": handler})
    assert integrated_registry.get("github_create_repo").risk is RiskClass.IRREVERSIBLE
    for level in AutonomyLevel:
        for name in (
            "github_create_repo",
            "github_push_default",
            "vercel_create_project",
            "vercel_deploy",
        ):
            assert decide(level, registry.get(name).risk) is GateDecision.REQUIRE_APPROVAL
        assert decide(level, registry.get("github_create_pr").risk) is (
            GateDecision.REQUIRE_APPROVAL
            if level is AutonomyLevel.STEP_BY_STEP
            else GateDecision.AUTO
        )


def test_built_in_templates_and_later_hosting_interfaces() -> None:
    nextjs = load_template("nextjs")
    vite = load_template("vite-react")
    assert "app/page.tsx" in nextjs and "app/layout.tsx" in nextjs
    assert "src/main.tsx" in vite and "index.html" in vite
    with pytest.raises(ValueError, match="Unknown application template"):
        load_template("unknown")
    for name in ("netlify", "cloudflare_pages", "render"):
        adapter = DeferredHostingAdapter(name)
        assert adapter.kind == name
