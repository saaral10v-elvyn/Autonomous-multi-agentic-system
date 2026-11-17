# M9 Security Review Checklist

Complete this checklist for every release candidate and attach evidence to the release record.

## Secrets and credentials

- [ ] `MASTER_KEY` is generated, stored in a secret manager, backed up separately, and never committed.
- [ ] Provider and integration tokens are encrypted at rest; responses, logs, traces, events, and prompts contain no plaintext credentials.
- [ ] Production uses secure, HttpOnly, SameSite cookies and an exact `WEB_ORIGIN` allowlist.
- [ ] Rotation and recovery procedures have been exercised without exposing secret values.

## Tenant boundaries and APIs

- [ ] Every user-owned read and write is scoped through a repository requiring `user_id`.
- [ ] Cross-tenant tests cover runs, projects, artifacts, integrations, billing records, and replay endpoints.
- [ ] Redis-backed per-user request limits and concurrent-run caps are enabled and monitored.
- [ ] Authentication, CSRF, input size, pagination, and failure responses are checked for information leaks.

## Agents and untrusted content

- [ ] Tool registry rejects tools without a risk class; all tool execution passes through the gate engine.
- [ ] Irreversible actions require approval at every autonomy level.
- [ ] Retrieved pages and tool outputs are treated as untrusted; they cannot change permissions or gate outcomes.
- [ ] Budget checks happen before each model call; fake providers are disabled in production.

## Sandbox and infrastructure

- [ ] Sandbox uses an unprivileged user, resource limits, TTL cleanup, no host mounts, and restricted egress.
- [ ] Docker socket access is isolated to the sandbox manager host; sandbox containers cannot reach it.
- [ ] Database and Redis are private; admin ports are not exposed publicly.
- [ ] Backups are encrypted, retention is defined, and restore is tested.

## Dependencies and release evidence

- [x] Audit locked Python and web dependencies (`pip-audit` against a pinned `uv export`; `pnpm audit`).
- [x] Review and remediate dependency findings. The 2026-11-17 audit reports no known Python or web advisories.
- [ ] Run API and web checks, migration from an empty database, secret-leak checks, replay checks, and target-VPS load test.
- [ ] Record reviewer, date, commit, findings, and evidence links. Do not mark unchecked items complete.

## Dependency audit evidence

- 2026-11-17: `pip-audit -r .audit-requirements.txt --no-deps --disable-pip` against the pinned Python runtime export: no known vulnerabilities. The export included hashes from `uv.lock`.
- 2026-11-17: `pnpm audit --audit-level low` against `apps/web/pnpm-lock.yaml`: no known vulnerabilities.
- Remediations: upgraded Next.js and Vitest, pinned patched Vite and transitive web packages, and overrode transitive `filelock` to the audited fixed release.
