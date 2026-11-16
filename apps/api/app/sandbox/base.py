"""Typed sandbox contract and path safety helpers."""

from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

OutputCallback = Callable[[str, str], Awaitable[None]]
MAX_COMMAND_TIMEOUT_SECONDS = 300


class SandboxSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    image: str = "forge-sandbox:latest"
    cpu: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=1024, gt=0, le=16384)
    pids_limit: int = Field(default=256, gt=0, le=4096)
    disk_mb: int = Field(default=2048, gt=0, le=65536)
    ttl_s: int = Field(default=3600, gt=0, le=86400)
    network_mode: Literal["none", "allowlisted"] = "none"
    allowlist: tuple[str, ...] = ()
    owner_user_id: str | None = None


class SandboxHandle(BaseModel):
    model_config = ConfigDict(frozen=True)
    sandbox_id: str
    run_id: str
    agent_id: str
    expires_at: float
    owner_user_id: str | None = None
    network_name: str | None = None
    proxy_id: str | None = None
    proxy_host: str | None = None


class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = Field(ge=0)
    truncated: bool = False


class SandboxProvider(Protocol):
    async def create(self, run_id: str, agent_id: str, spec: SandboxSpec) -> SandboxHandle: ...
    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
        on_output: OutputCallback | None = None,
    ) -> ExecResult: ...
    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> str: ...
    async def write_file(
        self, handle: SandboxHandle, path: str, content: str, max_bytes: int
    ) -> None: ...
    async def list_dir(self, handle: SandboxHandle, path: str = "/workspace") -> list[str]: ...
    async def is_dir(self, handle: SandboxHandle, path: str) -> bool: ...
    async def destroy(self, handle: SandboxHandle) -> None: ...
    async def list_active(self) -> list[SandboxHandle]: ...
    async def cleanup_expired(self) -> int: ...


def workspace_path(path: str, *, base: str = "/workspace") -> str:
    candidate = PurePosixPath(path)
    if "\x00" in path or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Path must stay inside /workspace.")
    root = PurePosixPath(base)
    resolved = root.joinpath(candidate)
    if not resolved.is_relative_to(root):
        raise ValueError("Path must stay inside /workspace.")
    return str(resolved)


def output_chunks(value: str, max_bytes: int, chunk_bytes: int = 4096) -> list[str]:
    if max_bytes <= 0 or chunk_bytes <= 0:
        return []
    raw = value.encode("utf-8")[:max_bytes]
    return [
        raw[i : i + chunk_bytes].decode("utf-8", errors="replace")
        for i in range(0, len(raw), chunk_bytes)
    ]
