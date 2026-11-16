"""No-Docker contracts for sandbox safety and registered tools."""

import asyncio
from collections.abc import Callable, Mapping
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.demo_tools import agent_tool_registry
from app.agents.registry import ROLE_TEMPLATES
from app.agents.tools import ToolRegistry
from app.core.config import Settings
from app.core.db import SessionFactory
from app.events.bus import EventBus
from app.gate.engine import RiskClass
from app.models.agent import Agent
from app.models.event import Event
from app.models.tool_execution import ToolExecution
from app.orchestrator.state import IllegalRunTransition
from app.repos.runs import RunRepository
from app.sandbox.base import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    ExecResult,
    OutputCallback,
    SandboxHandle,
    SandboxSpec,
    output_chunks,
    workspace_path,
)
from app.sandbox.docker_provider import _bounded, docker_create_args
from app.sandbox.egress import (
    dns_nxdomain_response,
    handle_connect,
    host_is_allowed,
    normalize_allowlist,
)
from app.sandbox.fake_provider import FakeSandboxProvider
from app.sandbox.registry import sandbox_handlers
from app.sandbox.tools import (
    SandboxToolHandlers,
    register_sandbox_tools,
    validate_tester_execution,
)
from app.sandbox.tools import (
    TesterResult as SandboxTesterResult,
)
from app.services.tools import ToolExecutionPipeline, _is_passing_reviewer
from tests.conftest import WEB_ORIGIN


class RecordingBus(EventBus):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def append(
        self, user_id: UUID, run_id: UUID, *, event_type: str, payload: dict[str, object]
    ) -> Event | None:
        del user_id, run_id, event_type
        self.events.append(payload)
        return None

    async def publish(self, event: Event) -> None:
        del event


class CapturingFake(FakeSandboxProvider):
    seen_env: dict[str, str] | None = None

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        self.seen_env = env
        return await super().exec(handle, cmd, cwd, env, timeout_s, on_output)


def test_workspace_path_rejects_absolute_traversal_and_nul() -> None:
    assert workspace_path("src/main.py") == "/workspace/src/main.py"
    for path in ("../outside", "src/../../x", "/etc/passwd", "bad\x00path"):
        with pytest.raises(ValueError):
            workspace_path(path)


def test_m6_role_templates_have_narrow_sandbox_tool_sets() -> None:
    assert set(ROLE_TEMPLATES["architect"].tool_allowlist) == {
        "read_file",
        "write_file",
        "list_dir",
        "finish",
    }
    coder_tools = set(ROLE_TEMPLATES["coder"].tool_allowlist)
    assert {"sandbox_exec", "write_file", "git_diff", "request_review"} <= coder_tools
    assert "git push" not in coder_tools and not any("deploy" in tool for tool in coder_tools)
    assert {"sandbox_exec", "tester_report", "finish"} <= set(
        ROLE_TEMPLATES["tester"].tool_allowlist
    )
    assert {"read_file", "git_diff", "review_complete"} <= set(
        ROLE_TEMPLATES["reviewer"].tool_allowlist
    )
    assert "structured" in ROLE_TEMPLATES["tester"].system_prompt


def test_docker_args_have_required_hardening_and_no_mounts() -> None:
    args = docker_create_args("r", "a", SandboxSpec())
    for flag in (
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges:true",
        "10001:10001",
        "--cpus",
        "--memory",
        "--pids-limit",
        "none",
        "--tmpfs",
        "forge.run_id=r",
        "forge.agent_id=a",
        "forge.expires_at=",
    ):
        assert any(flag in arg for arg in args)
    assert "-v" not in args and "--volume" not in args
    with pytest.raises(ValueError, match="internal network"):
        docker_create_args(
            "r",
            "a",
            SandboxSpec(network_mode="allowlisted", allowlist=("registry.npmjs.org",)),
        )
    allowlisted = docker_create_args(
        "r",
        "a",
        SandboxSpec(network_mode="allowlisted", allowlist=("registry.npmjs.org",)),
        "forge-egress-r",
        "forge-proxy-r",
        "172.30.0.2",
    )
    assert "forge-egress-r" in allowlisted and "none" not in allowlisted
    assert "forge.network_name=forge-egress-r" in allowlisted
    assert "forge.proxy_id=forge-proxy-r" in allowlisted
    assert "forge.proxy_host=172.30.0.2" in allowlisted
    assert allowlisted[allowlisted.index("--dns") + 1] == "172.30.0.2"
    with pytest.raises(ValueError, match="private network address"):
        docker_create_args(
            "r",
            "a",
            SandboxSpec(network_mode="allowlisted", allowlist=("registry.npmjs.org",)),
            "forge-egress-r",
            "forge-proxy-r",
            "8.8.8.8",
        )


def test_egress_allowlist_rejects_unsafe_entries_and_matches_exact_hosts() -> None:
    approved = normalize_allowlist(("Registry.NPMJS.org.", "github.com"))
    assert host_is_allowed("registry.npmjs.org", approved)
    assert not host_is_allowed("registry.npmjs.org.attacker.test", approved)
    assert not host_is_allowed("api.github.com", approved)
    for value in ("*.example.com", "https://example.com", "127.0.0.1", "bad..example"):
        with pytest.raises(ValueError):
            normalize_allowlist((value,))
    with pytest.raises(ValueError, match="allowlist hostname"):
        Settings(sandbox_allowlist_hosts=("*.example.com",))
    assert Settings(sandbox_allowlist_hosts=()).sandbox_network_mode == "none"
    with pytest.raises(ValueError, match="requires SANDBOX_PROVIDER=docker"):
        Settings(sandbox_provider="fake", sandbox_network_mode="allowlisted")


def test_sandbox_dns_stub_returns_nxdomain_without_forwarding() -> None:
    query = (
        b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    )
    response = dns_nxdomain_response(query)
    assert response[:2] == b"\x12\x34"
    assert int.from_bytes(response[2:4], "big") & 0xF == 3
    assert int.from_bytes(response[6:8], "big") == 0
    assert response[12:] == query[12:]


def test_egress_proxy_allows_only_approved_public_https_connect_targets() -> None:
    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, value: bytes) -> None:
            self.data.extend(value)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def check() -> None:
        approved = normalize_allowlist(("registry.npmjs.org",))
        seen: list[tuple[str, int]] = []

        async def resolve(host: str) -> list[str]:
            assert host == "registry.npmjs.org"
            return ["93.184.216.34"]

        async def connect(host: str, port: int) -> tuple[asyncio.StreamReader, Writer]:
            seen.append((host, port))
            upstream_reader = asyncio.StreamReader()
            upstream_reader.feed_eof()
            return upstream_reader, Writer()

        reader = asyncio.StreamReader()
        reader.feed_data(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")
        reader.feed_eof()
        writer = Writer()
        await handle_connect(reader, writer, approved, resolve=resolve, open_connection=connect)
        assert bytes(writer.data).startswith(b"HTTP/1.1 200")
        assert seen == [("93.184.216.34", 443)]

        async def forbidden_resolve(_: str) -> list[str]:
            raise AssertionError("Blocked hosts must be rejected before DNS lookup.")

        for authority in (
            "example.com:443",
            "registry.npmjs.org:444",
            "127.0.0.1:443",
        ):
            blocked_reader = asyncio.StreamReader()
            blocked_reader.feed_data(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode())
            blocked_reader.feed_eof()
            blocked_writer = Writer()
            await handle_connect(
                blocked_reader,
                blocked_writer,
                approved,
                resolve=forbidden_resolve,
                open_connection=connect,
            )
            assert bytes(blocked_writer.data).startswith(b"HTTP/1.1 403")

        async def private_resolve(_: str) -> list[str]:
            return ["127.0.0.1"]

        private_reader = asyncio.StreamReader()
        private_reader.feed_data(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")
        private_reader.feed_eof()
        private_writer = Writer()
        await handle_connect(
            private_reader,
            private_writer,
            approved,
            resolve=private_resolve,
            open_connection=connect,
        )
        assert bytes(private_writer.data).startswith(b"HTTP/1.1 403")
        assert seen == [("93.184.216.34", 443)]

    asyncio.run(check())


def test_fake_filesystem_symlink_cleanup_and_limits() -> None:
    provider = FakeSandboxProvider(max_output_bytes=4)

    async def run() -> None:
        with pytest.raises(ValueError, match="network_mode='none'"):
            await provider.create(
                "allowlisted-fake",
                "agent",
                SandboxSpec(network_mode="allowlisted", allowlist=("registry.npmjs.org",)),
            )
        handle = await provider.create("r", "a", SandboxSpec())
        await provider.write_file(handle, "src/a.py", "print(1)", 32)
        assert await provider.read_file(handle, "/workspace/src/a.py", 32) == "print(1)"
        assert await provider.list_dir(handle) == ["src"]
        provider.add_symlink(handle, "escape", "/etc")
        with pytest.raises(ValueError):
            await provider.read_file(handle, "escape/passwd", 32)
        with pytest.raises(ValueError):
            await provider.write_file(handle, "../bad", "x", 32)
        provider._handles[handle.sandbox_id] = handle.model_copy(update={"expires_at": 0.0})
        assert await provider.cleanup_expired() == 1
        await provider.destroy(handle)
        assert await provider.list_active() == []

    asyncio.run(run())


def test_output_chunks_and_tester_schema() -> None:
    assert _bounded(b"abcdef", 3) == ("abc", True)
    assert [len(x) for x in output_chunks("x" * 9000, 5000, 2048)] == [2048, 2048, 904]
    result = SandboxTesterResult.model_validate(
        {
            "command": "pytest",
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "exit_code": 0,
            "summary": "passed",
            "failing_tests": [],
        }
    )
    assert result.passed == 1
    with pytest.raises(ValueError):
        SandboxTesterResult.model_validate({"command": "pytest", "summary": "invalid"})
    with pytest.raises(ValueError, match="failed tests"):
        SandboxTesterResult.model_validate(
            {
                "command": "pytest",
                "passed": 1,
                "failed": 1,
                "skipped": 0,
                "exit_code": 0,
                "summary": "inconsistent",
            }
        )


def test_tester_report_matches_latest_executed_command_and_exit_code() -> None:
    execution = ToolExecution(
        id=uuid4(),
        run_id=uuid4(),
        agent_id=uuid4(),
        idempotency_key="test-execution",
        tool_name="sandbox_exec",
        input_payload={"cmd": ["python3", "-m", "unittest"]},
        output_payload={"exit_code": 0, "timed_out": False},
        status="completed",
    )
    result = SandboxTesterResult.model_validate(
        {
            "command": "python3 -m unittest",
            "passed": 2,
            "failed": 0,
            "skipped": 0,
            "exit_code": 0,
            "summary": "2 tests passed",
        }
    )
    assert validate_tester_execution(result, execution) == str(execution.id)
    with pytest.raises(ValueError, match="match the executed command"):
        validate_tester_execution(result.model_copy(update={"command": "pytest"}), execution)
    with pytest.raises(ValueError, match="match the executed command"):
        validate_tester_execution(result.model_copy(update={"exit_code": 1}), execution)


def test_sandbox_command_timeout_is_bounded() -> None:
    provider = FakeSandboxProvider()

    async def run() -> None:
        handle = await provider.create("timeout-run", "tester", SandboxSpec())
        normal = await provider.exec(handle, ["true"], timeout_s=MAX_COMMAND_TIMEOUT_SECONDS)
        assert normal.exit_code == 0 and not normal.timed_out
        with pytest.raises(ValueError, match="timeout"):
            await provider.exec(handle, ["true"], timeout_s=MAX_COMMAND_TIMEOUT_SECONDS + 1)

    asyncio.run(run())


def test_tester_must_report_a_passing_latest_command_before_successful_finish(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("app.api.v1.runs.enqueue_advance", no_enqueue)
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-tester-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    user_id = UUID(api_client.get("/api/v1/me").json()["id"])
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Tester enforcement", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])

    async def setup_tester() -> UUID:
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            assert run is not None
            run.status = "EXECUTING"
            agent = Agent(
                run_id=run_id,
                role="tester",
                config={"role": "tester"},
                status="running",
            )
            session.add(agent)
            await session.commit()
            return agent.id

    agent_id = api_client.portal.call(setup_tester)
    provider = FakeSandboxProvider(
        results=[ExecResult(exit_code=0, stdout="Ran 2 tests; OK", duration_ms=1)]
    )
    bus = RecordingBus()
    monkeypatch.setattr("app.sandbox.registry.get_sandbox_provider", lambda: provider)
    handlers = api_client.portal.call(sandbox_handlers, user_id, run_id, agent_id, bus)
    registry = agent_tool_registry(extra_handlers=handlers)
    pipeline = ToolExecutionPipeline(SessionFactory, registry, bus)

    async def execute_workflow() -> None:
        with pytest.raises(IllegalRunTransition, match="without a passing report"):
            await pipeline.execute(
                user_id,
                run_id,
                agent_id,
                "finish",
                {"status": "completed", "summary": "Tests passed."},
                tool_call_id="finish-before-report",
                reason="Test required report gate.",
            )
        await pipeline.execute(
            user_id,
            run_id,
            agent_id,
            "sandbox_exec",
            {"cmd": ["python3", "-m", "unittest"]},
            tool_call_id="run-tests",
            reason="Execute tests.",
        )
        await pipeline.execute(
            user_id,
            run_id,
            agent_id,
            "tester_report",
            {
                "command": "python3 -m unittest",
                "passed": 2,
                "failed": 0,
                "skipped": 0,
                "exit_code": 0,
                "summary": "Two tests passed.",
            },
            tool_call_id="report-tests",
            reason="Persist test report.",
        )
        completed = await pipeline.execute(
            user_id,
            run_id,
            agent_id,
            "finish",
            {"status": "completed", "summary": "Tests passed."},
            tool_call_id="finish-after-report",
            reason="Finish after passing test report.",
        )
        assert completed.status == "completed"

    api_client.portal.call(execute_workflow)


def test_reviewer_gate_blocks_protected_tool_until_reviewer_passes(
    api_client: TestClient,
    test_emails: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_enqueue(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("app.api.v1.runs.enqueue_advance", no_enqueue)
    signup = api_client.post(
        "/api/v1/auth/signup",
        json={"email": test_emails(), "password": "sandbox-reviewer-password"},
        headers={"Origin": WEB_ORIGIN},
    )
    assert signup.status_code == 201
    user_id = UUID(api_client.get("/api/v1/me").json()["id"])
    created = api_client.post(
        "/api/v1/runs",
        json={"idea": "Reviewer enforcement", "autonomy_level": "autopilot", "budget_usd": 5},
        headers={"Origin": WEB_ORIGIN},
    )
    assert created.status_code == 201
    run_id = UUID(created.json()["id"])

    async def setup_agents() -> tuple[UUID, UUID]:
        async with SessionFactory() as session:
            run = await RunRepository(session).get_for_user(user_id, run_id, lock=True)
            assert run is not None
            run.status = "EXECUTING"
            coder = Agent(run_id=run_id, role="coder", config={}, status="running")
            reviewer = Agent(run_id=run_id, role="reviewer", config={}, status="running")
            session.add_all([coder, reviewer])
            await session.commit()
            return coder.id, reviewer.id

    agent_id, reviewer_id = api_client.portal.call(setup_agents)
    registry = ToolRegistry()

    async def protected_handler(payload: Mapping[str, object], key: str) -> dict[str, object]:
        del payload, key
        return {"deployed": True}

    registry.register(
        "deploy_preview",
        protected_handler,
        risk=RiskClass.SANDBOX_WRITE,
        requires_reviewer_pass=True,
    )
    pipeline = ToolExecutionPipeline(SessionFactory, registry, RecordingBus())

    async def verify_gate() -> None:
        with pytest.raises(IllegalRunTransition, match="passing Reviewer result"):
            await pipeline.execute(
                user_id,
                run_id,
                agent_id,
                "deploy_preview",
                {},
                tool_call_id="deploy-before-review",
                reason="Verify review gate.",
            )
        async with SessionFactory() as session:
            event = await EventBus().persist(
                session,
                user_id,
                run_id,
                event_type="review.completed",
                payload={"agent_id": str(reviewer_id), "passed": True},
            )
            assert event is not None
            await session.commit()
        result = await pipeline.execute(
            user_id,
            run_id,
            agent_id,
            "deploy_preview",
            {},
            tool_call_id="deploy-after-review",
            reason="Deploy after passing review.",
        )
        assert result.status == "completed"
        assert result.output == {"deployed": True}

    api_client.portal.call(verify_gate)


def test_sandbox_tool_risks_and_reviewer_gate() -> None:
    async def handler(payload: Mapping[str, object], key: str) -> dict[str, object]:
        del payload, key
        return {"ok": True}

    registry = ToolRegistry()
    register_sandbox_tools(
        registry,
        {
            name: handler
            for name in (
                "sandbox_exec",
                "read_file",
                "write_file",
                "list_dir",
                "git_status",
                "git_diff",
                "git_branch",
                "git_commit",
            )
        },
    )
    assert registry.get("sandbox_exec").risk is RiskClass.SANDBOX_WRITE
    assert registry.get("read_file").risk is RiskClass.READ
    assert registry.get("git_branch").risk is RiskClass.SANDBOX_WRITE
    assert not _is_passing_reviewer({"passed": True}, "coder")
    assert _is_passing_reviewer({"passed": True}, "reviewer")


def test_fake_exec_streams_capped_logs_without_host_environment() -> None:
    provider = CapturingFake(
        results=[ExecResult(exit_code=0, stdout="abcdef-secret", duration_ms=1)], max_output_bytes=6
    )
    bus = RecordingBus()
    tool = SandboxToolHandlers(provider, SessionFactory, bus, uuid4(), uuid4(), uuid4(), 6)

    async def run() -> None:
        result = await (await tool.handlers())["sandbox_exec"](
            {"cmd": ["python3", "-c", "print('ok')"]}, "call"
        )
        assert result["stdout"] == "abcdef" and result["truncated"] is True
        assert provider.seen_env is None
        assert bus.events[0]["stream"] == "stdout" and bus.events[0]["chunk"] == "abcdef"

    asyncio.run(run())
