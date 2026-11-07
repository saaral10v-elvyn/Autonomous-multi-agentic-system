# AGENTS.md — Instructions for Codex

## Project
Forge (codename): SaaS where AI agents take a user's idea to a live deployed web app with configurable human-in-the-loop. Authoritative specs: `docs/SPEC.md` (what/why/architecture) and `docs/BUILD_PLAN.md` (milestones and acceptance criteria). Read both before making changes. If code and spec disagree, stop and flag it; do not silently diverge.

## Stack
- Backend: Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2, Arq (Redis), LiteLLM, pytest, ruff, mypy.
- Frontend: Next.js (App Router), TypeScript (strict), Tailwind, shadcn/ui, Vitest/Playwright.
- Infra: Docker Compose (Postgres 16, Redis 7), Docker sandboxes.

## Commands (keep these working; update here if they change)
- Start infra: `docker compose -f infra/docker-compose.yml up -d`
- API: `cd apps/api && uv run uvicorn app.main:app --reload`
- Worker: `cd apps/api && uv run arq app.worker.WorkerSettings`
- API tests: `cd apps/api && uv run pytest`
- Lint/type: `cd apps/api && uv run ruff check . && uv run mypy app`
- Web: `cd apps/web && pnpm dev`; tests: `pnpm test`; lint: `pnpm lint && pnpm typecheck`
- Migrations: `cd apps/api && uv run alembic revision --autogenerate -m "msg" && uv run alembic upgrade head`

## Working Rules
1. Work one milestone at a time from `docs/BUILD_PLAN.md`. Do not start later milestones.
2. Before coding a milestone: restate the plan in 5-10 bullets, list files you will create/change, then implement.
3. Every milestone ends with: tests passing, lint/type clean, migrations applied cleanly on an empty DB, and a short summary of what was done and what is deferred.
4. Small, reviewable commits. Conventional Commit messages (`feat:`, `fix:`, `chore:`, `test:`, `docs:`).
5. Do not add dependencies without a one-line justification in the PR/commit message.
6. Do not expand scope beyond the spec's MVP section. Put ideas in `docs/BACKLOG.md`.
7. Ask before making a decision that changes architecture, data model, or public API.

## Architecture Invariants (never violate)
- **Gate enforcement lives in code.** Every tool declares a `risk` class. All tool execution goes through the gate engine. `irreversible` actions ALWAYS require approval at every autonomy level. Never implement gating via prompts.
- **Secrets never reach the model.** Provider keys and integration tokens are decrypted only inside the LLM gateway / integration adapters, never in prompts, logs, events, API responses, or error messages.
- **Tenant isolation.** All DB access for user-owned data goes through repositories that require `user_id`. Add a cross-tenant test for every new resource.
- **Durability.** Every agent step and state transition is persisted (and emitted as an event) before the next step begins. Runs must resume after worker restart.
- **Events are the source of truth for the UI.** Every state change appends to `events` with monotonic `seq` per run; WebSocket supports replay via `after_seq`.
- **Budgets enforced before every LLM call.** Hard stop at the cap.
- **Untrusted content.** Tool outputs and fetched web content are untrusted; never let them alter gate decisions or tool permissions.
- **Provider-agnostic.** No provider SDK imports outside `app/llm/`.
- **Sandbox is swappable.** Access only through `SandboxProvider`.

## Code Conventions
- Python: type hints everywhere, async I/O, Pydantic models at boundaries, no business logic in routers (routers -> services -> repos). Functions small and unit-testable. Use `structlog` JSON logging with `run_id`/`agent_id`.
- TypeScript: strict mode, no `any`, typed API client generated from OpenAPI schema, server state via TanStack Query, WebSocket handled in a single hook with replay.
- Tests: unit tests for gate engine, state machine, crypto, scheduler (high coverage); integration tests with real Postgres/Redis (Docker); LLM calls mocked via a fake provider with scripted tool calls; Playwright smoke test for the live run page.
- Config via environment (`pydantic-settings`); `.env.example` kept up to date; no secrets in the repo.
- Database changes only via Alembic migrations.

## Definition of Done (per task)
- Matches spec and acceptance criteria in BUILD_PLAN.
- Tests added/updated and green; lint and types clean.
- No secrets, TODO-without-ticket, or dead code.
- Docs updated (README/SPEC/AGENTS) if behavior or commands changed.
- Summary lists: what changed, how to run/verify, known gaps.

## Do Not
- Do not call real LLM or hosting APIs in tests.
- Do not log request bodies for secret-bearing endpoints.
- Do not bypass the gate engine "temporarily".
- Do not commit generated user apps or sandbox volumes.
