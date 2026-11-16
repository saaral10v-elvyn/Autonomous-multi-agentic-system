# Forge sandbox image

Build from the repository root with `docker build -t forge-sandbox:latest -f infra/sandbox/Dockerfile .`.
The image includes Node.js LTS, Python, Git, and build tools. It contains no credentials or sudo.
The runtime provides a non-root user, a read-only root filesystem, a writable `/workspace` tmpfs,
CPU, memory, PID, and disk limits, and no host bind mounts. Commands have a hard maximum timeout
of 300 seconds; the in-container `timeout` utility also terminates the process if the worker's
client call is interrupted.

Networking is default-deny (`SANDBOX_NETWORK_MODE=none`). To allow package downloads, set
`SANDBOX_NETWORK_MODE=allowlisted`. Each sandbox then gets a per-run Docker internal network and a
separate unprivileged proxy attached to that network and Docker's outbound bridge. The sandbox has
no direct outbound route; only the proxy can reach outside. The proxy accepts HTTPS CONNECT on port
443 for exact hostnames in `SANDBOX_ALLOWLIST_HOSTS`, resolves each name itself, rejects private or
otherwise non-public IP addresses, and connects to the checked IP to prevent DNS rebinding. It does
not support plain HTTP, arbitrary ports, IP literals, wildcard entries, or unlisted hosts. Sandbox
DNS points only to a local proxy stub that returns `NXDOMAIN` and never forwards requests; proxy
traffic uses the proxy's private network address, so DNS cannot provide a side channel around the
hostname policy.

The default allowlist covers npm, PyPI, Python package files, and GitHub endpoints used for source
and release downloads. Add only exact domains required by the project. The proxy and per-run network
are removed with the sandbox and by TTL cleanup. The image must be rebuilt after updating the proxy
implementation.

The cleanup worker removes expired and terminal-run containers. It runs every five minutes, so TTL
cleanup may occur up to one interval after expiry; per-command timeouts do not rely on that worker.
