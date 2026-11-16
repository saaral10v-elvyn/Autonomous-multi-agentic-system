"""Opt-in Docker security checks. CI builds the image before running this marker."""

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from app.sandbox.base import MAX_COMMAND_TIMEOUT_SECONDS, SandboxSpec
from app.sandbox.docker_provider import DockerSandboxProvider
from app.sandbox.tools import sandbox_diff

pytestmark = pytest.mark.docker


def test_docker_provider_security_and_timeout() -> None:
    image = os.environ.get("SANDBOX_IMAGE")
    if not image or not shutil.which("docker"):
        pytest.skip("Docker CLI or SANDBOX_IMAGE is unavailable")
    if subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=10).returncode:
        pytest.skip("Docker daemon is unavailable")
    if subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode:
        pytest.skip("SANDBOX_IMAGE is not built")

    async def run() -> None:
        provider = DockerSandboxProvider()
        handle = await provider.create("m6-test", "agent", SandboxSpec(image=image, ttl_s=300))
        try:
            identity = await provider.exec(handle, ["id", "-u"])
            assert identity.exit_code == 0, identity.stderr
            assert identity.stdout.strip() == "10001"
            host_path = os.path.abspath("pyproject.toml")
            assert (await provider.exec(handle, ["test", "-e", host_path])).exit_code != 0
            docker_socket = await provider.exec(handle, ["test", "-S", "/var/run/docker.sock"])
            assert docker_socket.exit_code != 0
            network = await provider.exec(
                handle,
                ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), 2)"],
                timeout_s=5,
            )
            assert network.exit_code != 0
            template = (
                "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}};"
                "{{json .HostConfig.Tmpfs}};{{json .Mounts}}"
            )
            inspected = subprocess.run(
                ["docker", "inspect", "--format", template, handle.sandbox_id],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            limits, tmpfs, mounts = inspected.split(";", maxsplit=2)
            assert limits.split() == [str(1024 * 1024 * 1024), "1000000000", "256"]
            assert "/workspace" in json.loads(tmpfs)
            assert all(mount["Type"] != "bind" for mount in json.loads(mounts))

            await provider.write_file(
                handle,
                "package.json",
                '{"scripts":{"build":"node build.js","test":"node --test"}}\n',
                1024,
            )
            await provider.write_file(
                handle,
                "app.cjs",
                'module.exports = () => "hello from sandbox";\n',
                1024,
            )
            await provider.write_file(
                handle,
                "app.test.cjs",
                'const test = require("node:test");\n'
                'const assert = require("node:assert/strict");\n'
                'const app = require("./app.cjs");\n'
                'test("trivial app works", () => assert.equal(app(), "hello from sandbox"));\n',
                2048,
            )
            await provider.write_file(
                handle,
                "build.js",
                'const fs = require("node:fs"); fs.mkdirSync("dist", {recursive:true}); '
                'fs.copyFileSync("app.cjs", "dist/app.cjs");\n',
                1024,
            )
            app_build = await provider.exec(handle, ["npm", "run", "build"])
            assert app_build.exit_code == 0
            assert await provider.read_file(handle, "dist/app.cjs", 1024) == (
                'module.exports = () => "hello from sandbox";\n'
            )
            app_test = await provider.exec(handle, ["npm", "test"])
            assert app_test.exit_code == 0 and "# pass 1" in app_test.stdout

            assert (await provider.exec(handle, ["git", "init"])).exit_code == 0
            assert (
                await provider.exec(handle, ["git", "config", "user.name", "Sandbox Test"])
            ).exit_code == 0
            assert (
                await provider.exec(handle, ["git", "config", "user.email", "sandbox@example.test"])
            ).exit_code == 0
            await provider.write_file(handle, "tracked.txt", "base\n", 128)
            assert (await provider.exec(handle, ["git", "add", "tracked.txt"])).exit_code == 0
            assert (await provider.exec(handle, ["git", "commit", "-m", "initial"])).exit_code == 0
            await provider.write_file(handle, "tracked.txt", "modified\n", 128)
            await provider.write_file(handle, "staged.txt", "staged content\n", 128)
            assert (await provider.exec(handle, ["git", "add", "staged.txt"])).exit_code == 0
            await provider.write_file(handle, "untracked.txt", "new content\n", 128)
            assert (
                await provider.exec(handle, ["ln", "-s", "/etc/passwd", "outside-link"])
            ).exit_code == 0
            diff = await sandbox_diff(provider, handle)
            assert "tracked.txt" in diff and "modified" in diff
            assert "staged.txt" in diff and "staged content" in diff
            assert "untracked.txt" in diff and "new content" in diff
            assert "outside-link" in diff
            assert "root:x:" not in diff

            with pytest.raises(ValueError, match="timeout"):
                await provider.exec(
                    handle,
                    ["true"],
                    timeout_s=MAX_COMMAND_TIMEOUT_SECONDS + 1,
                )
            timeout = await provider.exec(handle, ["sleep", "5"], timeout_s=1)
            assert timeout.timed_out
            assert not any(h.sandbox_id == handle.sandbox_id for h in await provider.list_active())

            egress = await provider.create(
                "m6-egress-test",
                "agent",
                SandboxSpec(
                    image=image,
                    ttl_s=300,
                    network_mode="allowlisted",
                    allowlist=("registry.npmjs.org",),
                ),
            )
            try:
                allowed = await provider.exec(
                    egress,
                    [
                        "python3",
                        "-c",
                        "import urllib.request; print(\n"
                        "    urllib.request.urlopen(\n"
                        "        'https://registry.npmjs.org/-/ping', timeout=15\n"
                        "    ).status\n"
                        ")",
                    ],
                    timeout_s=30,
                )
                assert allowed.exit_code == 0 and "200" in allowed.stdout
                egress_inspect = json.loads(
                    subprocess.run(
                        ["docker", "inspect", egress.sandbox_id],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                )[0]
                assert egress_inspect["HostConfig"]["Dns"] == [egress.proxy_host]
                dns_probe = f"""import socket, struct, subprocess
proxy = {egress.proxy_host!r}
resolver = open('/etc/resolv.conf').read()
nameserver = next(
    line.split()[1] for line in resolver.splitlines()
    if line.startswith('nameserver ')
)
assert nameserver == '127.0.0.11'
labels = [part.encode() for part in 'registry.npmjs.org'.split('.')]
qname = b''.join(bytes([len(part)]) + part for part in labels) + b'\\0'
header = struct.pack('!HHHHHH', 0x1234, 0x0100, 1, 0, 0, 0)
question = struct.pack('!HH', 1, 1)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3)
sock.sendto(header + qname + question, (proxy, 53))
response = sock.recvfrom(4096)[0]
assert int.from_bytes(response[2:4], 'big') & 0xF == 3
direct_dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
direct_dns.settimeout(2)
try:
    direct_dns.sendto(header + qname + question, ('1.1.1.1', 53))
    direct_dns.recvfrom(4096)
    raise AssertionError('Direct public DNS unexpectedly returned a response')
except OSError:
    pass
try:
    subprocess.run(
        ['python3', '-c', "import socket; socket.gethostbyname('registry.npmjs.org')"],
        check=True, timeout=2
    )
    raise AssertionError('System DNS unexpectedly resolved a hostname')
except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
    pass
print('DNS denied by proxy stub')
"""
                dns_blocked = await provider.exec(
                    egress,
                    ["python3", "-c", dns_probe],
                    timeout_s=10,
                )
                assert (
                    dns_blocked.exit_code == 0 and "DNS denied by proxy stub" in dns_blocked.stdout
                )
                blocked = await provider.exec(
                    egress,
                    [
                        "python3",
                        "-c",
                        "import urllib.request\n"
                        "try:\n"
                        "    urllib.request.urlopen('https://example.com', timeout=10)\n"
                        "except Exception as exc:\n"
                        "    print(type(exc).__name__, str(exc))",
                    ],
                    timeout_s=20,
                )
                assert blocked.exit_code == 0 and "403 Forbidden" in blocked.stdout
                direct = await provider.exec(
                    egress,
                    [
                        "python3",
                        "-c",
                        "import socket; socket.create_connection(('1.1.1.1', 443), 2)",
                    ],
                    timeout_s=5,
                )
                assert direct.exit_code != 0
            finally:
                await provider.destroy(egress)
            assert await provider.list_active() == []

            expiring = await provider.create(
                "m6-ttl-test", "agent", SandboxSpec(image=image, ttl_s=1)
            )
            await asyncio.sleep(1.1)
            assert await provider.cleanup_expired() == 1
            assert not any(
                h.sandbox_id == expiring.sandbox_id for h in await provider.list_active()
            )
        finally:
            await provider.destroy(handle)

    asyncio.run(run())
