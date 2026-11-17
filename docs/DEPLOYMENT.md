# Production Deployment Guide (Single VPS)

This guide describes a private single-VPS deployment. Place a TLS reverse proxy in front of the web app and API. Keep PostgreSQL, Redis, and the Docker daemon on private interfaces; do not publish their ports to the internet.

## Topology and prerequisites

- Linux VPS sized from the measured M9 load test, with a supported Docker Engine and Compose plugin.
- Web process, stateless FastAPI process, one or more Arq worker processes, PostgreSQL 16, Redis 7, and a sandbox manager with Docker access.
- A domain with TLS termination and a separate private OTLP collector endpoint if telemetry export is enabled.
- Persistent encrypted volumes for PostgreSQL and Redis; sandbox workspaces are disposable.

## Configuration

Set production values through the deployment secret manager, never a committed `.env` file. Required values include `APP_ENV=production`, `DATABASE_URL`, `REDIS_URL`, a random 32-byte `MASTER_KEY`, exact `WEB_ORIGIN`, `COOKIE_SECURE=true`, and explicit sandbox limits. Keep `FAKE_PROVIDER_ENABLED=false`, `SANDBOX_PROVIDER=docker`, `PLATFORM_PAID_ENABLED=false`; platform-paid execution is not implemented. Set `OTEL_EXPORTER_OTLP_ENDPOINT` only to a trusted private collector. Set per-user API rate and concurrency limits from measured capacity. Configure Uvicorn to trust forwarded headers only from the known reverse-proxy addresses (`--proxy-headers --forwarded-allow-ips=<proxy-addresses>`); never trust client-supplied forwarding headers directly.

The API and worker must use the same database, Redis, encryption key, and sandbox-host configuration. Run Alembic migrations as a one-off release step before deploying new application processes. Do not run migrations automatically in every replica.

## Release sequence

1. Verify database and Redis backups, then take a pre-release database backup.
2. Build immutable API, worker, web, and sandbox images from the reviewed commit.
3. Apply `uv run alembic upgrade head` from `apps/api`.
4. Deploy API and worker processes; confirm `/healthz`, Redis connectivity, queue health, and telemetry export.
5. Deploy the web app and confirm authentication, a disposable sandbox run, approval gating, report generation, and event replay.
6. Monitor error rate, queue age, cost, active runs, sandbox counts, and database capacity before removing the prior release image.

## Backups and secrets

Follow [`RUNBOOK.md`](RUNBOOK.md) for backup, restore, key recovery, incident response, and rollback. Keep encrypted database backups off the VPS. Back up the master key independently with restricted access; without it, encrypted provider and integration credentials cannot be recovered. Restore into an isolated environment before relying on a backup.

## Capacity and network

Run [`apps/api/scripts/load_test.py`](../apps/api/scripts/load_test.py) against the staging VPS before launch and record the tested concurrency and resource envelope. Create a dedicated staging account, verify `FORGE_LOAD_BASE_URL` targets a disposable staging environment, set `FORGE_LOAD_EMAIL`, `FORGE_LOAD_PASSWORD`, and `FORGE_LOAD_CONCURRENCY`, then set `FORGE_LOAD_CONFIRM_STAGING=I_CONFIRMED_DISPOSABLE_STAGING` and run `python apps/api/scripts/load_test.py`. The script refuses to run without an explicit target and this confirmation because it creates runs. Keep the API/worker and data services on private networks. Sandbox egress stays disabled unless the run requires the configured allowlist proxy. Firewall inbound access to TLS only; administrative access uses a restricted VPN or SSH allowlist.

The M9 OpenTelemetry API, SDK, and OTLP HTTP exporter dependencies are included to emit API and worker traces plus request/job metrics to a configured collector. The web dependency upgrades and `filelock` override remediate advisories found by the M9 locked-dependency audit.
