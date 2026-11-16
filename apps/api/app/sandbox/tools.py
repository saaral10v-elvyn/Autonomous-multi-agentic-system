"""Sandbox file, command, local Git, Tester, and Reviewer tool handlers."""

import asyncio
import difflib
import re
import shlex
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.tools import ToolHandler
from app.events.bus import EventBus
from app.gate.engine import RiskClass
from app.models.agent import Agent
from app.models.run import Run
from app.models.tool_execution import ToolExecution
from app.repos.artifacts import ArtifactRepository
from app.sandbox.base import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    ExecResult,
    SandboxHandle,
    SandboxProvider,
    output_chunks,
    workspace_path,
)
from app.sandbox.factory import sandbox_spec

_RUN_LOCKS: dict[str, asyncio.Lock] = {}


class TesterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=1000)
    passed: int = Field(ge=0, le=1_000_000)
    failed: int = Field(ge=0, le=1_000_000)
    skipped: int = Field(ge=0, le=1_000_000)
    exit_code: int
    summary: str = Field(min_length=1, max_length=5000)
    failing_tests: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_counts(self) -> "TesterResult":
        if self.exit_code == 0 and self.passed + self.skipped == 0:
            raise ValueError("A successful test command must report at least one test.")
        if len(self.failing_tests) > self.failed:
            raise ValueError("failing_tests cannot exceed the failed test count.")
        if self.exit_code == 0 and (self.failed > 0 or self.failing_tests):
            raise ValueError("A successful test command cannot report failed tests.")
        if self.exit_code != 0 and self.failed == 0 and self.passed > 0:
            raise ValueError("A failing test command cannot report only passed tests.")
        return self


def validate_tester_execution(result: TesterResult, execution: ToolExecution) -> str:
    """Require report fields to match the latest completed sandbox command."""
    raw_command = execution.input_payload.get("cmd")
    output = execution.output_payload
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or not all(isinstance(item, str) for item in raw_command)
    ):
        raise ValueError("The executed test command is unavailable.")
    if not isinstance(output, dict):
        raise ValueError("The executed test result is unavailable.")
    command = shlex.join(raw_command)
    if result.command != command:
        raise ValueError("Tester report command must match the executed command.")
    actual_exit_code = output.get("exit_code")
    if type(actual_exit_code) is not int or result.exit_code != actual_exit_code:
        raise ValueError("Tester report exit_code must match the executed command.")
    if output.get("timed_out") is True:
        raise ValueError("A timed-out command cannot be reported as a completed test run.")
    return str(execution.id)


async def sandbox_diff(
    provider: SandboxProvider,
    handle: SandboxHandle,
    max_output_bytes: int = 65536,
) -> str:
    """Return staged, unstaged, and untracked diffs using workspace-only paths."""
    budget = max(0, max_output_bytes)
    parts: list[str] = []
    used = 0
    git_environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_DIR": "/workspace/.git",
        "GIT_WORK_TREE": "/workspace",
        "GIT_CEILING_DIRECTORIES": "/workspace",
    }

    async def append_diff(result: ExecResult, *, accepted_codes: set[int]) -> bool:
        nonlocal used
        if result.exit_code not in accepted_codes:
            raise RuntimeError("Could not inspect sandbox Git diff.")
        value = result.stdout
        encoded = value.encode("utf-8")
        remaining = max(0, budget - used)
        clipped = encoded[:remaining].decode("utf-8", errors="replace")
        if clipped:
            parts.append(clipped)
            used += len(clipped.encode("utf-8"))
        return result.truncated or len(encoded) > remaining or used >= budget

    try:
        repo = await provider.is_dir(handle, ".git")
    except (LookupError, RuntimeError, ValueError):
        repo = False
    if repo:
        for args in (
            ["git", "diff", "--no-ext-diff", "--", "."],
            ["git", "diff", "--cached", "--no-ext-diff", "--", "."],
        ):
            result = await provider.exec(handle, args, "/workspace", git_environment, timeout_s=30)
            if await append_diff(result, accepted_codes={0}):
                return "".join(parts) + "\n[diff truncated at configured output limit]\n"
        listing = await provider.exec(
            handle,
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "."],
            "/workspace",
            git_environment,
            timeout_s=30,
        )
        if listing.exit_code == 0:
            untracked_paths = listing.stdout.split("\x00")
            if listing.truncated and untracked_paths:
                untracked_paths = untracked_paths[:-1]
        else:
            untracked_paths = []
    else:
        untracked_paths = await _workspace_file_paths(provider, handle)

    for relative_path in untracked_paths:
        if not relative_path:
            continue
        workspace_path(relative_path)
        try:
            content = await provider.read_file(handle, relative_path, max(1, budget - used))
        except ValueError:
            marker = (
                f"diff --git a/{relative_path} b/{relative_path}\n"
                "[untracked file omitted: exceeds diff limit or resolves outside /workspace]\n"
            )
            remaining = max(0, budget - used)
            clipped = marker.encode()[:remaining].decode(errors="replace")
            if clipped:
                parts.append(clipped)
                used += len(clipped.encode())
            if len(marker.encode()) > remaining:
                return "".join(parts) + "\n[diff truncated at configured output limit]\n"
            continue
        diff_lines = difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{relative_path}",
        )
        untracked_diff = f"diff --git a/{relative_path} b/{relative_path}\n" + "".join(diff_lines)
        encoded = untracked_diff.encode()
        remaining = max(0, budget - used)
        clipped = encoded[:remaining].decode(errors="replace")
        if clipped:
            parts.append(clipped)
            used += len(clipped.encode())
        if len(encoded) > remaining or used >= budget:
            return "".join(parts) + "\n[diff truncated at configured output limit]\n"
    return "".join(parts)


async def _workspace_file_paths(provider: SandboxProvider, handle: SandboxHandle) -> list[str]:
    paths: list[str] = []

    async def walk(directory: str, relative: str) -> None:
        for name in await provider.list_dir(handle, directory):
            child_relative = f"{relative}/{name}" if relative else name
            if child_relative == ".git" or child_relative.startswith(".git/"):
                continue
            child_path = workspace_path(child_relative)
            if await provider.is_dir(handle, child_path):
                await walk(child_path, child_relative)
            else:
                paths.append(child_relative)

    await walk("/workspace", "")
    return paths


class SandboxToolHandlers:
    def __init__(
        self,
        provider: SandboxProvider,
        sessions: async_sessionmaker[AsyncSession],
        bus: EventBus,
        user_id: UUID,
        run_id: UUID,
        agent_id: UUID,
        max_output_bytes: int,
    ) -> None:
        self.provider, self.sessions, self.bus = provider, sessions, bus
        self.user_id, self.run_id, self.agent_id = user_id, run_id, agent_id
        self.max_output_bytes = max_output_bytes

    async def _handle(self) -> SandboxHandle:
        key = str(self.run_id)
        async with _RUN_LOCKS.setdefault(key, asyncio.Lock()):
            active = next((h for h in await self.provider.list_active() if h.run_id == key), None)
            return active or await self.provider.create(
                key, str(self.agent_id), sandbox_spec(owner_user_id=str(self.user_id))
            )

    async def handlers(self) -> dict[str, ToolHandler]:
        async def exec_tool(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
            cmd = payload.get("cmd")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(v, str) for v in cmd):
                raise ValueError("cmd must be a non-empty array of strings.")
            command_text = " ".join(cmd).casefold()
            if "git push" in command_text or "git remote" in command_text:
                raise ValueError("Remote Git operations are deferred until M7.")
            cwd, timeout = payload.get("cwd", "/workspace"), payload.get("timeout_s", 120)
            if not isinstance(cwd, str) or type(timeout) is not int:
                raise ValueError("Invalid cwd or timeout_s.")
            if not 0 < timeout <= MAX_COMMAND_TIMEOUT_SECONDS:
                raise ValueError(f"timeout_s must be between 1 and {MAX_COMMAND_TIMEOUT_SECONDS}.")

            async def log(stream: str, chunk: str) -> None:
                for part in output_chunks(chunk, self.max_output_bytes):
                    await self.bus.append(
                        self.user_id,
                        self.run_id,
                        event_type="sandbox.log",
                        payload={"agent_id": str(self.agent_id), "stream": stream, "chunk": part},
                    )

            result = await self.provider.exec(await self._handle(), cmd, cwd, None, timeout, log)
            return result.model_dump(mode="json")

        async def file_tool(
            action: str, payload: Mapping[str, object], _: str
        ) -> Mapping[str, object]:
            path = payload.get("path", "/workspace")
            if not isinstance(path, str):
                raise ValueError("path must be a string.")
            provider, handle = self.provider, await self._handle()
            if action == "read_file":
                return {
                    "path": path,
                    "content": await provider.read_file(handle, path, self.max_output_bytes),
                }
            if action == "write_file":
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a string.")
                await provider.write_file(handle, path, content, self.max_output_bytes)
                return {"path": path, "written": True}
            return {"path": path, "entries": await provider.list_dir(handle, path)}

        async def git_tool(
            action: str, payload: Mapping[str, object], _: str
        ) -> Mapping[str, object]:
            raw_cwd = payload.get("cwd", "/workspace")
            if not isinstance(raw_cwd, str):
                raise ValueError("cwd must be a string.")
            cwd = (
                "/workspace"
                if raw_cwd == "/workspace"
                else workspace_path(raw_cwd.removeprefix("/workspace/"))
            )
            handle = await self._handle()
            if action == "diff":
                return ExecResult(
                    exit_code=0,
                    stdout=await sandbox_diff(self.provider, handle, self.max_output_bytes),
                    duration_ms=0,
                ).model_dump(mode="json")
            args = ["git"]
            if action in {"init", "status"}:
                args.append(action)
            elif action == "branch":
                name = payload.get("name")
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", name)
                    or ".." in name
                ):
                    raise ValueError("A valid local branch name is required.")
                args.extend(["branch", "--", name])
            elif action == "add":
                paths = payload.get("paths", ["."])
                if (
                    not isinstance(paths, list)
                    or not paths
                    or not all(isinstance(p, str) for p in paths)
                ):
                    raise ValueError("paths must be a non-empty string array.")
                for path in paths:
                    if path != ".":
                        workspace_path(path)
                args.extend(["add", "--", *paths])
            elif action == "commit":
                message = payload.get("message")
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("A commit message is required.")
                args = [
                    "git",
                    "-c",
                    "user.name=Forge",
                    "-c",
                    "user.email=forge@localhost",
                    "commit",
                    "-m",
                    message,
                ]
            elif action == "checkout":
                branch = payload.get("branch")
                if (
                    not isinstance(branch, str)
                    or not branch
                    or branch.startswith("-")
                    or branch.startswith("origin/")
                ):
                    raise ValueError("Only a local branch name is allowed.")
                exists = await self.provider.exec(
                    handle,
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    cwd,
                    None,
                    30,
                )
                if exists.exit_code != 0:
                    raise ValueError("Branch does not exist locally.")
                args.extend(["checkout", branch])
            else:
                raise ValueError("Unsupported local Git action.")
            result = await self.provider.exec(handle, args, cwd, None, 120)
            return result.model_dump(mode="json")

        async def tester_report(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
            result = TesterResult.model_validate(payload)
            async with self.sessions() as session:
                tester = await session.scalar(
                    select(Agent).where(
                        Agent.id == self.agent_id,
                        Agent.run_id == self.run_id,
                        Agent.role == "tester",
                    )
                )
                if tester is None:
                    raise ValueError("Only the Tester role can submit test results.")
                execution = await session.scalar(
                    select(ToolExecution)
                    .join(Run, Run.id == ToolExecution.run_id)
                    .where(
                        Run.user_id == self.user_id,
                        Run.id == self.run_id,
                        ToolExecution.agent_id == self.agent_id,
                        ToolExecution.tool_name == "sandbox_exec",
                        ToolExecution.status == "completed",
                    )
                    .order_by(ToolExecution.completed_at.desc(), ToolExecution.created_at.desc())
                    .limit(1)
                )
                if execution is None:
                    raise ValueError("Tester must execute a test command before reporting results.")
                source_execution_id = validate_tester_execution(result, execution)
                saved_result = {
                    **result.model_dump(mode="json"),
                    "source_execution_id": source_execution_id,
                }
                artifact, changed = await ArtifactRepository(session).save_tester_result_for_user(
                    self.user_id, self.run_id, saved_result
                )
                if artifact is None:
                    raise LookupError("Run not found.")
                event = (
                    await self.bus.persist(
                        session,
                        self.user_id,
                        self.run_id,
                        event_type="artifact.created",
                        payload={
                            "artifact_id": str(artifact.id),
                            "kind": artifact.kind,
                            "name": artifact.name,
                        },
                    )
                    if changed
                    else None
                )
                await session.commit()
                await session.refresh(artifact)
                if event is not None:
                    await session.refresh(event)
            if event is not None:
                await self.bus.publish(event)
            return saved_result

        async def request_review(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
            event = await self.bus.append(
                self.user_id,
                self.run_id,
                event_type="review.requested",
                payload={
                    "agent_id": str(self.agent_id),
                    "summary": str(payload.get("summary", "Review requested."))[:1000],
                },
            )
            if event is None:
                raise LookupError("Run not found.")
            return {"status": "requested", "seq": event.seq}

        async def review_complete(payload: Mapping[str, object], _: str) -> Mapping[str, object]:
            passed, summary = payload.get("passed"), payload.get("summary")
            if not isinstance(passed, bool) or not isinstance(summary, str):
                raise ValueError("passed and summary are required.")
            async with self.sessions() as session:
                reviewer = await session.scalar(
                    select(Agent).where(
                        Agent.id == self.agent_id,
                        Agent.run_id == self.run_id,
                        Agent.role == "reviewer",
                    )
                )
                if reviewer is None:
                    raise ValueError("Only the Reviewer role can complete a review.")
            event = await self.bus.append(
                self.user_id,
                self.run_id,
                event_type="review.completed",
                payload={
                    "agent_id": str(self.agent_id),
                    "passed": passed,
                    "summary": summary[:5000],
                },
            )
            if event is None:
                raise LookupError("Run not found.")
            return {"status": "completed", "passed": passed, "seq": event.seq}

        handlers: dict[str, ToolHandler] = {
            "sandbox_exec": exec_tool,
            "tester_report": tester_report,
            "request_review": request_review,
            "review_complete": review_complete,
        }
        for name, action in (
            ("read_file", "read_file"),
            ("write_file", "write_file"),
            ("list_dir", "list_dir"),
        ):

            async def bound_file(
                payload: Mapping[str, object], key: str, file_action: str = action
            ) -> Mapping[str, object]:
                return await file_tool(file_action, payload, key)

            handlers[name] = bound_file
        for name in (
            "git_init",
            "git_status",
            "git_diff",
            "git_add",
            "git_commit",
            "git_branch",
            "git_checkout",
        ):
            action = name.removeprefix("git_")

            async def bound_git(
                payload: Mapping[str, object], key: str, git_action: str = action
            ) -> Mapping[str, object]:
                return await git_tool(git_action, payload, key)

            handlers[name] = bound_git
        return handlers


def register_sandbox_tools(registry: Any, handlers: Mapping[str, ToolHandler]) -> None:
    read_tools = {
        "read_file",
        "list_dir",
        "git_status",
        "git_diff",
        "request_review",
        "review_complete",
    }
    for name, handler in handlers.items():
        risk = RiskClass.READ if name in read_tools else RiskClass.SANDBOX_WRITE
        registry.register(
            name,
            handler,
            risk=risk,
            description=f"{name.replace('_', ' ')} in the run sandbox.",
            requires_reviewer_pass=name.startswith("deploy_"),
        )
