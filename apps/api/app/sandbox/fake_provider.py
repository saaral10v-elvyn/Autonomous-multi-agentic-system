"""In-memory sandbox for tests and non-production demos."""

import time
from collections import deque
from uuid import uuid4

from app.sandbox.base import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    ExecResult,
    OutputCallback,
    SandboxHandle,
    SandboxSpec,
    workspace_path,
)


class FakeSandboxProvider:
    def __init__(
        self, results: list[ExecResult] | None = None, max_output_bytes: int = 65536
    ) -> None:
        self._files: dict[str, dict[str, str]] = {}
        self._dirs: dict[str, set[str]] = {}
        self._handles: dict[str, SandboxHandle] = {}
        self._links: dict[str, dict[str, str]] = {}
        self._results = deque(results or [])
        self._max_output_bytes = max_output_bytes

    async def create(self, run_id: str, agent_id: str, spec: SandboxSpec) -> SandboxHandle:
        if spec.network_mode != "none":
            raise ValueError("Fake sandbox provider supports network_mode='none' only.")
        current = next(
            (
                h
                for h in self._handles.values()
                if h.run_id == run_id and h.expires_at > time.time()
            ),
            None,
        )
        if current:
            return current
        handle = SandboxHandle(
            sandbox_id=uuid4().hex,
            run_id=run_id,
            agent_id=agent_id,
            expires_at=time.time() + spec.ttl_s,
            owner_user_id=spec.owner_user_id,
        )
        self._handles[handle.sandbox_id] = handle
        self._files[handle.sandbox_id] = {}
        self._dirs[handle.sandbox_id] = {"/workspace"}
        self._links[handle.sandbox_id] = {}
        return handle

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        del env, cmd
        if type(timeout_s) is not int or not 0 < timeout_s <= MAX_COMMAND_TIMEOUT_SECONDS:
            raise ValueError(
                f"Command timeout must be between 1 and {MAX_COMMAND_TIMEOUT_SECONDS} seconds."
            )
        self._safe(handle, cwd)
        result = (
            self._results.popleft() if self._results else ExecResult(exit_code=0, duration_ms=0)
        )
        stdout, out_cut = _truncate(result.stdout, self._max_output_bytes)
        stderr, err_cut = _truncate(result.stderr, self._max_output_bytes)
        result = result.model_copy(
            update={
                "stdout": stdout,
                "stderr": stderr,
                "truncated": result.truncated or out_cut or err_cut,
            }
        )
        if on_output:
            if stdout:
                await on_output("stdout", stdout)
            if stderr:
                await on_output("stderr", stderr)
        return result

    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> str:
        try:
            value = self._files[self._require(handle)][self._safe(handle, path)]
        except KeyError:
            raise LookupError("File not found.") from None
        if len(value.encode()) > max_bytes:
            raise ValueError("File exceeds the configured size limit.")
        return value

    async def write_file(
        self, handle: SandboxHandle, path: str, content: str, max_bytes: int
    ) -> None:
        if len(content.encode()) > max_bytes:
            raise ValueError("File exceeds the configured size limit.")
        sandbox_id = self._require(handle)
        safe = self._safe(handle, path)
        self._files[sandbox_id][safe] = content
        self.make_dir(handle, safe.rsplit("/", 1)[0])

    async def list_dir(self, handle: SandboxHandle, path: str = "/workspace") -> list[str]:
        root = self._safe(handle, path).rstrip("/") + "/"
        sandbox_id = self._require(handle)
        children = self._files[sandbox_id].keys() | self._dirs[sandbox_id]
        return sorted(
            {
                item[len(root) :].split("/")[0]
                for item in children
                if item.startswith(root) and item != root[:-1]
            }
        )

    async def is_dir(self, handle: SandboxHandle, path: str) -> bool:
        sandbox_id = self._require(handle)
        safe = self._safe(handle, path)
        return safe in self._dirs[sandbox_id] or any(
            item.startswith(safe.rstrip("/") + "/") for item in self._files[sandbox_id]
        )

    def make_dir(self, handle: SandboxHandle, path: str) -> None:
        safe = self._safe(handle, path)
        if safe == "/workspace":
            return
        current = "/workspace"
        for part in safe.removeprefix("/workspace/").split("/"):
            if part:
                current += "/" + part
                self._dirs[handle.sandbox_id].add(current)

    async def destroy(self, handle: SandboxHandle) -> None:
        for store in (self._files, self._dirs, self._handles, self._links):
            store.pop(handle.sandbox_id, None)

    async def list_active(self) -> list[SandboxHandle]:
        return list(self._handles.values())

    async def cleanup_expired(self) -> int:
        expired = [h for h in self._handles.values() if h.expires_at <= time.time()]
        for handle in expired:
            await self.destroy(handle)
        return len(expired)

    def add_symlink(self, handle: SandboxHandle, path: str, target: str) -> None:
        self._links[handle.sandbox_id][workspace_path(path)] = target

    def _require(self, handle: SandboxHandle) -> str:
        if handle.sandbox_id not in self._files or handle.expires_at <= time.time():
            raise LookupError("Sandbox not found or expired.")
        return handle.sandbox_id

    def _safe(self, handle: SandboxHandle, path: str) -> str:
        if path == "/workspace":
            normalized = "/workspace"
        elif path.startswith("/workspace/"):
            normalized = workspace_path(path[len("/workspace/") :])
        else:
            normalized = workspace_path(path)
        current = "/workspace"
        pieces = (
            normalized.removeprefix("/workspace/").split("/") if normalized != "/workspace" else []
        )
        for index, part in enumerate(p for p in pieces if p):
            current += "/" + part
            target = self._links.get(handle.sandbox_id, {}).get(current)
            if target is not None:
                if target != "/workspace" and not target.startswith("/workspace/"):
                    raise ValueError("Symlink escapes /workspace.")
                current = workspace_path("/".join(p for p in pieces if p)[index + 1 :], base=target)
        return current


def _truncate(value: str, cap: int) -> tuple[str, bool]:
    raw = value.encode()
    return (value, False) if len(raw) <= cap else (raw[:cap].decode(errors="replace"), True)
