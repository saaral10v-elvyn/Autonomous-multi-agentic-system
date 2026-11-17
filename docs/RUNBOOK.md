# Production Operations Runbook

## Health and alerts

Check `/healthz`, API 5xx and 429 rates, Arq queue age, worker heartbeats, database connections/disk, Redis memory/availability, active sandboxes, sandbox TTL cleanup, and OTLP export health. Alert on sustained failures, queue growth, exhausted disk, backup failure, or sandbox counts beyond the configured concurrency envelope. Do not put run ideas, prompts, credentials, or request bodies into telemetry labels.

For replay verification, select a disposable completed run, fetch all pages from `GET /api/v1/runs/{id}/events?after_seq=...`, and verify sequence numbers are strictly increasing without gaps. Reconnect its WebSocket from a middle sequence and confirm the replay resumes at the next event without duplicate side effects. Compare the reconstructed state and task/agent outcomes with `GET /runs/{id}`, `/agents`, and `/tasks`; record the run ID and result.

## Backup and restore

1. Take encrypted PostgreSQL backups daily and before every schema migration; retain off-host copies according to the service retention policy.
2. Back up Redis only if required for the chosen queue durability configuration; PostgreSQL remains the source of truth for run state and events.
3. Store `MASTER_KEY` separately in the secret manager with restricted recovery access and versioned rotation records.
4. Restore a backup to an isolated host, validate migration state, row counts, ownership boundaries, and application startup, then record recovery time and data-loss window.

## Deploy and rollback

Use [`DEPLOYMENT.md`](DEPLOYMENT.md). For rollback, restore the prior immutable application image only when the current schema remains backward compatible. If a migration is not reversible, use a forward fix; do not downgrade production data without a reviewed recovery plan. Preserve logs and events for incident review.

## Incidents

- **Credential exposure:** disable the affected integration/provider key, rotate it at the source, inspect access and audit events, then replace the encrypted credential. Never paste credential values into incident notes.
- **Runaway usage:** pause affected runs, disable queue workers if needed, preserve events, and review budgets, concurrency limits, and rate-limit telemetry before resuming.
- **Sandbox compromise:** stop scheduling, isolate the sandbox host, revoke credentials reachable by the host, preserve forensic metadata, and rebuild from a trusted image.
- **Database/Redis outage:** stop workers before recovery to avoid duplicate scheduling; restore service, verify event sequence and queue state, then resume workers gradually.

Record incident timeline, affected run/user identifiers, mitigations, data-integrity checks, and follow-up actions without including secrets or sensitive prompts.

## Load-test record

Before launch, attach the command, commit, VPS shape, concurrency, duration, success/429/error counts, p95/p99 latency, CPU/memory/disk, database connections, Redis memory, and queue age. A test run from a developer workstation is not evidence for target-VPS capacity.
