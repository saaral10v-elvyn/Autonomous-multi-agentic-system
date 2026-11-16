"""Hardened Docker CLI sandbox provider."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import shlex
import time
from datetime import UTC, datetime
from typing import Any

from app.sandbox.base import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    ExecResult,
    OutputCallback,
    SandboxHandle,
    SandboxSpec,
    workspace_path,
)
from app.sandbox.egress import normalize_allowlist


def docker_create_args(
    run_id: str,
    agent_id: str,
    spec: SandboxSpec,
    network_name: str | None = None,
    proxy_name: str | None = None,
    proxy_host: str | None = None,
) -> list[str]:
    if spec.network_mode == "allowlisted":
        normalize_allowlist(spec.allowlist)
        if not network_name:
            raise ValueError("Allowlisted networking requires an isolated internal network.")
        if not proxy_name:
            raise ValueError("Allowlisted networking requires the restricted egress proxy.")
        if not proxy_host:
            raise ValueError("Allowlisted networking requires the proxy's internal address.")
        proxy_address = ipaddress.ip_address(proxy_host)
        if not proxy_address.is_private or proxy_address.is_loopback or proxy_address.is_link_local:
            raise ValueError("Egress proxy address must be a private network address.")
    elif spec.network_mode != "none":
        raise ValueError("Unsupported sandbox network mode.")
    elif any((network_name, proxy_name, proxy_host)):
        raise ValueError("Proxy resources are unsupported when network_mode='none'.")
    expires = datetime.fromtimestamp(time.time() + spec.ttl_s, tz=UTC).isoformat()
    args = [
        "docker",
        "run",
        "--detach",
        "--init",
        "--rm",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "10001:10001",
        "--workdir",
        "/workspace",
        "--tmpfs",
        f"/workspace:rw,nosuid,nodev,size={spec.disk_mb}m,uid=10001,gid=10001",
        "--cpus",
        str(spec.cpu),
        "--memory",
        f"{spec.memory_mb}m",
        "--pids-limit",
        str(spec.pids_limit),
        "--network",
        (network_name or "none") if spec.network_mode == "allowlisted" else "none",
        "--label",
        f"forge.run_id={run_id}",
        "--label",
        f"forge.agent_id={agent_id}",
        "--label",
        f"forge.expires_at={expires}",
    ]
    if network_name:
        args.extend(["--label", f"forge.network_name={network_name}"])
    if proxy_name:
        args.extend(["--label", f"forge.proxy_id={proxy_name}"])
    if proxy_host:
        args.extend(["--label", f"forge.proxy_host={proxy_host}"])
    if spec.network_mode == "allowlisted":
        args.extend(["--dns", proxy_host or ""])
    args.extend([spec.image, "sleep", "infinity"])
    if spec.owner_user_id:
        args[-3:-3] = ["--label", f"forge.user_id={spec.owner_user_id}"]
    return args


class DockerSandboxProvider:
    def __init__(self, max_output_bytes: int = 65536) -> None:
        self._max_output_bytes = max_output_bytes

    async def create(self, run_id: str, agent_id: str, spec: SandboxSpec) -> SandboxHandle:
        for handle in await self.list_active():
            if handle.run_id == run_id:
                if handle.expires_at > time.time():
                    return handle
                await self.destroy(handle)
        network_name: str | None = None
        proxy_id: str | None = None
        proxy_host: str | None = None
        if spec.network_mode == "allowlisted":
            hosts = sorted(normalize_allowlist(spec.allowlist))
            suffix = hashlib.sha256(run_id.encode()).hexdigest()[:16]
            network_name = f"forge-egress-{suffix}"
            proxy_name = f"forge-egress-proxy-{suffix}"
            proxy_id = proxy_name
            proxy_expires = datetime.fromtimestamp(time.time() + spec.ttl_s, tz=UTC).isoformat()
            await _command(
                [
                    "docker",
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    f"forge.run_id={run_id}",
                    "--label",
                    "forge.egress=true",
                    "--label",
                    f"forge.expires_at={proxy_expires}",
                    network_name,
                ]
            )
            try:
                await _command(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--rm",
                        "--name",
                        proxy_name,
                        "--network",
                        "bridge",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "NET_BIND_SERVICE",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--user",
                        "10001:10001",
                        "--cpus",
                        "0.25",
                        "--memory",
                        "128m",
                        "--pids-limit",
                        "64",
                        "--label",
                        f"forge.run_id={run_id}",
                        "--label",
                        "forge.egress_proxy=true",
                        "--label",
                        f"forge.expires_at={proxy_expires}",
                        "--label",
                        f"forge.network_name={network_name}",
                        "--env",
                        "FORGE_EGRESS_ALLOWLIST=" + json.dumps(hosts),
                        spec.image,
                        "python3",
                        "/opt/forge/egress_proxy.py",
                    ]
                )
                await _command(["docker", "network", "connect", network_name, proxy_name])
                await _wait_for_proxy(proxy_name)
                proxy_inspect = json.loads(await _command(["docker", "inspect", proxy_name]))[0]
                networks = proxy_inspect["NetworkSettings"]["Networks"]
                proxy_host = str(networks[network_name]["IPAddress"])
                if not proxy_host:
                    raise RuntimeError("Sandbox egress proxy has no internal network address.")
                sandbox_id = (
                    await _command(
                        docker_create_args(
                            run_id, agent_id, spec, network_name, proxy_name, proxy_host
                        )
                    )
                ).strip()
            except Exception:
                if proxy_id:
                    await _command(["docker", "rm", "--force", proxy_id], allow_failure=True)
                await _command(["docker", "network", "rm", network_name], allow_failure=True)
                raise
        else:
            sandbox_id = (await _command(docker_create_args(run_id, agent_id, spec))).strip()
        return SandboxHandle(
            sandbox_id=sandbox_id,
            run_id=run_id,
            agent_id=agent_id,
            expires_at=time.time() + spec.ttl_s,
            owner_user_id=spec.owner_user_id,
            network_name=network_name,
            proxy_id=proxy_id,
            proxy_host=proxy_host if spec.network_mode == "allowlisted" else None,
        )

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_s: int = 120,
        on_output: OutputCallback | None = None,
    ) -> ExecResult:
        if handle.expires_at <= time.time():
            await self.destroy(handle)
            raise LookupError("Sandbox has expired.")
        if (
            not cmd
            or type(timeout_s) is not int
            or not 0 < timeout_s <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"Command timeout must be between 1 and {MAX_COMMAND_TIMEOUT_SECONDS} seconds."
            )
        safe_cwd = await self._safe_path(handle, cwd, must_exist=True)
        args = ["docker", "exec", "--workdir", safe_cwd]
        command_env = dict(env or {})
        if handle.network_name:
            command_env.update(
                {
                    "HTTPS_PROXY": f"http://{handle.proxy_host}:3128",
                    "HTTP_PROXY": f"http://{handle.proxy_host}:3128",
                    "https_proxy": f"http://{handle.proxy_host}:3128",
                    "http_proxy": f"http://{handle.proxy_host}:3128",
                }
            )
        for key, value in command_env.items():
            if not key.replace("_", "").isalnum():
                raise ValueError("Invalid environment variable name.")
            args.extend(["--env", f"{key}={value}"])
        args.extend(
            [
                handle.sandbox_id,
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{timeout_s}s",
                *cmd,
            ]
        )
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert proc.stdout is not None and proc.stderr is not None

        async def drain(stream: str, reader: asyncio.StreamReader) -> tuple[bytes, bool]:
            data = bytearray()
            truncated = False
            while chunk := await reader.read(4096):
                available = max(0, self._max_output_bytes - len(data))
                accepted = chunk[:available]
                if accepted:
                    data.extend(accepted)
                    if on_output is not None:
                        await on_output(stream, accepted.decode(errors="replace"))
                truncated |= len(accepted) < len(chunk)
            return bytes(data), truncated

        out_task = asyncio.create_task(drain("stdout", proc.stdout))
        err_task = asyncio.create_task(drain("stderr", proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s + 10)
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
            await self.destroy(handle)
        (out, out_cut), (err, err_cut) = await asyncio.gather(out_task, err_task)
        if proc.returncode == 124:
            timed_out = True
        if timed_out:
            await self.destroy(handle)
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - start) * 1000),
            truncated=out_cut or err_cut,
        )

    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> str:
        safe = await self._safe_path(handle, path, must_exist=True)
        value = await _command(
            ["docker", "exec", handle.sandbox_id, "head", "-c", str(max_bytes + 1), "--", safe]
        )
        if len(value.encode()) > max_bytes:
            raise ValueError("File exceeds the configured size limit.")
        return value

    async def write_file(
        self, handle: SandboxHandle, path: str, content: str, max_bytes: int
    ) -> None:
        if len(content.encode()) > max_bytes:
            raise ValueError("File exceeds the configured size limit.")
        safe = await self._safe_path(handle, path, must_exist=False)
        parent = safe.rsplit("/", 1)[0]
        script = f"mkdir -p -- {shlex.quote(parent)} && base64 -d > {shlex.quote(safe)}"
        await _command_input(
            ["docker", "exec", "-i", handle.sandbox_id, "sh", "-lc", script],
            base64.b64encode(content.encode()),
        )

    async def list_dir(self, handle: SandboxHandle, path: str = "/workspace") -> list[str]:
        safe = await self._safe_path(handle, path, must_exist=True)
        result = await _command(
            [
                "docker",
                "exec",
                handle.sandbox_id,
                "find",
                safe,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-printf",
                "%f\\n",
            ]
        )
        return sorted(line for line in result.splitlines() if line)

    async def is_dir(self, handle: SandboxHandle, path: str) -> bool:
        safe = await self._safe_path(handle, path, must_exist=True)
        kind = await _command(["docker", "exec", handle.sandbox_id, "stat", "-c", "%F", "--", safe])
        return kind.strip() == "directory"

    async def destroy(self, handle: SandboxHandle) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "--force",
            handle.sandbox_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if handle.proxy_id:
            await _command(["docker", "rm", "--force", handle.proxy_id], allow_failure=True)
        if handle.network_name:
            await _command(["docker", "network", "rm", handle.network_name], allow_failure=True)

    async def list_active(self) -> list[SandboxHandle]:
        raw = await _command(
            ["docker", "ps", "-a", "--filter", "label=forge.run_id", "--format", "{{json .}}"],
            allow_failure=True,
        )
        handles: list[SandboxHandle] = []
        for line in raw.splitlines():
            try:
                item: dict[str, Any] = json.loads(line)
                labels = dict(
                    pair.split("=", 1)
                    for pair in str(item.get("Labels", "")).split(",")
                    if "=" in pair
                )
                expiry = datetime.fromisoformat(labels["forge.expires_at"]).timestamp()
                handles.append(
                    SandboxHandle(
                        sandbox_id=str(item["ID"]),
                        run_id=labels["forge.run_id"],
                        agent_id=labels["forge.agent_id"],
                        expires_at=expiry,
                        owner_user_id=labels.get("forge.user_id"),
                        network_name=labels.get("forge.network_name"),
                        proxy_id=labels.get("forge.proxy_id"),
                        proxy_host=labels.get("forge.proxy_host"),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return handles

    async def cleanup_expired(self) -> int:
        expired = [h for h in await self.list_active() if h.expires_at <= time.time()]
        for handle in expired:
            await self.destroy(handle)
        # Also recover a proxy/network left behind if the worker stopped between
        # creating those resources and creating the labeled sandbox container.
        raw = await _command(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                "label=forge.egress_proxy=true",
                "--format",
                "{{json .}}",
            ],
            allow_failure=True,
        )
        swept = 0
        for line in raw.splitlines():
            try:
                item: dict[str, Any] = json.loads(line)
                labels = dict(
                    pair.split("=", 1)
                    for pair in str(item.get("Labels", "")).split(",")
                    if "=" in pair
                )
                expiry = datetime.fromisoformat(labels["forge.expires_at"]).timestamp()
                if expiry <= time.time():
                    await _command(["docker", "rm", "--force", str(item["ID"])], allow_failure=True)
                    network_name = labels.get("forge.network_name")
                    if network_name:
                        await _command(
                            ["docker", "network", "rm", network_name], allow_failure=True
                        )
                    swept += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        networks_raw = await _command(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                "label=forge.egress=true",
                "--format",
                "{{.ID}}",
            ],
            allow_failure=True,
        )
        for network_id in networks_raw.splitlines():
            try:
                inspected = json.loads(
                    await _command(["docker", "network", "inspect", network_id])
                )[0]
                labels = inspected.get("Labels") or {}
                expiry = datetime.fromisoformat(labels["forge.expires_at"]).timestamp()
                if expiry <= time.time():
                    await _command(["docker", "network", "rm", network_id], allow_failure=True)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return len(expired) + swept

    async def _safe_path(self, handle: SandboxHandle, path: str, *, must_exist: bool) -> str:
        candidate = _absolute_workspace_path(path)
        args = ["docker", "exec", handle.sandbox_id, "realpath"]
        args.append("-e" if must_exist else "-m")
        args.append(candidate)
        resolved = (await _command(args)).strip()
        if resolved != "/workspace" and not resolved.startswith("/workspace/"):
            raise ValueError("Resolved path escapes /workspace.")
        return resolved


async def _command(args: list[str], *, allow_failure: bool = False) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode and not allow_failure:
        raise RuntimeError(
            f"Sandbox command failed ({proc.returncode}): {err[:512].decode(errors='replace')}"
        )
    return out.decode(errors="replace")


async def _wait_for_proxy(container_name: str) -> None:
    for _ in range(50):
        try:
            await _command(
                [
                    "docker",
                    "exec",
                    container_name,
                    "python3",
                    "-c",
                    "import socket; s=socket.create_connection(('127.0.0.1',3128),1); s.close()",
                ]
            )
            return
        except RuntimeError:
            await asyncio.sleep(0.1)
    raise RuntimeError("Sandbox egress proxy failed to start.")


async def _command_input(args: list[str], data: bytes) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode:
        raise RuntimeError(
            f"Sandbox command failed ({proc.returncode}): {err[:512].decode(errors='replace')}"
        )
    return out.decode(errors="replace")


def _absolute_workspace_path(path: str) -> str:
    if path == "/workspace":
        return path
    if path.startswith("/workspace/"):
        return workspace_path(path[len("/workspace/") :])
    return workspace_path(path)


def _bounded(data: bytes, limit: int) -> tuple[str, bool]:
    return (data[:limit].decode(errors="replace"), len(data) > limit)
