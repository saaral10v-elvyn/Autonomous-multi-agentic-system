"""Risk-classified Deployer handlers bound to the run owner and stored credentials."""

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Mapping
from http.client import HTTPSConnection
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.tools import ToolHandler, ToolRegistry
from app.core.config import get_settings
from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.db import SessionFactory
from app.gate.engine import RiskClass
from app.integrations.github import GitHubAdapter
from app.integrations.vercel import VercelAdapter
from app.repos.integrations import IntegrationRepository


def register_deployer_tools(registry: ToolRegistry, handlers: Mapping[str, ToolHandler]) -> None:
    risks = {
        "integration_list": RiskClass.READ,
        "github_create_repo": RiskClass.IRREVERSIBLE,
        "github_repo_info": RiskClass.READ,
        "github_branch_head": RiskClass.READ,
        "github_create_branch": RiskClass.REVERSIBLE_EXTERNAL,
        "github_commit_file": RiskClass.REVERSIBLE_EXTERNAL,
        "github_create_pr": RiskClass.REVERSIBLE_EXTERNAL,
        "github_push_default": RiskClass.IRREVERSIBLE,
        "vercel_create_project": RiskClass.IRREVERSIBLE,
        "vercel_deploy": RiskClass.IRREVERSIBLE,
        "vercel_deployment_status": RiskClass.READ,
        "vercel_deployment_url": RiskClass.READ,
        "http_check": RiskClass.READ,
    }
    schemas: dict[str, dict[str, object]] = {
        "integration_list": {"type": "object", "properties": {}, "additionalProperties": False},
        "github_create_repo": _schema("integration_id", "name", "private"),
        "github_repo_info": _schema("integration_id", "owner", "repo"),
        "github_branch_head": _schema("integration_id", "owner", "repo", "branch"),
        "github_create_branch": _schema("integration_id", "owner", "repo", "branch", "sha"),
        "github_commit_file": _schema(
            "integration_id", "owner", "repo", "branch", "path", "message", "content"
        ),
        "github_create_pr": _schema(
            "integration_id", "owner", "repo", "title", "head", "base", "body"
        ),
        "github_push_default": _schema("integration_id", "owner", "repo", "branch", "sha"),
        "vercel_create_project": _schema("integration_id", "name", "framework", "repository"),
        "vercel_deploy": _schema("integration_id", "name", "project_id", "repo_id", "branch"),
        "vercel_deployment_status": _schema("integration_id", "deployment_id"),
        "vercel_deployment_url": _schema("integration_id", "deployment_id"),
        "http_check": _schema("url"),
    }
    for name, handler in handlers.items():
        risk = risks[name]
        descriptions = {
            "github_commit_file": "Create or update a Base64 file on a forge/ feature branch.",
            "github_create_branch": "Create a forge/ feature branch from a commit SHA.",
            "github_push_default": "Update the repository's default branch after approval.",
            "vercel_deploy": "Deploy after a passing Reviewer result and user approval.",
        }
        registry.register(
            name,
            handler,
            risk=risk,
            description=descriptions.get(name, f"{name.replace('_', ' ')}."),
            parameters=schemas[name],
            requires_reviewer_pass=name == "vercel_deploy",
        )


def integration_handlers(
    user_id: UUID, sessions: async_sessionmaker[AsyncSession] = SessionFactory
) -> dict[str, ToolHandler]:
    async def invoke(kind: str, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        raw_id = payload.get("integration_id")
        if not isinstance(raw_id, str):
            raise ValueError("integration_id is required.")
        try:
            integration_id = UUID(raw_id)
        except ValueError:
            raise ValueError("Integration not found.") from None
        async with sessions() as session:
            item = await IntegrationRepository(session).get_for_user(user_id, integration_id)
            if item is None or item.kind != kind:
                raise ValueError("Integration not found.")
            token = decrypt_secret(
                EncryptedSecret(item.token_ciphertext, item.token_nonce),
                user_id=user_id,
                record_id=item.id,
                master_key=get_settings().master_key,
            )
        adapter = GitHubAdapter(token) if kind == "github" else VercelAdapter(token)
        safe_payload = {key: value for key, value in payload.items() if key != "integration_id"}
        return await adapter.execute(action, safe_payload)

    handlers: dict[str, ToolHandler] = {}

    async def integration_list(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
        del payload
        async with sessions() as session:
            items = await IntegrationRepository(session).list_for_user(user_id)
            return {
                "integrations": [
                    {"id": str(item.id), "kind": item.kind, "account_label": item.account_label}
                    for item in items
                ]
            }

    handlers["integration_list"] = integration_list
    for name, kind, action in (
        ("github_create_repo", "github", "create_repo"),
        ("github_repo_info", "github", "repo_info"),
        ("github_branch_head", "github", "branch_head"),
        ("github_create_branch", "github", "create_branch"),
        ("github_commit_file", "github", "commit_file"),
        ("github_create_pr", "github", "create_pr"),
        ("github_push_default", "github", "push_default"),
        ("vercel_create_project", "vercel", "create_project"),
        ("vercel_deploy", "vercel", "deploy"),
        ("vercel_deployment_status", "vercel", "deployment_status"),
        ("vercel_deployment_url", "vercel", "deployment_url"),
    ):

        async def bound(
            payload: Mapping[str, object], _: str, k: str = kind, a: str = action
        ) -> Mapping[str, object]:
            return await invoke(k, a, payload)

        handlers[name] = bound

    async def http_check(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
        url = payload.get("url")
        if not isinstance(url, str):
            raise ValueError("A public HTTPS URL is required.")
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise ValueError("A public HTTPS URL is required.")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or 443, type=socket.SOCK_STREAM
        )
        ips = {str(ipaddress.ip_address(row[4][0])) for row in addresses}
        if not ips or any(not ipaddress.ip_address(address).is_global for address in ips):
            raise ValueError("Smoke check target must resolve only to public addresses.")

        def pinned_status(address: str) -> int:
            class PinnedHTTPS(HTTPSConnection):
                def connect(self) -> None:
                    raw = socket.create_connection((address, self.port), timeout=10)
                    self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)

            connection = PinnedHTTPS(host, parsed.port or 443, timeout=10)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={"Host": host})
            return connection.getresponse().status

        try:
            status = await asyncio.to_thread(pinned_status, sorted(ips)[0])
        except (OSError, TimeoutError):
            status = 0
        return {"ok": status == 200, "status_code": status}

    handlers["http_check"] = http_check
    return handlers


def deployer_tools_for_user(user_id: UUID) -> dict[str, ToolHandler]:
    return integration_handlers(user_id)


def _schema(*required: str) -> dict[str, object]:
    properties: dict[str, object] = {name: {"type": "string"} for name in required}
    if "private" in properties:
        properties["private"] = {"type": "boolean"}
    return {
        "type": "object",
        "properties": properties,
        "required": [name for name in required if name not in {"private", "body", "framework"}],
        "additionalProperties": False,
    }
