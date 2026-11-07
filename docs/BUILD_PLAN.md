# BUILD_PLAN.md — Milestones for Codex

Use the standard kickoff prompt at the start of each milestone, then the milestone block.

## Standard Kickoff Prompt (paste each time)

```
Read AGENTS.md, docs/SPEC.md, and docs/BUILD_PLAN.md.
We are working on milestone <Mx> only.
1. Restate the plan in 5-10 bullets and list files to create/change.
2. Implement it.
3. Run tests, lint, and type checks; fix failures.
4. Finish with a summary: what was done, how to verify, what is deferred.
Do not start other milestones. Ask before changing architecture, data model, or public API.
```

---

## M0 — Scaffold & Infra
**Goal:** Running monorepo with CI.
**Build:**
- Monorepo per SPEC section 17; `docs/` populated with SPEC.md and BUILD_PLAN.md; AGENTS.md at root.
- `infra/docker-compose.yml` (Postgres 16, Redis 7); `.env.example`.
- FastAPI app with `/healthz`, config (pydantic-settings), structured logging, async DB session, Alembic initialized.
- Arq worker skeleton that processes a no-op job.
- Next.js app (TypeScript, Tailwind, shadcn/ui) with a landing page and API health check.
- GitHub Actions CI: ruff, mypy, pytest, web lint/typecheck/test.
**Accept:** `docker compose up`, API + worker + web start; CI green; `/healthz` tested.

## M1 — Auth, Crypto, BYOK, LLM Gateway
**Goal:** Users sign up, store encrypted provider keys, and make a test LLM call through the gateway.
**Build:**
- `users`, `provider_keys` models + migrations; signup/login/logout/me (argon2, HttpOnly cookie sessions, CSRF).
- `core/crypto.py`: AES-256-GCM envelope encryption with env master key; unit tests incl. tamper detection.
- Repository layer requiring `user_id`; cross-tenant tests.
- `llm/` gateway on LiteLLM: `LLMClient.complete(...)`, model tiers config (fast/balanced/strong per provider), retries, cost accounting, `FakeProvider` for tests.
- Endpoints: provider-keys CRUD (write-only, return last4 only), `POST /provider-keys/{id}/test`.
- Web: login/signup, `/settings/providers`.
**Accept:** Keys never appear in responses/logs (test asserts); test-call works via FakeProvider; cross-tenant access fails.

## M2 — Runs, Events, Realtime
**Goal:** Create a run and watch events stream live.
**Build:**
- Models: `projects`, `runs`, `messages`, `events`, `artifacts` + migrations.
- Event bus: append-only `events` with per-run monotonic `seq`, Redis pub/sub, `GET /runs/{id}/events?after_seq=`.
- WebSocket `/ws/runs/{id}?after_seq=N` with replay then live; client->server message handling stubs.
- `POST /runs`, `GET /runs`, `GET /runs/{id}`; run creation emits `run.state_changed`.
- Web: `/new`, `/runs`, `/runs/[id]` skeleton with `useRunStream` hook (reconnect + replay).
**Accept:** Disconnect/reconnect shows no gaps or duplicates (test); seq monotonic under concurrency (test).

## M3 — Orchestrator, Clarify, Plan
**Goal:** Idea -> questions -> answers -> plan -> approval, using real agent loop with FakeProvider tests.
**Build:**
- `orchestrator/state.py`: state machine per SPEC section 6 with validated transitions + tests (all legal/illegal transitions).
- `questions` table; `ask_user` tool; structured question schema (single/multi choice, free text, "decide for me").
- Clarifier and Planner role templates; Planner output schema (brief, stack, milestones, task graph with `depends_on`).
- Endpoints: `POST /runs/{id}/answers`, `PUT /runs/{id}/plan`.
- Web: questions form, plan viewer/editor, approve button.
- Worker job `advance_run(run_id)` that moves the run until it needs input.
**Accept:** E2E test with FakeProvider: idea -> questions -> answers -> plan -> approved -> state `EXECUTING`. Run pauses (no compute) while awaiting input.

## M4 — Gate Engine & Autonomy Levels
**Goal:** Approval system fully enforced in code.
**Build:**
- `gate/engine.py`: pure `decide(level, risk, context) -> AUTO | REQUIRE_APPROVAL` implementing the SPEC section 4 matrix.
- `approvals` table; create/resolve flow; edit-payload support; rejection feedback to agent.
- Tool registry with mandatory `risk` field (registration fails without it).
- Mid-run autonomy level change (applies at next gate).
- Web: approvals queue panel with payload preview, approve/reject/edit; autonomy selector.
**Accept:** Exhaustive matrix test (3 levels x 4 risks); property test that `irreversible` never auto-approves; suspended run resumes after decision, including after worker restart.

## M5 — Agent Runtime & Dynamic Sub-agents
**Goal:** Durable agent loop, task-graph scheduler, dynamic spawning.
**Build:**
- `agents`, `tasks`, `agent_steps` models; agent loop per SPEC 7.3 with persistence before each next step.
- Role template registry; `spawn_agent` tool with depth (3) and concurrency (4) limits; ad-hoc role validation.
- Scheduler: runs ready tasks (dependencies met) concurrently; handles retries, failures, replans.
- Budget enforcement per agent/run (hard stop, 80% warning event); cost meter events.
- Human controls: `user.message` targeting run/agent, pause/resume/cancel, agent interrupt.
- Web: agent tree, task graph view, message-agent box, cost meter.
**Accept:** With FakeProvider scripted runs: parallel tasks respect dependencies; limits enforced; worker kill mid-run resumes without duplicate side effects (idempotency keys on tools); budget stop works.

## M6 — Sandbox & Code Tools
**Goal:** Agents can write, run, and test code safely.
**Build:**
- `SandboxProvider` interface + Docker implementation; resource limits, non-root, network allowlist, TTL, cleanup job.
- `infra/sandbox/Dockerfile` (Node, Python, git, build tools).
- Tools: `sandbox_exec`, `read_file`, `write_file`, `list_dir`, `git_*` with correct risk classes; log streaming as `sandbox.log` events.
- Coder, Tester, Reviewer, Architect role templates; Tester must run tests and report structured results; Reviewer gate before deploy.
- Web: file tree/diff viewer, live logs.
**Accept:** Security tests: no network outside allowlist, resource limits enforced, no host access, container destroyed on completion/cancel. Scripted run builds a trivial app and passes tests in sandbox.

## M7 — Integrations & Deploy
**Goal:** Push to user's GitHub and deploy to their Vercel.
**Build:**
- `integrations` table; GitHub OAuth/token connect, Vercel token connect; `Integration` protocol.
- GitHub tools: create repo (irreversible), branch/commit/PR (reversible_external), push default branch (irreversible).
- Vercel tools: create project, trigger deploy (irreversible), poll status, fetch URL.
- Deployer role; `http_check` smoke test on live URL; templates (Next.js, Vite+React).
- Web: `/settings/integrations`, deploy approval card with exact target/account.
- Stubs/adapters interface ready for Netlify/Cloudflare Pages/Render.
**Accept:** Adapter contract tests against recorded fixtures (no live calls in CI); deploy always requires approval at all levels; tokens never in model context (test).

## M8 — Reporter, Projects, End-to-End
**Goal:** Full idea-to-live-app flow with final report.
**Build:**
- Reporter role + Markdown report per SPEC section 16; `GET /runs/{id}/report`; report view/download in web.
- `projects` linking runs; "iterate" follow-up run reusing repo and context.
- Failure handling UX: failed tasks, retry from step, resume.
- Manual E2E checklist in `docs/E2E.md` with a real idea on each autonomy level.
**Accept:** Scripted E2E passes in CI (FakeProvider + fake integrations); manual E2E produces a live URL, repo, and report.

## M9 — Hardening & Launch Prep
**Build:**
- Rate limits (per-user API, concurrent runs), abuse guards, sandbox quotas.
- OpenTelemetry tracing, metrics, run replay verification.
- Security review checklist (secrets, tenant isolation, prompt injection, sandbox); dependency audit.
- Billing groundwork: `credit_ledger`, `subscriptions` tables, platform-paid mode behind a feature flag (no payments yet).
- Production deployment guide (single VPS: API, workers, web, Postgres, Redis, sandbox host), backups, secrets management.
**Accept:** Checklist complete; load test with N concurrent runs on target VPS; documented runbook.

---

## Cross-Cutting Test Matrix (maintain throughout)
- Gate matrix (M4) runs on every PR.
- Cross-tenant isolation test for every new resource.
- Secret-leak test: seed a known key, run a full flow, grep logs/events/responses/prompts for it.
- Durability test: kill worker at random steps; run completes exactly once per side effect.
- Provider contract tests with fixtures for Claude, OpenAI, Gemini tool-calling.

## Suggested Order of Work in Codex
1. Create repo, copy `AGENTS.md` to root, `SPEC.md` and `BUILD_PLAN.md` to `docs/`.
2. Run M0 with the kickoff prompt; review and merge.
3. Proceed milestone by milestone; review each diff before the next.
4. After M5, do a manual walkthrough of the human-in-the-loop UX before building M6+.
